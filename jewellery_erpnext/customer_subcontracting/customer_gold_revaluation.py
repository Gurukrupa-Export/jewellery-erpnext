# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Revalue remaining customer gold before it funds a NEW order -- the SOP's Example E.

WHAT MAKES THIS DIFFERENT FROM A RETURN
---------------------------------------
Both act on the same leftover metal, and the SOP gives them opposite rate rules on purpose:

* **Return** (Example D) -- the metal goes back to the customer. Use the BOOKED rate; the
  obligation was fixed the day it arrived.
* **Revaluation** (Example E) -- the metal stays with GK and funds the customer's next order. Fetch
  the CURRENT approved rate, because the customer is in effect re-supplying it today.

Getting these the wrong way round is not a rounding error. On the SOP's own figures the gap is
Rs.670.34 on 2 g, and it lands in whichever party's favour the mistake happens to fall.

    "If the customer keeps gold for the next order, revalue and repack it at the current nominal
     rate before allocating it to that order."

THE MECHANISM, PROVED BEFORE IT WAS USED
-----------------------------------------
Sec 4.2 of the recovery spec forbids inventing a valuation path, so this reuses a standard one:
**Stock Reconciliation**, which is erpnext's supported way to restate value without moving
quantity. Two facts make it fit, both read from the installed controller rather than assumed:

* its contra leg is the document's own ``expense_account``
  (``stock_reconciliation.py:967`` passes it straight into ``get_gl_entries``), so naming the
  Customer Gold Liability account posts **Dr Stock / Cr Customer Gold Liability** for an increase
  -- precisely the SOP's required entry; and
* ``validate_expense_account`` (``:969-984``) restricts only P&L accounts, and only for opening
  entries. Its own message asks for "a Asset/Liability type account", so a liability account is
  not a workaround here, it is the contemplated case.

A Repack would NOT do: it conserves value by construction, which is the one thing a revaluation
must not do.

QUANTITY AND OWNER DO NOT MOVE
------------------------------
The reconciliation restates the rate and re-asserts the quantity it already found. The custody
event it writes carries ``cg_gross_qty_delta = 0`` -- the customer holds exactly what they held
before, worth more or less than it was.
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
	EVENT_REVALUATION,
	LEDGER_DOCTYPE,
	STAGE_RM,
	STATUS_KNOWN,
	_write_event,
	build_event_key,
	is_ledger_schema_ready,
)
from jewellery_erpnext.customer_subcontracting.customer_gold_rate import (
	resolve_customer_gold_rate_for_date,
)
from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
	get_booked_rate,
	get_returnable_qty,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_customer_gold_company_settings,
	get_customer_gold_valuation_policy,
	is_customer_gold_enabled,
)


# Bound on how many times one batch/rate/date may be revalued and cancelled before the
# retry is treated as a loop rather than a correction. See ``_resolve_revaluation_event_key``.
_MAX_REVALUATION_ATTEMPTS = 100


# POST-only: this SUBMITS a Stock Reconciliation, and a state-changing endpoint reachable by
# GET is exactly what the C13 hardening in this same change set fixed on ``update_bom_detail``
# and ``update_tracking_bom_detail``. The standard applies to new endpoints too.
@frappe.whitelist(methods=["POST"])
def revalue_customer_gold(
	company, customer, batch_no, warehouse, item_code, posting_date=None, new_rate=None
):
	"""Restate the whole batch's carrying value at the current approved rate.

	**Whole batch only, deliberately.** A Stock Reconciliation sets the valuation for everything
	in the (item, warehouse, batch) it names -- it has no notion of revaluing 1 g of a 2 g batch.
	Accepting a partial quantity and silently restating all of it would misstate the obligation
	without saying so, which is the worst available outcome. Partial revaluation needs the batch
	split first, and that is not built; see ``_reject_partial``.

	Returns ``(stock_reconciliation_name, delta)``.
	"""
	if not is_customer_gold_enabled():
		frappe.throw(
			frappe._("The Customer Gold flow is not enabled for this site."),
			title=frappe._("Customer Gold Disabled"),
		)

	if not is_ledger_schema_ready():
		frappe.throw(
			frappe._(
				"This site's Customer Gold Ledger Entry schema is incomplete, so a "
				"revaluation cannot be recorded."
			),
			title=frappe._("Customer Gold Schema Incomplete"),
		)

	if get_customer_gold_valuation_policy() != VALUATION_NOMINAL:
		# Under Zero Value there is no carrying amount to restate. Posting one would create the
		# very liability the policy says was never taken on.
		frappe.throw(
			frappe._(
				"Revaluation applies only under the Nominal valuation policy. This site "
				"records customer gold at zero value."
			),
			title=frappe._("Not Applicable Under Zero Value"),
		)

	frappe.has_permission("Stock Reconciliation", ptype="create", throw=True)

	owner = frappe.db.get_value("Batch", batch_no, "custom_customer")
	if owner != customer:
		# Neither the owner nor the batch id is named: batch ids embed the customer's code.
		frappe.throw(
			frappe._("The selected batch is not held for {0}.").format(
				frappe.bold(customer)
			),
			title=frappe._("Customer Gold Entitlement"),
		)

	free = get_returnable_qty(company, customer, batch_no, warehouse, item_code)
	if free <= 0:
		frappe.throw(
			frappe._(
				"None of this batch is free to revalue -- it is already delivered, or has "
				"left {0} for manufacturing."
			).format(frappe.bold(warehouse)),
			title=frappe._("No Free Customer Gold"),
		)

	_reject_partial(batch_no, warehouse, item_code, free)
	_reject_zero_value_item(item_code)

	booked_rate = get_booked_rate(company, customer, batch_no)
	if booked_rate is None:
		frappe.throw(
			frappe._(
				"Batch {0} has no booked carrying value, so there is no baseline to revalue "
				"from."
			).format(frappe.bold(batch_no)),
			title=frappe._("No Booked Carrying Value"),
		)

	posting_date = posting_date or frappe.utils.nowdate()

	# The CURRENT approved rate -- the opposite rule from a return, and the reason this resolver
	# is called here and deliberately not there.
	if new_rate is None:
		new_rate = flt(resolve_customer_gold_rate_for_date(posting_date).per_gram_rate)
	new_rate = flt(new_rate)

	delta = flt((new_rate - flt(booked_rate)) * free, 2)

	# IDENTITY OF THE BUSINESS OPERATION, NOT OF THE DOCUMENT IT CREATES.
	#
	# This key used to be built from ``entry`` -- the Stock Reconciliation minted a few lines
	# below -- so every call produced a fresh name, a fresh key, and a row the UNIQUE index on
	# ``cg_event_key`` could never collide with. Revaluing the same batch to the same rate twice
	# therefore posted the delta TWICE. It is not self-correcting either: ``get_booked_rate``
	# reads ``Receipt`` events only, so the baseline never moves and the second call computes
	# the same non-zero delta as the first.
	#
	# Settlement already had this protection through ``cg_settlement_voucher``; revaluation had
	# none. Keyed on what actually identifies the operation -- this batch, this rate, this
	# posting date -- a repeat now collides.
	#
	# The rate is formatted at fixed precision so that 7500 and 7500.0 produce one key rather
	# than two.
	event_key = _resolve_revaluation_event_key(company, batch_no, posting_date, new_rate)

	entry = _build_revaluation_entry(
		company,
		batch_no,
		warehouse,
		item_code,
		free,
		new_rate,
		booked_rate,
		posting_date,
	)

	_write_event(
		cg_event_key=event_key,
		cg_event_kind=EVENT_REVALUATION,
		cg_stage=STAGE_RM,
		company=company,
		customer=customer,
		reference_doctype="Stock Reconciliation",
		reference_docname=entry,
		item_code=item_code,
		batch_no=batch_no,
		# ZERO ON EVERY BASIS. A revaluation changes what the metal is worth, never how much
		# there is -- so fine gold and reference quantity are zero too, not NULL. Here a
		# measurement WAS taken and its answer is genuinely nothing moved.
		cg_gross_qty_delta=0,
		cg_fine_gold_delta=0,
		cg_reference_qty_delta=0,
		# ...and Known says so. Without these the three zeroes above are byte-identical to the
		# zeroes a column default writes when nothing could be measured, and the statement
		# "nothing moved" would be unreadable from the row.
		cg_fine_measurement_status=STATUS_KNOWN,
		cg_reference_measurement_status=STATUS_KNOWN,
		cg_carrying_value_delta=delta,
		cg_currency=frappe.get_cached_value("Company", company, "default_currency"),
	)

	return entry, delta



def _resolve_revaluation_event_key(company, batch_no, posting_date, new_rate):
	"""The ledger key for this revaluation, or a refusal if it has already been posted.

	IDENTITY OF THE BUSINESS OPERATION, NOT OF THE DOCUMENT IT CREATES.

	The key used to be built from the Stock Reconciliation minted below, so every call produced
	a fresh name, a fresh key, and a row the UNIQUE index on ``cg_event_key`` could never collide
	with. Revaluing the same batch to the same rate twice therefore posted the delta TWICE, and
	it is not self-correcting: ``get_booked_rate`` reads ``Receipt`` events only, so the baseline
	never moves and the second call computes the same non-zero delta as the first.

	Keyed on what actually identifies the operation -- this batch, this rate, this posting date --
	a repeat collides. The rate is formatted at fixed precision so 7500 and 7500.0 give one key.

	WHY A ROW IS NOT ENOUGH TO REFUSE ON.

	Nothing reverses a revaluation event when its Stock Reconciliation is cancelled: ``hooks.py``
	registers no ``on_cancel`` for Stock Reconciliation, and ``_reverse_events`` is wired only to
	the delivery and stock-entry paths. ``Customer Gold Ledger Entry`` is ``is_submittable: 0``,
	so frappe's ``check_if_doc_is_linked`` never blocks that cancel either. The row therefore
	outlives the document that justified it.

	Refusing on the row alone would mean that cancelling a revaluation -- a routine correction --
	permanently barred that batch from being revalued to that rate on that date ever again, with
	no way out but a manual delete.

	WHY THE DEAD KEY CANNOT SIMPLY BE REUSED.

	``cg_event_key`` carries a UNIQUE index (``unique: 1``, and a real unique index in MariaDB).
	Letting the retry through on the original key would submit the Stock Reconciliation and then
	fail the ledger insert on that index -- leaving a submitted document that moved stock value
	with no custody event to explain it. That is the exact state the pre-check exists to prevent.

	So a superseded attempt does not free its key; it consumes it, and the retry is allocated the
	next slot. The first attempt keeps the bare key, so the common path is byte-identical to
	before.
	"""
	for attempt in range(1, _MAX_REVALUATION_ATTEMPTS + 1):
		discriminator = f"{batch_no}|{posting_date}|{flt(new_rate):.6f}"
		if attempt > 1:
			discriminator = f"{discriminator}|attempt-{attempt}"

		key = build_event_key(
			company,
			"Stock Reconciliation",
			discriminator,
			batch_no,
			EVENT_REVALUATION,
		)

		prior = frappe.get_all(
			LEDGER_DOCTYPE,
			filters={"cg_event_key": key},
			fields=["name", "reference_doctype", "reference_docname"],
			limit=1,
		)
		if not prior:
			# CHECKED BEFORE THE STOCK RECONCILIATION IS BUILT, DELIBERATELY. The caller
			# submits a document that moves stock value on the strength of this key being
			# free. The UNIQUE index stays as the backstop for a genuine race.
			return key

		if _revaluation_event_is_live(prior[0]):
			frappe.throw(
				frappe._(
					"Batch {0} has already been revalued to {1} on {2}. Revaluing it again "
					"would post the difference a second time against metal that has not moved."
				).format(
					frappe.bold(batch_no),
					frappe.bold(frappe.utils.fmt_money(new_rate)),
					frappe.bold(str(posting_date)),
				),
				title=frappe._("Customer Gold Already Revalued"),
			)

	# Only reachable if the same batch/rate/date has been revalued and cancelled a hundred
	# times. That is not a correction pattern, it is a loop, and inventing a hundred-and-first
	# key would hide it.
	frappe.throw(
		frappe._(
			"Batch {0} has been revalued to {1} on {2} and cancelled {3} times. Refusing to "
			"allocate another attempt -- investigate why the correction keeps being undone."
		).format(
			frappe.bold(batch_no),
			frappe.bold(frappe.utils.fmt_money(new_rate)),
			frappe.bold(str(posting_date)),
			_MAX_REVALUATION_ATTEMPTS,
		),
		title=frappe._("Customer Gold Revaluation Retried Too Often"),
	)


def _revaluation_event_is_live(row):
	"""Does this ledger row still describe value that is actually posted?

	Judged by the SOURCE VOUCHER's docstatus, because the row itself has none -- the ledger is
	not submittable. A cancelled Stock Reconciliation has moved no value, so the operation it
	recorded has not happened and may be performed again.

	A row with no usable reference is treated as LIVE. That is the safe direction: it refuses a
	second posting rather than risking a double one.
	"""
	if row.get("reference_doctype") != "Stock Reconciliation" or not row.get("reference_docname"):
		return True

	docstatus = frappe.db.get_value(
		row["reference_doctype"], row["reference_docname"], "docstatus"
	)
	if docstatus is None:
		# Hard-deleted. No document, no posting -- same as cancelled.
		return False

	return cint(docstatus) != 2


def _reject_partial(batch_no, warehouse, item_code, free):
	"""Refuse when the free quantity is not the whole batch in this warehouse.

	Stock Reconciliation restates the entire (item, warehouse, batch) balance. If some of the
	batch is elsewhere -- issued to a job, already delivered -- restating "the batch" would also
	restate metal this caller never selected and may not be entitled to revalue.

	Blocking is the honest outcome while batch splitting is unbuilt. The alternative is a silent
	misstatement of the obligation, which no error message can undo later.
	"""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	total = flt(get_batch_qty(batch_no=batch_no, item_code=item_code))
	here = flt(
		get_batch_qty(batch_no=batch_no, warehouse=warehouse, item_code=item_code)
	)

	if flt(free, 3) < flt(here, 3) or flt(here, 3) < flt(total, 3):
		frappe.throw(
			frappe._(
				"Only part of this batch is free in {0} ({1} of {2}), and a revaluation "
				"restates the whole batch. Split the free quantity into its own batch first."
			).format(frappe.bold(warehouse), frappe.bold(free), frappe.bold(total)),
			title=frappe._("Partial Revaluation Not Supported"),
		)


def _reject_zero_value_item(item_code):
	"""``is_customer_provided_item`` silently zeroes the rate we are about to set.

	``StockReconciliation.set_zero_value_for_customer_provided_items``
	(``stock_reconciliation.py:986-995``) rewrites ``valuation_rate`` to 0 for any item carrying
	that flag. On an item flagged that way the revaluation would appear to succeed and post the
	customer's entire carrying value away. Caught here rather than discovered in a ledger.
	"""
	if frappe.get_cached_value("Item", item_code, "is_customer_provided_item"):
		frappe.throw(
			frappe._(
				"Item {0} is marked 'Is Customer Provided Item', which forces its "
				"reconciliation value to zero. Clear that flag before revaluing customer gold."
			).format(frappe.bold(item_code)),
			title=frappe._("Item Would Be Zero-Valued"),
		)


def _build_revaluation_entry(
	company, batch_no, warehouse, item_code, qty, new_rate, current_rate, posting_date
):
	"""The Stock Reconciliation, with the liability account as its contra leg."""
	accounts = get_customer_gold_company_settings(company)

	entry = frappe.new_doc("Stock Reconciliation")
	entry.company = company
	entry.purpose = "Stock Reconciliation"
	entry.posting_date = posting_date
	entry.set_posting_time = 1
	# The contra leg. Naming the liability here is what turns a stock-value restatement into the
	# SOP's Dr Stock / Cr Customer Gold Liability.
	entry.expense_account = accounts.liability_account
	# ``current_qty`` IS THE LOAD-BEARING FIELD HERE. ``current_valuation_rate`` is not.
	#
	# ``set_current_serial_and_batch_bundle`` returns early for a ``use_serial_batch_fields`` row
	# during validation -- ``stock_reconciliation.py:243`` is a bare
	# ``if not save and item.use_serial_batch_fields: continue`` -- so erpnext never resolves the
	# batch's existing value for itself. Left unset it reads as ZERO, and the reconciliation then
	# posts the ENTIRE new value as the difference: Rs.15,000 credited to the liability instead of
	# Rs.670.34. The integration suite caught exactly that, and the arithmetic looked right in the
	# returned delta while the GL was wrong -- which is why the GL is asserted separately.
	#
	# It cannot be read from the Stock Ledger Entry either: in v16 ``SLE.batch_no`` is normally
	# NULL because the batch lives in the Serial and Batch Bundle, so a query by batch returns no
	# rows and would confirm the same false zero.
	#
	# WHICH of the two fields fixes it was settled by mutation, not by reading:
	#   * removing ``current_qty``            -> the Rs.15,000 bug returns;
	#   * removing ``current_valuation_rate`` -> nothing changes.
	# Supplying ``current_qty`` is what lets ``make_bundle_for_current_qty``
	# (``stock_reconciliation.py:131-157``) build the outward bundle, and erpnext then derives the
	# existing rate from that bundle itself. ``current_valuation_rate`` is kept because it states
	# the baseline the delta is computed against and would catch a divergence if erpnext's derived
	# rate ever disagreed with the booked one -- but it is documentation, not the mechanism.
	entry.append(
		"items",
		{
			"item_code": item_code,
			"warehouse": warehouse,
			"batch_no": batch_no,
			"use_serial_batch_fields": 1,
			# Re-asserted, not changed: the quantity is what it already was.
			"qty": qty,
			"valuation_rate": new_rate,
			"current_qty": qty,
			"current_valuation_rate": current_rate,
		},
	)
	entry.flags.ignore_permissions = True
	entry.save()
	entry.submit()
	return entry.name


@frappe.whitelist()
def get_revaluation_history(company, customer, batch_no=None):
	"""Every revaluation of this customer's metal, for the SOP Sec 8 'Revalued' report.

	WHY THIS IS WHITELISTED AND PERMISSION-GATED, RATHER THAN DELETED
	-----------------------------------------------------------------
	It had no caller anywhere -- a bench-wide grep returned exactly one line, its own definition
	-- and no decorator, so nothing could reach it. Dead code in a module that posts stock and
	liability is worth removing rather than carrying.

	It survives because the SOP names the consumer: Sec 8 lists "Revalued" among the required
	reports, so this is unfinished rather than abandoned, and the query it already implements is
	the right one.

	Completing it means gating it. It reads custody rows for an arbitrary ``company`` and
	``customer`` passed by the caller, so without a check any authenticated user could enumerate
	another customer's revaluation history -- the disclosure this module is careful about
	everywhere else. The check is on the LEDGER, not on Stock Entry as the two writers use: those
	create documents, this one reads events, and ``Customer Gold Ledger Entry`` grants read to
	System Manager, Stock Manager and Accounts Manager only.

	GET is correct here and deliberate. Its two siblings in this module are ``methods=["POST"]``
	because they submit documents; this one changes nothing.
	"""
	frappe.has_permission(LEDGER_DOCTYPE, ptype="read", throw=True)

	filters = {
		"company": company,
		"customer": customer,
		"cg_event_kind": EVENT_REVALUATION,
	}
	if batch_no:
		filters["batch_no"] = batch_no

	return frappe.get_all(
		LEDGER_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"batch_no",
			"item_code",
			"cg_carrying_value_delta",
			"cg_currency",
			"reference_docname",
			"creation",
		],
		order_by="creation asc",
	)
