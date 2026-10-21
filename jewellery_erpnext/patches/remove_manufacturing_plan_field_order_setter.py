"""Drop the site-level field order on Manufacturing Plan so the DocType's own order applies.

A Customize Form save in July 2026 stored ``Manufacturing Plan-main-field_order`` as a
Property Setter. It overrides ``manufacturing_plan.json``'s ``field_order`` on every load, so
the Get Sales Order / Get Repair Order / Get MWO buttons sat right under Select Manufacture
Order whatever the JSON said, and it still lists fields that no longer exist
(``sales_order``, ``manufacturing_work_order``, ``get_items_for_production_order``).
The layout now lives in the JSON: the buttons sit after the subcontracting fields, right
above the Manufacturing Plan Table. Idempotent; other Manufacturing Plan Property Setters
(e.g. ``get_mwo`` depends_on) are left alone.
"""

import frappe


def execute():
	frappe.db.delete(
		"Property Setter",
		{"doc_type": "Manufacturing Plan", "property": "field_order"},
	)
	frappe.clear_cache(doctype="Manufacturing Plan")
