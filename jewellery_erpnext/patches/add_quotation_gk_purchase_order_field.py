import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	"""Create Quotation.custom_gk_purchase_order.

	Declared in custom_fields/quotation.json, which nothing applies: that directory is read only by
	migrate.after_migrate(), and the hook is commented out (hooks.py). A field declared there alone
	never reaches a real site -- the recurring patch-only custom-field gap fetch_from_guard names.

	Data, deliberately, not a Link to Purchase Order. The KGGK sites build their Quotation from a
	Purchase Order fetched over the API from the GKExport site, so that Purchase Order has no row
	here. get_invalid_links validates every Link field with no exemption, so a Link would turn the
	value into a hard throw on save.
	"""
	custom_fields = {
		"Quotation": [
			{
				"fieldname": "custom_gk_purchase_order",
				"fieldtype": "Data",
				"label": "GK Purchase Order",
				"insert_after": "valid_till",
				"read_only": 1,
				"no_copy": 1,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_quotation_gk_purchase_order_field: ensured Quotation.custom_gk_purchase_order"
	)
