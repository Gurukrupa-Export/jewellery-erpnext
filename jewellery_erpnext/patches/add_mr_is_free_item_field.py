import frappe

FIELD = {
	"fieldname": "is_free_item",
	"label": "Is Free Item",
	"fieldtype": "Check",
	"insert_after": "batch_no",
	"default": "0",
	"read_only": 1,
	"hidden": 1,
	"no_copy": 1,
	"print_hide": 1,
	"translatable": 0,
	"description": (
		"Present only to satisfy ERPNext's batch-wise rate block in "
		"accounts_controller.set_missing_item_details, which reads is_free_item "
		"whenever a row carries a batch_no. Always 0 -- Material Request is "
		"excluded from pricing rules, so a free item can never be booked here."
	),
}


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	if frappe.db.has_column("Material Request Item", FIELD["fieldname"]):
		return

	create_custom_fields({"Material Request Item": [FIELD]}, ignore_validate=True)
	frappe.logger().info(
		"add_mr_is_free_item_field: created Material Request Item.is_free_item"
	)
