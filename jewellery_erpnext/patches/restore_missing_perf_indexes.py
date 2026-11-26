"""Index the Employee IR Receive submit path: re-create the two MOP Log indexes a fresh install
never built, and index the voucher lookups on Serial and Batch Bundle / Entry that every Process
Loss entry pays for.

MOP LOG
-------
``add_make_receive_entry_indexes`` (``mop_balance_idx``) and ``add_jewellery_perf_indexes``
(``mop_voucher_cancel_idx``) are in ``tabPatch Log``, but a fresh install only MARKS patches as
run (``frappe.installer.set_all_patches_as_completed``; see ``jewellery_erpnext.install``), so a
site installed after they were added never built the indexes -- and migrate never re-runs a
logged patch. ``get_current_mop_balance_rows`` documents its filter as "index-served" by
``mop_balance_idx``; without it every ``tabMOP Log`` read by ``manufacturing_operation`` walks
the whole ``creation`` index -- 1.7-2.1 s per query on the prod restore (484k rows), 4 ms with
the index. An Employee IR Receive issues ~10-15 of them: ``MOPLog.validate`` ->
``recalculate_manufacturing_operation_weights`` on every MOP Log save, the source lookups of
``create_mop_log_for_employee_ir_receive``, and ``get_last_mop_index`` /
``get_current_mop_balance_rows`` under ``update_new_mop_wtg``. ``mop_voucher_cancel_idx`` serves
the voucher lookups of the same clone writer and of the receive/issue cancel.

SERIAL AND BATCH BUNDLE
-----------------------
``update_parent_batch_id`` (serial_and_batch_bundle/doc_events/utils.py) finds a Repack's
Outward bundles by ``voucher_no`` once per produced bundle -- once per loss row of an Employee
IR's Process Loss entry. ERPNext's ``v16_0.set_posting_datetime_for_sabb_and_drop_indexes``
dropped ``voucher_no_index``, so the lookup falls back to the low-selectivity ``voucher_type``
index (~198k rows examined, 10.6 s per lookup on the prod restore). The index here has an
app-owned name: that ERPNext patch drops ``voucher_no_index`` by name and never touches it.

SERIAL AND BATCH ENTRY
----------------------
ERPNext's ``StockController.validate_reserved_batches`` (stock reservation enabled) runs after
every Stock Entry / Delivery Note / Sales Invoice posts its ledger, and reads
``tabSerial and Batch Entry`` by ``voucher_type`` / ``voucher_no``, which ERPNext does not index:
a range scan of ~260k rows on ``batch_no``, 14 s for one Process Loss entry on the prod restore.
Its result is only an IN-list for ``get_reserved_batches``, so the row order an index produces
cannot matter.

ONLY THESE FOUR
---------------
The other indexes of those two patches stay out on purpose: an index leading with ``parent``
can become the plan of Frappe's ``FOR UPDATE`` child-row load, and the Stock Reservation Entry /
Serial and Batch Entry ones can reorder ``loss_stock_entry._query_batch_and_qty_sres`` (no ORDER
BY), whose tie-break picks which reservation a loss is booked against. These four only serve
plain reads on the Receive path: ``tabMOP Log`` is row-locked by primary key alone, nothing locks
a Serial and Batch Bundle or Entry by voucher, and the Entry UPDATEs filter by ``name`` or
``parent``.

Idempotent: skipped when an index with the same name or the same leading column list already
exists, logged and skipped when a column is missing. Built online (``ALGORITHM=INPLACE,
LOCK=NONE``), so writes continue while it runs. Also run on a fresh install by
``jewellery_erpnext.install._run_schema_patches``, since the installer only marks patches.
"""

import frappe

SPECS = (
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
		"mop_voucher_cancel_idx",
		("voucher_type", "voucher_no", "is_cancelled"),
	),
	(
		"tabSerial and Batch Bundle",
		"sbb_voucher_no_idx",
		("voucher_no", "voucher_type"),
	),
	(
		"tabSerial and Batch Entry",
		"sbe_voucher_no_idx",
		("voucher_no", "voucher_type"),
	),
)


def _indexes(table: str) -> dict:
	"""``{index_name: (column, ...)}`` for ``table``, columns in index order."""
	rows = frappe.db.sql(
		"""
		SELECT index_name, column_name
		FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name = %s
		ORDER BY index_name, seq_in_index
		""",
		(table,),
	)
	indexes = {}
	for index_name, column_name in rows:
		indexes.setdefault(index_name, []).append(column_name)
	return {name: tuple(cols) for name, cols in indexes.items()}


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
	existing = _indexes(table)
	if index_name in existing:
		if existing[index_name] != columns:
			frappe.log_error(
				title=f"restore_missing_perf_indexes: {index_name} differs",
				message=(
					f"`{table}`.`{index_name}` exists on {existing[index_name]}, expected {columns}; "
					"left as it is."
				),
			)
		return
	if any(cols[: len(columns)] == columns for cols in existing.values()):
		return  # the same columns are already indexed under another name
	if not _table_has_columns(table, columns):
		frappe.log_error(
			title=f"restore_missing_perf_indexes: skip {index_name}",
			message=(
				f"Table `{table}` is missing one or more of {columns}; "
				"index not added. Re-run after the doctype is fully migrated."
			),
		)
		return
	# Column names are hard-coded literals from SPECS -- no user input.
	col_list = ", ".join(f"`{c}`" for c in columns)
	frappe.db.sql_ddl(
		f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({col_list}), ALGORITHM=INPLACE, LOCK=NONE"
	)


def execute():
	for table, index_name, columns in SPECS:
		_add_index_if_missing(table, index_name, columns)
