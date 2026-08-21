"""
Add per-Sales-Order precision-override custom fields to Sales Order.

These two Check fields let a Sales Order override the customer-level precision
defaults used when the BOM is (re)built during Sales Order validation
(_get_bom_context in doc_events/sales_order.py):
  - custom_precision            -> when ticked, forces metal_precision = 2
  - custom_precision_for_stone  -> when ticked, forces stone_precision = 2

They are declared only in custom_fields/sales_order.json, but this app's
custom_fields/*.json are dead config on real and CI sites: the after_migrate
hook that would sync them is disabled (hooks.py), and install-app marks patches
"complete" without running them on fresh sites. So the fields were referenced in
code but never created in the DB, causing every Sales Order save to crash with:
  AttributeError: 'SalesOrder' object has no attribute 'custom_precision'

This patch provisions them via the canonical helper. Idempotent (keyed on
(dt, fieldname)). Ad-hoc entry point:
  bench --site <site> execute jewellery_erpnext.patches.add_sales_order_precision_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Sales Order": [
			{
				"fieldname": "custom_precision",
				"fieldtype": "Check",
				"label": "Precision For Metal(2 Digit)",
				"insert_after": "precision",
				"is_system_generated": 1,
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_precision_for_stone",
				"fieldtype": "Check",
				"label": "Precision For Stone(2 Digit)",
				"insert_after": "custom_precision",
				"is_system_generated": 1,
				"module": "Jewellery Erpnext",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_sales_order_precision_fields: custom fields created/updated on Sales Order"
	)
