import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Department": [
			{
				"fieldname": "custom_stock_reconciliation_window_section",
				"fieldtype": "Section Break",
				"label": "Stock Reconciliation Window",
				"insert_after": "custom_is_finding",
			},
			{
				"fieldname": "custom_stock_reconciliation_from_time",
				"fieldtype": "Time",
				"label": "From Time",
				"insert_after": "custom_stock_reconciliation_window_section",
				"description": (
					"Start of the daily window during which Stock Reconciliation is allowed "
					"for this department. All other stock movement is blocked while the window is open."
				),
			},
			{
				"fieldname": "custom_stock_reconciliation_column_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_stock_reconciliation_from_time",
			},
			{
				"fieldname": "custom_stock_reconciliation_to_time",
				"fieldtype": "Time",
				"label": "To Time",
				"insert_after": "custom_stock_reconciliation_column_break",
				"description": (
					"End of the daily window. Once this time passes, stock movement is "
					"automatically allowed again."
				),
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_department_stock_recon_window_fields: ensured Department "
		"custom_stock_reconciliation_from_time / custom_stock_reconciliation_to_time"
	)
