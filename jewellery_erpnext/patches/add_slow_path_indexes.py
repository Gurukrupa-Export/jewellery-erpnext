"""Index the lookups behind slow cancel, linked-document, Settle and EOD screens.

Measured on the KGGK UAT copy (49k Items, 57k Batches, 656k Serial and Batch Entries), with the
indexes built temporarily and dropped again: Settle planning over 15 real Material Requests went
from 13.5-14.7 s to 10.4-11.5 s on its own, and from 6.0-6.9 s to 2.9 s together with the Settle
Item-lookup change (#1710); the cancel dialog's linked-document lookup over 6 real Stock Entries
went from 13-17 s to 5.4-8.2 s. Plans and linked-document results were identical in every run.
Each index built in 1-3 s there; production tables are larger.

The 30-day production review (Oct 17 - Nov 15 2026) showed request averages of 30-100 s for
``get_submitted_linked_docs``, form cancel and ``settle_material_request``, and top slow query
shapes on Stock Entry Detail, Serial and Batch Bundle / Entry, Order Form joins and BOM routes.
Each index below serves one of those reads; none of the columns had a usable index.

SERIAL AND BATCH BUNDLE ``(voucher_no, voucher_type)``
	ERPNext v16's posting-datetime patch dropped the ``voucher_no`` index; lookups by voucher
	fell back to the low-selectivity ``voucher_type`` index. ERPNext restores it in v16.55.0
	(frappe/erpnext#64543) as ``frappe.db.add_index(..., ["voucher_no", "voucher_type"])``,
	which is named ``voucher_no_voucher_type_index`` and is skipped by name when it exists. This
	patch uses that exact name, so the upgrade finds it instead of building a duplicate.

SERIAL AND BATCH ENTRY ``(voucher_no, voucher_type)``
	``StockController.validate_reserved_batches`` reads it by voucher after every Stock Entry
	posts its ledger. Same index and name as kggk_uat's ``restore_missing_perf_indexes``.

STOCK ENTRY DETAIL ``(stock_entry, docstatus)``
	Frappe's cancel back-link check (``check_no_back_links_exist``) queries every Link field
	pointing at Stock Entry by value, including this custom one. Without an index each Stock
	Entry cancel scans the whole table.

STOCK ENTRY DETAIL ``manufacturing_operation(140)`` / ``custom_manufacturing_work_order(140)``
	gke_customization's fixture defines both as Small Text (a TEXT column), and the MOP, Serial
	Number Creator and EOD paths filter them with ``=`` / ``IN``. A TEXT index needs a prefix;
	these values are document names, far shorter than 140 characters, so the prefix is exact.

MATERIAL REQUEST ITEM ``(batch_no, parent)``
	Customer Gold Settle's ``_open_request_claims`` filters ``batch_no IN`` and joins on
	``parent``.

ITEM / QUOTATION ITEM / SALES ORDER ITEM ``(order_form_id, order_form_type)``
	The Order report joins on ``order_form_id``; linked-document lookups filter the Dynamic Link
	by id and type.

BOM ``(show_in_website, route(140))``
	Frappe's website router looks up every web-view doctype by ``route`` and
	``show_in_website = 1`` for unknown paths (``_find_matching_document_webview``); BOM is an
	ERPNext website generator, so bot and 404 traffic scanned all of tabBOM.

BATCH ``(item, custom_customer)`` / ``(custom_customer, item)``
	Settle's owner-batch finder (``snc._owner_batch_names``) filters ``item`` and
	``custom_customer``; its other-holdings fallback (``cg_settle._fetch_customer_other_holdings``)
	filters ``custom_customer`` with ``item LIKE '%-<colour>'``. Neither column had an index, so
	every Settle row scanned tabBatch. Both reads keep their ``ORDER BY creation``.

SERIAL AND BATCH ENTRY ``(serial_no, batch_no)``
	The customer-gold lineage walk (``customer_gold_trace._bundle_entries``) asks for
	``batch_no IN (...) OR (serial_no IN (...) AND batch_no empty)``. With no index leading with
	``serial_no`` the OR scanned the whole table; with it MariaDB merges two index lookups.

SERIAL AND BATCH BUNDLE ``(voucher_type, creation)`` / BOM ``(custom_creation_doctype, creation)``
	The cancel dialog (``get_submitted_linked_docs``) builds its Dynamic Link map with
	``SELECT DISTINCT <type> ... WHERE <type> IN (...) ORDER BY creation DESC``. Covering both
	columns answers that from the index alone. Frappe's ``ORDER BY`` still makes it read every
	matching index entry, so this narrows the cost rather than removing it. ERPNext v16.55 also
	drops the single-column Serial and Batch Bundle ``voucher_type`` index (frappe/erpnext#64543);
	this composite keeps the read off a full table scan after that upgrade.

STOCK ENTRY ``(repack_entry | source_stock_entry | outgoing_stock_entry, docstatus)``
	The same dialog, and the cancel back-link check, look Stock Entries up by these Link fields
	pointing back at the one being cancelled; each read scanned tabStock Entry.

WHY THESE SHAPES
	Frappe's schema sync drops a single-column index on a varchar column whose field has no
	``search_index``, never touches an index on a TEXT column, and leaves composite indexes
	alone. Every varchar index here is therefore composite, and the single-column ones are on
	TEXT columns. No field metadata changes. None leads with ``parent``, which could become the
	plan of Frappe's ``FOR UPDATE`` child-row load, and none is on a table whose unordered reads
	pick a reservation (Stock Reservation Entry is left out for that reason).

HOW IT RUNS
	Idempotent: an index with the same name, or any index already leading with the same
	columns, counts as present. A missing table column is logged and skipped. Each index is
	built online (``ALGORITHM=INPLACE, LOCK=NONE``) under a bounded ``lock_wait_timeout`` so a
	long open transaction cannot leave the ALTER queued (and every reader of the table queued
	behind it). A failed build is logged and the rest still run; call ``execute()`` again with
	``bench execute`` to retry. The time each build took is logged.
"""

import time

import frappe

#: Seconds an ALTER may wait for the table's metadata lock before giving up.
LOCK_WAIT_TIMEOUT = 60

TEXT_TYPES = frozenset({"text", "mediumtext", "longtext", "tinytext"})

#: (table, index name, ((column, prefix length or None), ...))
SPECS = (
	(
		"tabSerial and Batch Bundle",
		"voucher_no_voucher_type_index",
		(("voucher_no", None), ("voucher_type", None)),
	),
	(
		"tabSerial and Batch Entry",
		"sbe_voucher_no_idx",
		(("voucher_no", None), ("voucher_type", None)),
	),
	(
		"tabStock Entry Detail",
		"sed_stock_entry_idx",
		(("stock_entry", None), ("docstatus", None)),
	),
	(
		"tabStock Entry Detail",
		"sed_manufacturing_operation_idx",
		(("manufacturing_operation", 140),),
	),
	(
		"tabStock Entry Detail",
		"sed_manufacturing_work_order_idx",
		(("custom_manufacturing_work_order", 140),),
	),
	(
		"tabMaterial Request Item",
		"mri_batch_no_parent_idx",
		(("batch_no", None), ("parent", None)),
	),
	(
		"tabItem",
		"item_order_form_idx",
		(("order_form_id", None), ("order_form_type", None)),
	),
	(
		"tabQuotation Item",
		"qi_order_form_idx",
		(("order_form_id", None), ("order_form_type", None)),
	),
	(
		"tabSales Order Item",
		"soi_order_form_idx",
		(("order_form_id", None), ("order_form_type", None)),
	),
	(
		"tabBOM",
		"bom_website_route_idx",
		(("show_in_website", None), ("route", 140)),
	),
	(
		"tabBatch",
		"batch_item_customer_idx",
		(("item", None), ("custom_customer", None)),
	),
	(
		"tabBatch",
		"batch_customer_item_idx",
		(("custom_customer", None), ("item", None)),
	),
	(
		"tabSerial and Batch Entry",
		"sbe_serial_no_batch_no_idx",
		(("serial_no", None), ("batch_no", None)),
	),
	(
		"tabSerial and Batch Bundle",
		"sbb_voucher_type_creation_idx",
		(("voucher_type", None), ("creation", None)),
	),
	(
		"tabBOM",
		"bom_creation_doctype_creation_idx",
		(("custom_creation_doctype", None), ("creation", None)),
	),
	(
		"tabStock Entry",
		"se_repack_entry_idx",
		(("repack_entry", None), ("docstatus", None)),
	),
	(
		"tabStock Entry",
		"se_source_stock_entry_idx",
		(("source_stock_entry", None), ("docstatus", None)),
	),
	(
		"tabStock Entry",
		"se_outgoing_stock_entry_idx",
		(("outgoing_stock_entry", None), ("docstatus", None)),
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


def _column_types(table: str) -> dict:
	"""``{column: data_type}`` for ``table`` (``data_type`` is lower case, e.g. ``varchar``)."""
	rows = frappe.db.sql(
		"""
		SELECT column_name, data_type
		FROM information_schema.columns
		WHERE table_schema = DATABASE() AND table_name = %s
		""",
		(table,),
	)
	return {column: (data_type or "").lower() for column, data_type in rows}


def _column_sql(column: str, prefix, data_type: str) -> str:
	"""A prefix is only legal (and only needed) on a TEXT column; a varchar keeps the full value."""
	if prefix and data_type in TEXT_TYPES:
		return f"`{column}`({int(prefix)})"
	return f"`{column}`"


def _add_index_if_missing(table: str, index_name: str, spec: tuple) -> str:
	"""Build one index unless it is already there. Returns what happened, for the log."""
	columns = tuple(column for column, _prefix in spec)
	existing = _indexes(table)
	if index_name in existing:
		if existing[index_name] != columns:
			frappe.log_error(
				title=f"add_slow_path_indexes: {index_name} differs",
				message=(
					f"`{table}`.`{index_name}` exists on {existing[index_name]}, expected {columns}; "
					"left as it is."
				),
			)
		return "present"
	if any(cols[: len(columns)] == columns for cols in existing.values()):
		return "present"  # the same columns are already indexed under another name

	types = _column_types(table)
	missing = [column for column in columns if column not in types]
	if missing:
		frappe.log_error(
			title=f"add_slow_path_indexes: skip {index_name}",
			message=(
				f"Table `{table}` has no column {missing}; index not added. "
				"Re-run after the doctype is fully migrated."
			),
		)
		return "skipped"

	# Column names are hard-coded literals from SPECS -- no user input.
	col_list = ", ".join(
		_column_sql(column, prefix, types[column]) for column, prefix in spec
	)
	started = time.monotonic()
	try:
		frappe.db.sql_ddl(
			f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({col_list}), "
			"ALGORITHM=INPLACE, LOCK=NONE"
		)
	except Exception:
		frappe.log_error(
			title=f"add_slow_path_indexes: {index_name} not built",
			message=(
				f"Building `{table}`.`{index_name}` failed (often a metadata-lock wait over "
				f"{LOCK_WAIT_TIMEOUT}s behind an open transaction). Retry with "
				"`bench execute jewellery_erpnext.patches.add_slow_path_indexes.execute`.\n\n"
				f"{frappe.get_traceback()}"
			),
		)
		return "failed"
	frappe.logger("jewellery_erpnext").info(
		f"add_slow_path_indexes: built `{table}`.`{index_name}` in {time.monotonic() - started:.1f}s"
	)
	return "built"


def execute():
	previous = frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0]
	frappe.db.sql(f"SET SESSION lock_wait_timeout = {int(LOCK_WAIT_TIMEOUT)}")
	try:
		for table, index_name, spec in SPECS:
			_add_index_if_missing(table, index_name, spec)
	finally:
		frappe.db.sql(f"SET SESSION lock_wait_timeout = {int(previous)}")
