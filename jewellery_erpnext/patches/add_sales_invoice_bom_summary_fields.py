"""
Add the BOM-summary custom fields referenced by doc_events/sales_invoice.py to
Sales Invoice and Sales Invoice Item.

validate() in doc_events/sales_invoice.py copies per-row BOM totals onto each
Sales Invoice Item (custom_diamond_pcs, custom_gemstone_pcs, custom_metal_weight,
custom_finding_weight, custom_gemstone_weight, custom_diamond_weight,
custom_other_weight, custom_gross_weight) and then sums each of those across
self.items into the matching field on Sales Invoice itself.

They are declared only in custom_fields/sales_invoice.json and
custom_fields/sales_invoice_item.json, but this app's custom_fields/*.json are
dead config on real and CI sites: the after_migrate hook that would sync them
is disabled (hooks.py), and install-app marks patches "complete" without
running them on fresh sites. So the fields were referenced in code but never
created in the DB, causing every Sales Invoice save to crash with:
  AttributeError: 'SalesInvoiceItem' object has no attribute 'custom_diamond_pcs'

This patch provisions them via the canonical helper, matching the field defs
in custom_fields/*.json exactly. Idempotent (keyed on (dt, fieldname)).
Ad-hoc entry point:
  bench --site <site> execute jewellery_erpnext.patches.add_sales_invoice_bom_summary_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Sales Invoice": [
			{
				"fieldname": "custom_metal_weight",
				"fieldtype": "Float",
				"is_system_generated": 1,
				"insert_after": "items",
				"label": "Metal Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_finding_weight",
				"fieldtype": "Float",
				"is_system_generated": 1,
				"insert_after": "custom_metal_weight",
				"label": "Finding Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gemstone_weight",
				"fieldtype": "Float",
				"is_system_generated": 1,
				"insert_after": "custom_finding_weight",
				"label": "Gemstone Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_diamond_weight",
				"fieldtype": "Float",
				"is_system_generated": 1,
				"insert_after": "custom_gemstone_weight",
				"label": "Diamond Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_other_weight",
				"fieldtype": "Float",
				"is_system_generated": 1,
				"insert_after": "custom_diamond_weight",
				"label": "Other Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_diamond_pcs",
				"fieldtype": "Int",
				"is_system_generated": 1,
				"insert_after": "custom_other_weight",
				"label": "Diamond Pcs",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gemstone_pcs",
				"fieldtype": "Int",
				"is_system_generated": 1,
				"insert_after": "custom_diamond_pcs",
				"label": "Gemstone Pcs",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gross_weight",
				"fieldtype": "Float",
				"insert_after": "custom_gemstone_pcs",
				"label": "Gross Weight",
				"module": "Jewellery Erpnext",
			},
		],
		"Sales Invoice Item": [
			{
				"fieldname": "custom_metal_weight",
				"fieldtype": "Float",
				"insert_after": "taxable_value",
				"label": "Metal Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_finding_weight",
				"fieldtype": "Float",
				"insert_after": "custom_metal_weight",
				"label": "Finding Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gemstone_weight",
				"fieldtype": "Float",
				"insert_after": "custom_finding_weight",
				"label": "Gemstone Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_diamond_weight",
				"fieldtype": "Float",
				"insert_after": "custom_gemstone_weight",
				"label": "Diamond Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_other_weight",
				"fieldtype": "Float",
				"insert_after": "custom_diamond_weight",
				"label": "Other Weight",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_diamond_pcs",
				"fieldtype": "Int",
				"insert_after": "custom_other_weight",
				"label": "Diamond Pcs",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gemstone_pcs",
				"fieldtype": "Int",
				"insert_after": "custom_diamond_pcs",
				"label": "Gemstone Pcs",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gross_weight",
				"fieldtype": "Float",
				"insert_after": "custom_gemstone_pcs",
				"label": "Gross Weight",
				"module": "Jewellery Erpnext",
			},
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_sales_invoice_bom_summary_fields: custom fields created/updated on "
		"Sales Invoice and Sales Invoice Item"
	)
