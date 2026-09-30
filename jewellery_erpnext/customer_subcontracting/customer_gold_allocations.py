# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Write and read ``Customer Gold Allocation`` -- the receipt each disposition drew on.

Every writer here runs inside the transaction of the document whose event it decomposes, and
every row carries a deterministic ``allocation_key`` under a UNIQUE constraint: a retry of the
same business operation computes the same key and is absorbed, exactly like the ledger's own
``cg_event_key``.
"""

import hashlib
from contextlib import suppress

import frappe
from frappe.utils import flt, now_datetime

from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
	EVENT_RECEIPT,
	EVENT_REVERSAL,
	LEDGER_DOCTYPE,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)

ALLOCATION_DOCTYPE = "Customer Gold Allocation"

DISPOSITION_FG_DELIVERY = "FG Delivery"
DISPOSITION_DELIVERY_RETURN = "Delivery Return"
DISPOSITION_RAW_RETURN = "Raw Return"
DISPOSITION_REVERSAL = "Reversal"

BASIS_COMPONENT = "Component Share"
BASIS_DIRECT = "Direct Batch"
BASIS_RECEIPT_LINK = "Receipt Link"

#: Dispositions that draw on a receipt's entitlement. Positive draws, negative restores.
DRAWING_DISPOSITIONS = (
	DISPOSITION_FG_DELIVERY,
	DISPOSITION_DELIVERY_RETURN,
	DISPOSITION_RAW_RETURN,
	DISPOSITION_REVERSAL,
)

AMOUNT_PRECISION = 2
QTY_PRECISION = 6


def is_allocation_schema_ready():
	"""False on a site that has this code but not yet the DocType. Every writer no-ops there, so
	a deploy that lands code before migrate degrades to today's behaviour instead of failing a
	delivery."""
	return bool(frappe.db.table_exists(ALLOCATION_DOCTYPE))


def allocation_key(*parts):
	raw = "|".join(str(part or "") for part in parts)
	return hashlib.sha1(raw.encode()).hexdigest()


def _insert(values):
	doc = frappe.get_doc({"doctype": ALLOCATION_DOCTYPE, **values})
	try:
		doc.insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.clear_last_message()
		return frappe.db.get_value(
			ALLOCATION_DOCTYPE, {"allocation_key": values["allocation_key"]}, "name"
		)
	return doc.name


# ------------------------------------------------------------------------------------------
# Receipts behind a batch
# ------------------------------------------------------------------------------------------


def effective_receipt_events(filters):
	"""Receipt events matching ``filters`` that no Reversal has undone."""
	events = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={**filters, "cg_event_kind": EVENT_RECEIPT},
		fields=[
			"name",
			"company",
			"customer",
			"reference_docname",
			"cg_source_row",
			"item_code",
			"batch_no",
			"cg_gross_qty_delta",
			"cg_fine_gold_delta",
			"cg_carrying_value_delta",
			"cg_currency",
		],
		order_by="creation",
	)
	if not events:
		return []
	reversed_ = set(
		frappe.get_all(
			LEDGER_DOCTYPE,
			filters={
				"cg_event_kind": EVENT_REVERSAL,
				"cg_reversal_of": ["in", [e.name for e in events]],
			},
			pluck="cg_reversal_of",
		)
	)
	return [e for e in events if e.name not in reversed_]


def receipts_of_batch(company, customer, batch_no):
	"""The receipt rows whose metal sits in ``batch_no`` itself, each with its share of it.

	One batch normally carries one receipt row. When a receipt row reused an existing batch it
	carries several, and a draw on the batch is split between them in proportion to what each
	received -- the same pro-rata rule the traceability replay uses.
	"""
	events = effective_receipt_events(
		{"company": company, "customer": customer, "batch_no": batch_no}
	)
	total = sum(flt(e.cg_gross_qty_delta) for e in events)
	for event in events:
		event.share = (flt(event.cg_gross_qty_delta) / total) if total else 0.0
	return events


def open_receipts_of_batch(company, customer, batch_no):
	"""``receipts_of_batch``, each receipt's share taken from what it still has OPEN.

	A shared batch splits a draw pro-rata by what each receipt row RECEIVED only while no
	receipt has drawn on it alone. A receipt-linked raw return takes metal from one receipt:
	with A 5 g + B 5 g in one batch and A's 5 g returned, what is left is B's, and splitting the
	next 5 g delivery 50/50 drew A to 7.5 g and left B 2.5 g "open" with no metal behind it
	(review F2, 29 Sep). Each share is therefore ``received - net drawn`` (net of reversals and
	returns, ``drawn_by_receipt``), floored at zero.

	A batch of one receipt is unchanged. When nothing is open -- an over-draw the report flags --
	the received proportions stand. A plain read on purpose: the stock layer already stops a
	physical over-draw, and a locking read here would add a lock-order path with returns.
	"""
	receipts = receipts_of_batch(company, customer, batch_no)
	if len(receipts) < 2:
		return receipts

	drawn = drawn_by_receipt([r.name for r in receipts])
	open_qty = {
		r.name: max(
			flt(r.cg_gross_qty_delta) - flt((drawn.get(r.name) or {}).get("gross")), 0.0
		)
		for r in receipts
	}
	total = sum(open_qty.values())
	if total <= 0:
		return receipts
	for receipt in receipts:
		receipt.share = open_qty[receipt.name] / total
	return receipts


def lineage_receipts(company, customer, batch_no):
	"""The receipts a CONVERTED batch was made from -- to name them, never to price them.

	A conversion mints a batch with no Receipt event of its own, so ``receipts_of_batch`` finds
	nothing behind it. What it holds is recorded only in its ``Batch Component`` rows, whose
	``source_batch`` is the batch each part came from; this follows the customer's parts back to
	the receipts of those batches, so a refusal can say where the metal came from.

	Only ``customer``'s gold is followed -- ``Customer Goods`` components of that customer whose
	item is gold, the rows ``_customer_share`` settles -- so the company's alloy and another
	customer's receipts are never named. Effective receipts only, each once, oldest first.

	``[]`` when the batch has effective Receipt events of its own (it is a receipt batch, not a
	converted one), when no component leads to a receipt, and on ANY failure: every caller is
	already refusing, and a lineage that cannot be read must leave the refusal it would have
	given anyway, never raise a different error. Reads only; a failure is logged as "Customer
	Gold: lineage lookup failed".
	"""
	try:
		from jewellery_erpnext.customer_subcontracting.customer_gold_components import (
			_recorded_components,
		)
		from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
			CUSTOMER_GOODS,
			_is_customer_gold_item,
		)

		if not (company and customer and batch_no) or receipts_of_batch(
			company, customer, batch_no
		):
			return []

		found = {}
		for component in _recorded_components(batch_no):
			source = component.get("source_batch")
			if (
				source
				and component.get("inventory_type") == CUSTOMER_GOODS
				and component.get("customer") == customer
				and _is_customer_gold_item(component.get("item_code"))
			):
				for receipt in receipts_of_batch(company, customer, source):
					found.setdefault(receipt.name, receipt)
		if not found:
			return []

		oldest_first = frappe.get_all(
			LEDGER_DOCTYPE,
			filters={"name": ["in", list(found)]},
			order_by="creation asc",
			pluck="name",
		)
		rank = {name: index for index, name in enumerate(oldest_first)}
		return sorted(found.values(), key=lambda r: rank.get(r.name, len(rank)))
	except Exception:
		# Never raises, but never silently either: a failure here quietly turns the named
		# refusal back into the generic one, and only this log says so. Deferred, because every
		# caller goes on to refuse, and the request's rollback would take an ordinary insert
		# with it; suppressed, because a log that cannot be written must not raise either.
		with suppress(Exception):
			frappe.log_error(
				title="Customer Gold: lineage lookup failed", defer_insert=True
			)
		return []


def booked_rate_of(event):
	"""Rate per receipt-item unit this receipt booked, or None when it was never valued."""
	qty = flt(event.cg_gross_qty_delta)
	if qty <= 0 or not event.cg_currency:
		return None
	return flt(event.cg_carrying_value_delta) / qty


def fine_of(item_code, qty):
	purity = get_purity_percentage(item_code) if item_code else None
	return flt(qty) * flt(purity) / 100.0 if purity else 0.0


# ------------------------------------------------------------------------------------------
# Writers
# ------------------------------------------------------------------------------------------


def write_allocation(
	event,
	receipt,
	disposition,
	basis,
	gross_qty,
	amount,
	currency,
	part_key=None,
	reversal_of=None,
):
	"""One (event, receipt) row. ``event`` is the ledger row (dict-like) being decomposed."""
	return _insert(
		{
			"allocation_key": allocation_key(
				event.name, receipt.name, part_key, disposition, reversal_of
			),
			"disposition": disposition,
			"basis": basis,
			"company": event.company,
			"customer": event.customer,
			"cg_event": event.name,
			"reference_doctype": event.reference_doctype,
			"reference_docname": event.reference_docname,
			"receipt_event": receipt.name,
			"receipt_voucher": receipt.reference_docname,
			"receipt_row": receipt.cg_source_row,
			"source_batch": receipt.batch_no,
			"gross_qty": flt(gross_qty, QTY_PRECISION),
			"fine_qty": flt(fine_of(receipt.item_code, gross_qty), QTY_PRECISION),
			"amount": flt(amount, AMOUNT_PRECISION) if currency else 0,
			"currency": currency,
			"reversal_of": reversal_of,
			"recorded_at": now_datetime(),
		}
	)


def allocate_event(event, parts, disposition, basis, currency, total_amount=None):
	"""Write ``parts`` -- [(receipt_event, gross_qty, amount)] -- against ``event``.

	When ``total_amount`` is given, the rounding residual lands on the largest part, so the rows
	under one event sum to that event's value to the paisa. Rounding each part on its own would
	leave the JE and its decomposition a few paise apart for ever.
	"""
	parts = [p for p in parts if flt(p[1]) or flt(p[2])]
	if not parts:
		return []
	amounts = [flt(p[2], AMOUNT_PRECISION) for p in parts]
	if currency and total_amount is not None:
		residual = flt(
			flt(total_amount, AMOUNT_PRECISION) - sum(amounts), AMOUNT_PRECISION
		)
		if residual:
			largest = max(range(len(parts)), key=lambda i: abs(flt(parts[i][2])))
			amounts[largest] = flt(amounts[largest] + residual, AMOUNT_PRECISION)
	return [
		write_allocation(
			event,
			receipt,
			disposition,
			basis,
			gross_qty,
			amount,
			currency,
			part_key=index,
		)
		for index, ((receipt, gross_qty, _), amount) in enumerate(
			zip(parts, amounts, strict=True)
		)
	]


def stamp_settlement_voucher(event_names, voucher):
	"""Point the allocations under ``event_names`` at the Journal Entry that released them."""
	if not voucher or not event_names or not is_allocation_schema_ready():
		return
	frappe.db.sql(
		"""
		UPDATE `tabCustomer Gold Allocation`
		SET settlement_voucher = %s
		WHERE cg_event IN %s AND IFNULL(settlement_voucher, '') = ''
		""",
		(voucher, tuple(event_names)),
	)


def reconcile_to_settlement(event_names, per_customer, precision=AMOUNT_PRECISION):
	"""Tie the allocations under a settlement JE to its per-customer line, to the paisa.

	Each event's allocations already sum to that event's value, but the JE rounds the customer's
	TOTAL once, so a document of several serials can leave the decomposition a paisa off the JE.
	The residual lands on the largest allocation; anything larger than a paisa per row is a real
	difference and is left for the report to show rather than hidden here.
	"""
	if not event_names or not is_allocation_schema_ready():
		return
	rows = frappe.get_all(
		ALLOCATION_DOCTYPE,
		filters={
			"cg_event": ["in", list(event_names)],
			"disposition": [
				"in",
				[DISPOSITION_FG_DELIVERY, DISPOSITION_DELIVERY_RETURN],
			],
		},
		fields=["name", "customer", "amount"],
	)
	by_customer = {}
	for row in rows:
		by_customer.setdefault(row.customer, []).append(row)
	for customer, amount in (per_customer or {}).items():
		mine = by_customer.get(customer) or []
		if not mine:
			continue
		residual = flt(
			flt(amount, precision) - sum(flt(r.amount) for r in mine), precision
		)
		if residual and abs(residual) <= 0.01 * len(mine):
			largest = max(mine, key=lambda r: abs(flt(r.amount)))
			frappe.db.set_value(
				ALLOCATION_DOCTYPE,
				largest.name,
				"amount",
				flt(flt(largest.amount) + residual, precision),
				update_modified=False,
			)


def reverse_allocations(doc):
	"""On cancel: one negated row per allocation ``doc`` wrote. Never an edit, never a delete,
	and never recomputed -- a cancellation restores exactly what was drawn."""
	if not is_allocation_schema_ready():
		return
	originals = frappe.get_all(
		ALLOCATION_DOCTYPE,
		filters={
			"reference_doctype": doc.doctype,
			"reference_docname": doc.name,
			"disposition": ["!=", DISPOSITION_REVERSAL],
		},
		fields=["*"],
	)
	already = set(
		frappe.get_all(
			ALLOCATION_DOCTYPE,
			filters={"reversal_of": ["in", [o.name for o in originals] or [""]]},
			pluck="reversal_of",
		)
	)
	for original in originals:
		if original.name in already:
			continue
		_insert(
			{
				"allocation_key": allocation_key(
					original.allocation_key, DISPOSITION_REVERSAL
				),
				"disposition": DISPOSITION_REVERSAL,
				"basis": original.basis,
				"company": original.company,
				"customer": original.customer,
				"cg_event": original.cg_event,
				"reference_doctype": original.reference_doctype,
				"reference_docname": original.reference_docname,
				"settlement_voucher": original.settlement_voucher,
				"receipt_event": original.receipt_event,
				"receipt_voucher": original.receipt_voucher,
				"receipt_row": original.receipt_row,
				"source_batch": original.source_batch,
				"gross_qty": -flt(original.gross_qty),
				"fine_qty": -flt(original.fine_qty),
				"amount": -flt(original.amount),
				"currency": original.currency,
				"reversal_of": original.name,
				"recorded_at": now_datetime(),
			}
		)


# ------------------------------------------------------------------------------------------
# Readers
# ------------------------------------------------------------------------------------------


def drawn_by_receipt(receipt_events, for_update=False):
	"""{receipt_event: {gross, fine, amount}} drawn so far, net of reversals and returns.

	``for_update`` makes it a LOCKING read. It must be one when the answer gates a submit: under
	REPEATABLE READ a plain SELECT after taking a lock still reads the transaction's first
	snapshot, so two concurrent returns would both see the other's draw as absent.
	"""
	if not receipt_events or not is_allocation_schema_ready():
		return {}
	rows = frappe.db.sql(
		f"""
		SELECT receipt_event, gross_qty, fine_qty, amount
		FROM `tabCustomer Gold Allocation`
		WHERE receipt_event IN %s
		{"FOR UPDATE" if for_update else ""}
		""",
		(tuple(receipt_events),),
		as_dict=True,
	)
	result = {}
	for row in rows:
		bucket = result.setdefault(
			row.receipt_event, frappe._dict(gross=0.0, fine=0.0, amount=0.0)
		)
		bucket.gross += flt(row.gross_qty)
		bucket.fine += flt(row.fine_qty)
		bucket.amount += flt(row.amount)
	return result


def allocated_events():
	"""Names of every ledger event that has allocation rows -- the rest are legacy."""
	if not is_allocation_schema_ready():
		return set()
	return set(frappe.get_all(ALLOCATION_DOCTYPE, distinct=True, pluck="cg_event"))
