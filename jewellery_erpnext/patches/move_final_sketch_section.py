"""Move the ``final_sketch_rejected`` and ``final_sketch_hold`` Tables plus
``customer`` and ``inventory_type`` into their own full-width section
immediately after ``usa_states``.

All four fields currently render mid-section inside the territories tab, after
``usa_states`` and before the next Section Break ``inventory_dimension``. This
patch:

1. creates a dedicated Section Break ``custom_section_final_sketch``
   (insert_after ``usa_states``),
2. injects the section followed by the two Tables, then ``customer`` and
   ``inventory_type``, into the ``field_order`` Property Setter immediately
   after ``usa_states`` and before ``inventory_dimension``,
3. pins the two Tables to 100% width,

keeping the exact same fieldname / fieldtype / options so existing
``Final Sketch Rejected`` and ``Final Sketch Hold`` child rows (linked by
``parent`` = Sketch Order name) remain intact.

Anchoring on ``usa_states`` (the section's last field before the next Section
Break) means the section captures exactly the four fields and nothing trailing
(``inventory_dimension`` stays a fresh section below).

Safe to re-run: every step is guarded by existence checks and idempotent
keyed lookups. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.move_final_sketch_section.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Sketch Order"
ANCHOR = "usa_states"
SECTION_FIELD = "custom_section_final_sketch"
SECTION_LABEL = "Final Sketch"
TABLES = ("final_sketch_rejected", "final_sketch_hold")
FIELDS = TABLES + ("customer", "inventory_type")


def execute():
	doctype = DOCTYPE

	# 1. Create the Section Break after `usa_states`. create_custom_fields is
	# idempotent keyed on (dt, fieldname) — no-op if already present.
	create_custom_fields(
		{
			doctype: [
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

	# 2. Direct DB update bypasses Frappe's custom-field chronological sort
	# bug; Property Setters do NOT re-order Custom Fields.
	frappe.db.set_value(
		"Custom Field",
		{"dt": doctype, "fieldname": SECTION_FIELD},
		"insert_after",
		ANCHOR,
	)
	frappe.db.delete(
		"Property Setter",
		{"doc_type": doctype, "field_name": SECTION_FIELD, "property": "insert_after"},
	)

	# Force the Tables to 100% width.
	from frappe.custom.doctype.property_setter.property_setter import (
		make_property_setter,
	)

	for field in TABLES:
		make_property_setter(doctype, field, "width", "100%", "Data")

	# 3. The `field_order` Property Setter overrides the entire layout. Inject
	# the section, the two Tables, then `customer` and `inventory_type`, right
	# after `usa_states` (before `inventory_dimension`). Only touch it when the
	# anchor is present — otherwise removing the fields would silently drop
	# them from the layout.
	field_order_str = frappe.db.get_value(
		"Property Setter", {"doc_type": doctype, "property": "field_order"}, "value"
	)
	if field_order_str and ANCHOR in json.loads(field_order_str):
		field_order = json.loads(field_order_str)

		# Remove the fields if present anywhere, so the injection below is
		# deterministic (no duplicates).
		for f in (SECTION_FIELD,) + FIELDS:
			if f in field_order:
				field_order.remove(f)

		anchor_idx = field_order.index(ANCHOR) + 1
		inject = (SECTION_FIELD,) + FIELDS
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
	print(
		f"[move_final_sketch_section] {list(FIELDS)} -> {SECTION_FIELD} "
		f"({SECTION_LABEL}) after {ANCHOR}"
	)
