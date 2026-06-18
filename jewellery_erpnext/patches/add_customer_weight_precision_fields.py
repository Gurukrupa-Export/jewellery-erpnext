"""
Add weight-precision custom fields to Customer.

These two Int fields control the decimal rounding of gross weight and net
(metal + finding) weight on the BOM generated during Sales Order validation
(_get_bom_context / _update_bom_totals in doc_events/sales_order.py).

They were referenced in code but never created in the DB, causing every Sales
Order save to crash with: Unknown column 'custom_precision_for_net_weight'.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Customer": [
			{
				"fieldname": "custom_precision_for_net_weight",
				"fieldtype": "Int",
				"label": "Precision for Net Weight",
				"insert_after": "custom_precision_for_stone",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_precision_for_gross_weight",
				"fieldtype": "Int",
				"label": "Precision for Gross Weight",
				"insert_after": "custom_precision_for_net_weight",
				"module": "Jewellery Erpnext",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_customer_weight_precision_fields: custom fields created/updated on Customer"
	)
