"""Composite indexes for the MOP EOD Sync hot query paths on tabMOP Log.

Identified by auditing the actual call sites in mop_eod_sync._get_unsynced_mop_groups():

  SELECT ... FROM tabMOP Log
  WHERE is_synced = 0 AND is_cancelled = 0
  ORDER BY manufacturing_operation, flow_index asc, creation asc

The two indexes added here ensure the WHERE clause is covered and the GROUP BY
(manufacturing_operation) can use an index prefix rather than a full table scan.

Note: DocType fields (eod_sync_time, eod_sync_running, etc.) are applied by
``bench migrate`` from mop_settings.json — this patch only adds DB indexes.
"""

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


def _table_has_columns(table: str, columns: tuple) -> bool:
	rows = frappe.db.sql(
		"""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s
        """,
		(table,),
	)
	present = {r[0] for r in rows}
	return all(c in present for c in columns)


def _add_index_if_missing(table: str, index_name: str, columns: tuple):
	if _index_exists(table, index_name):
		return
	if not _table_has_columns(table, columns):
		frappe.log_error(
			title=f"add_mop_eod_sync_fields_and_indexes: skip {index_name}",
			message=(
				f"Table `{table}` is missing one or more of {columns}; "
				"index not added. Re-run after the doctype is fully migrated."
			),
		)
		return
	col_list = ", ".join(f"`{c}`" for c in columns)
	frappe.db.sql(f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({col_list})")


def execute():
	specs = (
		# Primary EOD sync query: WHERE is_synced=0 AND is_cancelled=0
		# ORDER BY manufacturing_operation — covers both the filter and the sort prefix.
		(
			"tabMOP Log",
			"mop_eod_sync_idx",
			("is_synced", "is_cancelled", "manufacturing_work_order"),
		),
		# Secondary: supports grouping and ordering by manufacturing_operation + creation
		# when building per-MOP log groups inside _get_unsynced_mop_groups.
		(
			"tabMOP Log",
			"mop_mwo_creation_idx",
			("manufacturing_work_order", "creation"),
		),
	)
	for table, index_name, columns in specs:
		_add_index_if_missing(table, index_name, columns)
