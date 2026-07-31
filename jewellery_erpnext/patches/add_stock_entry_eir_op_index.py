import frappe

from jewellery_erpnext.patches.add_stock_entry_idempotency_indexes import (
	_add_index_if_missing,
)


def execute():
	_add_index_if_missing(
		"tabStock Entry",
		"se_eir_op_idx",
		("employee_ir", "custom_eir_operation_row", "auto_created", "docstatus"),
	)
	frappe.logger().info("add_stock_entry_eir_op_index: ensured se_eir_op_idx")
