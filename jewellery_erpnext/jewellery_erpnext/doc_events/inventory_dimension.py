# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Stamp the inventory-dimension lane onto SALES rows (Sales Invoice / Delivery Note).

``doc_events/stock_entry.py`` closed this hole for Stock Entry: its blanket default fills
``inventory_type`` on every row and ``set_target_inventory_dimensions`` mirrors it onto
``to_inventory_type``, so both legs of a Stock Entry carry a lane. Nothing ever did the
same on the sales side. ``Sales Invoice Item.inventory_type`` and
``Delivery Note Item.inventory_type`` exist and are registered inventory dimensions, but
no code in this bench has ever written them.

ERPNext tags a sales SLE straight off that field
(``controllers/stock_controller.py:1395-1404``): for BOTH a normal outward sale and an
inward sales return it takes the ``row.get(dimension.source_fieldname)`` branch. A blank
field therefore writes a NULL dimension onto the Stock Ledger Entry.

That was invisible until ``StockLedgerEntry.validate_serial_no_inventory_dimension``
(erpnext#58394, backported as #58419, arrived with erpnext 16.34.1) started comparing an
outward serialized SLE against that serial's LAST INWARD SLE. A piece sold and then taken
back on a credit note re-enters stock with a NULL lane, and the next Stock Entry that
moves it carries "Regular Stock" from the blanket default, so the serial is rejected::

    Serial No X is not available in the selected inventory dimensions:
    Inventory Type: expected "Not Set", got "Regular Stock"

Both directions are fixed here, not just the return: a normal sale's OUTWARD leg reads the
same blank field, so it fails in mirror image (``expected "Regular Stock", got "Not Set"``)
whenever the piece was received correctly tagged.

The lane comes from the BATCH, never from a hardcoded default. That is the same source of
truth ``CustomStockEntry.update_batches`` uses to backfill ``inventory_type`` /
``customer`` on Stock Entry rows (customization/stock_entry/stock_entry.py:217-221), so a
row stamped here and the Stock Entry row that later moves the same piece resolve to the
SAME value -- which is exactly what the validator compares. Writing "Regular Stock"
without consulting the batch would book a customer's returned gold as company stock, the
precise failure ``customization/utils/row_ownership`` exists to prevent.

Precedence is ``resolve_batch_ownership``'s rule 1 -- the batch wins, the row's own value
is only a fallback -- because the batch is the physical truth and a stale row value must
not override it. The per-row query that helper issues is replaced by two bulk reads, since
a jewellery invoice routinely carries hundreds of rows; the shared normalisation rules are
then applied per row, so a customer type with no resolvable customer still downgrades
rather than minting an incoherent pair.

MUST be its own hook entry rather than a call inside ``doc_events/sales_invoice.validate``:
that function returns early for ``is_return``, which would skip every credit note -- i.e.
exactly the documents this exists to fix. hooks.py already carries the same warning on
``serial_reference.set_serial_reference`` for the same reason.
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)


def _child_doctype(doc, rows):
	"""Item-table doctype, read off a row so this never hardcodes a doctype name."""
	return (rows[0].get("doctype") if rows else None) or f"{doc.doctype} Item"


def _bundle_batches(bundles):
	"""``{bundle: batch_no}`` for bundles that resolve to exactly ONE batch.

	A v16 sales row usually carries its batch in a Serial and Batch Bundle rather than in
	``batch_no``, so the bundle has to be consulted or every serialized row would fall
	through to the default. A bundle spanning several batches has no single lane, so it is
	deliberately left unresolved and the row falls back to its own value rather than
	adopting an arbitrary one.
	"""
	bundles = sorted({b for b in bundles if b})
	if not bundles:
		return {}

	entries = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": ("in", bundles), "batch_no": ("is", "set")},
		fields=["parent", "batch_no"],
		limit_page_length=0,
	)

	by_bundle = {}
	for entry in entries:
		by_bundle.setdefault(entry.parent, set()).add(entry.batch_no)

	return {
		bundle: next(iter(batches))
		for bundle, batches in by_bundle.items()
		if len(batches) == 1
	}


def _batch_ownership(batch_nos):
	"""``{batch_no: (custom_inventory_type, custom_customer)}`` in ONE query."""
	batch_nos = sorted({b for b in batch_nos if b})
	if not batch_nos:
		return {}

	rows = frappe.get_all(
		"Batch",
		filters={"name": ("in", batch_nos)},
		fields=["name", "custom_inventory_type", "custom_customer"],
		limit_page_length=0,
	)
	return {r.name: (r.custom_inventory_type, r.custom_customer) for r in rows}


def set_sales_inventory_type(self, method=None):
	"""Fill ``inventory_type`` / ``customer`` on every sales row from its source batch."""
	if self.docstatus > 1:
		return

	rows = self.get("items") or []
	if not rows:
		return

	meta = frappe.get_meta(_child_doctype(self, rows))
	if not meta.has_field("inventory_type"):
		return

	has_customer = meta.has_field("customer")
	# ``to_<field>`` is read only for the INWARD leg of an internal-customer transfer
	# (stock_controller.py:1406-1414). It is inert on every other sales document, so the
	# mirror is gated rather than unconditional -- an ordinary invoice must not grow a
	# target lane that ERPNext would never read.
	mirror = bool(self.get("is_internal_customer")) and meta.has_field(
		"to_inventory_type"
	)
	mirror_customer = mirror and meta.has_field("to_customer")

	bundle_batches = _bundle_batches(
		row.get("serial_and_batch_bundle") for row in rows if not row.get("batch_no")
	)
	ownership = _batch_ownership(
		[row.get("batch_no") for row in rows] + list(bundle_batches.values())
	)

	for row in rows:
		batch_no = row.get("batch_no") or bundle_batches.get(
			row.get("serial_and_batch_bundle")
		)
		batch_inventory_type, batch_customer = ownership.get(batch_no) or (None, None)

		# Rule 1: batch first, row second. ``normalize_ownership`` supplies the
		# "Regular Stock" default and keeps the (type, customer) pair coherent.
		inventory_type, customer = normalize_ownership(
			batch_inventory_type or row.get("inventory_type"),
			batch_customer or (row.get("customer") if has_customer else None),
			batch_no=batch_no,
			item_code=row.get("item_code"),
		)

		row.set("inventory_type", inventory_type)
		if has_customer:
			row.set("customer", customer)

		if mirror and row.get("target_warehouse"):
			row.set("to_inventory_type", inventory_type)
			if mirror_customer:
				row.set("to_customer", customer)
