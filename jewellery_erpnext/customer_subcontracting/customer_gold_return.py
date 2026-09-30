# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Return unused customer gold at its BOOKED carrying value -- the SOP's Example D.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
The SOP states it twice, once as a business rule and once inside the worked example:

    "If unused gold is returned, use its booked carrying nominal value; do not fetch a new rate."
    "When giving unused gold back, use the booked carrying nominal rate. Do not use the latest
     market rate."

So this module deliberately does **not** call ``resolve_customer_gold_rate_for_date``. That
resolver is correct for a receipt, where today's quote is the thing being booked, and wrong here,
where the obligation was fixed the day the metal arrived. Returning 2 g booked at Rs.7,164.83/g
clears Rs.14,329.66 whatever gold is worth this morning -- and if the market has moved to
Rs.7,500 the difference belongs to the customer's next order through an explicit revaluation, not
silently to the return.

WHY THE ACCOUNTING NEEDS NO JOURNAL ENTRY
-----------------------------------------
The receipt credits the liability by putting it on the row's ``expense_account`` and letting
``StockController`` post the contra leg (``customer_gold_receipt.apply_valuation_policy``). An
outgoing Stock Entry with the same account posts the same pair in reverse -- **Cr Stock /
Dr Customer Gold Liability** -- which is exactly the SOP's required net effect. Adding a JE on top
would be the duplicate posting S07 Sec 7.1 forbids.

That symmetry is the reason this is a Stock Entry and not a bespoke document.

WHERE THE BOOKED RATE COMES FROM
--------------------------------
From the custody ledger's own ``Receipt`` event -- ``cg_carrying_value_delta / cg_gross_qty_delta``
for the batch being returned. Not from the Stock Entry's ``custom_gold_rate_per_gram``, even though
that field holds the same number, because the ledger row is the record that the liability was
actually created against and is what a reconciliation reads. One source of truth, and it is the one
the accounts were posted from.

WHAT IS GUARDED, AND WHAT IS NOT YET RECORDABLE
-----------------------------------------------
A return is refused unless the metal is genuinely free:

* the customer must still own that quantity in the ledger, and
* the metal must physically still be in the custody warehouse.

The second test is what currently answers "is it allocated, in WIP, or in FG" -- because metal in
any of those states has physically left the custody warehouse. It is an honest guard, not the
intended one: ``Allocation``, ``Release`` and ``Production`` events are still never written, so
``cg_stage`` cannot yet distinguish reserved-but-present metal from free metal. When those events
land, this check tightens rather than changes shape.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
	EVENT_RECEIPT,
	EVENT_RETURN,
	LEDGER_DOCTYPE,
	STAGE_RM,
	_write_event,
	build_event_key,
	is_ledger_schema_ready,
	quantity_basis,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_customer_gold_company_settings,
	get_customer_gold_settings,
	get_customer_gold_valuation_policy,
	is_customer_gold_enabled,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)


def get_booked_rate(company, customer, batch_no):
	"""The rate this batch's metal was booked at, from the Receipt events that created it.

	Returns ``None`` when the batch has no valued Receipt event -- a batch received under Zero
	Value, or one that predates the custody ledger. The caller blocks rather than guessing: a
	return valued at a rate nobody booked is worse than a return that does not happen.

	Returns ``0.0`` for a batch that WAS valued, at zero: a stone received under Nominal at a
	typed rate of 0. The value column cannot tell that apart from "never valued" (it is ``NOT
	NULL DEFAULT 0``), but ``cg_currency`` can -- ``record_receipt`` stamps it only when valuing.

	Averaged across Receipt events rather than taking the latest, because one batch can legitimately
	carry metal from two receipts at different rates and the obligation is their sum, not the more
	recent of them.
	"""
	rows = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={
			"company": company,
			"customer": customer,
			"batch_no": batch_no,
			"cg_event_kind": EVENT_RECEIPT,
		},
		fields=["cg_gross_qty_delta", "cg_carrying_value_delta", "cg_currency"],
	)

	qty = flt(sum(flt(r.cg_gross_qty_delta) for r in rows))
	value = flt(
		sum(flt(r.cg_carrying_value_delta) for r in rows if r.cg_carrying_value_delta)
	)

	if qty <= 0:
		return None
	if not value:
		return 0.0 if any(r.cg_currency for r in rows) else None

	return value / qty


def get_returnable_qty(company, customer, batch_no, warehouse, item_code):
	"""How much of this batch may be handed back: owned AND physically present.

	The minimum of the two, because either alone is wrong. Ledger position alone would allow
	returning metal already issued to the shop floor; physical stock alone would allow returning
	another customer's metal that happens to sit in the same warehouse.
	"""
	# ``get_batch_qty``, not ``get_stock_balance``: the latter has no batch parameter in v16
	# (``erpnext/stock/utils.py:96-104``) and would return the warehouse total for the item --
	# which on a shared custody warehouse is every customer's metal at once.
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	owned = flt(
		sum(
			flt(r.cg_gross_qty_delta)
			for r in frappe.get_all(
				LEDGER_DOCTYPE,
				filters={
					"company": company,
					"customer": customer,
					"batch_no": batch_no,
				},
				fields=["cg_gross_qty_delta"],
			)
		)
	)

	physical = flt(
		get_batch_qty(batch_no=batch_no, warehouse=warehouse, item_code=item_code)
	)

	return max(min(owned, physical), 0.0)


# POST-only: this SUBMITS a Stock Entry. See ``revalue_customer_gold`` for the reasoning --
# both are state-changing and neither should be reachable by a GET.
@frappe.whitelist(methods=["POST"])
def make_customer_gold_return(
	company, customer, batch_no, qty, warehouse, item_code, posting_date=None
):
	"""Hand unused customer gold back. Returns the submitted Stock Entry name.

	Whitelisted, so every check here is a server-side check: the caller's permission on Stock
	Entry, the ownership of the batch, the eligibility of the quantity and the existence of a
	booked rate. None of it may be assumed from the UI having offered the action.
	"""
	qty = flt(qty)

	if not is_customer_gold_enabled():
		frappe.throw(
			frappe._("The Customer Gold flow is not enabled for this site."),
			title=frappe._("Customer Gold Disabled"),
		)

	if not is_ledger_schema_ready():
		frappe.throw(
			frappe._(
				"This site's Customer Gold Ledger Entry schema is incomplete, so a return "
				"cannot be recorded. Complete the schema migration first."
			),
			title=frappe._("Customer Gold Schema Incomplete"),
		)

	frappe.has_permission("Stock Entry", ptype="create", throw=True)

	if qty <= 0:
		frappe.throw(frappe._("Return quantity must be greater than zero."))

	_validate_batch_owner(batch_no, customer)

	# Converted metal is named for what it is, straight after the owner check. This only ADDS a
	# refusal: a batch with a booked rate, or with no recorded lineage, meets the checks below in
	# the order it always did.
	booked_rate = get_booked_rate(company, customer, batch_no)
	if booked_rate is None:
		_refuse_converted_metal(company, customer, batch_no)

	available = get_returnable_qty(company, customer, batch_no, warehouse, item_code)
	if qty > available:
		frappe.throw(
			frappe._(
				"Only {0} of batch {1} is free to return -- the rest is either already "
				"delivered or has left {2} for manufacturing. Requested {3}."
			).format(
				frappe.bold(available),
				frappe.bold(batch_no),
				frappe.bold(warehouse),
				frappe.bold(qty),
			),
			title=frappe._("Insufficient Free Customer Gold"),
		)

	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	if not nominal:
		# Zero Value: nothing is released, whatever the batch was booked at.
		booked_rate = None

	if nominal and booked_rate is None:
		frappe.throw(
			frappe._(
				"Batch {0} has no booked carrying value, so the amount to release from the "
				"Customer Gold Liability cannot be established. Returning it at a current "
				"market rate is not permitted."
			).format(frappe.bold(batch_no)),
			title=frappe._("No Booked Carrying Value"),
		)

	# The Return event and its receipt allocation are written by ``record_return`` -- the
	# Stock Entry's own on_submit hook -- so this API and the desk "Create > Issue" button go
	# through exactly one writer and cannot disagree.
	se = _build_return_entry(
		company,
		customer,
		batch_no,
		qty,
		warehouse,
		item_code,
		booked_rate,
		posting_date,
	)
	return se.name


def _validate_batch_owner(batch_no, customer):
	"""Refuse outright to hand one customer's metal to another."""
	owner = frappe.db.get_value("Batch", batch_no, "custom_customer")
	if owner != customer:
		# NEITHER the owner NOR THE BATCH may be named. ``batch_rename`` builds batch ids as
		# ``{customer}-{...}-{item}-{seq}``, so the batch id CONTAINS the owning customer's code
		# -- printing it discloses the owner just as surely as printing the owner. The same trap
		# caught the delivery entitlement guard, and the same assertion caught it again here.
		frappe.throw(
			frappe._(
				"The selected batch is not held for {0}, so it cannot be returned to them."
			).format(frappe.bold(customer)),
			title=frappe._("Customer Gold Entitlement"),
		)


def _refuse_converted_metal(company, customer, batch_no):
	"""Refuse to return CONVERTED customer metal, naming the receipts it was made from.

	A conversion mints a batch with no Receipt event of its own, so ``get_booked_rate`` has
	nothing for it, and "no booked carrying value" was all this API used to say -- the wrong
	cause, since the metal WAS booked, on the receipts it came from. The refusal itself is
	deliberate: whether converted metal may be returned against a receipt, in what unit and at
	what value, and who bears the alloy inside it, is Finance decision D07/D08.

	Another item or purity is refused naming the receipts and those decisions, and sends the
	user to Accounts. It prescribes no route. ``_validate_descendant``'s route, "Material
	Request > Settle, then Create > Issue", failed when it was run on MCON00333's shape: the
	desk's Material Request is same-warehouse with no customer, Settle draws receipts
	oldest-first, its mirror leg found no target batch, and the Issue after it is a
	receipt-batch return that skips the D08 tolerance (Rs 40.14 over-released, 2.893 g
	stranded). The same item keeps ``_resolve_return_row``'s "Create > Issue" wording, since
	that return is held to the D08 tolerance.

	Which of the two it is follows the batch's OWN item, never the caller's ``item_code``, so a
	wrong ``item_code`` cannot make the message misstate what the batch is.

	Returns quietly when the batch has no recorded lineage (``lineage_receipts``), leaving the
	refusal it always had. Only the verified owner's receipts are ever read or named.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		lineage_receipts,
	)

	receipts = lineage_receipts(company, customer, batch_no)
	if not receipts:
		return

	batch_item = frappe.db.get_value("Batch", batch_no, "item")
	received = list(dict.fromkeys(r.item_code for r in receipts))
	if any(item != batch_item for item in received):
		frappe.throw(
			frappe._(
				"Batch {0} is {1} made from {2} received on {3}. Returning a different item or "
				"purity against a receipt is not an approved settlement: whether converted "
				"metal may be returned, in what unit and at what value, awaits Finance "
				"decisions D07/D08. Ask Accounts how to proceed."
			).format(
				frappe.bold(batch_no),
				frappe.bold(batch_item),
				", ".join(frappe.bold(item) for item in received),
				_receipt_names(receipts),
			),
			title=frappe._("Customer Gold Return: Different Purity"),
		)

	frappe.throw(
		frappe._(
			"Batch {0} was made from customer gold received on {1} and has no receipt of its "
			"own. Create the return from the receipt (Create > Issue) so it names the receipt "
			"row it gives back."
		).format(frappe.bold(batch_no), _receipt_names(receipts)),
		title=frappe._("Customer Gold Return"),
	)


def _receipt_names(receipts):
	"""Each receipt voucher once, in the order given, for a message."""
	return ", ".join(
		frappe.bold(name)
		for name in dict.fromkeys(r.reference_docname for r in receipts)
	)


def _build_return_entry(
	company, customer, batch_no, qty, warehouse, item_code, booked_rate, posting_date
):
	"""The outgoing Stock Entry, valued at the booked rate.

	``set_basic_rate_manually`` is what makes the booked rate survive: erpnext's loop at
	``stock_entry.py:1615-1619`` takes ``continue`` for such a row, so the rate is not
	recalculated from the batch's current valuation. Same mechanism the nominal receipt relies on,
	used in the opposite direction.
	"""
	settings = get_customer_gold_settings()
	entry_type = settings.get("customer_gold_return_stock_entry_type")
	if not entry_type:
		frappe.throw(
			frappe._(
				"Set the Customer Gold Return Stock Entry Type in {0} before returning "
				"customer gold."
			).format(frappe.bold(frappe._("Subcontracting Settings"))),
			title=frappe._("Customer Gold Configuration Missing"),
		)

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = entry_type
	se.company = company
	se._customer = customer
	if posting_date:
		se.posting_date = posting_date
		se.set_posting_time = 1

	row = {
		"item_code": item_code,
		"qty": qty,
		"s_warehouse": warehouse,
		"batch_no": batch_no,
		"use_serial_batch_fields": 1,
		"inventory_type": "Customer Goods",
		"customer": customer,
	}

	if booked_rate:
		row["basic_rate"] = booked_rate
		row["set_basic_rate_manually"] = 1
		row["allow_zero_valuation_rate"] = 0
		# THE CONTRA LEG, and the reason no Journal Entry is needed. An outgoing Stock Entry
		# credits stock and debits the row's expense_account -- so naming the liability account
		# here posts Cr Stock / Dr Customer Gold Liability, the SOP's required net effect.
		row["expense_account"] = get_customer_gold_company_settings(
			company
		).liability_account
	else:
		row["allow_zero_valuation_rate"] = 1

	se.append("items", row)
	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return se


# ------------------------------------------------------------------------------------------
# Receipt-linked return: every Stock Entry of the configured return type
# ------------------------------------------------------------------------------------------
#
# WHY HOOKS, AND NOT ONLY THE API
# -------------------------------
# Accounts return unused gold from the receipt itself: Customer Goods Received -> Create ->
# Issue (``doc_events/stock_entry.make_stock_in_entry``). That desk path built a Stock Entry of
# the configured return type and submitted it with no custody event, no liability leg and no
# check against what the receipt still had to give back -- ``record_stock_movement`` skips the
# return type because ``make_customer_gold_return`` used to write the event itself, and nothing
# ever called that API. So a raw return left the Customer Gold Liability standing and the ledger
# still showing the gold as held, and the same receipt could be returned twice.
#
# Hooking the Stock Entry catches both paths with one writer.

#: How far a DESCENDANT batch's own rate may sit from the rate its receipt booked, relative. A
#: conversion rounds its target quantity to 3 dp, which moves a per-gram rate by well under
#: 0.01% on any real lot; company alloy that carried value into the lane moves it by whole
#: percents. 0.05% separates the two with room to spare, and anything beyond it is a valuation
#: question (decision D08) rather than a return.
DESCENDANT_RATE_TOLERANCE = 0.0005


def _applies(doc):
	from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
		_is_customer_gold_return,
	)

	return (
		doc.doctype == "Stock Entry"
		and is_customer_gold_enabled()
		and is_ledger_schema_ready()
		and _is_customer_gold_return(doc)
	)


def _return_plans(doc):
	"""One resolved plan per TRACKED row: which receipt row it returns against, from which batch,
	at which booked rate. Raises on a tracked row that is not a legitimate return.

	Rows the custody ledger has never tracked -- no batch, company stock, or a customer batch
	received before the ledger existed -- are left exactly as the desk Issue always left them:
	no link, no event, no entitlement check. Refusing them would strand legacy customer goods
	that were received while the flow was off.
	"""
	plans = (_resolve_return_row(doc, row) for row in doc.get("items") or [])
	return [plan for plan in plans if plan]


def _has_ledger_history(batch_no):
	return bool(frappe.db.exists(LEDGER_DOCTYPE, {"batch_no": batch_no}))


def _resolve_return_row(doc, row):
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		booked_rate_of,
		effective_receipt_events,
		receipts_of_batch,
	)
	from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
		_batch_owner,
		_row_batches,
	)

	batches = _row_batches(row)
	if not batches:
		return None
	if len(batches) > 1:
		if any(_batch_owner(b) and _has_ledger_history(b) for b in batches):
			frappe.throw(
				frappe._(
					"Row {0}: a Customer Gold return must name exactly one batch, so it can be "
					"matched to the receipt it gives back."
				).format(row.idx),
				title=frappe._("Customer Gold Return"),
			)
		return None
	batch_no = batches[0]
	owner = _batch_owner(batch_no)
	if not owner:
		# Company stock: not customer gold, so nothing here governs it.
		return None

	receipt = None
	if row.get("against_stock_entry") and row.get("ste_detail"):
		found = effective_receipt_events(
			{
				"reference_docname": row.against_stock_entry,
				"cg_source_row": row.ste_detail,
			}
		)
		if not found and not _voucher_has_receipt_events(row.against_stock_entry):
			# Received before the custody ledger existed: nothing to link or enforce.
			return None
		if len(found) != 1:
			frappe.throw(
				frappe._(
					"Row {0}: receipt {1} has no effective Customer Gold receipt for the "
					"linked row -- it was cancelled, or was not a Customer Gold receipt."
				).format(row.idx, frappe.bold(row.against_stock_entry)),
				title=frappe._("Customer Gold Return"),
			)
		receipt = found[0]
	elif doc.get("custom_cg_issue_against"):
		found = effective_receipt_events(
			{"reference_docname": doc.custom_cg_issue_against}
		)
		if not found and not _voucher_has_receipt_events(doc.custom_cg_issue_against):
			return None
		matching = [r for r in found if r.batch_no == batch_no] or (
			found if len(found) == 1 else []
		)
		if len(matching) != 1:
			frappe.throw(
				frappe._(
					"Row {0}: select the row of receipt {1} this quantity is returned against."
				).format(row.idx, frappe.bold(doc.custom_cg_issue_against)),
				title=frappe._("Customer Gold Return"),
			)
		receipt = matching[0]
	else:
		found = receipts_of_batch(doc.company, owner, batch_no)
		if not found and not _has_ledger_history(batch_no):
			return None
		if len(found) != 1:
			frappe.throw(
				frappe._(
					"Row {0}: the batch does not identify a single Customer Gold receipt. "
					"Create the return from the receipt (Create > Issue) so it names the "
					"receipt row it gives back."
				).format(row.idx),
				title=frappe._("Customer Gold Return"),
			)
		receipt = found[0]

	if receipt.company != doc.company or receipt.customer != owner:
		# Same rule as ``_validate_batch_owner``: name neither the owner nor the batch.
		frappe.throw(
			frappe._(
				"Row {0}: the selected batch is not held for the customer of receipt {1}, so "
				"it cannot be returned against it."
			).format(row.idx, frappe.bold(receipt.reference_docname)),
			title=frappe._("Customer Gold Entitlement"),
		)

	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	if batch_no == receipt.batch_no:
		plan_kind = "receipt"
		# Unchanged from ``make_customer_gold_return``: the batch's own receipts' booked rate.
		booked_rate = get_booked_rate(doc.company, owner, batch_no) if nominal else None
	else:
		plan_kind = "descendant"
		_validate_descendant(row, batch_no, receipt)
		booked_rate = booked_rate_of(receipt) if nominal else None

	if nominal and booked_rate is None:
		frappe.throw(
			frappe._(
				"Row {0}: receipt {1} has no booked carrying value, so the amount to release "
				"from the Customer Gold Liability cannot be established. Returning it at a "
				"current market rate is not permitted."
			).format(row.idx, frappe.bold(receipt.reference_docname)),
			title=frappe._("No Booked Carrying Value"),
		)

	return frappe._dict(
		row=row,
		batch_no=batch_no,
		customer=owner,
		receipt=receipt,
		kind=plan_kind,
		booked_rate=booked_rate,
		# From qty x factor, not transfer_qty: at before_validate transfer_qty is still whatever
		# the mapper copied, so an edited qty would be checked against a stale figure until submit.
		qty=flt(row.get("qty")) * (flt(row.get("conversion_factor")) or 1.0),
		warehouse=row.get("s_warehouse"),
	)


def _voucher_has_receipt_events(voucher):
	"""Whether ANY Receipt event -- effective or reversed -- was ever written for ``voucher``."""
	return bool(
		frappe.db.exists(
			LEDGER_DOCTYPE,
			{"reference_docname": voucher, "cg_event_kind": EVENT_RECEIPT},
		)
	)


def _validate_descendant(row, batch_no, receipt):
	"""A batch other than the receipt's own may be returned against it only when it IS the
	receipt's metal -- produced from it -- and is the same item the customer handed in.

	Returning a different purity (22KT against a 24KT receipt) is refused rather than priced.
	How much 22KT settles a 24KT obligation, at what rate and who bears the alloy, is decision
	D07/D08; the supported route is to convert it back first (the Material Request's Settle
	action) and return the receipt's own item.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_trace import (
		discover_scope,
	)

	if row.get("item_code") != receipt.item_code:
		frappe.throw(
			frappe._(
				"Row {0}: receipt {1} received {2}; this row returns {3}. Returning a "
				"different item or purity against a receipt is not an approved settlement. "
				"Convert it to {2} first (Material Request > Settle), then return that."
			).format(
				row.idx,
				frappe.bold(receipt.reference_docname),
				frappe.bold(receipt.item_code),
				frappe.bold(row.get("item_code")),
			),
			title=frappe._("Customer Gold Return: Different Purity"),
		)

	if batch_no not in discover_scope({receipt.batch_no}):
		frappe.throw(
			frappe._(
				"Row {0}: the selected batch was not produced from receipt {1}'s metal, so it "
				"cannot be returned against that receipt."
			).format(row.idx, frappe.bold(receipt.reference_docname)),
			title=frappe._("Customer Gold Return"),
		)


def _batch_rate(batch_no, warehouse):
	"""The batch's own carrying rate in ``warehouse``: value over quantity of its effective
	bundle entries. This is what the outgoing Stock Ledger Entry will actually be valued at
	under batch-wise valuation, and therefore what the liability leg will actually post."""
	row = frappe.db.sql(
		"""
		SELECT SUM(sbe.qty), SUM(sbe.stock_value_difference)
		FROM `tabSerial and Batch Entry` sbe
		JOIN `tabSerial and Batch Bundle` sbb ON sbb.name = sbe.parent
		WHERE sbe.batch_no = %s AND sbe.warehouse = %s
			AND sbb.is_cancelled = 0 AND sbb.docstatus = 1
		""",
		(batch_no, warehouse),
	)
	qty, value = (row[0] if row else (None, None)) or (None, None)
	return (flt(value) / flt(qty)) if flt(qty) > 0 else None


def _check_plans(doc, plans, for_update):
	"""Quantity, entitlement and valuation checks. Called twice: at validate for an early,
	readable answer, and again at before_submit under a lock for the one that counts."""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	requested = {}
	for plan in plans:
		key = (plan.batch_no, plan.warehouse)
		requested[key] = requested.get(key, 0.0) + plan.qty

	for plan in plans:
		if plan.qty <= 0:
			frappe.throw(
				frappe._("Row {0}: return quantity must be greater than zero.").format(
					plan.row.idx
				)
			)

	for (batch_no, warehouse), qty in requested.items():
		free = flt(get_batch_qty(batch_no=batch_no, warehouse=warehouse))
		if qty > free + 1e-6:
			frappe.throw(
				frappe._(
					"Only {0} of the selected batch is free in {1} -- the rest is reserved, "
					"on the shop floor or already delivered. This return asks for {2}."
				).format(
					frappe.bold(flt(free, 3)), frappe.bold(warehouse), frappe.bold(qty)
				),
				title=frappe._("Insufficient Free Customer Gold"),
			)

	for plan in plans:
		if plan.kind != "descendant" or plan.booked_rate is None:
			continue
		actual = _batch_rate(plan.batch_no, plan.warehouse)
		if actual is None or abs(actual - plan.booked_rate) > abs(
			plan.booked_rate * DESCENDANT_RATE_TOLERANCE
		):
			frappe.throw(
				frappe._(
					"Row {0}: the batch is carried at {1} per unit, but receipt {2} booked "
					"{3}. Returning it would release a different amount from the Customer Gold "
					"Liability than the stock that leaves. How to settle that difference is an "
					"open accounting decision (D08); the return is held until it is made."
				).format(
					plan.row.idx,
					frappe.bold(flt(actual or 0, 2)),
					frappe.bold(plan.receipt.reference_docname),
					frappe.bold(flt(plan.booked_rate, 2)),
				),
				title=frappe._("Customer Gold Return: Valuation Differs"),
			)

	from jewellery_erpnext.customer_subcontracting.customer_gold_trace import trace

	_check_shared_batch_rates(doc, plans)

	by_receipt = {}
	for plan in plans:
		by_receipt.setdefault(plan.receipt.name, [plan.receipt, 0.0])[1] += plan.qty

	_, replay, _ = trace(doc.company, customer=plans[0].customer)
	remaining = receipt_remaining(
		doc.company,
		plans[0].customer,
		[entry[0] for entry in by_receipt.values()],
		for_update=for_update,
		replay=replay,
	)
	for name, (receipt, qty) in by_receipt.items():
		left = remaining.get(name, 0.0)
		if qty > left + 1e-6:
			frappe.throw(
				frappe._(
					"Receipt {0} row {1} has {2} left to return; this entry returns {3}. The "
					"rest has already been returned or delivered."
				).format(
					frappe.bold(receipt.reference_docname),
					receipt.cg_source_row,
					frappe.bold(flt(max(left, 0), 3)),
					frappe.bold(flt(qty, 3)),
				),
				title=frappe._("Customer Gold Return Exceeds Receipt"),
			)

	_check_receipt_share(plans, replay)


def _check_shared_batch_rates(doc, plans):
	"""A batch holding several receipt rows at DIFFERENT booked rates cannot be returned against
	one of them: the stock credit -- and so the liability released -- is the batch's blended rate,
	not the named receipt's. Which receipt bears the difference is decision D08; until it is made
	the return is held rather than posting a release that matches no receipt."""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		booked_rate_of,
		receipts_of_batch,
	)

	for plan in plans:
		if plan.kind != "receipt" or plan.booked_rate is None:
			continue
		rates = [
			booked_rate_of(r)
			for r in receipts_of_batch(doc.company, plan.customer, plan.batch_no)
		]
		rates = [r for r in rates if r is not None]
		if len(rates) > 1 and max(rates) - min(rates) > abs(
			max(rates) * DESCENDANT_RATE_TOLERANCE
		):
			frappe.throw(
				frappe._(
					"Row {0}: the batch holds metal from {1} receipts booked at different rates "
					"({2} to {3}). Returning it against receipt {4} would release the batch's "
					"blended value, not that receipt's. This is an open accounting decision (D08)."
				).format(
					plan.row.idx,
					len(rates),
					frappe.bold(flt(min(rates), 2)),
					frappe.bold(flt(max(rates), 2)),
					frappe.bold(plan.receipt.reference_docname),
				),
				title=frappe._("Customer Gold Return: Valuation Differs"),
			)


def _check_receipt_share(plans, replay):
	"""A return may not take more out of a holding than the named receipt's own share of it.

	Out of a batch two receipts share, anything beyond this receipt's part is the other receipt's
	metal: the replay would charge it to that receipt, cutting an entitlement nobody drew on.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_trace import (
		receipt_key,
	)

	requested = {}
	for plan in plans:
		key = (plan.batch_no, plan.warehouse, plan.receipt.name)
		requested.setdefault(key, [plan, 0.0])[1] += plan.qty

	holdings = {(b, w): shares for b, w, _qty, shares in replay.positions()}
	for (batch_no, warehouse, _name), (plan, qty) in requested.items():
		rkey = receipt_key(plan.receipt.reference_docname, plan.receipt.cg_source_row)
		receipt = replay.receipts.get(rkey)
		shares = holdings.get((batch_no, warehouse))
		if not receipt or shares is None:
			continue
		share = flt(shares.get(rkey))
		per_unit = (
			flt(receipt.get("per_unit")) if receipt.get("unit") == "fine" else 1.0
		)
		if not per_unit:
			per_unit = flt(get_purity_percentage(plan.row.item_code)) / 100.0 or 1.0
		own = share / per_unit
		others = sum(flt(v) for k, v in shares.items() if k != rkey)
		if others > 1e-6 and qty > own + 0.0005:
			frappe.throw(
				frappe._(
					"Row {0}: only {1} of this batch in {2} is receipt {3}'s metal; the rest belongs "
					"to other receipts of the same customer. This return asks for {4}."
				).format(
					plan.row.idx,
					frappe.bold(flt(own, 3)),
					frappe.bold(warehouse),
					frappe.bold(plan.receipt.reference_docname),
					frappe.bold(flt(qty, 3)),
				),
				title=frappe._("Customer Gold Return Exceeds Receipt"),
			)


def receipt_remaining(company, customer, receipts, for_update=False, replay=None):
	"""{receipt event: quantity still returnable against it}, in the receipt item's own unit.

	What has been drawn is the LARGER of two independent measures:

	* the ``Customer Gold Allocation`` rows -- exact, but only for dispositions written since
	  allocations existed; read under a lock when ``for_update``, so two concurrent returns
	  against one receipt serialise and the second sees the first;
	* the traceability replay's delivered + returned share -- derived from the stock ledger, so
	  it also covers deliveries made before allocations existed.

	Loss does NOT reduce it: the customer is still owed metal lost on the floor (SOP §5.5).

	Received is the receipt row's STOCK quantity (``transfer_qty``), the unit every draw is in.
	The ledger's own gross is the row qty in its transaction UOM (the known K43 split), which would
	compare decagrams with grams. ``replay`` lets a caller that already traced the customer reuse it.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		drawn_by_receipt,
	)
	from jewellery_erpnext.customer_subcontracting.customer_gold_trace import (
		DISPOSITION_DELIVERED,
		DISPOSITION_RETURNED,
		receipt_key,
		trace,
	)

	names = sorted(r.name for r in receipts)
	if not names:
		return {}
	if for_update:
		# Serialise on the receipt rows themselves, in a stable order, before reading what has
		# been drawn against them.
		frappe.db.sql(
			"SELECT name FROM `tabCustomer Gold Ledger Entry` WHERE name IN %s ORDER BY name FOR UPDATE",
			(tuple(names),),
		)
	drawn = drawn_by_receipt(names, for_update=for_update)
	if replay is None:
		_, replay, _ = trace(company, customer=customer)
	stock_qty = dict(
		frappe.get_all(
			"Stock Entry Detail",
			filters={"name": ["in", [r.cg_source_row for r in receipts]]},
			fields=["name", "transfer_qty"],
			as_list=True,
		)
	)

	result = {}
	for receipt in receipts:
		received = flt(stock_qty.get(receipt.cg_source_row)) or flt(
			receipt.cg_gross_qty_delta
		)
		allocated = flt((drawn.get(receipt.name) or {}).get("gross"))
		key = receipt_key(receipt.reference_docname, receipt.cg_source_row)
		physical = 0.0
		if key in replay.receipts:
			left = replay.dispositions[key]
			measure = flt(left[DISPOSITION_DELIVERED]) + flt(left[DISPOSITION_RETURNED])
			per_unit = flt(replay.receipts[key].get("per_unit"))
			if not per_unit and replay.receipts[key].get("unit") == "fine":
				per_unit = flt(get_purity_percentage(receipt.item_code)) / 100.0
			physical = measure / per_unit if per_unit else measure
		result[receipt.name] = received - max(allocated, physical)
	return result


def prepare_return_entry(doc, method=None):
	"""``before_validate`` (last) for Stock Entry. Makes a return a receipt-linked return.

	Resolves each row's receipt row and stamps the link onto it (``against_stock_entry`` /
	``ste_detail``, and ``custom_cg_issue_against`` on the header when it is one receipt), sets
	the ownership columns, and -- under Nominal -- the liability contra account, so the stock
	credit posts against the Customer Gold Liability exactly as a receipt posted it.
	"""
	if not _applies(doc):
		return

	plans = _return_plans(doc)
	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	liability = (
		get_customer_gold_company_settings(doc.company).liability_account
		if nominal
		else None
	)

	for plan in plans:
		row = plan.row
		row.inventory_type = "Customer Goods"
		row.customer = plan.customer
		row.against_stock_entry = plan.receipt.reference_docname
		row.ste_detail = plan.receipt.cg_source_row
		if not row.get("serial_and_batch_bundle"):
			# Build the outward bundle from THIS batch. Left at 0 -- copied from a bundle-based
			# receipt row -- erpnext picks by FIFO at submit, whichever customer's metal is oldest.
			row.use_serial_batch_fields = 1
		if nominal and plan.booked_rate:
			row.basic_rate = plan.booked_rate
			row.set_basic_rate_manually = 1
			row.allow_zero_valuation_rate = 0
			row.expense_account = liability
		elif nominal:
			# Booked at zero -- a stone typed at 0. Nothing to release, and a zero-valued
			# batch must be allowed out at zero, exactly as ``_build_return_entry`` does.
			row.allow_zero_valuation_rate = 1

	vouchers = {plan.receipt.reference_docname for plan in plans}
	if len(vouchers) == 1 and not doc.get("custom_cg_issue_against"):
		doc.custom_cg_issue_against = next(iter(vouchers))
	customers = {plan.customer for plan in plans}
	if len(customers) == 1 and not doc.get("_customer"):
		doc._customer = next(iter(customers))

	if plans:
		_check_plans(doc, plans, for_update=False)


def lock_return_entitlement(doc, method=None):
	"""``before_submit`` for Stock Entry: the check that counts, taken under a lock."""
	if not _applies(doc):
		return
	plans = _return_plans(doc)
	if plans:
		_check_plans(doc, plans, for_update=True)


def record_return(doc, method=None):
	"""``on_submit`` for Stock Entry: one Return event and its receipt allocation per row.

	The event key is the one the API used to write -- company, voucher row, kind -- so a
	document submitted before this hook existed and replayed through it collides and is absorbed
	rather than written twice. ``cg_source_event`` names the receipt the metal is returned
	against; this is the field's first writer.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		BASIS_RECEIPT_LINK,
		DISPOSITION_RAW_RETURN,
		allocate_event,
		is_allocation_schema_ready,
	)

	if not _applies(doc):
		return

	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	currency = (
		frappe.get_cached_value("Company", doc.company, "default_currency")
		if nominal
		else None
	)

	plans = _return_plans(doc)
	released = _released_per_row(doc, plans) if nominal else {}
	for plan in plans:
		row = plan.row
		value = released.get(row.name) if nominal else None
		event_name = _write_event(
			cg_event_key=build_event_key(
				doc.company, doc.doctype, row.name, None, EVENT_RETURN
			),
			cg_event_kind=EVENT_RETURN,
			cg_stage=STAGE_RM,
			company=doc.company,
			customer=plan.customer,
			reference_doctype=doc.doctype,
			reference_docname=doc.name,
			cg_source_row=row.name,
			cg_source_event=plan.receipt.name,
			item_code=row.item_code,
			batch_no=plan.batch_no,
			stock_uom=row.get("stock_uom") or row.get("uom"),
			cg_gross_qty_delta=-plan.qty,
			**quantity_basis(row.item_code, -plan.qty, doc.company),
			# Negative: the obligation this return discharged -- exactly what the stock credit
			# posted against the liability, which the booked-rate checks above have already held
			# to the rate the receipt booked (never today's quote).
			cg_carrying_value_delta=-value if nominal else None,
			cg_currency=currency,
		)
		if is_allocation_schema_ready():
			allocate_event(
				frappe._dict(
					name=event_name,
					company=doc.company,
					customer=plan.customer,
					reference_doctype=doc.doctype,
					reference_docname=doc.name,
				),
				[(plan.receipt, plan.qty, value)],
				DISPOSITION_RAW_RETURN,
				BASIS_RECEIPT_LINK,
				currency,
				total_amount=value,
			)


def _released_per_row(doc, plans):
	"""{row: liability released}, to the paisa, tied to the voucher's own GL.

	The amount is the row's outgoing stock value -- the number the liability leg was posted
	from -- not ``booked rate x qty`` recomputed here. The two agree to within rounding (the
	checks before submit hold the batch to the booked rate), but a recomputation lands on the
	other side of a half-paisa often enough to leave the ledger, the allocation and the GL a paisa
	apart (0.5 g at Rs.7,164.83: GL 3,582.41, recomputed 3,582.42). Any residual against the GL
	debit on the liability account lands on the largest row, so the rows sum to the GL exactly.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
		_row_carrying_value,
	)

	values = {}
	for plan in plans:
		posted = _row_carrying_value(doc, plan.row)
		values[plan.row.name] = (
			abs(flt(posted)) if posted is not None else flt(plan.booked_rate) * plan.qty
		)

	liability = get_customer_gold_company_settings(doc.company).liability_account
	debit = frappe.db.sql(
		"""
		SELECT SUM(debit - credit) FROM `tabGL Entry`
		WHERE voucher_type = %s AND voucher_no = %s AND account = %s AND is_cancelled = 0
		""",
		(doc.doctype, doc.name, liability),
	)
	gl_total = flt(debit[0][0]) if debit and debit[0][0] is not None else None

	rounded = {name: flt(value, 2) for name, value in values.items()}
	if gl_total is not None and rounded:
		residual = flt(gl_total - sum(rounded.values()), 2)
		if residual and abs(residual) <= 0.01 * len(rounded):
			largest = max(rounded, key=lambda name: rounded[name])
			rounded[largest] = flt(rounded[largest] + residual, 2)
	return rounded


def fill_return_batch(doc, method=None):
	"""``before_validate`` (FIRST) for Stock Entry: give a receipt-linked return row its batch back.

	Runs before ``update_batches``, which FIFO-fills any batch-tracked row that has no batch --
	and FIFO in a shared custody warehouse is whichever customer's metal is oldest. An amended
	return loses ``batch_no`` (no_copy), and a row mapped from a bundle-only receipt may carry
	none; both would otherwise be refused, or worse, pick another receipt's batch. Also forces
	``use_serial_batch_fields`` so the outward bundle is built from THIS batch, not by FIFO.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		effective_receipt_events,
	)

	if not _applies(doc):
		return

	for row in doc.get("items") or []:
		if not row.get("s_warehouse"):
			continue
		if not row.get("batch_no"):
			receipt = None
			if row.get("against_stock_entry") and row.get("ste_detail"):
				found = effective_receipt_events(
					{
						"reference_docname": row.against_stock_entry,
						"cg_source_row": row.ste_detail,
					}
				)
				receipt = found[0] if len(found) == 1 else None
			elif doc.get("custom_cg_issue_against"):
				found = [
					r
					for r in effective_receipt_events(
						{"reference_docname": doc.custom_cg_issue_against}
					)
					if r.item_code == row.get("item_code")
				]
				receipt = found[0] if len(found) == 1 else None
			if receipt:
				row.batch_no = receipt.batch_no
		if row.get("batch_no") and not row.get("serial_and_batch_bundle"):
			row.use_serial_batch_fields = 1


def block_receipt_cancel_with_dispositions(doc, method=None):
	"""``before_cancel`` for Stock Entry: a receipt with live returns or deliveries stays.

	Cancelling a Customer Gold receipt after metal was returned or delivered against it used to
	be stopped only by chance -- erpnext's negative-batch guard, when the batch was empty. A batch
	refilled by Settle, or a return from a descendant batch, let the cancel through and left
	Return events and allocations pointing at a receipt that no longer exists.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		effective_receipt_events,
		is_allocation_schema_ready,
	)

	if doc.doctype != "Stock Entry" or not is_ledger_schema_ready():
		return
	receipts = [
		e.name for e in effective_receipt_events({"reference_docname": doc.name})
	]
	if not receipts:
		return

	# The same lock ``lock_return_entitlement`` takes, in the same order: a return being submitted
	# against this receipt and this cancel serialise, and whichever waits sees the other's commit
	# through the locking read below rather than through a stale snapshot.
	frappe.db.sql(
		"SELECT name FROM `tabCustomer Gold Ledger Entry` WHERE name IN %s ORDER BY name FOR UPDATE",
		(tuple(sorted(receipts)),),
	)
	dependents = set()
	if is_allocation_schema_ready():
		rows = frappe.db.sql(
			"""
			SELECT name, reference_docname, reversal_of
			FROM `tabCustomer Gold Allocation`
			WHERE receipt_event IN %s
			FOR UPDATE
			""",
			(tuple(receipts),),
			as_dict=True,
		)
		reversed_ = {r.reversal_of for r in rows if r.reversal_of}
		dependents |= {
			r.reference_docname
			for r in rows
			if not r.reversal_of and r.name not in reversed_
		}
	returns = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={"cg_source_event": ["in", receipts], "cg_event_kind": EVENT_RETURN},
		fields=["name", "reference_docname"],
	)
	if returns:
		undone = set(
			frappe.get_all(
				LEDGER_DOCTYPE,
				filters={"cg_reversal_of": ["in", [r.name for r in returns]]},
				pluck="cg_reversal_of",
			)
		)
		dependents |= {r.reference_docname for r in returns if r.name not in undone}

	if dependents:
		frappe.throw(
			frappe._(
				"{0} cannot be cancelled while customer gold received on it has been returned or "
				"delivered through {1}. Cancel those first."
			).format(frappe.bold(doc.name), ", ".join(sorted(dependents))),
			title=frappe._("Customer Gold Receipt In Use"),
		)


@frappe.whitelist()
def get_customer_gold_return_preview(receipt, receipt_row=None, qty=None):
	"""What a return against ``receipt`` could give back, and how. Reads only.

	Per receipt row: what was received, what is still owed back, and every place the receipt's
	metal is now -- including converted descendants -- with how much of THIS receipt's share is
	free there. Then the route: Direct (the receipt's own item is free in its custody warehouse),
	Transfer (it is free elsewhere -- raise a Material Request), Settle then Issue (only another
	purity is left), or a shortfall with its reason. ``direct_available`` is what can go out on an
	Issue right now, whatever the route for the full quantity.

	``fallback`` tells the desk to open the classic Issue: the flow is off, or the receipt predates
	the custody ledger, so there is nothing to preview and nothing to enforce.
	"""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		effective_receipt_events,
	)
	from jewellery_erpnext.customer_subcontracting.customer_gold_trace import (
		receipt_key,
		stage_of,
		trace,
	)

	entry = frappe.get_doc("Stock Entry", receipt)
	entry.check_permission("read")

	if not (is_customer_gold_enabled() and is_ledger_schema_ready()):
		return {"rows": [], "fallback": True}

	filters = {"reference_docname": receipt}
	if receipt_row:
		filters["cg_source_row"] = receipt_row
	events = effective_receipt_events(filters)
	if not events:
		return {"rows": [], "fallback": True}

	customer = events[0].customer
	_, replay, _ = trace(entry.company, customer=customer)
	remaining = receipt_remaining(entry.company, customer, events, replay=replay)
	custody = {row.name: row.t_warehouse for row in entry.items}

	rows = []
	for event in events:
		key = receipt_key(event.reference_docname, event.cg_source_row)
		purity = get_purity_percentage(event.item_code)
		requested = flt(qty) if qty else max(flt(remaining.get(event.name)), 0.0)
		holdings = []
		for batch_no, warehouse, held_qty, shares in replay.positions():
			if key not in shares or not held_qty:
				continue
			item_code = frappe.db.get_value("Batch", batch_no, "item")
			free = min(
				flt(get_batch_qty(batch_no=batch_no, warehouse=warehouse)), held_qty
			)
			share = shares[key]
			equivalent = share / (flt(purity) / 100.0) if purity else share
			holdings.append(
				{
					"batch_no": batch_no,
					"warehouse": warehouse,
					"item_code": item_code,
					"purity": get_purity_percentage(item_code),
					"stage": stage_of(warehouse, batch_no, replay),
					"qty": flt(held_qty, 3),
					"free_qty": flt(free, 3),
					"receipt_share": flt(share, 3),
					"receipt_equivalent": flt(equivalent, 3),
					# Only THIS receipt's part of the free stock, in receipt-item units. A batch two
					# receipts share is not all this receipt's to hand back.
					"free_receipt_qty": flt(equivalent * free / held_qty, 3),
					"same_item": item_code == event.item_code,
				}
			)
		custody_warehouse = custody.get(event.cg_source_row)
		route, limit = _choose_route(event, holdings, requested, custody_warehouse)
		direct_available = min(
			_direct_available(event, holdings, custody_warehouse),
			max(flt(remaining.get(event.name)), 0.0),
		)
		rows.append(
			{
				"receipt_event": event.name,
				"receipt_row": event.cg_source_row,
				"item_code": event.item_code,
				"purity": purity,
				"batch_no": event.batch_no,
				"custody_warehouse": custody_warehouse,
				"received": flt(event.cg_gross_qty_delta, 3),
				"remaining": flt(remaining.get(event.name), 3),
				"requested": flt(requested, 3),
				"direct_available": flt(direct_available, 3),
				"route": route,
				"limiting_factor": limit,
				"holdings": holdings,
			}
		)
	return {"rows": rows}


#: Stages whose metal can be requested back and, if need be, converted. Finished pieces and
#: loss/scrap are not unused raw metal.
RETURNABLE_STAGES = ("RM", "Transit", "WIP")


def _direct_available(event, holdings, custody_warehouse):
	return sum(
		h["free_receipt_qty"]
		for h in holdings
		if h["stage"] in RETURNABLE_STAGES
		and h["batch_no"] == event.batch_no
		and h["warehouse"] == custody_warehouse
	)


def _choose_route(event, holdings, requested, custody_warehouse):
	if requested <= 0:
		return "Nothing to return", "The receipt has been fully returned or delivered."
	holdings = [h for h in holdings if h["stage"] in RETURNABLE_STAGES]
	if _direct_available(event, holdings, custody_warehouse) + 1e-6 >= requested:
		return "Direct", None
	same_item = sum(h["free_receipt_qty"] for h in holdings if h["same_item"])
	if same_item + 1e-6 >= requested:
		return (
			"Transfer via Material Request",
			"The receipt's item is free, but not all of it in the receipt's custody warehouse.",
		)
	converted = sum(
		h["free_receipt_qty"] for h in holdings if not h["same_item"] and h["purity"]
	)
	if same_item + converted + 1e-6 >= requested:
		return (
			"Settle, then Issue",
			"Part of the receipt's metal is now another purity; convert it back with the "
			"Material Request's Settle action before issuing.",
		)
	return (
		"Shortfall",
		"Free metal traceable to this receipt covers only {0} of the {1} requested; the rest "
		"is reserved, in work or already dispatched.".format(
			flt(same_item + converted, 3), flt(requested, 3)
		),
	)
