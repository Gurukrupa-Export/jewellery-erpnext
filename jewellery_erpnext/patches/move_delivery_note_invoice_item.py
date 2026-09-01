"""Move ``Delivery Note.custom_invoice_item`` into its own full-width section
immediately after the ``net_total`` field.

Currently the Table renders at index 54 inside the ``custom_section_break_by05h``
items section. This patch:

1. creates a dedicated Section Break ``custom_section_invoice_item``
   (insert_after ``net_total``),
2. points the Table's ``insert_after`` at that section,
3. injects both fields into the ``field_order`` Property Setter immediately
   after ``net_total`` and before ``taxes_section``,

keeping the exact same fieldname / fieldtype / options so existing
``Delivery Note E Invoice Item`` child rows (linked by ``parent`` = Delivery
Note name) remain intact.

Safe to re-run: every step is guarded by existence checks and idempotent
keyed lookups. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.move_delivery_note_invoice_item.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SECTION_FIELD = "custom_section_invoice_item"
TABLE_FIELD = "custom_invoice_item"
DOCTYPE = "Delivery Note"
ANCHOR = "net_total"


def execute():
	doctype = DOCTYPE

	# 1. Create the Section Break after `net_total`. create_custom_fields is
	# idempotent keyed on (dt, fieldname) — no-op if already present.
	create_custom_fields(
		{
			doctype: [
				{
					"fieldname": SECTION_FIELD,
					"label": "Invoice Item Section",
					"fieldtype": "Section Break",
					"insert_after": ANCHOR,
				}
			]
		},
		update=True,
	)

	# 2. Direct DB update bypasses Frappe's custom-field chronological sort
	# bug; Property Setters do NOT re-order Custom Fields.
	frappe.db.set_value(
		"Custom Field",
		{"dt": doctype, "fieldname": SECTION_FIELD},
		"insert_after",
		ANCHOR,
	)
	frappe.db.set_value(
		"Custom Field",
		{"dt": doctype, "fieldname": TABLE_FIELD},
		"insert_after",
		SECTION_FIELD,
	)

	# Clean up any Property Setters that would override insert_after / width.
	frappe.db.delete(
		"Property Setter",
		{"doc_type": doctype, "field_name": TABLE_FIELD, "property": "insert_after"},
	)
	frappe.db.delete(
		"Property Setter",
		{"doc_type": doctype, "field_name": SECTION_FIELD, "property": "insert_after"},
	)

	# Force the Table to 100% width.
	from frappe.custom.doctype.property_setter.property_setter import (
		make_property_setter,
	)

	make_property_setter(doctype, TABLE_FIELD, "width", "100%", "Data")

	# 3. The `field_order` Property Setter overrides the entire layout. Inject
	# both fields right after `net_total` (before `taxes_section`).
	field_order_str = frappe.db.get_value(
		"Property Setter", {"doc_type": doctype, "property": "field_order"}, "value"
	)
	if field_order_str and ANCHOR in json.loads(field_order_str):
		field_order = json.loads(field_order_str)

		# Remove both fields if present anywhere, so the injection below is
		# deterministic (no duplicates).
		for f in (SECTION_FIELD, TABLE_FIELD):
			if f in field_order:
				field_order.remove(f)

		anchor_idx = field_order.index(ANCHOR) + 1
		for i, f in enumerate((SECTION_FIELD, TABLE_FIELD)):
			field_order.insert(anchor_idx + i, f)

		frappe.db.set_value(
			"Property Setter",
			{"doc_type": doctype, "property": "field_order"},
			"value",
			json.dumps(field_order),
		)

	# Reset idx so the ordering is driven purely by insert_after / field_order.
	frappe.db.sql("UPDATE `tabCustom Field` SET idx = 0 WHERE dt = %s", (doctype,))
	frappe.db.commit()

	frappe.clear_cache(doctype=doctype)
	print(
		f"[move_delivery_note_invoice_item] {TABLE_FIELD} moved into {SECTION_FIELD} after {ANCHOR}"
	)
