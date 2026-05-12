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
	"""Add composite btree indexes for the Make Receive Entry / MOP Log hot path.

	Targets: latest-balance lookups on tabMOP Log (per MOP and per MWO),
	active-SRE filters on tabStock Reservation Entry, batch fan-out on
	tabSerial and Batch Entry, the already-received aggregation over Material
	Receive Stock Entries, and the per-row Stock Entry Detail filter used for
	the same aggregation.
	"""
	specs = (
		(
			"tabMOP Log",
			"mop_balance_idx",
			(
				"manufacturing_operation",
				"is_cancelled",
				"item_code",
				"batch_no",
				"creation",
			),
		),
		(
			"tabMOP Log",
			"mop_mwo_idx",
			("manufacturing_work_order", "is_cancelled", "item_code", "batch_no"),
		),
		(
			"tabStock Reservation Entry",
			"sre_mwo_active_idx",
			("manufacturing_work_order", "docstatus"),
		),
		(
			"tabSerial and Batch Entry",
			"sbe_parent_batch_idx",
			("parent", "batch_no"),
		),
		(
			"tabStock Entry",
			"se_mr_wo_idx",
			("manufacturing_work_order", "stock_entry_type", "docstatus"),
		),
		(
			"tabStock Entry Detail",
			"sed_parent_item_wh_idx",
			("parent", "item_code", "s_warehouse", "batch_no"),
		),
	)
	for table, index_name, columns in specs:
		if _index_exists(table, index_name):
			continue
		# Column names are hard-coded literals from this module — no user input.
		col_list = ", ".join(f"`{c}`" for c in columns)
		frappe.db.sql(f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({col_list})")
