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
	"""Add btree indexes for manufacturing_plan link lookups (PMO / PO dashboards)."""
	specs = (
		("tabParent Manufacturing Order", "manufacturing_plan_idx", "manufacturing_plan"),
		("tabPurchase Order", "manufacturing_plan_idx", "manufacturing_plan"),
	)
	for table, index_name, column in specs:
		if _index_exists(table, index_name):
			continue
		frappe.db.sql(
			f"ALTER TABLE `{table}` ADD INDEX `{index_name}` (`{column}`)",
		)
