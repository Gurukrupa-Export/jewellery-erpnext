import frappe


def _index_exists(table: str, index_name: str) -> bool:
	return bool(
		frappe.db.sql(
			"""
			SELECT 1 FROM information_schema.statistics
			WHERE table_schema = DATABASE()
			  AND table_name = %s
			  AND index_name = %s
			LIMIT 1
			""",
			(table, index_name),
		)
	)


def execute():
	table = "tabMaterial Request"
	index_name = "mr_manufacturing_order_idx"
	if _index_exists(table, index_name):
		return
	frappe.db.sql(
		f"ALTER TABLE `{table}` ADD INDEX `{index_name}` "
		"(`manufacturing_order`, `docstatus`)"
	)
	frappe.logger().info(
		"add_material_request_manufacturing_order_index: added "
		f"{index_name} on {table}(manufacturing_order, docstatus)"
	)
