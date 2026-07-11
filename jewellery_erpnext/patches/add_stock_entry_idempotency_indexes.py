"""Indexes for the submit-path idempotency guards on ``tabStock Entry`` (F-007).

Three "has this Stock Entry already been created for me?" guards run on the
submit path of Product Certification, Serial Number Creator and the Employee IR
main-slip injection, each an unindexed lookup that full-scans the whole
``tabStock Entry`` table (~236k rows on the production restore, ``type=ALL
key=NULL`` per EXPLAIN). Worse, each runs *inside* the submit transaction while
that transaction already holds the ``MAT-STE-`` naming-series row and its Bin
locks, so the scan directly lengthens the window every other Stock-Entry-
producing flow queues behind (an indirect contributor to the F-001 naming
contention). Indexing the guard columns drops each from a full scan to ~1 row
(PC / SNC) or <=269 rows (EIR), shortening the held-lock window.

Guard call sites (audited, no blind indexing):
* PC   -- ``product_certification.py`` ``get_value("Stock Entry",
  {product_certification: ..., docstatus: 1}, "name")``.
* SNC  -- ``serial_number_creator.py`` (JOIN se<->sed,
  ``custom_serial_number_creator = %s AND stock_entry_type='Repack'
  AND docstatus != 2``).
* EIR  -- ``main_slip_inject.py`` ``_existing_injection_se`` /
  ``_existing_injection_se_types`` (``employee_ir, custom_eir_operation_row,
  auto_created = 1, docstatus != 2``).

A separate module (not appended to ``add_jewellery_perf_indexes``) because that
patch has already run on existing sites and appending specs would not re-trigger
it -- the patch hash is tracked in ``tabPatch Log``.

Delivers the three still-absent candidates from ``context/index_candidate_report.md``;
the 6-candidate ``add_performance_indexes_from_error_log`` module referenced by an
older plan was never on disk. Pre-production gate: measure the submit-path write
cost on a copy site (``tabStock Entry`` already carries ~15 indexes; the three
new columns are low-churn, set once at SE creation, so overhead is expected small
but must be confirmed <=5% on a representative Material Transfer / Manufacture
submit before applying to production).
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

	Mirrors ``add_jewellery_perf_indexes``: custom fields occasionally trail in
	rollout, so log-and-skip rather than fail migrate.
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
			title=f"add_stock_entry_idempotency_indexes: skip {index_name}",
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
		# PC submit-path guard: product_certification is the selective column
		# (~1 SE per value); docstatus makes it covering for the docstatus<2 filter.
		(
			"tabStock Entry",
			"se_product_cert_idx",
			("product_certification", "docstatus"),
		),
		# SNC submit-path guard: custom_serial_number_creator is unique per SE.
		(
			"tabStock Entry",
			"se_snc_idx",
			("custom_serial_number_creator", "docstatus"),
		),
		# EIR injection guard: (employee_ir, custom_eir_operation_row) provides
		# selectivity (<=269 rows on employee_ir alone, narrowed by the op row);
		# auto_created, docstatus are low-cardinality trailing post-filters kept
		# so the guard's WHERE is fully covered.
		(
			"tabStock Entry",
			"se_eir_op_idx",
			("employee_ir", "custom_eir_operation_row", "auto_created", "docstatus"),
		),
	)
	for table, index_name, columns in specs:
		_add_index_if_missing(table, index_name, columns)
