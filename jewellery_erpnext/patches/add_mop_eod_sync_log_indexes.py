"""
Add performance indexes for MOP EOD Sync Log and MOP EOD Sync Log Item tables.
Also adds indexes for MOP EOD Sync Work Order Filter child table.
"""

import frappe


def execute():
	_add_index_if_missing("tabMOP EOD Sync Log", "eod_sync_log_status", ["status"])
	_add_index_if_missing("tabMOP EOD Sync Log", "eod_sync_log_date", ["posting_date"])
	_add_index_if_missing(
		"tabMOP EOD Sync Log", "eod_sync_log_trigger", ["trigger_type"]
	)
	_add_index_if_missing(
		"tabMOP EOD Sync Log", "eod_sync_log_started_on", ["started_on"]
	)

	_add_index_if_missing("tabMOP EOD Sync Log Item", "esl_item_parent", ["parent"])
	_add_index_if_missing(
		"tabMOP EOD Sync Log Item", "esl_item_mwo", ["manufacturing_work_order"]
	)
	_add_index_if_missing(
		"tabMOP EOD Sync Log Item", "esl_item_mop", ["manufacturing_operation"]
	)
	_add_index_if_missing("tabMOP EOD Sync Log Item", "esl_item_status", ["status"])
	_add_index_if_missing(
		"tabMOP EOD Sync Log Item", "esl_item_error_type", ["error_type"]
	)
	_add_index_if_missing(
		"tabMOP EOD Sync Log Item", "esl_item_parent_status", ["parent", "status"]
	)
	_add_index_if_missing(
		"tabMOP EOD Sync Log Item",
		"esl_item_parent_mwo",
		["parent", "manufacturing_work_order"],
	)
	_add_index_if_missing(
		"tabMOP EOD Sync Log Item",
		"esl_item_parent_item_batch",
		["parent", "item_code", "batch_no"],
	)


def _add_index_if_missing(table, index_name, columns):
	"""Add an index only if the table exists and the index does not already exist."""
	if not frappe.db.table_exists(table):
		return

	existing = frappe.db.sql(
		f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", (index_name,)
	)
	if existing:
		return

	col_str = ", ".join(f"`{c}`" for c in columns)
	try:
		frappe.db.sql(f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({col_str})")
		frappe.logger().info("Added index %s on %s (%s)", index_name, table, col_str)
	except Exception as e:
		frappe.logger().warning(
			"Could not add index %s on %s: %s", index_name, table, e
		)
