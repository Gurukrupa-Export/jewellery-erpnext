"""Build the four read indexes the locked stock-posting path relies on, online and idempotently.

WHY THEY ARE MISSING
--------------------
``add_make_receive_entry_indexes`` (``mop_balance_idx``) and ``add_jewellery_perf_indexes``
(``mop_voucher_cancel_idx``) are in ``tabPatch Log`` but never ran: ``bench install-app`` calls
``frappe.installer.set_all_patches_as_completed``, which logs every ``patches.txt`` line as done
without executing it, and migrate never re-runs a logged patch. On the 4 Nov copy the seven JE
patches present at install were all logged within 42 ms, and production (17 Nov) has neither
index. Nothing ever indexed ``voucher_no`` on Serial and Batch Bundle / Entry.

WHAT THEY SERVE
---------------
Plain reads made while the naming-series row and the Bins are already locked, each a full walk
today (about 1.4-2.5 s per call on the copy):

* ``mop_balance_idx`` -- MOP Log by ``manufacturing_operation`` + ``is_cancelled``:
  ``recalculate_manufacturing_operation_weights`` (every MOP Log save),
  ``get_current_mop_balance_rows``, ``get_last_mop_index`` and the clone-source reads.
* ``mop_voucher_cancel_idx`` -- MOP Log by voucher: the Stock Entry bridge prefetch, the
  DISTINCT-MOP reads of the Employee / Department IR cancel, and the cancel UPDATEs, which
  without it scan, and under REPEATABLE READ lock, the whole table.
* ``voucher_no_voucher_type_index`` on Serial and Batch Bundle -- ``update_parent_batch_id`` and
  ERPNext ``get_batchwise_qty``. The name and columns are exactly those of ERPNext #64543
  (``on_doctype_update`` -> ``frappe.db.add_index(..., ["voucher_no", "voucher_type"])``, first
  released in v16.55.0). ``add_index`` checks the name only, so that upgrade becomes a no-op
  instead of a duplicate index.
* ``voucher_no_voucher_type_index`` on Serial and Batch Entry -- ERPNext
  ``StockController.validate_reserved_batches`` (stock reservation is on). Upstream has no such
  index; the name follows Frappe's ``add_index`` convention for the same reason.

None of them can change a result or a locking read: no locking read filters on their leading
column, JE has no index hints, and the ordered reads have no ties on the copy.

HOW
---
* Presence is decided by column list, read from ``information_schema``: an index of this name
  with exactly these columns, or any index whose leading columns are exactly these, counts.
* An index of this name with other columns is logged and left alone. The patch never drops or
  rebuilds an index it does not own; the only drop is an app-owned LEGACY name (the
  ``kggk_uat`` names) that has an exact twin under the upstream name, and a LEGACY index with
  no twin is renamed instead of rebuilt.
* ``ALGORITHM=INPLACE, LOCK=NONE``: writes continue while an index builds. There is no fallback
  to a table copy; an unsupported or failed build is logged as ``failed``.
* The session ``lock_wait_timeout`` (the metadata-lock wait; 86400 s on the copy) is capped for
  the DDL -- ``DEFAULT_LOCK_WAIT`` s, or site_config ``je_index_ddl_lock_wait`` (clamped to
  1-60) -- and ``max_statement_time`` is lifted, then both are restored. A metadata-lock
  timeout is reported as ``busy`` and retried by the next migrate.
* Non-fatal: every failure is logged (Error Log + migrate output) and the other indexes still
  build. Runs as a patch and again from ``after_migrate`` (hooks.py), so a site where the patch
  was only marked done, or where a build was ``busy``, gets the index on its next migrate.
  A second run issues no DDL.
* Every spec is composite, so Frappe's schema sync keeps it: ``MariaDBTable.alter`` drops only
  indexes that ``get_column_index`` returns, and that skips any index with a second column
  (verified at frappe 3d1284c5 and 4515014c).

OPS
---
Dry run, no DDL and no Error Log rows (prints what ``ensure`` would do, table sizes and open
transactions)::

    bench --site <site> execute jewellery_erpnext.patches.ensure_read_path_indexes.report

Build in a quiet window::

    bench --site <site> execute jewellery_erpnext.patches.ensure_read_path_indexes.ensure \
        --kwargs "{'lock_wait': 5}"

Rollback (no data changes): ``ALTER TABLE `<table>` DROP INDEX `<name>`, ALGORITHM=INPLACE,
LOCK=NONE``.
"""

from time import monotonic

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
	# The original JE spec: every voucher read also filters voucher_type.
	(
		"tabMOP Log",
		"mop_voucher_cancel_idx",
		("voucher_type", "voucher_no", "is_cancelled"),
	),
	# = ERPNext #64543, name and columns.
	(
		"tabSerial and Batch Bundle",
		"voucher_no_voucher_type_index",
		("voucher_no", "voucher_type"),
	),
	# Frappe add_index naming convention.
	(
		"tabSerial and Batch Entry",
		"voucher_no_voucher_type_index",
		("voucher_no", "voucher_type"),
	),
)

# App-owned names that kggk_uat's restore_missing_perf_indexes gave the same columns. No-op on
# a site that never had them.
LEGACY = {
	("tabSerial and Batch Bundle", "voucher_no_voucher_type_index"): (
		"sbb_voucher_no_idx",
	),
	("tabSerial and Batch Entry", "voucher_no_voucher_type_index"): (
		"sbe_voucher_no_idx",
	),
}

TEXT_TYPES = frozenset(
	(
		"tinytext",
		"text",
		"mediumtext",
		"longtext",
		"tinyblob",
		"blob",
		"mediumblob",
		"longblob",
	)
)
TEXT_PREFIX = 140

DEFAULT_LOCK_WAIT = 5  # seconds
MAX_LOCK_WAIT = 60
LOCK_WAIT_CONF_KEY = "je_index_ddl_lock_wait"
ONLINE = "ALGORITHM=INPLACE, LOCK=NONE"
LOG_PREFIX = "ensure_read_path_indexes"

# A status in this set is worth printing even on a quiet after_migrate.
_NOTEWORTHY = {
	"built",
	"renamed",
	"dropped-duplicate",
	"differs",
	"missing-columns",
	"busy",
	"failed",
}
_DRY_RUN_STATUS = {
	"built": "would-build",
	"renamed": "would-rename",
	"dropped-duplicate": "would-drop-duplicate",
}


def _indexes(table):
	"""``{index_name: ((column, sub_part), ...)}`` for ``table``, columns in index order."""
	rows = frappe.db.sql(
		"""
		SELECT index_name, column_name, sub_part
		FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name = %s AND index_type <> 'FULLTEXT'
		ORDER BY index_name, seq_in_index
		""",
		(table,),
	)
	indexes = {}
	for index_name, column_name, sub_part in rows:
		indexes.setdefault(index_name, []).append(
			(column_name, int(sub_part) if sub_part is not None else None)
		)
	return {name: tuple(parts) for name, parts in indexes.items()}


def _column_types(table):
	"""``{column: data_type}`` for ``table`` (empty when the table does not exist)."""
	rows = frappe.db.sql(
		"""
		SELECT column_name, data_type
		FROM information_schema.columns
		WHERE table_schema = DATABASE() AND table_name = %s
		""",
		(table,),
	)
	return {column: (data_type or "").lower() for column, data_type in rows}


def resolve_lock_wait(lock_wait=None):
	"""Seconds the DDL may wait for a metadata lock: the argument, else site_config, else 5."""
	value = lock_wait if lock_wait is not None else frappe.conf.get(LOCK_WAIT_CONF_KEY)
	try:
		value = int(value)
	except (TypeError, ValueError):
		value = DEFAULT_LOCK_WAIT
	return max(1, min(value, MAX_LOCK_WAIT))


def _ddl(sql, lock_wait):
	"""Run one online DDL with a bounded metadata-lock wait, restoring the session after."""
	prev_lock_wait, prev_statement_time = frappe.db.sql(
		"SELECT @@SESSION.lock_wait_timeout, @@SESSION.max_statement_time"
	)[0]
	frappe.db.sql(
		"SET SESSION lock_wait_timeout = %s, max_statement_time = 0", (lock_wait,)
	)
	try:
		frappe.db.sql_ddl(sql)  # commits first
	finally:
		try:
			frappe.db.sql(
				"SET SESSION lock_wait_timeout = %s, max_statement_time = %s",
				(prev_lock_wait, prev_statement_time),
			)
		except Exception:
			# Never mask the DDL's own error; a connection that cannot run SET is already gone.
			frappe.log_error(
				title=f"{LOG_PREFIX}: session settings not restored",
				message=frappe.get_traceback(),
			)


def _note(row, title, message, dry_run):
	row["detail"] = message
	if not dry_run:
		frappe.log_error(title=f"{LOG_PREFIX}: {title}", message=message)


def _apply(row, sql, status, lock_wait, dry_run):
	row["sql"] = sql
	if dry_run:
		row["status"] = _DRY_RUN_STATUS[status]
		return
	started = monotonic()
	_ddl(sql, lock_wait)
	row["seconds"] = round(monotonic() - started, 1)
	row["status"] = status


def _ensure_one(row, lock_wait, dry_run):
	table, name, columns = row["table"], row["index"], tuple(row["columns"])

	types = _column_types(table)
	missing = [c for c in columns if c not in types]
	if missing:
		row["status"] = "missing-columns"
		_note(
			row,
			f"skip {name}",
			f"`{table}` has no column(s) {missing}; `{name}` not built. "
			"It is retried on the next migrate.",
			dry_run,
		)
		return

	wanted = tuple(
		(c, TEXT_PREFIX if types[c] in TEXT_TYPES else None) for c in columns
	)
	existing = _indexes(table)
	legacy = [n for n in LEGACY.get((table, name), ()) if existing.get(n) == wanted]

	if name in existing:
		if existing[name] != wanted:
			row["status"] = "differs"
			_note(
				row,
				f"{name} differs",
				f"`{table}`.`{name}` is on {existing[name]}, expected {wanted}; left as it is.",
				dry_run,
			)
			return
		row["status"] = "present"
		for legacy_name in legacy:
			_apply(
				row,
				f"ALTER TABLE `{table}` DROP INDEX `{legacy_name}`, {ONLINE}",
				"dropped-duplicate",
				lock_wait,
				dry_run,
			)
		return

	if legacy:
		_apply(
			row,
			f"ALTER TABLE `{table}` RENAME INDEX `{legacy[0]}` TO `{name}`, {ONLINE}",
			"renamed",
			lock_wait,
			dry_run,
		)
		return

	equivalent = sorted(
		n for n, parts in existing.items() if parts[: len(wanted)] == wanted
	)
	if equivalent:
		row["status"] = "equivalent"
		row["detail"] = f"served by {equivalent}"
		return

	parts = ", ".join(f"`{c}`" + (f"({n})" if n else "") for c, n in wanted)
	_apply(
		row,
		f"ALTER TABLE `{table}` ADD INDEX `{name}` ({parts}), {ONLINE}",
		"built",
		lock_wait,
		dry_run,
	)


def ensure(lock_wait=None, verbose=True, dry_run=False):
	"""Make sure every index in ``SPECS`` exists. Returns one report row per spec.

	Never raises for a single index: a metadata-lock timeout is ``busy``, any other error is
	``failed``, and both are logged while the remaining specs still run. With ``dry_run`` it
	issues no DDL and writes no Error Log row; statuses read ``would-build`` and so on.
	"""
	lock_wait = resolve_lock_wait(lock_wait)
	report = []
	if frappe.db.db_type != "mariadb":
		return report

	for table, name, columns in SPECS:
		row = {
			"table": table,
			"index": name,
			"columns": list(columns),
			"status": None,
			"sql": None,
			"seconds": None,
			"detail": None,
		}
		report.append(row)
		try:
			_ensure_one(row, lock_wait, dry_run)
		except frappe.QueryTimeoutError:
			row["status"] = "busy"
			_note(
				row,
				f"{name} busy",
				f"`{table}`: metadata lock not granted within {lock_wait} s; nothing changed. "
				"The next migrate retries, or run ensure in a quiet window.\n"
				+ frappe.get_traceback(),
				dry_run,
			)
		except Exception:
			row["status"] = "failed"
			_note(
				row,
				f"{name} failed",
				f"`{table}`: {row['sql'] or 'inspection'} failed; nothing retried (no table-copy "
				"fallback).\n" + frappe.get_traceback(),
				dry_run,
			)

	if verbose or any(r["status"] in _NOTEWORTHY for r in report):
		_print(report, lock_wait, dry_run)
	return report


def _print(report, lock_wait, dry_run):
	mode = "dry run" if dry_run else f"metadata-lock wait {lock_wait} s"
	print(f"{LOG_PREFIX} ({mode}):")
	for r in report:
		line = f"  {r['table']}.{r['index']}: {r['status']}"
		if r["seconds"] is not None:
			line += f" in {r['seconds']} s"
		if r["status"] in ("busy", "failed", "differs", "missing-columns"):
			line += " -- see Error Log" if not dry_run else f" -- {r['detail']}"
		print(line)


def _table_sizes():
	names = sorted({table for table, _name, _cols in SPECS})
	rows = frappe.db.sql(
		f"""
		SELECT table_name, table_rows,
			ROUND(data_length / 1048576) AS data_mb,
			ROUND(index_length / 1048576) AS index_mb
		FROM information_schema.tables
		WHERE table_schema = DATABASE() AND table_name IN ({", ".join(["%s"] * len(names))})
		""",
		tuple(names),
		as_dict=True,
	)
	return [dict(r) for r in rows]


def _open_transactions(min_seconds=30):
	"""Transactions older than ``min_seconds``: each holds a metadata lock a build must wait for.

	Needs the PROCESS privilege; reported as unavailable without it.
	"""
	try:
		rows = frappe.db.sql(
			"""
			SELECT trx_mysql_thread_id AS thread_id, trx_state AS state,
				TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_s,
				LEFT(trx_query, 200) AS query
			FROM information_schema.innodb_trx
			WHERE trx_started < NOW() - INTERVAL %s SECOND
			ORDER BY trx_started
			""",
			(min_seconds,),
			as_dict=True,
		)
	except Exception as e:
		return f"unavailable: {e}"
	return [dict(r) for r in rows]


def report(lock_wait=None):
	"""Dry run for ops: what ``ensure`` would do, plus the rollout checks. Issues no DDL."""
	server = frappe.db.sql(
		"""
		SELECT @@version AS version,
			@@SESSION.lock_wait_timeout AS session_lock_wait_timeout,
			@@innodb_online_alter_log_max_size AS innodb_online_alter_log_max_size
		""",
		as_dict=True,
	)
	return {
		"indexes": ensure(lock_wait=lock_wait, verbose=True, dry_run=True),
		"lock_wait": resolve_lock_wait(lock_wait),
		"tables": _table_sizes(),
		"open_transactions_over_30s": _open_transactions(),
		"server": dict(server[0]) if server else {},
	}


def _run_non_fatal(where):
	try:
		ensure(verbose=where == "patch")
	except Exception:
		frappe.log_error(
			title=f"{where}: read path indexes", message=frappe.get_traceback()
		)
		# Printed as well: migrate output is what a deploy actually shows anyone.
		print(
			"WARNING: the read path indexes could not be checked. The migrate itself "
			f'succeeded; see Error Log "{where}: read path indexes". Re-run with: '
			"bench --site <site> execute jewellery_erpnext.patches.ensure_read_path_indexes.ensure"
		)


def execute():
	_run_non_fatal("patch")


def after_migrate():
	"""Wired from hooks.py. Re-checks on every migrate; quiet unless something changed or failed."""
	_run_non_fatal("after_migrate")
