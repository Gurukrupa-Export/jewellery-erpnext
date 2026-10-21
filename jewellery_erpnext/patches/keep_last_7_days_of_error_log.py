"""Keep only the last 7 days of Error Log, once.

On KGGK, ``tabError Log`` grew to tens of GB, mostly "Error Attaching File" rows for image
paths copied from GK without their files. The table is MyISAM in Frappe v16 (errors survive
rollbacks), so the daily ``run_log_clean_up`` DELETE locks the whole table while it runs and
never shrinks the file: the freed rows only show up as ``DATA_FREE``.

``clear_log_table`` is the code behind ``bench clear-log-table``. It copies the recent rows into
a new table, swaps it in and drops the old one, which gives the disk back once the copy is done.
On error it drops its copy and leaves the original table as it was.

This runs inside ``bench migrate``, which has lowered ``lock_wait_timeout`` to five minutes and
closed idle connections. Errors that other processes write while the copy runs land in the old
table and are dropped with it.

A housekeeping step must never block or hang a deploy:

* The copy needs free disk for the rows it keeps, and on a full disk MyISAM waits instead of
  failing. When those rows are estimated above ``MAX_COPY_BYTES``, nothing is done.
* A failure is printed to the migrate log and recorded with ``defer_insert``, because the table
  itself may be what is locked. Recording it can never raise.
* Nothing is retried in the background, where a live site would wait on the same locks. Every
  outcome other than success prints the command to run in a maintenance window instead.

Retention afterwards is unchanged: Log Settings keeps deciding what the daily clean-up removes.
"""

import contextlib

import frappe

DAYS = 7
MAX_COPY_BYTES = 5 * 1024**3


def execute():
	from frappe.core.doctype.log_settings.log_settings import clear_log_table

	manual = f'bench --site {frappe.local.site} clear-log-table --doctype "Error Log" --days {DAYS}'
	try:
		kept_bytes = _estimated_bytes_kept()
		if kept_bytes > MAX_COPY_BYTES:
			print(
				f"Error Log clean-up skipped: the last {DAYS} days are about "
				f"{kept_bytes / 1024**3:.1f} GB, too much to copy during a deploy. "
				f"Run it in a maintenance window: {manual}"
			)
			return

		clear_log_table("Error Log", days=DAYS)
		print(f"Error Log clean-up done: kept the last {DAYS} days, dropped the rest.")
	except Exception:
		frappe.db.rollback()
		print(
			f"Error Log clean-up failed and changed nothing. Run it in a maintenance window: {manual}"
		)
		print(frappe.get_traceback())
		with contextlib.suppress(Exception):
			frappe.log_error(title="Error Log clean-up patch failed", defer_insert=True)


def _estimated_bytes_kept():
	"""Rows newer than the cutoff times the table's average row length (data only)."""
	if frappe.db.db_type != "mariadb":
		return 0

	# The same cutoff clear_log_table copies with; a range scan on the creation index.
	rows = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabError Log` WHERE `creation` > NOW() - INTERVAL %s DAY",
		(DAYS,),
	)[0][0]
	average = frappe.db.sql(
		"""SELECT AVG_ROW_LENGTH FROM information_schema.TABLES
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabError Log'"""
	)
	return rows * ((average and average[0][0]) or 0)
