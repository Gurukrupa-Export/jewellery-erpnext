"""Ensure ``Purchase Order Item.custom_copy_bom`` exists.
"""
import frappe

FIELD = {
	"fieldname": "custom_copy_bom",
	"label": "Copy BOM",
	"fieldtype": "Link",
	"options": "BOM",
	"insert_after": "manufacturing_bom",
	"read_only": 1,
	"is_system_generated": 1,
	"module": "Jewellery Erpnext",
}


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	if frappe.db.has_column("Purchase Order Item", FIELD["fieldname"]):
		return

	create_custom_fields({"Purchase Order Item": [FIELD]}, ignore_validate=True)
	frappe.db.commit()
	frappe.logger().info(
		"add_purchase_order_item_copy_bom_field: created Purchase Order Item.custom_copy_bom"
	)
