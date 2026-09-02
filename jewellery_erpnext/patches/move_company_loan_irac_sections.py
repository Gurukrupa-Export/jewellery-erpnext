"""Move ``Company.loan_classification_ranges`` and ``irac_provisioning_configuration``
each into their own full-width section, placed after ``default_loss_warehouse``.

Both Loan tables currently render inside the generic ``loan_section_break_2``
(no label) in the Loan tab. This patch:

1. creates a dedicated Section Break per table (label = the table's label),
2. chains them so the *second* section inserts after the *first* table (not the
   shared anchor), making the order deterministic even on sites with no Company
   ``field_order`` Property Setter (fresh / CI installs),
3. if a Company ``field_order`` Property Setter exists, re-injects all four
   fields (section + table, per pair) immediately after ``default_loss_warehouse``,

keeping the exact same fieldname / fieldtype / options so existing child rows
(linked by ``parent`` = Company name) remain intact.

Safe to re-run: every step is guarded by existence checks and idempotent
keyed lookups. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.move_company_loan_irac_sections.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Company"
ANCHOR = "default_loss_warehouse"

# (table_fieldname, section_fieldname, section_label)
PAIRS = (
	(
		"loan_classification_ranges",
		"custom_section_loan_classification_ranges",
		"Loan Classification Ranges",
	),
	(
		"irac_provisioning_configuration",
		"custom_section_irac_provisioning_configuration",
		"IRAC Provisioning Configuration",
	),
)


def execute():
	doctype = DOCTYPE

	# 1. Create one Section Break per table. create_custom_fields is idempotent
	# keyed on (dt, fieldname) — no-op if already present.
	create_custom_fields(
		{
			doctype: [
				{
					"fieldname": section,
					"label": label,
					"fieldtype": "Section Break",
					"insert_after": ANCHOR,
				}
				for _, section, label in PAIRS
			]
		},
		update=True,
	)

	# 2. Direct DB update bypasses Frappe's custom-field chronological sort bug;
	# Property Setters do NOT re-order Custom Fields. Chain the second section
	# after the first table so the insert_after chain alone yields the intended
	# order (default_loss_warehouse -> section1 -> table1 -> section2 -> table2)
	# even where no field_order Property Setter exists.
	prev_field = ANCHOR
	for table, section, _ in PAIRS:
		frappe.db.set_value(
			"Custom Field",
			{"dt": doctype, "fieldname": section},
			"insert_after",
			prev_field,
		)
		frappe.db.set_value(
			"Custom Field", {"dt": doctype, "fieldname": table}, "insert_after", section
		)
		prev_field = table

		# Clean up any Property Setters that would override insert_after.
		for field in (section, table):
			frappe.db.delete(
				"Property Setter",
				{"doc_type": doctype, "field_name": field, "property": "insert_after"},
			)

	# Force the Tables to 100% width.
	from frappe.custom.doctype.property_setter.property_setter import (
		make_property_setter,
	)

	for table, _, _ in PAIRS:
		make_property_setter(doctype, table, "width", "100%", "Data")

	# 3. The `field_order` Property Setter overrides the entire layout. Only touch
	# it when the anchor is actually present — otherwise removing the four fields
	# would silently drop them from the layout.
	field_order_str = frappe.db.get_value(
		"Property Setter", {"doc_type": doctype, "property": "field_order"}, "value"
	)
	if field_order_str and ANCHOR in json.loads(field_order_str):
		field_order = json.loads(field_order_str)

		all_fields = [f for pair in PAIRS for f in pair[:2]]
		for f in all_fields:
			if f in field_order:
				field_order.remove(f)

		anchor_idx = field_order.index(ANCHOR) + 1
		# Each tuple is (table, section, label): emit section before its table.
		inject = [f for pair in PAIRS for f in (pair[1], pair[0])]
		for i, f in enumerate(inject):
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
	for table, section, label in PAIRS:
		print(
			f"[move_company_loan_irac_sections] {table} -> {section} ({label}) after {ANCHOR}"
		)
