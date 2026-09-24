# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""C12 -- record what leaves the building, once per physical entitlement.

Customer gold arrives, is transformed, and eventually ships. Until now nothing recorded the
last step: the customer's position existed only implicitly in ``Bin``/``Batch`` rows tagged
``Customer Goods``, which simply vanished when stock shipped, with no counterpart record.

WHAT COUNTS AS "IT PHYSICALLY LEFT"
-----------------------------------
One predicate, and it is not the document's name::

    Delivery Note  OR  (Sales Invoice AND update_stock)

A Delivery Note always posts Stock Ledger Entries -- there is no ``update_stock`` field on it.
A Sales Invoice posts them only when ``update_stock`` is set; otherwise it is a bill, and a
bill moves no metal.

TWO HARD CASES THAT CORE ALREADY GUARANTEES -- deliberately not reimplemented here
---------------------------------------------------------------------------------
* **An invoice against an already-delivered DN cannot settle again.**
  ``SalesInvoice.validate_delivery_note`` throws when ``update_stock`` is set and any row
  carries a ``delivery_note``. So such an invoice can never satisfy the predicate above.
  That is CG-T134, enforced upstream.
* **A credit-only return restores nothing.** With ``update_stock = 0`` the invoice posts no
  SLE at all, so it fails the predicate and no custody event is written. That is CG-T145.

WHY NOT ``serial_reference.py``
-------------------------------
It looks like the obvious source of "which serial went out on which document", and it is not.
It keeps ONE pointer per serial, written on ``validate`` (so a *draft* claims it), superseded
SO -> DN -> SI by rank, claimed by returns as well, and *cleared* rather than rewound on
cancel. It answers "where is this piece now", not "what happened to it". Physical truth comes
from the row and its Serial and Batch Bundle, which core builds inside ``on_submit`` before
``update_stock_ledger`` -- so both are readable from this hook.

IDEMPOTENCY
-----------
``cg_event_key`` is UNIQUE in the database. A repeated callback or a concurrent retry loses
the insert and is treated as already-done, rather than being prevented by a read-then-write
check that two workers can both pass. This is the pattern at ``jewellery_erpnext/utils.py``,
and explicitly NOT the pattern in ``Subcontracting Log``, whose writer uses ``bulk_insert``
and dedupes nothing.

MONEY
-----
``cg_carrying_value_delta``/``cg_currency`` are written only under the Nominal valuation
policy. Under Zero Value the event is still recorded without them -- a complete memorandum
record -- so this module works under either answer to D01.

Precisely which "without them" means, because it differs by fieldtype and an earlier revision
of this docstring said "left NULL" for both, which is true of only one:

* ``cg_currency`` is a Data/Link column, genuinely nullable, and really is NULL.
* ``cg_carrying_value_delta`` is Currency, which Frappe renders ``decimal(21,4) NOT NULL
  DEFAULT 0``. It reads **0.0**, not NULL.

So ``cg_currency`` -- not the amount -- is the discriminator between "valued at zero" and
"never valued". That is why it is stamped under Nominal even for a row whose SLE turned out to
carry no value.
"""

import hashlib

import frappe
from frappe.utils import cint, flt, now_datetime

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_customer_gold_company_settings,
	get_customer_gold_settings,
	get_customer_gold_valuation_policy,
	is_customer_gold_enabled,
	validate_settlement_accounts,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)

LEDGER_DOCTYPE = "Customer Gold Ledger Entry"

#: The Stock Entry Type that books process loss. Mirrors
#: ``employee_ir/doc_events/loss_stock_entry.py:32``.
PROCESS_LOSS_SE_TYPE = "Process Loss"
CUSTOMER_GOODS = "Customer Goods"

EVENT_RECEIPT = "Receipt"
EVENT_RETURN = "Return"
EVENT_REVALUATION = "Revaluation"
EVENT_APPROVED_LOSS = "Approved Loss"
EVENT_RECOVERY = "Recovery"
EVENT_TRANSFER_OUT = "Transfer Out"
EVENT_TRANSFER_IN = "Transfer In"
EVENT_CONVERSION_OUT = "Conversion Out"
EVENT_CONVERSION_IN = "Conversion In"
EVENT_PRODUCTION = "Production"
EVENT_ALLOCATION = "Allocation"
EVENT_RELEASE = "Release"
EVENT_DELIVERY = "Delivery"
EVENT_DELIVERY_RETURN = "Delivery Return"
EVENT_REVERSAL = "Reversal"

QTY_PRECISION = 3

#: Measurement statuses. Needed because every numeric column here is NOT NULL DEFAULT 0, so a
#: stored 0.0 cannot on its own separate "no gold" from "not measured".
STATUS_KNOWN = "Known"
STATUS_UNKNOWN = "Unknown"
STATUS_INVALID = "Invalid"

REASON_MISSING_ITEM_PURITY = "missing_item_purity"
REASON_AMBIGUOUS_SETTING = "ambiguous_manufacturing_setting"
REASON_MISSING_REFERENCE_ITEM = "missing_reference_item"
#: A REPORT-SIDE LABEL, never a stored value, and deliberately absent from
#: ``cg_measurement_reason``'s options. It classifies rows written before the status columns
#: existed, which carry NULL rather than any reason. Adding it to the Select would put an
#: option in the vocabulary that no writer can produce -- the exact defect the status columns
#: were added to fix, one level down.
REASON_LEGACY_SOURCE = "legacy_source_without_snapshot"

STAGE_RM = "RM"
STAGE_TRANSIT = "Transit"
STAGE_WIP = "WIP"
STAGE_RECOVERABLE_SCRAP = "Recoverable Scrap"
STAGE_CLOSED = "Closed"
STAGE_FG = "FG"


def is_ledger_schema_ready():
	"""True when the custody ledger can actually be read and written on this site.

	Both halves matter. ``gk`` carries an ORPHAN ``Customer Gold Ledger Entry`` DocType row
	predating this work with only 34 of the shipped 35 fields -- it has no ``serial_no`` column
	-- so ``frappe.db.exists("DocType", ...)`` alone says "ready" on a site where a write would
	raise ``MySQLdb.OperationalError: Unknown column``. Checking a field this app added is what
	distinguishes the shipped schema from the orphan.
	"""
	if not frappe.db.exists("DocType", LEDGER_DOCTYPE):
		return False

	try:
		return bool(frappe.get_meta(LEDGER_DOCTYPE).has_field("serial_no"))
	except frappe.DoesNotExistError:
		return False


def has_customer_gold_events(doc):
	"""Whether this document already has custody events -- regardless of configuration, and
	regardless of whether this site's ledger SCHEMA is complete.

	The question every history-preserving guard must ask, and the one the feature flag cannot
	answer: events written while the feature was on must stay reversible and protected after it
	is turned off.

	THE DEFECT THIS FUNCTION USED TO HAVE, AND WHY IT MATTERED
	-----------------------------------------------------------
	It opened with ``if not is_ledger_schema_ready(): return False``. Every one of its callers is
	INSIDE a ``if not is_ledger_schema_ready():`` branch -- that is the only situation any of them
	asks the question in. ``is_ledger_schema_ready`` is pure, so within that branch this function
	returned ``False`` unconditionally and **every guard built on it was unreachable code**::

	    if not is_ledger_schema_ready():
	        if has_customer_gold_events(doc):  # always False here
	            frappe.throw(...)  # never ran
	        return

	The comment above those guards reads *"failing silently is the one unacceptable outcome"*, and
	failing silently is exactly what happened: on ``gk`` and ``kg-gk`` -- which carry an orphan
	``Customer Gold Ledger Entry`` DocType with 34 of the 35 shipped fields and no ``serial_no``
	-- cancelling a Delivery Note or Stock Entry that carried custody events returned SUCCESS and
	wrote no reversal, and deleting one was not blocked.

	WHY RAW SQL, AND NOT ``frappe.get_all``
	----------------------------------------
	The precondition cannot simply be deleted, because the incomplete-schema site is precisely
	where this now has to run, and every ORM read path resolves the doctype through
	``frappe.get_meta`` -- the meta being what disagrees with the table on an orphan site. A
	``SELECT`` naming its own columns depends on the table and nothing else.

	The two columns it names are safe there: verified on ``gk``, the orphan table carries both
	``reference_doctype`` and ``reference_docname`` (the latter indexed). ``serial_no``, the
	column that is actually missing, is never mentioned.

	``table_exists`` guards the case where there is no table at all -- a site that never had this
	app -- which would otherwise raise ``ProgrammingError`` out of a cancel.
	"""
	if not frappe.db.table_exists(LEDGER_DOCTYPE):
		return False

	return bool(
		frappe.db.sql(
			"""
			SELECT name
			FROM `tabCustomer Gold Ledger Entry`
			WHERE reference_doctype = %s AND reference_docname = %s
			LIMIT 1
			""",
			(doc.doctype, doc.name),
		)
	)


def _is_customer_gold_return(doc):
	"""Whether ``doc`` is the configured Customer Gold return Stock Entry.

	Mirrors ``customer_gold_receipt._receipt_settings``: one settings read per document, never
	per row, and a missing or unconfigured type answers False rather than raising -- a site that
	has not configured returns simply has no returns to recognise.
	"""
	if doc.doctype != "Stock Entry":
		return False

	settings = get_customer_gold_settings()
	configured_type = settings.get("customer_gold_return_stock_entry_type")
	return bool(configured_type) and doc.get("stock_entry_type") == configured_type


def is_physical_fulfilment(doc):
	"""Whether this document actually moves stock out of (or back into) the company.

	A Delivery Note always does. A Sales Invoice does only with ``update_stock``.
	"""
	if doc.doctype == "Delivery Note":
		return True
	return doc.doctype == "Sales Invoice" and bool(cint(doc.get("update_stock")))


def _batch_owner(batch_no):
	"""Owner of a batch, or ``None`` when it is not customer-owned.

	``Batch.custom_customer`` is the only authoritative owner in the schema.
	``Serial No.custom_ownership_tag`` is a free-form Data tag, not a Customer link, so it
	cannot answer this -- see the C12 note in the plan.
	"""
	if not batch_no:
		return None

	batch = frappe.db.get_value(
		"Batch", batch_no, ["custom_customer", "custom_inventory_type"], as_dict=True
	)
	if not batch or batch.custom_inventory_type != CUSTOMER_GOODS:
		return None

	return batch.custom_customer or None


def _row_batches(row):
	"""Every batch this row actually moves, from the bundle first and the field second.

	READING ``row.batch_no`` ALONE IS NOT ENOUGH, and that is not a style preference -- it
	silently loses two whole classes of movement:

	1. **Returns built from a reference document.** ``StockController.make_bundle_for_non_rejected_qty``
	   (``erpnext/controllers/stock_controller.py:556-565``) does
	   ``row.db_set({"serial_and_batch_bundle": bundle, "batch_no": "", "serial_no": ""})`` --
	   it **wipes ``batch_no``** -- and it runs inside ``on_submit`` BEFORE ``update_stock_ledger``
	   and therefore before this module's handler. So any return made through "Make Return Entry"
	   arrives here with no ``batch_no`` at all and was skipped entirely.
	2. **Bundle-first deliveries.** When the operator picks a Serial and Batch Bundle rather than
	   the legacy fields -- v16's default whenever ``use_serial_batch_fields`` is 0 --
	   ``batch_no`` is empty from the start.

	Neither was visible in the tests, because the fixtures set ``batch_no`` by hand and set no
	``dn_detail``. ``_row_serials`` has read the bundle from the beginning; this is the same
	lookup for the batch column.

	Returns a list, because one bundle can carry several batches.
	"""
	bundle = row.get("serial_and_batch_bundle")
	if bundle:
		batches = frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": bundle},
			pluck="batch_no",
		)
		batches = [b for b in dict.fromkeys(batches) if b]
		if batches:
			return batches

	return [row.batch_no] if row.get("batch_no") else []


def _row_serials(row):
	"""Serials on this row, from the bundle if there is one, else the plain field.

	Returns ``[None]`` when the row carries no serials at all, so a non-serialised row still
	produces exactly one event rather than none.
	"""
	bundle = row.get("serial_and_batch_bundle")
	if bundle:
		serials = frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": bundle},
			pluck="serial_no",
		)
		serials = [s for s in serials if s]
		if serials:
			return serials

	raw = row.get("serial_no")
	if raw:
		return [s.strip() for s in str(raw).split("\n") if s.strip()]

	return [None]


def _restate_qty(qty, held_item, source_item):
	"""``qty`` grams of ``held_item`` expressed as grams of ``source_item``, or ``None``.

	Fine gold is the bridge, because it is the one measure a purity change conserves::

	    fine   = qty x purity(held) / 100
	    result = fine / purity(source) x 100  ==  qty x purity(held) / purity(source)

	The division cancels the 100s, so no rounding is introduced beyond the caller's own.

	``None`` means "not restatable", never a guess. The items differ and a purity is missing or
	non-positive, so there is no defensible conversion -- and a settlement computed from an
	indefensible one is money moved on a number nobody can reconstruct.

	Identity when the items match, which is the ordinary case: raw batches and same-item repacks
	reach this with ``held_item == source_item`` and must come out bit-for-bit unchanged. The
	equality test comes FIRST for that reason -- an item whose purity is unmapped still restates
	into itself correctly, and refusing there would break paths that never had a unit problem.
	"""
	qty = flt(qty)
	if not qty:
		return 0.0

	if held_item and source_item and held_item == source_item:
		return qty

	if not held_item or not source_item:
		# One of the two is unknown, so it cannot be shown that no conversion happened. The
		# quantity may well be in the right units already; "may well be" is not good enough to
		# settle a liability on.
		return None

	held_purity = get_purity_percentage(held_item)
	source_purity = get_purity_percentage(source_item)
	if not held_purity or not source_purity or flt(source_purity) <= 0:
		return None

	return qty * flt(held_purity) / flt(source_purity)


def _customer_share(doc, batch_no, customer, moved_qty, valued):
	"""The customer's gold inside ``moved_qty`` of ``batch_no``: fine grams and booked value.

	Returns ``None`` when the batch records no component of this customer's -- every raw
	received batch -- so the raw path, which settles from the stock ledger, is untouched.
	Otherwise a dict: ``fine`` (fine grams, or ``None`` if a component's purity is unknown),
	``value`` (rupees at the booked rate, or ``None``) and ``reason`` (why ``value`` is None).

	A MANUFACTURED PIECE IS NOT ITSELF CUSTOMER GOLD (F2)
	------------------------------------------------------
	Its stock value is customer metal plus company alloy, stones and production cost, and the
	invoice recovers the last three. The SOP settles only the customer's part::

	    "On Delivery Note, clear only the booked customer value included in the delivered
	     Serial Number."

	So the settlement follows ``Batch Component``, the recorded composition, back to each
	customer source batch, and prices each at the rate its own receipt booked
	(``get_booked_rate``) -- never a rate fetched today, never the finished piece's value::

	    released = component source grams x delivered / attribution basis x booked rate

	This used to restate every component through the purity of the item being DELIVERED. A
	finished piece is counted in Nos and has no Metal Purity -- 0 of 25,249 Nos items on kg-gk
	carry one -- so the restatement returned None, the code fell back to the piece's stock
	value, and KLHGX62F1119 released Rs.11.58 of a Rs.7,86,828.62 obligation. Component
	quantities are already in their own item's grams (see ``resolve_components``), so the only
	restatement left is between a component's item and its source batch's item, and in practice
	they are the same item.

	The denominator is ``attribution_basis`` -- the quantity the components describe, fixed at
	production -- never ``Batch.batch_qty``, the live balance that K45 showed releasing 166.7%
	across three instalments.

	NO FALLBACK TO THE STOCK LEDGER
	-------------------------------
	If this batch holds the customer's components but their value cannot be established, the
	answer is "not valued", never the piece's stock value. That figure includes company material
	and would discharge an obligation the customer never created. The caller logs it and
	settles nothing, so the open liability stays visible.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_components import (
		_recorded_components,
		attribution_basis,
	)
	from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
		get_booked_rate,
	)

	if not batch_no or not customer:
		return None

	components = _recorded_components(batch_no)
	mine = [
		component
		for component in components
		if component.get("inventory_type") == CUSTOMER_GOODS
		and component.get("customer") == customer
	]
	if not mine:
		return None

	share = frappe._dict(fine=None, value=None, reason=None)

	# The basis covers EVERY component, not just this customer's: company alloy and stones are
	# part of what the batch is made of.
	basis = attribution_basis(batch_no, components)
	if not basis:
		share.reason = "no quantity is recorded for what the batch was made of"
		return share

	fraction = flt(moved_qty) / flt(basis)

	fine = 0.0
	for component in mine:
		purity = get_purity_percentage(component.get("item_code"))
		if not purity:
			fine = None
			break
		fine += flt(component.get("qty")) * fraction * flt(purity) / 100.0
	share.fine = flt(fine, QTY_PRECISION) if fine is not None else None

	if not valued:
		return share

	total = 0.0
	for component in mine:
		source = component.get("source_batch")
		if not source:
			share.reason = "a customer component names no source batch"
			return share

		rate = get_booked_rate(doc.company, customer, source)
		if rate is None:
			share.reason = f"source batch {source} has no booked receipt value"
			return share

		# The rate is per gram of the SOURCE batch's item. A component is recorded in its own
		# item's grams, which is that same item unless something wrote it otherwise -- so this
		# is an identity, and a guard.
		source_item = frappe.db.get_value("Batch", source, "item") or component.get(
			"item_code"
		)
		source_qty = _restate_qty(
			flt(component.get("qty")), component.get("item_code"), source_item
		)
		if source_qty is None:
			share.reason = (
				f"component {component.get('item_code')} cannot be restated into "
				f"{source_item} grams"
			)
			return share

		total += source_qty * fraction * flt(rate)

	share.value = flt(total, 2)
	return share


def _log_unsettled(doc, row, batch_no, reason):
	"""Leave the liability open, visibly, rather than settle a number nobody can defend."""
	frappe.log_error(
		title="Customer Gold: delivery not settled",
		message=(
			f"{doc.doctype} {doc.name} row {row.name} delivers batch {batch_no}, which holds "
			f"customer gold, but its booked value could not be established: {reason}. No "
			f"liability was released for this row; it remains open until this is resolved."
		),
	)


def _row_carrying_value(doc, row):
	"""Carrying value that actually left the books for this row, or ``None``.

	Taken from the row's own Stock Ledger Entry rather than re-derived from a rate, for three
	reasons:

	1. **It cannot disagree with the GL.** ``stock_value_difference`` is the number erpnext
	   posted. A rate x qty recomputed here would be a second opinion, and the spec's S7.3
	   requires a difference to be *classified*, not absorbed -- so the safe move is not to
	   manufacture one.
	2. **The sign is erpnext's, not ours.** It is already negative for an outward move and
	   positive for a return, matching ``cg_gross_qty_delta``'s convention with no branch here.
	3. **It respects whatever valuation method the batch actually used** -- batch-wise,
	   moving average or FIFO -- which a per-gram rate cannot reproduce.

	Reading it here is safe because of hook ordering, verified rather than assumed:
	``Document.hook``'s ``compose`` (``frappe/model/document.py:1633-1647``) runs the
	controller's own ``on_submit`` FIRST and only then the ``doc_events`` handlers. The
	controller's ``on_submit`` is what calls ``update_stock_ledger``, so the SLE rows exist by
	the time this runs. ``test_carrying_value_matches_the_stock_ledger`` pins that ordering.

	Returns ``None`` when no SLE exists for the row -- a non-stock row, or a zero-valued
	receipt whose GL was dropped by ``merge_similar_entries``. ``None`` is recorded as "not
	valued", never as 0.0, because a real zero and an absent value are different facts.
	"""
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"voucher_detail_no": row.name,
			"is_cancelled": 0,
		},
		fields=["stock_value_difference"],
	)
	if not rows:
		return None

	return flt(sum(flt(r.stock_value_difference) for r in rows))


def build_event_key(company, voucher_type, voucher_detail_no, serial_no, event_kind):
	"""Deterministic identity for one event.

	Built from the SERVER's own view of the operation -- company, the voucher child row, the
	serial and the kind -- never from a client-supplied request id and never from a timestamp,
	so a retry of the same business operation computes the same key.

	Hashed because the components together exceed the field length, and the raw form is kept
	in ``cg_source_row`` for reading.
	"""
	raw = "|".join(
		str(part or "")
		for part in (company, voucher_type, voucher_detail_no, serial_no, event_kind)
	)
	return hashlib.sha1(raw.encode()).hexdigest()


#: Raised when the insert loses the race for ``cg_event_key``.
#:
#: It must be ``UniqueValidationError``, NOT ``DuplicateEntryError``. The two are unrelated
#: branches of the hierarchy and are raised by different mechanisms:
#:
#: * ``DuplicateEntryError(NameError)`` -- a document with the same *name* already exists.
#:   ``cg_event_key`` is not the name; this doctype is ``autoname: hash``, so two events with
#:   the same key get two different names and this never fires.
#: * ``UniqueValidationError(ValidationError)`` -- a UNIQUE *column* constraint rejected the
#:   row. ``base_document.py:873`` raises it after MariaDB error 1062. This is ours.
#:
#: Caught as the wrong one first, and the integration suite is what found it: three tests
#: died on an uncaught
#: ``UniqueValidationError(..., IntegrityError(1062, "Duplicate entry ... for key 'cg_event_key'"))``.
#: The unit suite could not have found it -- a mock raising the exception the code names will
#: always look right.
_DUPLICATE_KEY = frappe.UniqueValidationError


def _write_event(**kwargs):
	"""Insert one ledger row, treating a duplicate key as already-done.

	The UNIQUE constraint is the authority. Checking first and inserting second is a race two
	concurrent retries can both win.
	"""
	# THE POLICY IN FORCE **NOW**, STAMPED ONTO THE ROW.
	#
	# ``cg_policy_version``'s own field description is the requirement: *"The accounting policy
	# in force when this event posted. Historical behaviour must never depend on today's
	# configuration."* Until this line existed the column had no writer anywhere in the app,
	# while ``get_customer_gold_valuation_policy()`` was read LIVE at a dozen call sites -- so
	# flipping Nominal -> Zero Value silently reinterpreted every event ever written, which is
	# precisely what the field was declared to prevent.
	#
	# ``setdefault``, not assignment, and that matters: a reversal COPIES its original's policy
	# rather than re-deriving one, exactly as it already copies the measurement statuses. An
	# explicit ``None`` from a caller also survives, because ``setdefault`` keys on the key's
	# presence and not on its value -- a legacy event with no recorded policy reverses as "not
	# recorded" rather than acquiring today's.
	kwargs.setdefault("cg_policy_version", get_customer_gold_valuation_policy())
	kwargs.setdefault("cg_recorded_at", now_datetime())

	doc = frappe.get_doc({"doctype": LEDGER_DOCTYPE, **kwargs})
	try:
		doc.insert(ignore_permissions=True)
	except _DUPLICATE_KEY:
		frappe.clear_last_message()
		return frappe.db.get_value(
			LEDGER_DOCTYPE, {"cg_event_key": kwargs["cg_event_key"]}, "name"
		)
	return doc.name


def validate_customer_gold_entitlement(doc, method=None):
	"""``before_submit`` for Delivery Note and Sales Invoice. CG-T139 -- **Block**.

	WHY THIS EXISTS: RE-ATTRIBUTION IS NOT REJECTION
	------------------------------------------------
	``record_fulfilment`` resolves each event's customer from the batch, so a delivery of
	customer B's gold on a document billed to customer A produced a *correctly attributed* event
	-- and let the metal ship. The spec card is unambiguous: *"Wrong-customer serial on delivery
	... Reject owner mismatch before effective physical or nominal closure. Required acceptance
	outcome: **Block**."* Attributing the event to the right owner after the fact is bookkeeping,
	not entitlement.

	WHY ``before_submit``
	---------------------
	It has to run before any Stock Ledger Entry exists, and it has to see the batch.

	* ``validate`` is too early for a bundle-first row: the Serial and Batch Bundle is built in
	  ``on_submit`` (``delivery_note.py:470-471``), so ``batch_no`` can still be empty.
	* ``on_submit`` is too late: the controller's own ``on_submit`` posts the SLEs
	  (``delivery_note.py:478``) BEFORE any ``doc_events`` handler runs -- ``Document.hook``'s
	  ``compose`` calls ``fn(self, ...)`` first (``frappe/model/document.py:1635``).
	* ``before_submit`` is free across the whole chain -- Delivery Note, SellingController,
	  StockController and AccountsController define none -- and it is the same slot
	  ``customer_gold_receipt`` chose for the identical "batch_no does not exist at validate"
	  problem. A return row still carries its mapped ``batch_no`` here, because the bundle
	  rebuild that clears it happens later, in ``on_submit``.

	WHAT THE ERROR MAY SAY -- and the trap this app sets
	----------------------------------------------------
	Not who the owner is. §6.3: *"Do not disclose another customer's batch details merely to
	explain why an operation failed."*

	**It may not name the batch either**, which is not obvious and which the test caught. This
	app names batches through ``batch_rename`` as ``{customer}-{...}-{item}-{seq}`` -- the owning
	customer's code is EMBEDDED IN THE BATCH NAME. Naming the batch therefore discloses the owner
	just as surely as naming them outright. The first version of this throw did exactly that, and
	``test_wrong_customer_delivery_is_blocked`` failed on its own non-disclosure assertion.

	The message identifies the row by index and item -- enough to find the line on the document
	in front of the operator -- and nothing else.
	"""
	if not is_physical_fulfilment(doc) or not is_customer_gold_enabled():
		return

	document_customer = doc.get("customer")

	for row in doc.get("items") or []:
		for batch_no in _row_batches(row):
			owner = _batch_owner(batch_no)
			if not owner or owner == document_customer:
				continue

			frappe.throw(
				frappe._(
					"Row #{0} ({1}) holds customer-owned gold that is not entitled to {2}. "
					"A delivery may only move metal belonging to the document's customer. "
					"Raise this against the owning customer, or transfer ownership first."
				).format(
					row.idx,
					frappe.bold(row.get("item_code") or "-"),
					frappe.bold(document_customer or "-"),
				),
				title=frappe._("Customer Gold Entitlement"),
			)


def reference_purity(company):
	"""Reference purity % for ``company``, or ``None`` when it cannot be resolved.

	NEVER THROWS, and that is the whole point of it existing separately.

	The authoritative resolution lives in ``doc_events/stock_entry.py:222-248`` and deliberately
	**throws** when a company keeps one Manufacturing Setting per manufacturer and none is
	company-wide -- picking an arbitrary sibling there would silently cost metal off another
	manufacturer's ``pure_gold_item``. That is right for ``custom_pure_qty``, which is a stock
	field the document depends on.

	It is wrong here. A custody event is a RECORD of something that already happened; refusing to
	write it because a master is ambiguous would fail a legitimate receipt or delivery for a
	reason that has nothing to do with the metal moving. So this mirrors the same precedence --
	single setting, else the single company-wide one -- and returns ``None`` rather than raising.

	``None`` then propagates to a NULL ``cg_reference_qty_delta``, which reads as "not measured on
	this basis". That is the truth, and it is the same distinction the carrying value already
	draws between NULL and 0.0.
	"""
	if not company:
		return None

	settings = frappe.get_all(
		"Manufacturing Setting",
		filters={"company": company},
		fields=["name", "manufacturer", "pure_gold_item"],
		order_by="name",
	)

	pure_item = None
	if len(settings) == 1:
		pure_item = settings[0].pure_gold_item
	else:
		company_wide = [row for row in settings if not row.manufacturer]
		if len(company_wide) == 1:
			pure_item = company_wide[0].pure_gold_item

	if not pure_item:
		return None

	return get_purity_percentage(pure_item)


def quantity_basis(item_code, gross_qty, company):
	"""The conserved measures for a gross quantity, ready to splat into an event.

	WHY THE LEDGER NEEDS MORE THAN GROSS GRAMS
	------------------------------------------
	Gross quantity is not conserved through a purity change. The SOP's Example B turns **10 g of
	24KT into 11.1 g at 90%**: 1.1 g of company alloy joined it. Fine gold IS conserved -- 9.99 g
	in, 9.99 g out -- and reference-equivalent quantity is conserved too, being fine gold expressed
	against a fixed denominator.

	WHY THERE ARE STATUS FIELDS AND NOT JUST NUMBERS
	-------------------------------------------------
	This function used to return ``None`` for an unresolvable measure, and its docstring claimed
	**"Never 0.0 -- a zero would assert a measurement that was never taken."** That claim was
	false at the persistence boundary, and a review caught it.

	Every numeric column on this doctype is ``NOT NULL DEFAULT 0``::

	    cg_fine_gold_delta      decimal(21,6)  Null=NO  Default=0.000000
	    cg_reference_qty_delta  decimal(21,6)  Null=NO  Default=0.000000
	    cg_reference_purity     decimal(21,6)  Null=NO  Default=0.000000

	and ``base_document.get_valid_dict()`` sends float-like values through ``flt()``, where
	``flt(None)`` is ``0.0``. So a ``None`` here was stored as a zero indistinguishable from a
	genuine zero -- exactly the confusion it was written to prevent. The same correction had
	already been made for Currency and was not carried across to Float.

	A number cannot hold the distinction, so a status field does:

	* ``Known``          -- measured, and the value is the measurement. **Includes a real zero**: a
	  gold-free alloy contributes exactly 0.000 fine grams and that is a measurement, not a gap.
	* ``Unknown``        -- not measurable from the evidence available. The 0.0 in the column is a
	  storage artefact and must not be read as a quantity.
	* ``Invalid``        -- the evidence is present and unusable, e.g. a zero reference denominator.
	  Distinct from Unknown because it is a data defect someone can repair.
	There is deliberately no ``Not Applicable``. Every event this module writes moves metal or
	declares that it did not, so every one of them has a fine-gold answer; an option no writer
	can produce would be a declared-and-unwritten value, which is the defect these columns exist
	to close.
	"""
	gross = flt(gross_qty)
	item_purity = get_purity_percentage(item_code) if item_code else None

	if not item_purity:
		# No item purity: neither fine nor reference grams can be established. The zeroes below
		# are storage, and the statuses are what say so.
		return {
			"cg_fine_gold_delta": 0,
			"cg_reference_qty_delta": 0,
			"cg_reference_purity": 0,
			"cg_fine_measurement_status": STATUS_UNKNOWN,
			"cg_reference_measurement_status": STATUS_UNKNOWN,
			"cg_measurement_reason": REASON_MISSING_ITEM_PURITY,
		}

	return fine_basis(gross * flt(item_purity) / 100.0, company)


def fine_basis(fine, company):
	"""The conserved measures for a KNOWN fine-gold quantity, ready to splat into an event.

	``quantity_basis`` derives fine gold from an item's purity and ends here. A manufactured
	piece has no purity, so its delivery measures fine gold from the customer's components
	instead and enters here directly. ``fine`` is unrounded; the sign follows the movement.
	"""
	ref_purity = reference_purity(company)
	rounded = flt(fine, QTY_PRECISION)

	if ref_purity is None:
		# Fine gold IS measurable -- the item's own purity is known. Only the denominator is
		# missing, so the two statuses part company here. That is why they are separate fields.
		return {
			"cg_fine_gold_delta": rounded,
			"cg_reference_qty_delta": 0,
			"cg_reference_purity": 0,
			"cg_fine_measurement_status": STATUS_KNOWN,
			"cg_reference_measurement_status": STATUS_UNKNOWN,
			"cg_measurement_reason": REASON_AMBIGUOUS_SETTING,
		}

	if flt(ref_purity) <= 0:
		# Present and unusable. Dividing by it would produce a number, which is worse than
		# refusing to: a silent infinity or a ZeroDivisionError inside a submit.
		return {
			"cg_fine_gold_delta": rounded,
			"cg_reference_qty_delta": 0,
			"cg_reference_purity": 0,
			"cg_fine_measurement_status": STATUS_KNOWN,
			"cg_reference_measurement_status": STATUS_INVALID,
			"cg_measurement_reason": REASON_MISSING_REFERENCE_ITEM,
		}

	return {
		"cg_fine_gold_delta": rounded,
		# Sign follows the movement, so a negative custody movement stays negative on every basis.
		"cg_reference_qty_delta": flt(fine * 100.0 / flt(ref_purity), QTY_PRECISION),
		"cg_reference_purity": ref_purity,
		"cg_fine_measurement_status": STATUS_KNOWN,
		"cg_reference_measurement_status": STATUS_KNOWN,
		"cg_measurement_reason": None,
	}


def record_receipt(doc, method=None):
	"""``on_submit`` for Stock Entry. Writes the OPENING balance the ledger never had.

	WHY THIS EXISTS: THE LEDGER HAD NO STARTING POINT
	-------------------------------------------------
	``Customer Gold Ledger Entry`` declares fifteen event kinds. Before this function, code
	wrote three -- ``Delivery``, ``Delivery Return`` and ``Reversal`` -- and **the receipt wrote
	nothing at all**. So the ledger recorded only metal leaving, never metal arriving, and
	``get_customer_gold_position`` summed a column that had no opening row: after a 10 g receipt
	it returned ``0.0``, and after delivering 3 g of that gold it returned ``-3.0``.

	A custody ledger that reports a negative holding for a customer who is owed gold is not a
	rounding problem, it is the wrong sign of the wrong number. The SOP's first question --
	"how much of this customer's gold do we hold?" -- was unanswerable.

	It went unnoticed because the only caller was a test measuring a *delta* across one
	delivery (``before`` minus ``after``), which is correct whatever the baseline is. Nothing in
	production called it at all.

	WHY ONE EVENT PER ROW, NOT PER SERIAL
	-------------------------------------
	A delivery writes per serial because settlement is per serial -- each finished piece closes
	its own share of the obligation. A receipt has no such subdivision: the spec's S05 §5.2 asks
	only for "row-level provenance even when its rate snapshot is shared at header level".
	``cg_source_row`` carries that, and the Stock Entry Detail row name is stable across retries,
	which is what makes the event key idempotent.

	WHY THE VALUE COMES FROM THE SLE
	--------------------------------
	``_row_carrying_value`` reads ``stock_value_difference`` -- the number erpnext actually
	posted -- rather than recomputing ``qty x basic_rate``. That matters more here than anywhere
	else: the receipt is where the liability is created, so a value recorded here that disagreed
	with the GL would put the ledger and the accounts in conflict from the very first row. The
	sign is erpnext's too, and it is positive for an inward move, which is exactly the custody
	convention the delivery path already uses in the opposite direction.

	Under Zero Value the value is ``None``, never ``0.0`` -- "not valued" and "valued at zero"
	are different facts, and only one of them is true.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
		_receipt_settings,
	)

	# Gated on the flag AND the schema, matching every other writer: a site with the feature
	# off, or one whose ledger is the 34-field orphan, must be completely unaffected.
	if not is_customer_gold_enabled() or not is_ledger_schema_ready():
		return

	# The single authority on "is this a Customer Gold receipt" -- it matches the CONFIGURED
	# stock entry type, so an alternate configured type works here exactly as it does in
	# validation. Returns None for every ordinary Stock Entry, which is the common case.
	if not _receipt_settings(doc):
		return

	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	currency = (
		frappe.get_cached_value("Company", doc.company, "default_currency")
		if nominal
		else None
	)

	for row in doc.get("items") or []:
		batches = _row_batches(row)
		if len(batches) > 1:
			# Same reasoning as the delivery path: one custody event per row cannot honestly
			# represent a row spanning several batches, and guessing the split is worse than
			# recording nothing and saying so.
			frappe.log_error(
				title="Customer Gold: multi-batch receipt row not recorded",
				message=(
					f"{doc.doctype} {doc.name} row {row.name} received {len(batches)} batches "
					f"({', '.join(batches)}). No custody event was written for this row."
				),
			)
			continue

		batch_no = batches[0] if batches else None
		customer = _batch_owner(batch_no)
		if not customer:
			# Not customer-owned metal. The receipt validators have already rejected an
			# unresolved owner by this point, so this is an ordinary company row.
			continue

		_write_event(
			cg_event_key=build_event_key(
				doc.company, doc.doctype, row.name, None, EVENT_RECEIPT
			),
			cg_event_kind=EVENT_RECEIPT,
			cg_stage=STAGE_RM,
			company=doc.company,
			customer=customer,
			reference_doctype=doc.doctype,
			reference_docname=doc.name,
			cg_source_row=row.name,
			item_code=row.get("item_code"),
			batch_no=batch_no,
			stock_uom=row.get("stock_uom") or row.get("uom"),
			# POSITIVE: custody arrives. The delivery path negates; this does not.
			cg_gross_qty_delta=flt(row.get("qty")),
			**quantity_basis(row.get("item_code"), flt(row.get("qty")), doc.company),
			cg_carrying_value_delta=_row_carrying_value(doc, row) if nominal else None,
			cg_currency=currency,
		)


#: Event kinds a Stock Entry can produce. Used by the cancel path so a reversal never has to be
#: told which kinds a given voucher happened to write.
STOCK_ENTRY_KINDS = (
	EVENT_RECEIPT,
	EVENT_RETURN,
	EVENT_TRANSFER_OUT,
	EVENT_TRANSFER_IN,
	EVENT_CONVERSION_OUT,
	EVENT_CONVERSION_IN,
	EVENT_PRODUCTION,
	EVENT_APPROVED_LOSS,
	EVENT_RECOVERY,
)


def warehouse_stage(warehouse):
	"""Which custody stage a warehouse represents, from the warehouse's own metadata.

	Derived, never configured separately -- a second mapping of warehouse to stage would drift
	from the first one the day somebody adds a warehouse.

	* ``warehouse_type == "Transit"`` -> ``Transit``. erpnext's own classification.
	* a ``department`` set           -> ``WIP``. This app adds that field precisely to mark a
	  shop-floor warehouse (``install.py`` asserts it as a required field).
	* otherwise                      -> ``RM``.

	Returns ``None`` for no warehouse, which is what an issue row's missing target looks like.
	"""
	if not warehouse:
		return None

	info = frappe.get_cached_value(
		"Warehouse", warehouse, ["warehouse_type", "department"], as_dict=True
	)
	if not info:
		return None

	if info.warehouse_type == "Transit":
		return STAGE_TRANSIT

	return STAGE_WIP if info.department else STAGE_RM


def _write_conversion(doc, row, batch_no, customer, source_warehouse, currency):
	"""One Conversion event for a lane-tagged Repack row.

	WHY GROSS QUANTITY IS ALLOWED TO DISAGREE HERE
	----------------------------------------------
	Conversion is the one movement that does not conserve grams. The SOP's Example B consumes 10 g
	of 24KT and produces 11.1 g at 90%: 1.1 g of company alloy joined it. So a Conversion Out of
	−10 and a Conversion In of +11.1 are both correct, and their gross deltas do NOT net to zero.

	That is exactly why ``POSITION_KINDS`` excludes both. The holding is unchanged by a
	conversion -- the same metal is still there, restated at a different purity -- and a position
	that summed these would report the alloy as customer metal.

	``cg_fine_gold_delta`` is the basis that does net, for the SOP's own case where the alloy
	carries no gold. It will NOT net when the alloy is itself gold-bearing, and that is correct
	rather than a defect: company gold genuinely entered the lane. Which part of the result is
	whose is a COMPOSITION question, and ``Batch Component`` is the authority on it -- §5.3
	forbids a second one.

	Direction comes from the warehouse, not from the sign of anything: a Repack consume row has
	``s_warehouse``, a produce row has ``t_warehouse``.
	"""
	consumed = bool(source_warehouse)
	kind = EVENT_CONVERSION_OUT if consumed else EVENT_CONVERSION_IN
	qty = -flt(row.get("qty")) if consumed else flt(row.get("qty"))

	_write_event(
		cg_event_key=build_event_key(doc.company, doc.doctype, row.name, None, kind),
		cg_event_kind=kind,
		# Metal in a conversion is raw material on both sides of the operation -- it leaves RM as
		# one item and re-enters RM as another. It is not in transit and not on the floor.
		cg_stage=STAGE_RM,
		company=doc.company,
		customer=customer,
		reference_doctype=doc.doctype,
		reference_docname=doc.name,
		cg_source_row=row.name,
		item_code=row.get("item_code"),
		batch_no=batch_no,
		stock_uom=row.get("stock_uom") or row.get("uom"),
		cg_gross_qty_delta=qty,
		**quantity_basis(row.get("item_code"), qty, doc.company),
		# No carrying value. A conversion transforms metal the customer already owns; the booked
		# obligation is untouched, and the SOP is explicit that liability stays put through it.
		cg_carrying_value_delta=None,
		cg_currency=currency,
	)


def _write_production(doc, row, batch_no, customer, currency):
	"""One Production event per CONSUMED customer-owned row of a Manufacture entry.

	WHY THE CONSUMED ROWS AND NOT THE FINISHED-GOODS ROW
	----------------------------------------------------
	Not because the FG row lacks an owner -- it has one. ``_finished_goods_ownership``
	(``manufacturing_operation.py:905-953``) derives the finished row's ``inventory_type`` and
	``customer`` from the metal consumed, and it must, or a delivery of the piece could never
	settle the liability.

	The reason is double counting. The consumed row and the finished row are the SAME metal, once
	as input and once as output. An event on each would move the customer's holding twice for one
	physical fact. The consumed side is the one that carries the composition the spec asks for --
	"actual finished-goods composition" IS what went in -- so that is the side that writes.

	``record_stock_movement`` skips the produced row explicitly for this reason; see the branch
	there. It used to fall through to the unclassified diagnostic instead, which is how the double
	-counting question got asked in the first place.

	The consumed rows can, and they are also the better source on the merits: the spec asks for
	"actual finished-goods composition", and what was actually consumed IS the composition.
	``_get_source_raw_materials`` resolves it per batch from the originating Stock Entry Detail
	(``serial_number_creator.py:1710-1722``) and stamps it on these rows.

	WHAT THE SIGN MEANS HERE
	------------------------
	POSITIVE, at stage ``FG``. The event answers §5.5's "customer material embodied in WIP/FG",
	which is a quantity the customer still has with us -- just no longer as loose metal. It is NOT
	a custody change, so ``POSITION_KINDS`` excludes it and the holding does not move: the metal
	was already ours to account for before it was made into something.
	"""
	qty = flt(row.get("qty"))

	_write_event(
		cg_event_key=build_event_key(
			doc.company, doc.doctype, row.name, None, EVENT_PRODUCTION
		),
		cg_event_kind=EVENT_PRODUCTION,
		cg_stage=STAGE_FG,
		company=doc.company,
		customer=customer,
		reference_doctype=doc.doctype,
		reference_docname=doc.name,
		cg_source_row=row.name,
		item_code=row.get("item_code"),
		batch_no=batch_no,
		stock_uom=row.get("stock_uom") or row.get("uom"),
		cg_gross_qty_delta=qty,
		**quantity_basis(row.get("item_code"), qty, doc.company),
		# The frozen composition this consumption belongs to. Declared on the DocType and, until
		# now, written by nothing -- it is what lets a delivery trace back to what was actually
		# made rather than to a batch label.
		cg_composition_version=doc.get("custom_serial_number_creator"),
		cg_carrying_value_delta=None,
		cg_currency=currency,
	)


def _write_loss_or_recovery(doc, row, batch_no, customer, consumed, currency):
	"""Approved Loss on the consumed side, Recovery on the scrap that comes back.

	§5.5 IS THE HARD RULE HERE, AND IT IS EASY TO GET WRONG
	-------------------------------------------------------
	    "Physical loss and monetary obligation are not automatically identical. If customer
	     material is lost and the business still owes replacement, the obligation remains even
	     though the physical stock is lower. Do not force reconciliation by silently reducing
	     liability for every physical loss."

	So this writes a **quantity** change and deliberately **no carrying value**. Metal genuinely
	left, and ``POSITION_KINDS`` therefore includes both kinds -- the physical holding must fall.
	What must NOT happen is the obligation falling with it. A customer whose gold was lost on the
	shop floor is still owed that gold; deciding otherwise is a restitution decision with an
	approver, not an arithmetic consequence of a Repack posting.

	A carrying value here would make the two books agree by fiat, which is precisely the
	"forced reconciliation" the spec names.

	OWNERSHIP IS ALREADY RESOLVED UPSTREAM
	--------------------------------------
	``loss_stock_entry._prepare_loss_row`` stamps ``inventory_type`` and ``customer`` on both the
	consume and the produce row (``loss_stock_entry.py:1055-1056`` and ``:1078-1081``), with the
	comment "a customer's metal stays the customer's even after it is booked as loss". This reads
	that decision rather than re-making it.
	"""
	kind = EVENT_APPROVED_LOSS if consumed else EVENT_RECOVERY
	qty = -flt(row.get("qty")) if consumed else flt(row.get("qty"))

	_write_event(
		cg_event_key=build_event_key(doc.company, doc.doctype, row.name, None, kind),
		cg_event_kind=kind,
		# Recovered metal is scrap until it is refined -- a real stage, and one nothing wrote
		# before. Lost metal is recorded against the stage it left.
		cg_stage=STAGE_RM if consumed else STAGE_RECOVERABLE_SCRAP,
		company=doc.company,
		customer=customer,
		reference_doctype=doc.doctype,
		reference_docname=doc.name,
		cg_source_row=row.name,
		item_code=row.get("item_code"),
		batch_no=batch_no,
		stock_uom=row.get("stock_uom") or row.get("uom"),
		cg_gross_qty_delta=qty,
		**quantity_basis(row.get("item_code"), qty, doc.company),
		# NO VALUE. See the §5.5 note above -- this is the whole point of the function.
		cg_carrying_value_delta=None,
		cg_currency=currency,
	)


def record_stock_movement(doc, method=None):
	"""``on_submit`` for Stock Entry -- the custody moves a receipt does not cover.

	WHY ONE DISPATCHER RATHER THAN A HOOK PER FLOW
	-----------------------------------------------
	Customer metal is moved by at least three separate subsystems -- PC-to-Tagging
	(``pc_tagging_stock_sync.py``), Employee IR metal injection (``main_slip_inject.py``) and the
	customer-gold settlement helpers (``sub_utils/cg_settle.py``) -- and each builds its own
	Stock Entry with its own ownership stamping. They share exactly one thing: the Stock Entry
	they mint passes through this hook.

	Hooking each subsystem separately would mean four places to keep in step, and would miss the
	fifth one somebody adds next year. Hooking the Stock Entry catches every flow that actually
	moves metal, because moving metal IS a Stock Entry. Metal Conversions, Serial Number Creator
	and Refining Entry are not registered in ``doc_events`` at all; their entries still arrive here.

	WHY A TRANSFER WRITES TWO EVENTS
	--------------------------------
	One row carries both ``s_warehouse`` and ``t_warehouse``, so a single row is simultaneously a
	departure and an arrival. Recording it as one signed event would lose the stage transition --
	the very thing the ledger is being extended to capture. Two events, equal and opposite, net to
	zero in the position (``POSITION_KINDS`` excludes both) while ``cg_stage`` carries the meaning.
	"""
	if not is_customer_gold_enabled() or not is_ledger_schema_ready():
		return

	# A receipt is ``record_receipt``'s job. Running both would write two kinds for one row.
	from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
		_receipt_settings,
	)

	if _receipt_settings(doc):
		return

	# A RETURN is ``_record_return_event``'s job, for exactly the same reason, and it has to be
	# excluded here explicitly because a return does not look like anything this dispatcher
	# classifies: it is a one-sided, customer-owned Material Issue, so it falls all the way
	# through to the unclassified branch at the bottom.
	#
	# This was MEASURED, not anticipated. Adding the diagnostic to that branch produced seven
	# "unclassified one-sided movement" rows in the Error Log across one run of the raw-gold
	# return suite -- false alarms on a flow that is fully handled. A diagnostic that cries wolf
	# on a legitimate path is worse than the silence it replaced, because it buries the genuine
	# drift it was added to surface.
	if _is_customer_gold_return(doc):
		return

	currency = (
		frappe.get_cached_value("Company", doc.company, "default_currency")
		if get_customer_gold_valuation_policy() == VALUATION_NOMINAL
		else None
	)

	for row in doc.get("items") or []:
		source, target = row.get("s_warehouse"), row.get("t_warehouse")

		batches = _row_batches(row)
		if len(batches) > 1:
			# GENUINE AMBIGUITY, AND IT GETS SAID OUT LOUD.
			#
			# This used to read ``if len(batches) != 1: continue`` -- a bare continue with no
			# diagnostic, where the two sibling writers ``log_error`` on the very same condition
			# (``record_receipt`` and ``record_fulfilment``). Worse, ``!= 1`` also swallowed
			# every ZERO-batch row, so this dispatcher silently dropped a strictly larger set
			# than its siblings while saying less about it. A custody movement vanished with
			# nothing in the logs, the Error Log or the ledger to say it had, and the position
			# simply drifted away from the stock ledger.
			frappe.log_error(
				title="Customer Gold: multi-batch movement row not recorded",
				message=(
					f"{doc.doctype} {doc.name} row {row.name} spans {len(batches)} batches "
					f"({', '.join(batches)}). One custody event per row cannot honestly "
					f"represent it, so no event was written for this row."
				),
			)
			continue

		# A row with NO batch falls through to the owner check below rather than being dropped
		# here, which is what the siblings do. It is not logged, and that is deliberate: this
		# handler runs on every Stock Entry submit, ownership lives on the batch, and an
		# unbatched row therefore cannot be customer-owned. Logging them would bury the real
		# ambiguity above in noise from every ordinary company movement.
		customer = _batch_owner(batches[0]) if batches else None
		if not customer:
			# Not customer-owned. A conversion's alloy rows land here by design -- they are
			# booked Regular Stock even inside a customer lane, because GK's alloy stays GK's.
			continue

		if row.get("custom_conversion_lane"):
			_write_conversion(doc, row, batches[0], customer, source, currency)
			continue

		if doc.get("stock_entry_type") == PROCESS_LOSS_SE_TYPE:
			# A process-loss Repack carries both halves: the metal booked as lost, and the
			# scrap that is recoverable from it.
			_write_loss_or_recovery(
				doc, row, batches[0], customer, bool(source and not target), currency
			)
			continue

		if doc.get("purpose") == "Manufacture" and source and not target:
			# A consumed row of a manufacture. The produced row is skipped just below.
			_write_production(doc, row, batches[0], customer, currency)
			continue

		if doc.get("purpose") == "Manufacture" and target and not source:
			# The PRODUCED row of a manufacture -- finished goods, and any by-product. No event,
			# and no diagnostic either, because nothing is missing.
			#
			# The consumed row above already wrote the Production event for this metal. The
			# finished piece is that same metal in another shape, so writing a second event here
			# would count the customer's holding twice.
			#
			# This branch is new, and the reason is a regression the walkthrough caught. The FG
			# row used to be hardcoded ``Regular Stock``, so it was dropped by the owner check
			# long before reaching the bottom of this loop. Now that ``_finished_goods_ownership``
			# gives the finished piece its customer -- which it must, or the delivery can never
			# settle -- the row survives that check, is one-sided, and fell through to the
			# unclassified diagnostic below. It logged "No custody event was written; the holding
			# will not reflect this row" on EVERY customer-gold manufacture: 313 rows on the
			# integration site alone, each one a false alarm about correct behaviour.
			#
			# A diagnostic that cries wolf on the normal path is worse than no diagnostic, because
			# it trains people to ignore the log that also carries the real drift.
			continue

		if not (source and target):
			# A one-sided row this dispatcher does not classify -- loss and recovery are
			# separate work. Writing it as a transfer would assert a movement between
			# warehouses that did not happen.
			#
			# But this row is PAST the owner check, so it is confirmed customer metal that
			# moved and produced no custody event. That is the drift this ledger exists to
			# make impossible to have silently, so it is recorded even though the right
			# response is to teach the dispatcher the shape, not to guess at an event.
			frappe.log_error(
				title="Customer Gold: unclassified one-sided movement",
				message=(
					f"{doc.doctype} {doc.name} row {row.name} moves customer-owned "
					f"{row.get('item_code')} one-sided "
					f"(source={source or 'none'}, target={target or 'none'}, "
					f"purpose={doc.get('purpose')}, type={doc.get('stock_entry_type')}). "
					f"No custody event was written; the holding will not reflect this row."
				),
			)
			continue

		qty = flt(row.get("qty"))
		basis = quantity_basis(row.get("item_code"), qty, doc.company)
		out_basis = quantity_basis(row.get("item_code"), -qty, doc.company)

		common = {
			"company": doc.company,
			"customer": customer,
			"reference_doctype": doc.doctype,
			"reference_docname": doc.name,
			"cg_source_row": row.name,
			"item_code": row.get("item_code"),
			"batch_no": batches[0],
			"stock_uom": row.get("stock_uom") or row.get("uom"),
			# Deliberately NO carrying value. An internal move changes where metal is, never what
			# it is worth or what is owed for it -- spec §5.4 keeps both out of the position, and
			# a value here would be a second opinion on a number nothing re-posted.
			"cg_carrying_value_delta": None,
			"cg_currency": currency,
		}

		_write_event(
			cg_event_key=build_event_key(
				doc.company, doc.doctype, row.name, None, EVENT_TRANSFER_OUT
			),
			cg_event_kind=EVENT_TRANSFER_OUT,
			cg_stage=warehouse_stage(source),
			cg_gross_qty_delta=-qty,
			**out_basis,
			**common,
		)
		_write_event(
			cg_event_key=build_event_key(
				doc.company, doc.doctype, row.name, None, EVENT_TRANSFER_IN
			),
			cg_event_kind=EVENT_TRANSFER_IN,
			cg_stage=warehouse_stage(target),
			cg_gross_qty_delta=qty,
			**basis,
			**common,
		)


def record_allocation(doc, method=None):
	"""``on_submit`` for Stock Reservation Entry -- customer metal promised to an order.

	WHY A RESERVATION IS NOT A CUSTODY CHANGE
	------------------------------------------
	Reserving metal promises it to a Manufacturing Work Order. It does not move it, consume it or
	settle anything -- the customer has exactly as much with us afterwards as before. §5.4 makes
	that explicit by giving allocation its own formula rather than folding it into the holding:

	    Free eligible = eligible held − active unconsumed reservations − blocked/quarantined

	So ``POSITION_KINDS`` excludes both kinds, and their gross deltas feed
	:func:`get_customer_gold_free_quantity` instead. The spec's warning attached to that formula --
	"Do not subtract consumed reservations twice" -- is the reason Release exists as its own kind:
	a consumed reservation is released, and its Release cancels its Allocation, so the pair nets
	out and cannot be subtracted again by the movement that consumed it.

	WHY THE RESERVATION ENTRY AND NOT THE FOUR CALLERS
	--------------------------------------------------
	Reservations are released in at least four places -- Make Receive
	(``manufacturing_operation.py:5038``), PC-to-Tagging (``pc_tagging_stock_sync.py:779``),
	process-loss reduction (``loss_stock_entry.py:1122``) and SNC consumption
	(``serial_number_creator.py:964``). Hooking each would be four things to keep in step.
	``Stock Reservation Entry`` itself is the one place they all pass through.

	The reservation service is ownership-blind -- ``stock_reservation_entry_for_mwo``
	(``doc_events/stock_entry.py:1084``) never reads ``custom_customer``. Ownership is resolved
	here the same way every other writer resolves it: from the batch.
	"""
	_write_reservation_events(doc, EVENT_ALLOCATION, 1)


def release_allocation(doc, method=None):
	"""``on_cancel`` for Stock Reservation Entry -- the promise is given back."""
	_write_reservation_events(doc, EVENT_RELEASE, -1)


def _write_reservation_events(doc, kind, sign):
	"""One event per reserved batch, because one reservation can span several.

	Guarded on schema and not on the feature flag for the release direction, for the same reason
	every other reversal is: a reservation made while Customer Gold was enabled must still be
	releasable after it is switched off, or the free quantity stays understated for ever.
	"""
	if not is_ledger_schema_ready():
		return

	if kind == EVENT_ALLOCATION and not is_customer_gold_enabled():
		return

	currency = (
		frappe.get_cached_value("Company", doc.company, "default_currency")
		if get_customer_gold_valuation_policy() == VALUATION_NOMINAL
		else None
	)

	for entry in doc.get("sb_entries") or []:
		batch_no = entry.get("batch_no")
		if not batch_no:
			# A non-batch reservation cannot be attributed to an owner, and guessing from the
			# item alone would reserve against whichever customer happened to be first.
			continue

		customer = _batch_owner(batch_no)
		if not customer:
			continue

		qty = sign * abs(flt(entry.get("qty")))

		_write_event(
			cg_event_key=build_event_key(
				doc.company, doc.doctype, entry.name, None, kind
			),
			cg_event_kind=kind,
			# Reserved metal has not moved. It is still where it was.
			cg_stage=STAGE_RM,
			company=doc.company,
			customer=customer,
			reference_doctype=doc.doctype,
			reference_docname=doc.name,
			cg_source_row=entry.name,
			item_code=doc.item_code,
			batch_no=batch_no,
			stock_uom=doc.get("stock_uom"),
			cg_gross_qty_delta=qty,
			**quantity_basis(doc.item_code, qty, doc.company),
			# The order the metal is promised to, and the reservation itself. Both fields were
			# declared on the DocType and written by nothing.
			cg_order_reference=doc.get("voucher_no"),
			cg_reservation=doc.name,
			cg_carrying_value_delta=None,
			cg_currency=currency,
		)


def get_customer_gold_free_quantity(company, customer, item_code):
	"""§5.4's second formula: held minus what is already promised elsewhere.

	    Free eligible = eligible held − active unconsumed reservations − blocked/quarantined

	``Allocation`` adds to the reserved total and ``Release`` subtracts from it, so a reservation
	that has been consumed nets to zero and cannot be subtracted a second time by the movement
	that consumed it -- which is exactly what the spec warns against.

	``item_code`` IS MANDATORY. This is built from the gross position and inherits its unit
	problem exactly: a reservation is placed on a specific item, and subtracting one item's
	reserved grams from another item's held grams is arithmetic without a unit. See
	:func:`get_customer_gold_position`.

	Blocked/quarantined quantity is not yet modelled and is therefore not subtracted. Said plainly
	rather than silently assumed to be zero.
	"""
	held = get_customer_gold_position(company, customer, item_code)

	filters = {
		"company": company,
		"customer": customer,
		"item_code": item_code,
		"cg_event_kind": ["in", (EVENT_ALLOCATION, EVENT_RELEASE)],
	}

	reserved = flt(
		sum(
			flt(r.cg_gross_qty_delta)
			for r in frappe.get_all(
				LEDGER_DOCTYPE, filters=filters, fields=["cg_gross_qty_delta"]
			)
		)
	)

	return flt(held - reserved, QTY_PRECISION)


def record_fulfilment(doc, method=None):
	"""``on_submit`` for Delivery Note and Sales Invoice.

	Writes one event per customer-owned serial (or per row, when the row carries no serials).
	A return document writes ``Delivery Return`` with the opposite sign -- custody comes back.
	"""
	if not is_physical_fulfilment(doc) or not is_customer_gold_enabled():
		return

	is_return = bool(cint(doc.get("is_return")))
	kind = EVENT_DELIVERY_RETURN if is_return else EVENT_DELIVERY
	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	written = []

	for row in doc.get("items") or []:
		# The batch comes from the bundle when there is one; see ``_row_batches`` for the two
		# movement classes that ``row.batch_no`` alone loses. A row whose bundle spans several
		# batches is skipped with a log rather than guessed at -- splitting one row's quantity
		# across batches of different owners needs the per-batch quantities, which is the
		# component work in §4.4, not something to improvise here.
		batches = _row_batches(row)
		if len(batches) > 1:
			frappe.log_error(
				title="Customer Gold: multi-batch row not settled",
				message=(
					f"{doc.doctype} {doc.name} row {row.name} moves {len(batches)} batches "
					f"({', '.join(batches)}). One custody event per row cannot represent that "
					f"faithfully, so no event was written for this row."
				),
			)
			continue

		batch_no = batches[0] if batches else None
		customer = _batch_owner(batch_no)
		if not customer:
			continue

		serials = _row_serials(row)
		# THE SIGN IS ERPNEXT'S, AND IT IS APPLIED EXACTLY ONCE.
		#
		# This used to read ``per_serial if is_return else -per_serial``, which double-negated
		# every return. erpnext builds a return row with an ALREADY NEGATIVE qty
		# (``sales_and_purchase_return.py:540``) and enforces it
		# (``:236`` throws "must be negative in return document"). It then negates once more when
		# it posts the SLE (``selling_controller.py:707``: ``"actual_qty": -1 * flt(item_row.qty)``),
		# with no ``is_return`` branch anywhere in that path.
		#
		# So mirroring erpnext means negating unconditionally: a delivery row (+qty) becomes a
		# negative custody delta, a return row (-qty) becomes a positive one. Branching on
		# ``is_return`` applied the sign twice and made a return REDUCE the customer's position
		# again. It also put ``cg_gross_qty_delta`` and ``cg_carrying_value_delta`` into direct
		# contradiction, since ``stock_value_difference`` is positive on a return.
		per_serial = flt(row.get("qty")) / len(serials)
		signed = -per_serial

		# Money is recorded ONLY under Nominal. Under Zero Value the stock genuinely carries
		# no value, so a 0.0 here would assert a measurement that was never taken; NULL says
		# "not valued", which is the truth and is what makes the memorandum record complete
		# under either answer to D01.
		# A batch holding recorded customer components settles the customer's share of it and
		# nothing else -- never the finished item's stock value, not even as a fallback. Only a
		# raw batch (no components) settles from the stock ledger, which is the case that
		# reading was written for. See _customer_share.
		#
		# Same sign convention for value and fine gold as for the quantity delta: they LEAVE on a
		# delivery and come BACK on a return. erpnext builds a return row with an already
		# negative qty, so the sign is read off the row rather than branched on is_return.
		direction = -1 if flt(row.get("qty")) >= 0 else 1
		share = _customer_share(
			doc, batch_no, customer, abs(flt(row.get("qty"))), valued=nominal
		)
		carrying_value = None
		if nominal:
			if share is None:
				carrying_value = _row_carrying_value(doc, row)
			elif share.value is None:
				_log_unsettled(doc, row, batch_no, share.reason)
			else:
				carrying_value = direction * abs(share.value)
		value_per_serial = (
			flt(carrying_value) / len(serials) if carrying_value is not None else None
		)

		# Fine gold follows the same rule. A finished piece has no purity of its own, so reading
		# it off the delivered item recorded 0.000 fine grams, Unknown, for every piece shipped,
		# and the customer's fine position never came down. The customer's components say how
		# much of their gold left.
		if share is not None and share.fine is not None:
			basis = fine_basis(direction * abs(share.fine) / len(serials), doc.company)
		else:
			basis = quantity_basis(row.get("item_code"), signed, doc.company)

		for serial_no in serials:
			written.append(
				_write_event(
					cg_event_key=build_event_key(
						doc.company, doc.doctype, row.name, serial_no, kind
					),
					cg_event_kind=kind,
					cg_stage=STAGE_FG if is_return else STAGE_CLOSED,
					company=doc.company,
					customer=customer,
					reference_doctype=doc.doctype,
					reference_docname=doc.name,
					cg_source_row=row.name,
					item_code=row.get("item_code"),
					batch_no=batch_no,
					serial_no=serial_no,
					stock_uom=row.get("stock_uom") or row.get("uom"),
					cg_gross_qty_delta=signed,
					**basis,
					cg_carrying_value_delta=value_per_serial,
					# Currency is stamped whenever the policy is Nominal, even if the row turned
					# out to carry no SLE: it records which policy was in force when the event was
					# written, which is what makes a later reconciliation possible.
					cg_currency=frappe.get_cached_value(
						"Company", doc.company, "default_currency"
					)
					if nominal
					else None,
				)
			)

	settle_customer_gold_liability(doc, written)


def settle_customer_gold_liability(doc, event_names):
	"""Release the customer-gold liability for what was physically delivered. SOP Example C.

	THE HALF OF THE SOP THAT DID NOT EXIST
	--------------------------------------
	The SOP's spine is *"Receipt creates liability → … → Delivery/return clears liability"*. The
	creating half was built and proven; the clearing half was not built at all. Before this
	function there were **no Journal Entries anywhere** in this module: the configured Customer
	Gold COGS Adjustment account was validated on save and then never posted to, and the ledger's
	``cg_settlement_voucher`` field was declared and never filled. Liability, once created, stayed
	on the books for ever.

	WHAT IS SETTLED, AND WHY IT IS NOT THE FG VALUE
	-----------------------------------------------
	Only the **customer's nominal component** of the delivered rows -- never the finished item's
	full stock value. Company alloy, company gold, stones, findings and production cost are GK's
	and are recovered through the invoice, not through the liability. Settling the FG value would
	discharge more obligation than the customer ever created; S07 §7.2 is explicit.

	The amount therefore comes from the custody events just written, whose value is the SLE's own
	``stock_value_difference`` -- so the settlement cannot disagree with the stock posting it
	accompanies. A delivery's events are negative (value leaving), so the settlement debits the
	liability; a return's are positive and it credits, restoring the obligation.

	WHY EXACTLY ONCE, WITHOUT A PRE-CHECK
	-------------------------------------
	Settlement is claimed per EVENT, by stamping ``cg_settlement_voucher``. Only events that are
	still unstamped are settled, so a repeated callback finds nothing left to do. That is stronger
	than counting prior settlements on the document: the event rows are already unique on
	``cg_event_key``, so "has this metal been settled" is answered by the same constraint that
	answers "has this metal moved".

	Partial delivery needs no special case at all -- an undelivered serial simply has no event,
	so its share of the obligation stays open.

	ATOMIC WITH THE DELIVERY
	------------------------
	No try/except. This runs inside the submitting document's transaction, so a bad account, a
	missing dimension or an unbalanced entry fails the delivery rather than shipping metal whose
	obligation was never discharged. A silent partial success is the one outcome S07 forbids.
	"""
	if not event_names:
		return

	# Zero Value records custody only: there is no booked amount to release, and posting a
	# zero-value JE would assert a settlement that never happened.
	if get_customer_gold_valuation_policy() != VALUATION_NOMINAL:
		return

	# FOR UPDATE: the claim below is what stops a second JE for the same events, so reading the
	# unclaimed set must be a locking read. Two concurrent submits of one document would otherwise
	# both see the events unclaimed and both post. The second now waits, and its locking read sees
	# the first one's committed claim -- a plain read would keep its stale snapshot.
	rows = frappe.db.get_values(
		LEDGER_DOCTYPE,
		{
			"name": ["in", event_names],
			"cg_settlement_voucher": ["is", "not set"],
		},
		[
			"name",
			"customer",
			"cg_carrying_value_delta",
			"cg_source_row",
			"serial_no",
			"batch_no",
		],
		as_dict=True,
		order_by="name",
		for_update=True,
	)
	if not rows:
		return

	precision = (
		frappe.get_precision("Journal Entry Account", "debit_in_account_currency") or 2
	)

	# Grouped by customer so each debit line carries its own party. One document, one JE --
	# S07 §7.2 permits batching provided the per-event allocation is retained, which
	# ``cg_settlement_voucher`` does.
	per_customer = {}
	for row in rows:
		if row.cg_carrying_value_delta is None:
			continue
		# Negate: a delivery event's value is negative (stock left), and the settlement amount
		# is what that removes from the obligation.
		per_customer[row.customer] = flt(per_customer.get(row.customer, 0.0)) - flt(
			row.cg_carrying_value_delta
		)

	total = flt(sum(per_customer.values()), precision)
	if not total:
		# Every event was unvalued, or the amounts cancelled. Nothing to post, and the events
		# stay unstamped so a later correction can still settle them.
		return

	accounts = get_customer_gold_company_settings(doc.company)
	settled = [row for row in rows if flt(row.cg_carrying_value_delta)]
	je = _build_settlement_entry(doc, accounts, per_customer, total, precision, settled)

	# Claim only the events this JE actually settled. An event whose value could not be
	# established carries 0 -- ``_customer_share`` refused to guess it -- and stamping it would
	# mark it settled for good: the claim is what every later settlement filters on (F9).
	for row in rows:
		if not flt(row.cg_carrying_value_delta):
			continue
		frappe.db.set_value(
			LEDGER_DOCTYPE, row.name, "cg_settlement_voucher", je, update_modified=False
		)

	return je


def _build_settlement_entry(doc, accounts, per_customer, total, precision, events=()):
	"""Post the standard Journal Entry and return its name.

	Direction follows the sign of ``total``: positive means metal was delivered, so the
	obligation shrinks -- **Dr Customer Gold Liability / Cr Customer Gold COGS Adjustment**, the
	SOP's Example C posting. A physical return inverts both legs.
	"""
	# The settings are validated again HERE, not trusted from save time (F4). The save-time
	# validator returns early while the feature flag is off and never sees a configuration that
	# changed afterwards -- which is how the KGJPL row posted KGJPL-JE-JE-26-00018, Dr Customer
	# Goods Receive / Cr Advances from Customers, liability to liability.
	#
	# Blocking fails the delivery, and that is the right outcome: the alternative is a JE that
	# erpnext accepts, that discharges nothing, and that permanently claims the events via
	# cg_settlement_voucher so no later correction can settle them.
	validate_settlement_accounts(
		doc.company,
		accounts.liability_account,
		accounts.cogs_adjustment_account,
		where=f"{doc.doctype} {doc.name}",
	)

	# A party is set only when the account actually demands one. A Customer Gold Liability
	# account is commonly a plain Liability ledger, for which Frappe rejects a party outright;
	# stamping one unconditionally would fail every settlement on such a chart.
	liability_type = frappe.get_cached_value(
		"Account", accounts.liability_account, "account_type"
	)
	party_required = liability_type in ("Payable", "Receivable")

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = doc.company
	je.posting_date = doc.get("posting_date") or frappe.utils.nowdate()
	# The structural link is the ledger's own ``cg_settlement_voucher``, which points from every
	# settled event to this JE; each event carries its customer, document row, serial and batch,
	# and the batch's Batch Components name the customer's source receipts. The remark repeats
	# the events so a reader of the JE alone can find them.
	je.user_remark = frappe._(
		"Customer Gold settlement for {0} {1}. Events: {2}"
	).format(
		doc.doctype,
		doc.name,
		"; ".join(
			f"{e.name} (row {e.cg_source_row}, serial {e.serial_no or '-'}, batch {e.batch_no or '-'})"
			for e in events
		)
		or "-",
	)

	for customer, amount in per_customer.items():
		amount = flt(amount, precision)
		if not amount:
			continue
		# NO ``reference_type``/``reference_name`` on the line. ``Journal Entry Account``
		# restricts that Select to invoice-like doctypes and rejects "Delivery Note" outright
		# -- the integration suite is what surfaced it. The link that matters is the ledger's
		# own ``cg_settlement_voucher``, which points the other way and is not restricted.
		line = {
			"account": accounts.liability_account,
			"debit_in_account_currency": amount if amount > 0 else 0,
			"credit_in_account_currency": -amount if amount < 0 else 0,
		}
		if party_required:
			line["party_type"] = "Customer"
			line["party"] = customer
		je.append("accounts", line)

	je.append(
		"accounts",
		{
			"account": accounts.cogs_adjustment_account,
			"credit_in_account_currency": total if total > 0 else 0,
			"debit_in_account_currency": -total if total < 0 else 0,
		},
	)

	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def reverse_fulfilment(doc, method=None):
	"""``on_cancel`` for Delivery Note and Sales Invoice.

	A reversal is a NEW row pointing at the original, never an edit and never a delete: the
	doctype is not submittable and its history is the point. Cancelling twice is harmless --
	the reversal key is derived from the original's, so the second attempt collides.
	"""
	if not is_physical_fulfilment(doc):
		return

	# GUARDED ON SCHEMA, DELIBERATELY NOT ON THE FEATURE FLAG.
	#
	# This handler had no guard at all, and that was a live hazard: on a site whose ledger is the
	# 34-field orphan (no ``serial_no`` column), the reversal write raises
	# ``MySQLdb.OperationalError`` from inside ``on_cancel`` -- so CANCELLING A DELIVERY NOTE
	# FAILS, on a document that may have nothing to do with customer gold.
	#
	# The flag is the wrong guard here and always was. §4.6 is explicit: "Historical reversal and
	# delete protection must work even after current enablement or receipt configuration
	# changes." A document whose events were written while the feature was on must stay
	# reversible after it is turned off; gating on the flag would strand exactly that history.
	#
	# When the schema cannot support a safe reversal but events exist, failing silently is the
	# one unacceptable outcome -- it returns success without the reversal the audit trail needs.
	# That case blocks actionably instead.
	if not is_ledger_schema_ready():
		if has_customer_gold_events(doc):
			frappe.throw(
				frappe._(
					"{0} {1} carries Customer Gold custody events, but this site's "
					"Customer Gold Ledger Entry schema is incomplete, so they cannot be "
					"reversed safely. Complete the schema migration before cancelling."
				).format(doc.doctype, frappe.bold(doc.name)),
				title=frappe._("Customer Gold Schema Incomplete"),
			)
		return

	_cancel_settlement_entries(doc)
	_reverse_events(doc, [EVENT_DELIVERY, EVENT_DELIVERY_RETURN], STAGE_FG)


def _cancel_settlement_entries(doc):
	"""Cancel the settlement Journal Entries this document produced.

	A reversal event restores the CUSTODY position, but custody and the general ledger are two
	different books. Leaving the JE submitted while writing a reversal row would put them in
	permanent disagreement: the ledger would say the obligation is open again while the accounts
	said it was discharged.

	Cancelled through the standard document lifecycle, never by editing GL rows, so the reversal
	posting is Frappe's own and carries the normal audit trail. Already-cancelled entries are
	skipped, which makes a repeated cancellation callback harmless.
	"""
	names = {
		row.cg_settlement_voucher
		for row in frappe.get_all(
			LEDGER_DOCTYPE,
			filters={
				"reference_doctype": doc.doctype,
				"reference_docname": doc.name,
				# ``["is", "set"]`` rather than ``["not in", ["", None]]``: Frappe renders the
				# latter as a plain SQL ``NOT IN``, and ``NOT IN (..., NULL)`` is NULL for every
				# row, so it matched nothing -- cancelling a delivery left its settlement JE
				# submitted, with custody reversed and the GL still saying discharged. The
				# integration suite caught it.
				"cg_settlement_voucher": ["is", "set"],
			},
			fields=["cg_settlement_voucher"],
		)
		if row.cg_settlement_voucher
	}

	for name in names:
		if frappe.db.get_value("Journal Entry", name, "docstatus") != 1:
			continue
		entry = frappe.get_doc("Journal Entry", name)
		entry.flags.ignore_permissions = True
		entry.cancel()


def reverse_receipt(doc, method=None):
	"""``on_cancel`` for Stock Entry -- the mirror of :func:`record_receipt`.

	Guarded on SCHEMA, deliberately not on the feature flag, for the same reason
	``reverse_fulfilment`` is: a receipt posted while Customer Gold was enabled must stay
	reversible after it is switched off. Gating on the flag would strand exactly the history the
	ledger exists to keep.

	No ``_receipt_settings`` check either. That reads TODAY's configured stock entry type, and a
	site that has since reconfigured it would silently fail to reverse an older receipt. The
	events themselves are the authority: if this document wrote none, the query returns nothing
	and this is a no-op for every ordinary Stock Entry.
	"""
	if not is_ledger_schema_ready():
		if has_customer_gold_events(doc):
			frappe.throw(
				frappe._(
					"{0} {1} carries Customer Gold custody events, but this site's "
					"Customer Gold Ledger Entry schema is incomplete, so they cannot be "
					"reversed safely. Complete the schema migration before cancelling."
				).format(doc.doctype, frappe.bold(doc.name)),
				title=frappe._("Customer Gold Schema Incomplete"),
			)
		return

	# Both Stock-Entry-borne kinds. A cancelled RETURN gives the customer their holding back
	# just as a cancelled RECEIPT takes it away -- same document type, same handler.
	_reverse_events(doc, list(STOCK_ENTRY_KINDS), STAGE_RM)


def reverse_revaluation(doc, method=None):
	"""``on_cancel`` for Stock Reconciliation -- the mirror of ``revalue_customer_gold``.

	Until this existed, nothing undid a revaluation. ``hooks.py`` registered no ``on_cancel`` for
	Stock Reconciliation and ``_reverse_events`` was wired only to the delivery and stock-entry
	paths, so cancelling a revaluation reversed the stock value and the GL -- ERPNext does that
	itself -- while the custody event stayed on the ledger.

	That left the two books disagreeing, and not harmlessly. The settlement Journal Entry takes
	its amount from the ledger, so a phantom Revaluation event inflated what was settled against
	the customer's liability. It also made the revaluation idempotency key unusable as a record of
	what is actually posted, which is why ``_resolve_revaluation_event_key`` judges a prior event
	by its source voucher's docstatus rather than by the row's existence.

	Guarded on SCHEMA and deliberately not on the feature flag, for the same reason
	``reverse_receipt`` is: a revaluation posted while Customer Gold was enabled must stay
	reversible after it is switched off.

	The events themselves are the authority -- an ordinary Stock Reconciliation wrote none, the
	query returns nothing, and this is a no-op for every non-customer-gold document.
	"""
	if not is_ledger_schema_ready():
		if has_customer_gold_events(doc):
			frappe.throw(
				frappe._(
					"{0} {1} carries Customer Gold custody events, but this site's "
					"Customer Gold Ledger Entry schema is incomplete, so they cannot be "
					"reversed safely. Complete the schema migration before cancelling."
				).format(doc.doctype, frappe.bold(doc.name)),
				title=frappe._("Customer Gold Schema Incomplete"),
			)
		return

	_reverse_events(doc, [EVENT_REVALUATION], STAGE_RM)


def _reverse_events(doc, kinds, stage):
	"""Write one Reversal per effective event of ``kinds`` on ``doc``.

	Shared by the receipt and fulfilment cancel paths. A reversal is a NEW row pointing at the
	original -- never an edit, never a delete -- and its key is derived from the original's, so
	a second cancellation callback collides on ``cg_event_key`` and is absorbed.

	``stage`` is passed rather than copied from the original because the two callers mean
	different things by it: cancelling a delivery puts the metal back in FG, cancelling a
	receipt puts it back where it came from, which was never RM in the first place.
	"""
	originals = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={
			"reference_doctype": doc.doctype,
			"reference_docname": doc.name,
			"cg_event_kind": ["in", kinds],
		},
		fields=[
			"name",
			"cg_event_key",
			"company",
			"customer",
			"cg_source_row",
			"item_code",
			"batch_no",
			"serial_no",
			"stock_uom",
			"cg_gross_qty_delta",
			"cg_currency",
			"cg_fine_gold_delta",
			"cg_reference_qty_delta",
			"cg_reference_purity",
			"cg_carrying_value_delta",
			# The statuses are part of the basis. A reversal that negated an Unknown fine
			# figure while claiming nothing about it would leave the pair disagreeing.
			"cg_fine_measurement_status",
			"cg_reference_measurement_status",
			"cg_measurement_reason",
			# Copied for the same reason as the statuses: a reversal restates what WAS, and a
			# policy switched since must not be projected backwards onto the row that undoes it.
			"cg_policy_version",
		],
	)

	for original in originals:
		_write_event(
			# KEYED ON THE ORIGINAL EVENT, not on its source row.
			#
			# The row is not unique enough. One Stock Entry row now writes TWO events -- a
			# Transfer Out and a Transfer In -- and both carry the same ``cg_source_row`` and the
			# same (absent) serial. Keyed on the row, their two reversals computed the SAME key,
			# so the second collided with the first and was silently absorbed: a cancelled
			# transfer reversed its departure and left its arrival standing.
			#
			# ``original.name`` gives exactly one reversal per event, which is the real rule, and
			# stays idempotent across a repeated cancellation callback.
			cg_event_key=build_event_key(
				original.company,
				doc.doctype,
				original.name,
				original.serial_no,
				EVENT_REVERSAL,
			),
			cg_event_kind=EVENT_REVERSAL,
			cg_stage=stage,
			company=original.company,
			customer=original.customer,
			reference_doctype=doc.doctype,
			reference_docname=doc.name,
			cg_source_row=original.cg_source_row,
			cg_reversal_of=original.name,
			item_code=original.item_code,
			batch_no=original.batch_no,
			serial_no=original.serial_no,
			stock_uom=original.stock_uom,
			# Exactly undoes the original, whatever it was -- never recomputed from today's
			# quantities or rates. That applies to EVERY basis: a Manufacturing Setting edited
			# since the original was written must not restate what the reversal undoes.
			cg_gross_qty_delta=-flt(original.cg_gross_qty_delta),
			cg_fine_gold_delta=-flt(original.cg_fine_gold_delta)
			if original.cg_fine_gold_delta
			else None,
			cg_reference_qty_delta=-flt(original.cg_reference_qty_delta)
			if original.cg_reference_qty_delta
			else None,
			cg_reference_purity=original.cg_reference_purity,
			cg_carrying_value_delta=-flt(original.cg_carrying_value_delta)
			if original.cg_carrying_value_delta
			else None,
			cg_currency=original.cg_currency,
			# Copied, never re-derived. Negating a quantity does not measure it, so the
			# reversal of an Unknown stays Unknown and keeps the original's reason -- and a
			# Manufacturing Setting repaired since would otherwise silently upgrade a
			# historical gap to Known on the row that undoes it.
			cg_fine_measurement_status=original.cg_fine_measurement_status,
			cg_reference_measurement_status=original.cg_reference_measurement_status,
			cg_measurement_reason=original.cg_measurement_reason,
			# Passing the key at all is what stops ``_write_event``'s ``setdefault`` from
			# stamping today's policy. A legacy original carries None here, and the reversal
			# honestly records None rather than inventing a policy nobody chose.
			cg_policy_version=original.cg_policy_version,
		)


def block_delete_with_customer_gold_events(doc, method=None):
	"""``on_trash`` for Delivery Note and Sales Invoice.

	THE DECISION, and why it is a throw rather than a cascade or a shrug.

	``reference_doctype``/``reference_docname`` form a Dynamic Link. Deleting the voucher
	leaves rows pointing at a name that no longer resolves, and Frappe does not clean them up:
	Dynamic Links are not enforced by the database and ``delete_doc`` only checks the LINKED
	doctypes it can discover, which does not include a hand-written dynamic pair like this one.
	So the default behaviour is silent orphaning of an audit trail.

	Three options were on the table:

	* **Cascade-delete the events.** Rejected outright. An audit ledger that disappears when
	  the thing it audits disappears is not an audit ledger.
	* **Leave the rows orphaned.** Rejected: ``get_customer_gold_position`` would keep counting
	  a delivery whose document no longer exists, and no one could ever find out why.
	* **Refuse the delete.** Chosen. Cancelling is always available and is the correct action;
	  it writes a Reversal (``reverse_fulfilment``) so the net position returns to zero while
	  the history survives. That is CG-T143's "cancelled audit rows need not be deleted",
	  stated from the other side.

	GATED ON EVENT EXISTENCE, NOT ON THE FEATURE FLAG -- and it was the wrong way round.

	This used to return early when ``is_customer_gold_enabled()`` was false, which inverted
	the protection it exists to give: turning the feature OFF silently stopped protecting the
	events already written while it was ON. The rows do not disappear when the flag flips, so
	neither should the guard. §4.6: "Preserve the event-aware delete guard regardless of the
	current feature flag."

	Blast radius is still small, but for the right reason: a document with no custody events
	returns immediately, so a site that never used the feature sees exactly its previous
	deletion behaviour.
	"""
	if not is_physical_fulfilment(doc):
		return

	# NOT ``is_ledger_schema_ready()``. That asks whether the ledger can be WRITTEN, and this
	# guard only reads -- it needs ``name`` and the two reference columns, all of which the
	# 34-field orphan on ``gk``/``kg-gk`` carries. Gating a read on write-readiness turned the
	# protection off on exactly the sites whose schema is behind, which is the same defect
	# ``has_customer_gold_events`` carried; see its docstring.
	if not frappe.db.table_exists(LEDGER_DOCTYPE):
		return

	events = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={"reference_doctype": doc.doctype, "reference_docname": doc.name},
		fields=["name"],
		limit=5,
	)
	if not events:
		return

	frappe.throw(
		frappe._(
			"{0} {1} carries {2} Customer Gold custody event(s) and cannot be deleted, because "
			"deleting it would orphan them. Cancel it instead -- that writes a reversal and "
			"keeps the history. Events: {3}."
		).format(
			doc.doctype,
			frappe.bold(doc.name),
			len(events),
			", ".join(e.name for e in events),
		),
		title=frappe._("Customer Gold Custody Record Exists"),
	)


#: The ONLY event kinds that change how much metal the customer has with us.
#:
#: Spec §5.4 states the position as a formula, and it is a SHORT list on purpose:
#:
#:     Accepted receipt/opening source
#:     + valid physical returns
#:     − valid physical fulfilment
#:     − valid raw returns
#:     ± explicit corrections/reversals
#:
#: Approved Loss and Recovery join it because metal genuinely leaves and genuinely comes back.
#: Note what that does NOT extend to: §5.5 is explicit that a physical loss does not by itself
#: reduce the customer's monetary obligation. Quantity and liability are different books.
#:
#: Everything else -- Transfer, Conversion, Production, Allocation, Release, Revaluation --
#: changes the STAGE or the VALUE of metal the customer still has. Summing those into a holding
#: is how a ledger starts reporting metal that does not exist.
POSITION_KINDS = (
	EVENT_RECEIPT,
	EVENT_DELIVERY,
	EVENT_DELIVERY_RETURN,
	EVENT_RETURN,
	EVENT_REVERSAL,
	EVENT_APPROVED_LOSS,
	EVENT_RECOVERY,
)


def get_customer_gold_position(company, customer, item_code):
	"""Net gross grams of **one item** that entered and left custody **as that item**.

	``item_code`` IS MANDATORY, AND THAT IS THE §2.3 FIX
	----------------------------------------------------
	This used to default it to ``None`` and sum across every item the customer had. Filtering
	the kinds stopped an earlier +1.1 g leak but did not make that sum a holding, and a review
	traced what replaced it::

	    Receipt        item A 99.9%    +10.000   counted
	    Conversion Out item A          -10.000   not counted
	    Conversion In  item B          +13.249   not counted
	    Delivery       item B          -13.249   counted
	                                   --------
	                                    -3.249   <- an artificial shortfall

	Nothing was lost. The receipt is denominated in item A's gross grams and the delivery in
	item B's, and those are different units. There is no correct number to return for that
	question, so the question is refused rather than answered wrongly -- a clamp or a
	"corrected" sum would only hide which of the two units the caller meant.

	WHAT THIS NUMBER IS, AND IS NOT
	-------------------------------
	It is what SOP Example D needs: 10 g of 24KT came in, 2 g of the same 24KT went back, so 8 g
	of that item remain accounted for. Both sides are the same item, so the subtraction is real.

	It is **not** a physical stock balance. After a conversion, item A's figure stays at +10
	while no item A is physically present -- ``POSITION_KINDS`` excludes conversion on purpose,
	because a conversion changes the metal's form, not whether we owe for it. ``cg_stage`` says
	where the metal now is; physical stock comes from ``get_batch_qty``.

	For custody across items -- the only cross-item question with an answer -- use
	:func:`get_customer_gold_fine_position`. Fine gold is the same unit on both sides of a
	purity change, and closes the example above to exactly zero.

	:raises ValueError: if ``item_code`` is missing.
	"""
	if not item_code:
		raise ValueError(
			"get_customer_gold_position requires item_code: gross grams of different "
			"purities are different units and their sum is not a holding. For a "
			"customer-wide figure use get_customer_gold_fine_position."
		)
	return _position(company, customer, "cg_gross_qty_delta", item_code)


def get_customer_gold_fine_position(company, customer, item_code=None):
	"""Fine-gold content the customer still has with us -- **the authoritative holding**.

	Gross grams change when purity changes; absolute gold content does not. So this is the one
	basis that survives a Metal Conversion, and ``item_code`` is optional here precisely because
	fine grams of a 99.9% item and of a 75.4% item are the same unit and may be added.

	Returns ``0.0`` when no event carries a fine measure, which is the honest answer for a site
	whose items have no Metal Purity attribute -- not an assertion that the customer holds
	nothing. :func:`get_customer_gold_position_report` separates those two cases; this scalar
	cannot, and callers that must tell them apart should use the report.
	"""
	return _position(company, customer, "cg_fine_gold_delta", item_code)


#: Which status column qualifies which numeric column. Gross has none: it is the document's own
#: quantity and is always measured when a row exists at all. Carrying value has none either --
#: ``cg_currency`` already carries that distinction, being NULLable where a Currency column is not.
_STATUS_FIELD = {
	"cg_fine_gold_delta": "cg_fine_measurement_status",
	"cg_reference_qty_delta": "cg_reference_measurement_status",
}


def get_customer_gold_position_report(company, customer, item_code=None, basis="fine"):
	"""A subtotal **with its completeness**, per spec §2.4.

	WHY A SCALAR IS NOT ENOUGH
	--------------------------
	Every numeric column on the ledger is ``NOT NULL DEFAULT 0``, and ``flt(None)`` is ``0.0``.
	So a row whose fine gold could not be measured stores the same bytes as a row that measured
	exactly no gold, and any ``SUM()`` over the two is a number with no error bar. A subtotal
	drawn from an unmeasured source is INCOMPLETE, not exact, and saying so is the difference
	between a reconciliation and a guess.

	Returns ``total`` (every row, the scalar the other functions give), ``known_total`` (only
	rows whose status for this basis is ``Known``), the count of rows in each other state, the
	distinct reasons behind them, and ``complete`` -- true only when nothing is missing.

	``total`` and ``known_total`` being equal is not proof of completeness on its own: an
	Unknown row contributes its stored 0.0 to both. ``complete`` is the field to read.

	Legacy rows written before the status columns existed have neither status nor reason. They
	are counted as ``legacy_source_without_snapshot`` rather than assumed Known, because nothing
	about them was ever recorded either way.
	"""
	field = {
		"fine": "cg_fine_gold_delta",
		"gross": "cg_gross_qty_delta",
		"reference": "cg_reference_qty_delta",
	}.get(basis)
	if not field:
		raise ValueError(
			f"unknown basis {basis!r}: expected 'fine', 'gross' or 'reference'"
		)

	if basis == "gross" and not item_code:
		raise ValueError(
			"the gross basis requires item_code -- see get_customer_gold_position"
		)

	status_field = _STATUS_FIELD.get(field)
	fields = [field, "cg_measurement_reason"] + ([status_field] if status_field else [])

	filters = {
		"company": company,
		"customer": customer,
		"cg_event_kind": ["in", POSITION_KINDS],
	}
	if item_code:
		filters["item_code"] = item_code

	rows = frappe.get_all(LEDGER_DOCTYPE, filters=filters, fields=fields)

	total = known = 0.0
	counts = {STATUS_UNKNOWN: 0, STATUS_INVALID: 0}
	reasons = {}

	for row in rows:
		value = flt(row.get(field))
		total += value

		# Gross carries no status column, so every gross row is measured by definition.
		status = row.get(status_field) if status_field else STATUS_KNOWN
		if not status:
			status = STATUS_UNKNOWN
			reason = row.get("cg_measurement_reason") or REASON_LEGACY_SOURCE
		else:
			reason = row.get("cg_measurement_reason")

		if status == STATUS_KNOWN:
			known += value
			continue

		counts[status] = counts.get(status, 0) + 1
		if reason:
			reasons[reason] = reasons.get(reason, 0) + 1

	unmeasured = counts[STATUS_UNKNOWN] + counts[STATUS_INVALID]
	return {
		"basis": basis,
		"item_code": item_code,
		"rows": len(rows),
		"total": flt(total, QTY_PRECISION),
		"known_total": flt(known, QTY_PRECISION),
		"unknown_count": counts[STATUS_UNKNOWN],
		"invalid_count": counts[STATUS_INVALID],
		"reasons": reasons,
		"complete": unmeasured == 0,
	}


def _position(company, customer, field, item_code=None):
	"""Sum ``field`` over the position-changing kinds only.

	A bare sum: an unmeasured row contributes its stored 0.0 and leaves no trace here. Use
	:func:`get_customer_gold_position_report` wherever that matters.
	"""
	filters = {
		"company": company,
		"customer": customer,
		"cg_event_kind": ["in", POSITION_KINDS],
	}
	if item_code:
		filters["item_code"] = item_code

	rows = frappe.get_all(LEDGER_DOCTYPE, filters=filters, fields=[field])
	return flt(sum(flt(r.get(field)) for r in rows), QTY_PRECISION)
