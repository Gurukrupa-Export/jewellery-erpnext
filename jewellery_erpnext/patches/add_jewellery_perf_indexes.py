"""Composite indexes for high-frequency query patterns across jewellery_erpnext.

Each index here was identified by auditing the actual call sites — no blind
indexing. Existing coverage (from ``add_make_receive_entry_indexes``) handles
the Make Receive Entry / SRE / Stock Entry / SBE hot paths; this patch fills
the remaining gaps the audit surfaced:

* Employee IR / Department IR cancel cascade: bulk
  ``UPDATE tabMOP Log WHERE voucher_type = %s AND voucher_no = %s
   AND is_cancelled = 0`` in
  ``employee_ir.py::on_submit_issue_new`` (line 140) and
  ``employee_ir.py::on_submit_receive`` (line 292) — full table scan
  without a (voucher_type, voucher_no, is_cancelled) index.
* Employee IR Issue lookup JOIN in ``mop_log.py::resolve_employee_ir_issue_voucher_for_receive``
  (line 514) — ``WHERE eir.docstatus = 1 AND eir.type = 'Issue'
  AND op.manufacturing_operation = ?  ORDER BY eir.modified DESC``.
* Employee IR Operation existence check (``mop_log.py:504``,
  ``stock_entry.py``) and Department IR Operation draft-state check
  (``stock_entry.py:154``).

Skipped on purpose:
* (voucher_type, voucher_no, manufacturing_operation, loss_source_row,
  is_cancelled) — 5-column wide index; the (voucher_type, voucher_no,
  is_cancelled) prefix already narrows to a handful of rows, and the
  remaining post-filter is cheap.
* (docstatus, type) on Department IR — only one observed site, low impact.
* Per-row indexes on ``tabEmployee Loss Details`` /
  ``tabManually Book Loss Details`` — those tables are iterated as Python
  child rows after the parent load, never queried with a composite filter.
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
	"""Skip an index when one of its columns doesn't exist on the table.

	Custom apps occasionally trail behind in field rollout (e.g. a custom
	field added in one site but not yet migrated on another). Returning
	False here lets the patch log-and-skip rather than fail migrate.
	"""
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
			title=f"add_jewellery_perf_indexes: skip {index_name}",
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
		# Cancel-cascade and idempotency lookups by voucher across MOP Log.
		# Hot path: Employee IR cancel (Issue + Receive) bulk-flips
		# is_cancelled for every row of the voucher.
		(
			"tabMOP Log",
			"mop_voucher_cancel_idx",
			("voucher_type", "voucher_no", "is_cancelled"),
		),
		# Employee IR Issue lookup JOIN in
		# resolve_employee_ir_issue_voucher_for_receive: filters by
		# docstatus + type, orders by modified DESC. Adding ``modified`` to
		# the index lets the optimizer use it for the ORDER BY too.
		(
			"tabEmployee IR",
			"eir_docstatus_type_modified_idx",
			("docstatus", "type", "modified"),
		),
		# Employee IR Operation existence check and JOIN. ``parent`` is
		# Frappe-standard auto-indexed; the composite gives the optimizer
		# a covering path for (parent, manufacturing_operation) lookups.
		(
			"tabEmployee IR Operation",
			"eiro_parent_mop_idx",
			("parent", "manufacturing_operation"),
		),
		# Employee IR Operation draft-state validation in stock_entry.py.
		(
			"tabEmployee IR Operation",
			"eiro_mwo_docstatus_idx",
			("manufacturing_work_order", "docstatus"),
		),
		# Department IR Operation draft-state validation in stock_entry.py.
		(
			"tabDepartment IR Operation",
			"diro_mwo_docstatus_idx",
			("manufacturing_work_order", "docstatus"),
		),
	)
	for table, index_name, columns in specs:
		_add_index_if_missing(table, index_name, columns)
