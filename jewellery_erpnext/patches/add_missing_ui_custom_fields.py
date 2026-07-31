import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "custom_request_id",
				"fieldtype": "Data",
				"label": "Make Receive Entry Request ID",
				"length": 36,
				"insert_after": "source_stock_entry",
				"depends_on": 'eval:doc.stock_entry_type=="Material Receive (WORK ORDER)"',
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_eir_operation_row",
				"fieldtype": "Data",
				"label": "Employee IR Operation Row",
				"insert_after": "employee_ir",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
			},
		],
		"Company": [
			{
				"fieldname": "custom_freeze_entries",
				"fieldtype": "Check",
				"label": "Freeze Entries",
				"insert_after": "default_operating_cost_account",
			}
		],
		"Item": [
			{
				"fieldname": "custom_is_photoshop_images",
				"fieldtype": "Check",
				"label": "Is Photoshop Images",
				"insert_after": "is_customer_provided_item",
			}
		],
		"Serial No": [
			{
				"fieldname": "purchase_document_no",
				"fieldtype": "Data",
				"label": "Purchase Document No",
				"insert_after": "custom_finish_back_view",
			}
		],
		"Serial and Batch Bundle": [
			{
				"fieldname": "posting_date",
				"fieldtype": "Date",
				"label": "Posting Date",
				"insert_after": "returned_against",
			},
			{
				"fieldname": "posting_time",
				"fieldtype": "Time",
				"label": "Posting Time",
				"insert_after": "posting_date",
			},
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_missing_ui_custom_fields: ensured Stock Entry.custom_request_id, "
		"Stock Entry.custom_eir_operation_row, Company.custom_freeze_entries, "
		"Item.custom_is_photoshop_images, Serial No.purchase_document_no, "
		"Serial and Batch Bundle.posting_date/posting_time"
	)
