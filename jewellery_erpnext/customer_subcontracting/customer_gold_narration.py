# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Narration that lets accounts tally a Customer Gold posting against its receipts.

WHY THIS EXISTS
---------------
No Customer Gold posting put any text into the general ledger. Receipts and returns left
``Stock Entry.remarks`` blank, so erpnext wrote "Accounting Entry for Stock" on every GL line
(``StockController.get_gl_entries``). The settlement Journal Entry wrote its note into the
header ``user_remark``, which erpnext 16 never copies to GL: ``JournalEntry.build_gl_map``
takes each line's own ``user_remark`` plus the header ``remark``, and ``create_remarks`` keeps a
typed ``remark`` only when ``custom_remark`` is set. On kggk-uat (17 Nov 2026) all 73 Stock
Entry lines and all 5 JVs on Customer Goods Receive - KGJPL named no customer, no receipt and
no delivery.

The breakup was already recorded. ``Customer Gold Allocation`` names, for every disposition, the
customer, the document, the settlement JV, the receipt row, the batch, grams and amount. This
module turns it into text, and the same text serves new postings and the backfill patch:

* ``settlement_lines``: one liability line per receipt row behind a settlement JV, each with its
  own ``user_remark``, so the GL shows one line per receipt;
* ``settlement_summary``: the JV's header ``remark``;
* ``receipt_narration`` / ``return_narration``: the Stock Entry ``remarks``, which erpnext copies
  to every GL line of the entry.

Text only. Nothing here changes an amount, an account or a document's status.
"""

from collections import OrderedDict

import frappe
from frappe.utils import flt, fmt_money, formatdate

from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
	ALLOCATION_DOCTYPE,
	DISPOSITION_DELIVERY_RETURN,
	DISPOSITION_FG_DELIVERY,
	is_allocation_schema_ready,
)
from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
	EVENT_DELIVERY,
	EVENT_DELIVERY_RETURN,
	LEDGER_DOCTYPE,
)

#: First words of this module's block in a Stock Entry remark. The block is rewritten from the
#: first line that starts with one of them, so whatever the user typed above it is kept.
RECEIPT_HEADING = "Customer Gold received"
RETURN_HEADING = "Customer Gold returned"
BLOCK_HEADINGS = (RECEIPT_HEADING, RETURN_HEADING)

RELEASED_HEADING = "Customer Gold released"
RESTORED_HEADING = "Customer Gold restored"
SETTLEMENT_HEADING = "Customer Gold settlement"
UNTRACED = "Not traced to a receipt"

SEP = " | "


# ------------------------------------------------------------------------------------------
# Small formatters
# ------------------------------------------------------------------------------------------


def customer_label(customer):
	"""``ID - Name``, or the ID alone when the name adds nothing or the customer is gone."""
	if not customer:
		return "-"
	name = frappe.get_cached_value("Customer", customer, "customer_name")
	return f"{customer} - {name}" if name and name != customer else customer


def _qty(value, uom=None):
	return f"{flt(value, 3):.3f} {uom or 'g'}"


def _money(value, direction=None):
	"""Amount as erpnext formats it. With ``direction`` (the JE's settled total), the sign is
	shown only when ``value`` runs against it -- a difference the receipts over-cover."""
	value = flt(value, 3)
	if direction is not None and value * flt(direction) < 0:
		return "-" + fmt_money(abs(value), precision=3)
	return fmt_money(abs(value), precision=3)


def _receipt_rows(names):
	"""{Stock Entry Detail name: row} for the receipt rows named, for their idx and item."""
	names = [n for n in set(names or ()) if n]
	if not names:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Stock Entry Detail",
			filters={"name": ["in", names]},
			fields=["name", "parent", "idx", "item_code", "stock_uom"],
		)
	}


def _receipt_ref(voucher, row):
	if not voucher:
		return UNTRACED
	if not row:
		return f"Receipt: {voucher}"
	return f"Receipt: {voucher} row {row.idx} ({row.item_code})"


def _unique(values):
	return list(OrderedDict.fromkeys(v for v in values if v))


def document_context(doc):
	"""``Delivery Note: X | Sales Order: Y | Sales Invoice: Z``: what a delivery belongs to.

	A Delivery Note's invoice usually comes after the settlement, so it is named only when the
	note was made from one; a Sales Invoice that delivers names its own notes and orders.
	"""
	rows = doc.get("items") or []
	parts = [f"{doc.doctype}: {doc.name}"]
	orders = _unique(r.get("against_sales_order") or r.get("sales_order") for r in rows)
	invoices = _unique(r.get("against_sales_invoice") for r in rows)
	notes = _unique(r.get("delivery_note") for r in rows)
	if orders:
		parts.append(f"Sales Order: {', '.join(orders)}")
	if invoices:
		parts.append(f"Sales Invoice: {', '.join(invoices)}")
	if notes:
		parts.append(f"Delivery Note: {', '.join(notes)}")
	return SEP.join(parts)


# ------------------------------------------------------------------------------------------
# Settlement Journal Entry
# ------------------------------------------------------------------------------------------


def _settlement_allocations(event_names):
	if not event_names or not is_allocation_schema_ready():
		return []
	return frappe.get_all(
		ALLOCATION_DOCTYPE,
		filters={
			"cg_event": ["in", list(event_names)],
			"disposition": [
				"in",
				[DISPOSITION_FG_DELIVERY, DISPOSITION_DELIVERY_RETURN],
			],
		},
		fields=[
			"customer",
			"receipt_voucher",
			"receipt_row",
			"source_batch",
			"gross_qty",
			"fine_qty",
			"amount",
		],
		order_by="creation asc, name asc",
	)


def settlement_lines(doc, event_names, per_customer, precision):
	"""The liability lines of the settlement JE for ``doc``: one per receipt row drawn on.

	``[frappe._dict(customer, amount, remark)]``. Amounts are rounded to ``precision`` and, per
	customer, add up to that customer's settled amount in ``per_customer`` exactly, so the JE's
	totals are what they always were. The allocations of ``event_names`` are summed per
	(customer, receipt, receipt row, batch). Anything they do not cover becomes one more line
	for that customer, marked as not traced to a receipt: an event with no recorded provenance,
	or a residual ``reconcile_to_settlement`` would not absorb. A customer whose amount rounds to
	0 gets no line, as before.
	"""
	groups = OrderedDict()
	for a in _settlement_allocations(event_names):
		key = (a.customer, a.receipt_voucher, a.receipt_row, a.source_batch)
		group = groups.get(key)
		if group is None:
			group = groups[key] = frappe._dict(
				customer=a.customer,
				receipt_voucher=a.receipt_voucher,
				receipt_row=a.receipt_row,
				source_batch=a.source_batch,
				gross=0.0,
				fine=0.0,
				amount=0.0,
			)
		group.gross += flt(a.gross_qty)
		group.fine += flt(a.fine_qty)
		group.amount += flt(a.amount)

	rows = _receipt_rows(g.receipt_row for g in groups.values())
	context = document_context(doc)
	# A sales return settles a negative amount (custody comes back); the sign is the truth.
	restoring = sum(flt(v) for v in (per_customer or {}).values()) < 0
	heading = RESTORED_HEADING if restoring else RELEASED_HEADING

	lines = []
	for customer, total in (per_customer or {}).items():
		total = flt(total, precision)
		if not total:
			continue
		label = customer_label(customer)
		traced = 0.0
		for group in groups.values():
			if group.customer != customer:
				continue
			amount = flt(group.amount, precision)
			if not amount:
				continue
			traced = flt(traced + amount, precision)
			row = rows.get(group.receipt_row)
			lines.append(
				frappe._dict(
					customer=customer,
					amount=amount,
					receipt_voucher=group.receipt_voucher,
					remark=SEP.join(
						[
							heading,
							f"Customer: {label}",
							context,
							_receipt_ref(group.receipt_voucher, row),
							f"Batch: {group.source_batch or '-'}",
							f"Qty: {_qty(abs(group.gross), row.stock_uom if row else None)}",
							f"Fine: {_qty(abs(group.fine))}",
							f"Amount: {_money(amount, total)}",
						]
					),
				)
			)
		rest = flt(total - traced, precision)
		if rest:
			lines.append(
				frappe._dict(
					customer=customer,
					amount=rest,
					receipt_voucher=None,
					remark=SEP.join(
						[
							heading,
							f"Customer: {label}",
							context,
							UNTRACED,
							f"Amount: {_money(rest, total)}",
						]
					),
				)
			)
	return lines


def merges_similar_account_heads():
	"""Accounts Settings > Merge Similar Account Heads, read exactly as ``JournalEntry`` reads it."""
	return bool(
		frappe.get_single_value("Accounts Settings", "merge_similar_account_heads")
	)


def lines_as_posted(lines, by_customer, precision):
	"""``lines`` as the JE must carry them when erpnext merges similar account heads.

	With "Merge Similar Account Heads" on (it is on kggk-uat and kggk-prod),
	``JournalEntry.make_gl_entries`` folds lines that share account, cost center, party and
	``voucher_detail_no`` into ONE GL row, summing the amounts and keeping only the FIRST line's
	remark (``general_ledger.merge_similar_entries``; remarks are not in the merge key). Separate
	receipt lines would then show the whole amount against one receipt's text. So they are folded
	here first, one line per merge key: per customer when the liability line carries a party
	(``by_customer``), otherwise one line for all of them, since lines without a party merge
	across customers too. Each folded line's remark keeps every receipt line, one per text line;
	its amount is their sum, so the JE's totals do not change.
	"""
	folded = OrderedDict()
	for line in lines:
		key = line.customer if by_customer else None
		entry = folded.get(key)
		if entry is None:
			entry = folded[key] = frappe._dict(
				customer=line.customer, amount=0.0, receipt_voucher=None, remarks=[]
			)
		entry.amount = flt(entry.amount + flt(line.amount), precision)
		entry.remarks.append(line.remark)
	return [
		frappe._dict(
			customer=entry.customer,
			amount=entry.amount,
			receipt_voucher=entry.receipt_voucher,
			remark="\n".join(entry.remarks),
		)
		for entry in folded.values()
		if entry.amount
	]


def settlement_summary(doc, lines):
	"""The JE's header remark: the document, then each customer's total and receipts."""
	per_customer = OrderedDict()
	for line in lines:
		bucket = per_customer.setdefault(
			line.customer, frappe._dict(amount=0.0, receipts=[])
		)
		bucket.amount += flt(line.amount)
		if line.receipt_voucher and line.receipt_voucher not in bucket.receipts:
			bucket.receipts.append(line.receipt_voucher)

	parts = [SETTLEMENT_HEADING, document_context(doc)]
	for customer, bucket in per_customer.items():
		receipts = ", ".join(bucket.receipts) or UNTRACED.lower()
		parts.append(
			f"{customer_label(customer)}: {_money(bucket.amount)} from {receipts}"
		)
	return SEP.join(parts)


# ------------------------------------------------------------------------------------------
# Stock Entry: receipt and return
# ------------------------------------------------------------------------------------------


def _row_qty(row):
	return flt(row.get("transfer_qty")) or flt(row.get("qty"))


def _amount_parts(row):
	if not flt(row.get("basic_amount")):
		return []
	return [
		f"Rate: {_money(row.get('basic_rate'))}",
		f"Amount: {_money(row.get('basic_amount'))}",
	]


def receipt_narration(doc):
	"""The receipt's GL text: customer and gold rate, then each row's item, qty, batch, value."""
	header = [RECEIPT_HEADING, f"Customer: {customer_label(doc.get('_customer'))}"]
	if flt(doc.get("custom_gold_rate_per_gram")):
		rate = f"Gold rate: {_money(doc.custom_gold_rate_per_gram)} per g"
		if doc.get("custom_gold_rate_date"):
			rate += f" ({formatdate(doc.custom_gold_rate_date)})"
		header.append(rate)

	lines = [SEP.join(header)]
	for row in doc.get("items") or []:
		lines.append(
			SEP.join(
				[
					f"Row {row.idx}: {row.item_code}",
					f"Qty: {_qty(_row_qty(row), row.get('stock_uom'))}",
					f"Batch: {row.get('batch_no') or '-'}",
					*_amount_parts(row),
				]
			)
		)
	return "\n".join(lines)


def return_narration(doc):
	"""The return's GL text: customer, then each row's item, qty, batch and the receipt row
	it gives back (``against_stock_entry`` / ``ste_detail``, stamped by
	``customer_gold_return.prepare_return_entry``)."""
	items = doc.get("items") or []
	customers = _unique([r.get("customer") for r in items] + [doc.get("_customer")])
	header = [
		RETURN_HEADING,
		f"Customer: {', '.join(customer_label(c) for c in customers) or '-'}",
	]
	if doc.get("custom_cg_issue_against"):
		header.append(f"Receipt: {doc.custom_cg_issue_against}")

	receipt_rows = _receipt_rows(r.get("ste_detail") for r in items)
	lines = [SEP.join(header)]
	for row in items:
		lines.append(
			SEP.join(
				[
					f"Row {row.idx}: {row.item_code}",
					f"Qty: {_qty(_row_qty(row), row.get('stock_uom'))}",
					f"Batch: {row.get('batch_no') or '-'}",
					_receipt_ref(
						row.get("against_stock_entry"),
						receipt_rows.get(row.get("ste_detail")),
					).replace("Receipt: ", "Against receipt: ", 1),
					*_amount_parts(row),
				]
			)
		)
	return "\n".join(lines)


def merge_block(existing, block):
	"""``existing`` with this module's block replaced by ``block``; the user's text above it stays.

	An amended or re-validated entry therefore carries one block, never two.
	"""
	kept = []
	for line in (existing or "").splitlines():
		if line.startswith(BLOCK_HEADINGS):
			break
		kept.append(line)
	head = "\n".join(kept).rstrip()
	return f"{head}\n{block}" if head else block


def stock_entry_narration(doc):
	"""The block for a Customer Gold receipt or return, or None for any other Stock Entry."""
	from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
		_receipt_settings,
	)
	from jewellery_erpnext.customer_subcontracting.customer_gold_return import _applies

	if _receipt_settings(doc):
		return receipt_narration(doc)
	if _applies(doc):
		return return_narration(doc)
	return None


def apply_stock_entry_narration(doc, method=None):
	"""``before_submit`` for Stock Entry, last: erpnext copies ``remarks`` to every GL line at
	``on_submit``. Last, so batches are minted, the rate is frozen and the return rows are
	linked to their receipt rows. No-op for every other Stock Entry and while the flow is off."""
	block = stock_entry_narration(doc)
	if block:
		doc.remarks = merge_block(doc.get("remarks"), block)


# ------------------------------------------------------------------------------------------
# Backfill: postings made before this narration existed
# ------------------------------------------------------------------------------------------


def _vouchers_with_events(stock_entry_type):
	return frappe.db.sql_list(
		"""
		SELECT DISTINCT se.name
		FROM `tabStock Entry` se
		JOIN `tabCustomer Gold Ledger Entry` le
			ON le.reference_doctype = 'Stock Entry' AND le.reference_docname = se.name
		WHERE se.docstatus = 1 AND se.stock_entry_type = %s
		ORDER BY se.creation
		""",
		(stock_entry_type,),
	)


def _set_gl_remarks(voucher_type, voucher_no, remarks):
	frappe.db.sql(
		"""
		UPDATE `tabGL Entry` SET remarks = %s
		WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
		""",
		(remarks, voucher_type, voucher_no),
	)


def backfill_stock_entries(settings):
	"""Write the receipt / return block onto submitted Customer Gold Stock Entries and their GL.

	Only entries the custody ledger recorded, so a legacy entry of the same type that predates
	the flow is left alone. Returns the names changed.
	"""
	changed = []
	for stock_entry_type, build in (
		(settings.get("customer_goods_stock_entry_type"), receipt_narration),
		(settings.get("customer_gold_return_stock_entry_type"), return_narration),
	):
		if not stock_entry_type:
			continue
		for name in _vouchers_with_events(stock_entry_type):
			doc = frappe.get_doc("Stock Entry", name)
			remarks = merge_block(doc.get("remarks"), build(doc))
			if remarks == (doc.get("remarks") or ""):
				continue
			frappe.db.set_value(
				"Stock Entry", name, "remarks", remarks, update_modified=False
			)
			_set_gl_remarks("Stock Entry", name, remarks)
			changed.append(name)
	return changed


def settlement_remark_for(je_name, precision=None):
	"""The full narration for an existing settlement JE, rebuilt from its own events.

	Uses the same events, sign and per-customer grouping ``settle_customer_gold_liability`` used
	when it posted the JE. Its lines cannot be split once submitted, so the receipt lines go into
	the one remark under the summary. None when the JE settled no recorded event.
	"""
	events = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={
			"cg_settlement_voucher": je_name,
			"cg_event_kind": ["in", [EVENT_DELIVERY, EVENT_DELIVERY_RETURN]],
		},
		fields=[
			"name",
			"customer",
			"reference_doctype",
			"reference_docname",
			"cg_carrying_value_delta",
		],
		order_by="name",
	)
	if not events:
		return None
	if precision is None:
		# The precision the live settlement used (``settle_customer_gold_liability``).
		precision = (
			frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
			or 2
		)
	per_customer = OrderedDict()
	for event in events:
		per_customer[event.customer] = flt(per_customer.get(event.customer)) - flt(
			event.cg_carrying_value_delta
		)
	doc = frappe.get_doc(events[0].reference_doctype, events[0].reference_docname)
	lines = settlement_lines(doc, [e.name for e in events], per_customer, precision)
	return "\n".join([settlement_summary(doc, lines), *(line.remark for line in lines)])


def backfill_settlements():
	"""Write the narration onto submitted settlement JEs and their GL lines. Returns the names.

	``remark`` with ``custom_remark``, as a new JE gets it, and the GL lines' remarks rebuilt the
	way erpnext builds them (line ``user_remark``, then ``remark``).
	"""
	changed = []
	names = frappe.db.sql_list(
		"""
		SELECT DISTINCT le.cg_settlement_voucher
		FROM `tabCustomer Gold Ledger Entry` le
		JOIN `tabJournal Entry` je ON je.name = le.cg_settlement_voucher
		WHERE je.docstatus = 1
		ORDER BY le.cg_settlement_voucher
		"""
	)
	for name in names:
		remark = settlement_remark_for(name)
		if not remark:
			continue
		current = frappe.db.get_value(
			"Journal Entry", name, ["remark", "custom_remark"], as_dict=True
		)
		if current and current.remark == remark and current.custom_remark:
			continue
		frappe.db.set_value(
			"Journal Entry",
			name,
			{"remark": remark, "custom_remark": 1},
			update_modified=False,
		)
		line_remarks = set(
			frappe.get_all(
				"Journal Entry Account", filters={"parent": name}, pluck="user_remark"
			)
		)
		if line_remarks - {None, ""}:
			# Not one this module posted: its lines carry their own remarks, which a GL row
			# cannot be matched back to. The JE's remark is still corrected; its GL is left.
			frappe.log_error(
				title="Customer Gold: settlement GL remarks not backfilled",
				message=f"Journal Entry {name} has line remarks; GL Entry remarks left as they were.",
			)
		else:
			_set_gl_remarks("Journal Entry", name, remark)
		changed.append(name)
	return changed
