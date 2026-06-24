"""
Provision the `custom_jewelex_batch_no` Data field on Parent Manufacturing Order.

Manufacturing Work Order's `jewelex_batch_no` field fetches from
`manufacturing_order.custom_jewelex_batch_no`. The field was only declared in
custom_fields/parent_manufacturing_order.json (and previously misspelled as
"jwelex"), which no patch installs, so the column never existed on real sites.
A fetch_from pointing at a missing column hard-fails link validation on PMO
submit (1054 Unknown column). Creating the column here fixes that.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Parent Manufacturing Order": [
			{
				"fieldname": "custom_jewelex_batch_no",
				"fieldtype": "Data",
				"label": "Jewelex Batch No",
				"insert_after": "setting_type",
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_pmo_jewelex_batch_no_custom_field: custom_jewelex_batch_no created/updated on Parent Manufacturing Order"
	)
