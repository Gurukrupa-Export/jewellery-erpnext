import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Department": [
			{
				"fieldname": "custom_wo_receive_limit_section",
				"fieldtype": "Section Break",
				"label": "Work Order Limit",
				"insert_after": "custom_stock_reconciliation_to_time",
			},
			{
				"fieldname": "custom_employee_ir_work_order_limit",
				"fieldtype": "Int",
				"label": "Employee IR Work Order Limit",
				"non_negative": 1,
				"default": "0",
				"insert_after": "custom_wo_receive_limit_section",
				"description": (
					"Maximum number of Manufacturing Work Orders allowed in one Employee IR "
					"(Issue or Receive) for this department. 0 = no limit."
				),
			},
			{
				"fieldname": "custom_wo_receive_limit_column_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_employee_ir_work_order_limit",
			},
			{
				"fieldname": "custom_department_ir_work_order_limit",
				"fieldtype": "Int",
				"label": "Department IR Work Order Limit",
				"non_negative": 1,
				"default": "0",
				"insert_after": "custom_wo_receive_limit_column_break",
				"description": (
					"Maximum number of Manufacturing Work Orders allowed in one Department IR "
					"(Issue or Receive) for this department. 0 = no limit."
				),
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_department_wo_receive_limit_fields: ensured Department "
		"custom_employee_ir_work_order_limit / custom_department_ir_work_order_limit"
	)
