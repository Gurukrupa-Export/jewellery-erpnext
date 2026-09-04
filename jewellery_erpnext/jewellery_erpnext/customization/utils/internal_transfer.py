"""Resolving the Delivery Note / Sales Invoice Item that backs an internal-transfer
Purchase Order Item.

ERPNext's `validate_inter_company_reference` (erpnext/controllers/accounts_controller.py)
requires every internal-transfer Purchase Receipt Item to carry a `delivery_note_item`
value (and Purchase Invoice Item a `sales_invoice_item` value), plus the parent doc to
carry one of `inter_company_reference` (Purchase Receipt, Link to Delivery Note) or
`inter_company_invoice_reference` (Purchase Invoice, Link to Sales Invoice). Core's
Purchase Order -> Purchase Receipt / Purchase Invoice mappers
(erpnext/buying/doctype/purchase_order/purchase_order.py, `make_purchase_receipt` /
`get_mapped_purchase_invoice`) never set any of these -- they were written for the
ordinary purchase flow, where there is no linked Delivery Note / Sales Invoice to
reference. The only place ERPNext ever fills these in is the Delivery Note's own
`make_inter_company_purchase_receipt` and the Sales Invoice's own
`make_inter_company_purchase_invoice`, both of which start from the sell-side document,
not the Purchase Order.

For an internal transfer, a Purchase Order Item's (sales_order, sales_order_item) and a
Delivery Note Item's (against_sales_order, so_detail) / Sales Invoice Item's
(sales_order, so_detail) are populated from the same source Sales Order Item by
ERPNext's inter-company mappers, so they are directly comparable -- that pairing is the
join key used here. Purchase Invoice Item, unlike Purchase Receipt Item, carries no
sales_order/sales_order_item of its own (core's PO -> Purchase Invoice mapper only maps
`po_detail`, the source Purchase Order Item's row name), so resolving a Sales Invoice
Item link needs one extra hop through the original Purchase Order Item first.
"""

import frappe
from frappe import _


def _resolve_unconsumed(candidates_by_pair, consumed_names):
	"""Shared narrowing step: for each (sales_order, sales_order_item) pair's candidate
	rows, drop any already linked from a submitted receiving document, and only report a
	pair as resolved when exactly one candidate remains."""
	resolved = {}
	for pair, matches in candidates_by_pair.items():
		unconsumed = [row for row in matches if row.name not in consumed_names]
		if len(unconsumed) == 1:
			resolved[pair] = unconsumed[0]
	return resolved


def get_delivery_note_item_links(po_item_rows):
	"""Batched resolver: for each Purchase Order Item row (needs `.name`,
	`.sales_order`, `.sales_order_item`), find the submitted Delivery Note Item that
	represents the same delivered stock, preferring one not already linked from an
	existing submitted Purchase Receipt Item.

	Returns {po_item_row.name: {"delivery_note_item": <DNI name>, "delivery_note": <DN name>}}.
	Rows that don't resolve unambiguously (no match, or more than one candidate still
	tied after excluding already-consumed ones) are simply absent from the result --
	callers decide whether that is an error.
	"""
	rows = [
		row
		for row in po_item_rows
		if row.get("sales_order") and row.get("sales_order_item")
	]
	if not rows:
		return {}

	sales_orders = list({row.sales_order for row in rows})
	sales_order_items = list({row.sales_order_item for row in rows})

	DN = frappe.qb.DocType("Delivery Note")
	DNI = frappe.qb.DocType("Delivery Note Item")

	candidates = (
		frappe.qb.from_(DNI)
		.join(DN)
		.on(DN.name == DNI.parent)
		.select(DNI.name, DNI.parent, DNI.against_sales_order, DNI.so_detail)
		.where(DN.docstatus == 1)
		.where(DNI.against_sales_order.isin(sales_orders))
		.where(DNI.so_detail.isin(sales_order_items))
	).run(as_dict=True)

	if not candidates:
		return {}

	by_pair = {}
	for dni in candidates:
		by_pair.setdefault((dni.against_sales_order, dni.so_detail), []).append(dni)

	candidate_names = [dni.name for dni in candidates]
	PR = frappe.qb.DocType("Purchase Receipt")
	PRI = frappe.qb.DocType("Purchase Receipt Item")
	consumed = (
		frappe.qb.from_(PRI)
		.join(PR)
		.on(PR.name == PRI.parent)
		.select(PRI.delivery_note_item)
		.where(PR.docstatus == 1)
		.where(PRI.delivery_note_item.isin(candidate_names))
	).run(as_dict=True)
	consumed_names = {row.delivery_note_item for row in consumed}

	resolved_by_pair = _resolve_unconsumed(by_pair, consumed_names)

	links = {}
	for row in rows:
		dni = resolved_by_pair.get((row.sales_order, row.sales_order_item))
		if dni:
			links[row.name] = {
				"delivery_note_item": dni.name,
				"delivery_note": dni.parent,
			}

	return links


def get_sales_invoice_item_links(pi_item_rows):
	"""Batched resolver: for each Purchase Invoice Item row (needs `.name`,
	`.po_detail` -- the source Purchase Order Item's row name), find the submitted
	Sales Invoice Item that represents the same sale, preferring one not already linked
	from an existing submitted Purchase Invoice Item.

	Unlike Purchase Order Item, Purchase Invoice Item carries no sales_order /
	sales_order_item of its own (core's PO -> Purchase Invoice mapper doesn't map
	them), so this first looks up the originating Purchase Order Item's
	sales_order/sales_order_item via `po_detail`, then matches those against Sales
	Invoice Item the same way `get_delivery_note_item_links` matches Delivery Note Item.

	Returns {pi_item_row.name: {"sales_invoice_item": <SII name>, "sales_invoice": <SI name>}}.
	Rows that don't resolve unambiguously are simply absent from the result -- callers
	decide whether that is an error.
	"""
	rows = [row for row in pi_item_rows if row.get("po_detail")]
	if not rows:
		return {}

	po_detail_names = list({row.po_detail for row in rows})
	po_items = frappe.get_all(
		"Purchase Order Item",
		filters={"name": ["in", po_detail_names]},
		fields=["name", "sales_order", "sales_order_item"],
	)
	po_item_by_name = {po_item.name: po_item for po_item in po_items}

	rows_with_so = [
		row
		for row in rows
		if po_item_by_name.get(row.po_detail)
		and po_item_by_name[row.po_detail].sales_order
		and po_item_by_name[row.po_detail].sales_order_item
	]
	if not rows_with_so:
		return {}

	sales_orders = list(
		{po_item_by_name[row.po_detail].sales_order for row in rows_with_so}
	)
	sales_order_items = list(
		{po_item_by_name[row.po_detail].sales_order_item for row in rows_with_so}
	)

	SI = frappe.qb.DocType("Sales Invoice")
	SII = frappe.qb.DocType("Sales Invoice Item")

	candidates = (
		frappe.qb.from_(SII)
		.join(SI)
		.on(SI.name == SII.parent)
		.select(SII.name, SII.parent, SII.sales_order, SII.so_detail)
		.where(SI.docstatus == 1)
		.where(SII.sales_order.isin(sales_orders))
		.where(SII.so_detail.isin(sales_order_items))
	).run(as_dict=True)

	if not candidates:
		return {}

	by_pair = {}
	for sii in candidates:
		by_pair.setdefault((sii.sales_order, sii.so_detail), []).append(sii)

	candidate_names = [sii.name for sii in candidates]
	PI = frappe.qb.DocType("Purchase Invoice")
	PII = frappe.qb.DocType("Purchase Invoice Item")
	consumed = (
		frappe.qb.from_(PII)
		.join(PI)
		.on(PI.name == PII.parent)
		.select(PII.sales_invoice_item)
		.where(PI.docstatus == 1)
		.where(PII.sales_invoice_item.isin(candidate_names))
	).run(as_dict=True)
	consumed_names = {row.sales_invoice_item for row in consumed}

	resolved_by_pair = _resolve_unconsumed(by_pair, consumed_names)

	links = {}
	for row in rows_with_so:
		po_item = po_item_by_name[row.po_detail]
		sii = resolved_by_pair.get((po_item.sales_order, po_item.sales_order_item))
		if sii:
			links[row.name] = {
				"sales_invoice_item": sii.name,
				"sales_invoice": sii.parent,
			}

	return links


def fill_purchase_receipt_references(doc):
	"""Mutates an internal-transfer Purchase Receipt draft in place: sets
	`delivery_note_item` on each item row and `inter_company_reference` on the header,
	so it passes `validate_inter_company_reference`. Shared by every entry point that
	can produce such a draft -- currently the Purchase Order's "Create -> Purchase
	Receipt" override in doc_events/purchase_order.py."""
	links = get_delivery_note_item_links(doc.items)

	for row in doc.items:
		link = links.get(row.name)
		if link:
			row.delivery_note_item = link["delivery_note_item"]
			continue

		if not (row.get("sales_order") and row.get("sales_order_item")):
			frappe.throw(
				_(
					"Row {0}: This item isn't linked to a Sales Order, so its Delivery"
					" Note can't be auto-resolved. Create this Purchase Receipt from the"
					" Purchase Order's source Sales Order/Delivery Note directly instead."
				).format(row.idx)
			)

		frappe.throw(
			_(
				"Row {0}: More than one Delivery Note matches this item. Create this"
				" Purchase Receipt from the specific Delivery Note instead."
			).format(row.idx)
		)

	delivery_notes = [
		links[row.name]["delivery_note"] for row in doc.items if row.name in links
	]
	if delivery_notes:
		primary_dn = max(set(delivery_notes), key=delivery_notes.count)
		doc.inter_company_reference = primary_dn
		if len(set(delivery_notes)) > 1:
			frappe.msgprint(
				_(
					"Items on this receipt come from more than one Delivery Note. The"
					" header Inter Company Reference only reflects {0}; the per-row"
					" Delivery Note links are still correct for each item."
				).format(primary_dn)
			)


def fill_purchase_invoice_references(doc):
	"""Mutates an internal-transfer Purchase Invoice draft in place: sets
	`sales_invoice_item` on each item row and `inter_company_invoice_reference` on the
	header, so it passes `validate_inter_company_reference`. Shared by every entry
	point that can produce such a draft: the Purchase Order's "Create -> Purchase
	Invoice" override, and the Purchase Receipt's "Create -> Purchase Invoice" override
	(doc_events/purchase_order.py and doc_events/purchase_receipt.py respectively) --
	both land on a `po_detail` per row pointing at the same originating Purchase Order
	Item, which is all this needs."""
	links = get_sales_invoice_item_links(doc.items)

	for row in doc.items:
		link = links.get(row.name)
		if link:
			row.sales_invoice_item = link["sales_invoice_item"]
			continue

		frappe.throw(
			_(
				"Row {0}: This item's matching Sales Invoice couldn't be auto-resolved"
				" (either it isn't linked to a Sales Order, or more than one Sales"
				" Invoice matches it). Create this Purchase Invoice from the specific"
				" Sales Invoice instead."
			).format(row.idx)
		)

	sales_invoices = [
		links[row.name]["sales_invoice"] for row in doc.items if row.name in links
	]
	if sales_invoices:
		primary_si = max(set(sales_invoices), key=sales_invoices.count)
		doc.inter_company_invoice_reference = primary_si
		if len(set(sales_invoices)) > 1:
			frappe.msgprint(
				_(
					"Items on this invoice come from more than one Sales Invoice. The"
					" header Inter Company Invoice Reference only reflects {0}; the"
					" per-row Sales Invoice links are still correct for each item."
				).format(primary_si)
			)
