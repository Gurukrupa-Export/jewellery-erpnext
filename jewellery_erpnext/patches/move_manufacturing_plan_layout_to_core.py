"""Let ``manufacturing_plan.json`` own the Manufacturing Plan layout.

The layout was built on site with Customize Form: three Custom Fields (Estimated Delivery Date
and the column / section breaks around Flow Type ... Design Type) and a ``field_order``
Property Setter. The JSON now carries the same fields and order, and Frappe does not merge a
Custom Field into a standard field of the same name - it shows both - so the Custom Field rows
and the stale field order go.

Rows are deleted directly, not through Custom Field's on_trash: the
``estimated_delivery_date`` column and its data stay, now owned by the standard field, and its
Property Setters keep applying to it. Other Manufacturing Plan Property Setters (e.g. the
``get_mwo`` depends_on) are left alone. Idempotent.
"""

import frappe

DOCTYPE = "Manufacturing Plan"
CUSTOM_FIELDS = ("estimated_delivery_date", "custom_column_break_5wdau", "custom_section_break_edsf8")
# The breaks are standard as column_break_5wdau / section_break_edsf8, so their setters are orphans.
RENAMED_BREAKS = ("custom_column_break_5wdau", "custom_section_break_edsf8")


def execute():
	frappe.db.delete("Custom Field", {"dt": DOCTYPE, "fieldname": ("in", CUSTOM_FIELDS)})
	frappe.db.delete("Property Setter", {"doc_type": DOCTYPE, "field_name": ("in", RENAMED_BREAKS)})
	frappe.db.delete("Property Setter", {"doc_type": DOCTYPE, "property": "field_order"})
	frappe.clear_cache(doctype=DOCTYPE)
