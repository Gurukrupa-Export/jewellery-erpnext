"""Move two Item field groups into their own full-width sections.

``custom_item_theme_code`` and the pair ``custom_box_no`` / ``custom_box_table``
all currently render inside ``inventory_settings_section`` (between
``allow_negative_stock`` and ``sb_barcodes``). This patch:

1. creates a dedicated Section Break per group — ``custom_section_item_theme_code``
   and ``custom_section_box``,
2. chains them so the second section inserts after the first group's table (not
   the shared anchor), keeping the order deterministic even on sites with no
   ``field_order`` Property Setter,
3. if an Item ``field_order`` Property Setter exists, re-injects all five fields
   immediately after ``allow_negative_stock`` and before ``sb_barcodes``,

so each new section holds **only** its own fields and the rest of
``inventory_settings_section`` stays above.

keeping the exact same fieldname / fieldtype / options so existing
``Item Theme Code Detail`` and ``Item Box Table`` child rows (linked by
``parent`` = Item name) remain intact.

Safe to re-run: every step is guarded by existence checks and idempotent
keyed lookups. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.move_item_theme_code_section.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Item"
ANCHOR = "allow_negative_stock"

# (section_fieldname, section_label, (fields inside the section))
SECTIONS = (
	("custom_section_item_theme_code", "Item Theme Code", ("custom_item_theme_code",)),
	("custom_section_box", "Box Details", ("custom_box_no", "custom_box_table")),
)

TABLE_WIDTHS = ("custom_item_theme_code", "custom_box_table")


def execute():
	doctype = DOCTYPE

	# 1. Create one Section Break per group. create_custom_fields is idempotent
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
				for section, label, _ in SECTIONS
			]
		},
		update=True,
	)

	# 2. Direct DB update bypasses Frappe's custom-field chronological sort bug;
	# Property Setters do NOT re-order Custom Fields. Chain each group after the
	# previous one so the insert_after chain alone yields the intended order
	# (allow_negative_stock -> theme section -> theme table -> box section ->
	# box_no -> box_table) even where no field_order Property Setter exists.
	prev = ANCHOR
	for section, _, fields in SECTIONS:
		frappe.db.set_value(
			"Custom Field", {"dt": doctype, "fieldname": section}, "insert_after", prev
		)
		prev = section
		for f in fields:
			frappe.db.set_value(
				"Custom Field", {"dt": doctype, "fieldname": f}, "insert_after", prev
			)
			prev = f

			# Clean up any Property Setters that would override insert_after.
			frappe.db.delete(
				"Property Setter",
				{"doc_type": doctype, "field_name": f, "property": "insert_after"},
			)
		frappe.db.delete(
			"Property Setter",
			{"doc_type": doctype, "field_name": section, "property": "insert_after"},
		)

	# Force the Tables to 100% width.
	from frappe.custom.doctype.property_setter.property_setter import (
		make_property_setter,
	)

	for f in TABLE_WIDTHS:
		make_property_setter(doctype, f, "width", "100%", "Data")

	# 3. The `field_order` Property Setter overrides the entire layout. Only
	# touch it when the anchor is present — otherwise removing the fields would
	# silently drop them from the layout.
	field_order_str = frappe.db.get_value(
		"Property Setter", {"doc_type": doctype, "property": "field_order"}, "value"
	)
	if field_order_str and ANCHOR in json.loads(field_order_str):
		field_order = json.loads(field_order_str)

		remove = []
		for section, _, fields in SECTIONS:
			remove.append(section)
			remove.extend(fields)
		for f in remove:
			if f in field_order:
				field_order.remove(f)

		anchor_idx = field_order.index(ANCHOR) + 1
		inject = []
		for section, _, fields in SECTIONS:
			inject.append(section)
			inject.extend(fields)
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
	for section, label, fields in SECTIONS:
		print(
			f"[move_item_theme_code_section] {fields} -> {section} ({label}) after {ANCHOR}"
		)
