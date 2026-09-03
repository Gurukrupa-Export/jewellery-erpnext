"""Move ``customer_items`` Table from the Manufacturing tab to the Sales tab
on the Item doctype, wrapping it in a new ``Customer Items`` section break.

Current layout (from live field_order):
  Sales tab ends at [175] no_of_months → [176] uom_tab
  Manufacturing tab: [194] manufacturing → [195] customer_details →
    [196] customer_items → [197] is_sub_contracted_item ...

Target:
  Sales tab: ... [175] no_of_months → [NEW] custom_section_customer_items
    → [MOVED] customer_items → [176] uom_tab
  Manufacturing tab: [194] manufacturing → [195] customer_details →
    [197] is_sub_contracted_item (customer_items removed)

Both ``customer_items`` and ``customer_details`` are native DocFields, so
``insert_after`` on those rows is not DB-writable.  Layout is controlled
entirely by the field_order Property Setter.

Safe to re-run: guarded by existence checks and idempotent key lookups.

    bench --site <site> execute jewellery_erpnext.patches.move_item_customer_items_to_sales.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Item"
ANCHOR = "no_of_months"  # last field in Sales tab before uom_tab
SECTION_FIELD = "custom_section_customer_items"
SECTION_LABEL = "Customer Items"
TABLE_FIELD = "customer_items"
# Fields to remove from their old positions and re-inject after the new section
FIELDS_TO_MOVE = (SECTION_FIELD, TABLE_FIELD)


def execute():
	# 1. Create the Section Break custom field
	create_custom_fields(
		{
			DOCTYPE: [
				{
					"fieldname": SECTION_FIELD,
					"label": SECTION_LABEL,
					"fieldtype": "Section Break",
					"insert_after": ANCHOR,
				}
			]
		},
		update=True,
	)
	# Ensure insert_after points to ANCHOR (idempotent)
	frappe.db.set_value(
		"Custom Field",
		{"dt": DOCTYPE, "fieldname": SECTION_FIELD},
		"insert_after",
		ANCHOR,
	)
	# Clear any Property Setter insert_after on the new section
	frappe.db.delete(
		"Property Setter",
		{
			"doc_type": DOCTYPE,
			"field_name": SECTION_FIELD,
			"property": "insert_after",
		},
	)

	# 2. Update field_order Property Setter (the authoritative layout source)
	ps_row = frappe.db.sql(
		"SELECT name, value FROM `tabProperty Setter`"
		" WHERE doc_type=%s AND property='field_order'",
		(DOCTYPE,),
		as_dict=True,
	)
	if not ps_row:
		frappe.clear_cache(doctype=DOCTYPE)
		print(
			"[move_item_customer_items_to_sales] no field_order setter"
			" — section created, field_order is DB-default"
		)
		return

	field_order = json.loads(ps_row[0].value)
	changed = False

	# Remove TABLE_FIELD from its old position (if present)
	if TABLE_FIELD in field_order:
		field_order.remove(TABLE_FIELD)
		changed = True

	# Ensure SECTION_FIELD is not duplicated
	while SECTION_FIELD in field_order:
		field_order.remove(SECTION_FIELD)
		changed = True

	# Inject after ANCHOR
	if ANCHOR in field_order:
		anchor_idx = field_order.index(ANCHOR)
		for i, f in enumerate(FIELDS_TO_MOVE):
			field_order.insert(anchor_idx + 1 + i, f)
		changed = True

	if changed:
		frappe.db.set_value(
			"Property Setter",
			ps_row[0].name,
			"value",
			json.dumps(field_order),
		)

	# 3. Re-index Custom Field rows
	frappe.db.sql("UPDATE `tabCustom Field` SET idx = 0 WHERE dt = %s", (DOCTYPE,))
	frappe.db.commit()
	frappe.clear_cache(doctype=DOCTYPE)
	print(
		f"[move_item_customer_items_to_sales] moved {TABLE_FIELD} to Sales tab"
		f" under {SECTION_FIELD} ({SECTION_LABEL}) after {ANCHOR}"
	)
