"""Item doctype layout moves — two operations in one patch.

Move 1: ``customer_items`` Table from the Manufacturing tab to the Sales tab,
wrapping it in a new ``Customer Items`` section break.

Move 2: ``design_attribute`` Table inside the Design Attribute tab gets its
own ``Design Attribute`` section break (currently sits directly after
``custom_zodiac`` with no section header).

Current layout (from live field_order):
  Sales tab ends at [175] no_of_months → [176] uom_tab
  Manufacturing tab: [194] manufacturing → [195] customer_details →
    [196] customer_items → [197] is_sub_contracted_item ...
  Design Attribute tab: ... [323] custom_zodiac → [324] design_attribute
    → [325] creativity (Tab Break)  — no section header on design_attribute

Target:
  Sales tab: ... [175] no_of_months → [NEW] custom_section_customer_items
    → [MOVED] customer_items → [176] uom_tab
  Manufacturing tab: [194] manufacturing → [195] customer_details →
    [197] is_sub_contracted_item (customer_items removed)
  Design Attribute tab: ... custom_zodiac →
    [NEW] custom_section_design_attribute → design_attribute →
    [325] creativity (Tab Break)

Safe to re-run: guarded by existence checks and idempotent key lookups.

    bench --site <site> execute jewellery_erpnext.patches.move_item_customer_items_to_sales.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import rewrite_insert_after_chain

DOCTYPE = "Item"

# --- Move 1: customer_items from Manufacturing tab to Sales tab ---
SALES_ANCHOR = "no_of_months"  # last field in Sales tab before uom_tab
SALES_SECTION = "custom_section_customer_items"
SALES_SECTION_LABEL = "Customer Items"
SALES_TABLE = "customer_items"
# Inherited from erpnext's `customer_details` Section Break, which is the only
# thing gating `customer_items` today (the table has no depends_on of its own).
SALES_SECTION_DEPENDS_ON = "eval:!doc.is_fixed_asset"

# --- Move 2: design_attribute gets its own section in Design Attribute tab ---
DA_ANCHOR = "custom_zodiac"  # last field before design_attribute
DA_SECTION = "custom_section_design_attribute"
DA_SECTION_LABEL = "Design Attribute"
DA_TABLE = "design_attribute"

ALL_NEW_SECTIONS = (SALES_SECTION, DA_SECTION)


def execute():
	# 1. Create both Section Breaks as Custom Fields
	create_custom_fields(
		{
			DOCTYPE: [
				{
					"fieldname": SALES_SECTION,
					"label": SALES_SECTION_LABEL,
					"fieldtype": "Section Break",
					"insert_after": SALES_ANCHOR,
					# erpnext gates `customer_items` through its enclosing
					# `customer_details` section; carry that gate over so the table
					# does not start showing on fixed-asset Items.
					"depends_on": SALES_SECTION_DEPENDS_ON,
				},
				{
					"fieldname": DA_SECTION,
					"label": DA_SECTION_LABEL,
					"fieldtype": "Section Break",
					"insert_after": DA_ANCHOR,
				},
			]
		},
		update=True,
	)
	# Anchor each Section Break on the field that PRECEDES it and chain its table
	# off the section, so the insert_after chain on its own yields the intended
	# order where no field_order Property Setter exists. rewrite_insert_after_chain
	# skips block members with no Custom Field row, so the standard `customer_items`
	# field is left untouched while `design_attribute` is re-anchored on its section.
	rewrite_insert_after_chain(DOCTYPE, (SALES_SECTION, SALES_TABLE), SALES_ANCHOR)
	rewrite_insert_after_chain(DOCTYPE, (DA_SECTION, DA_TABLE), DA_ANCHOR)

	# Drop any insert_after Property Setter that would override the chain above.
	for sec_field in ALL_NEW_SECTIONS:
		frappe.db.delete(
			"Property Setter",
			{
				"doc_type": DOCTYPE,
				"field_name": sec_field,
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
			" — sections created, field_order is DB-default"
		)
		return

	field_order = json.loads(ps_row[0].value)
	changed = False

	# --- Move 1: customer_items into Sales tab ---
	# Remove from old position(s)
	for f in (SALES_TABLE, SALES_SECTION):
		while f in field_order:
			field_order.remove(f)
			changed = True

	# Inject after SALES_ANCHOR
	if SALES_ANCHOR in field_order:
		idx = field_order.index(SALES_ANCHOR) + 1
		for i, f in enumerate((SALES_SECTION, SALES_TABLE)):
			field_order.insert(idx + i, f)
		changed = True

	# --- Move 2: design_attribute under its own section ---
	# Remove from old position(s)
	for f in (DA_TABLE, DA_SECTION):
		while f in field_order:
			field_order.remove(f)
			changed = True

	# Inject after DA_ANCHOR
	if DA_ANCHOR in field_order:
		idx = field_order.index(DA_ANCHOR) + 1
		for i, f in enumerate((DA_SECTION, DA_TABLE)):
			field_order.insert(idx + i, f)
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
		f"[move_item_customer_items_to_sales]"
		f" {SALES_TABLE} under {SALES_SECTION} ({SALES_SECTION_LABEL})"
		f" after {SALES_ANCHOR};"
		f" {DA_TABLE} under {DA_SECTION} ({DA_SECTION_LABEL})"
		f" after {DA_ANCHOR}"
	)
