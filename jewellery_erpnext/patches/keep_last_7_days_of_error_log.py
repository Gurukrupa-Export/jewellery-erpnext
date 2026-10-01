"""Keep only the last 7 days of Error Log, once.

On KGGK, ``tabError Log`` grew to tens of GB, mostly "Error Attaching File" rows for image
paths copied from GK without their files. The table is MyISAM in Frappe v16 (errors survive
rollbacks), so the daily ``run_log_clean_up`` DELETE locks the whole table while it runs and
never shrinks the file: the freed rows only show up as ``DATA_FREE``.

``clear_log_table`` is the code behind ``bench clear-log-table``. It copies the recent rows into
a new table, swaps it in and drops the old one, which gives the disk back at once. On error it
drops its copy and leaves the original table as it was.

This runs inside ``bench migrate``. Frappe has already lowered ``lock_wait_timeout`` to five
minutes and closed idle connections, and errors raised in maintenance mode are deferred, so the
swap does not race users. A housekeeping step must never block a deploy, so a failure is logged
and the same call is queued once on the long queue. If Redis is unreachable during migrate,
``frappe.enqueue`` runs that retry immediately instead; it is caught all the same.

Retention afterwards is unchanged: Log Settings keeps deciding what the daily clean-up removes.
"""

import frappe

DAYS = 7
CLEAR_LOG_TABLE = "frappe.core.doctype.log_settings.log_settings.clear_log_table"


def execute():
	from frappe.core.doctype.log_settings.log_settings import clear_log_table

	try:
		clear_log_table("Error Log", days=DAYS)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="Error Log clean-up patch failed; retrying in background"
		)
		_queue_retry()


def _queue_retry():
	try:
		frappe.enqueue(
			CLEAR_LOG_TABLE,
			queue="long",
			timeout=60 * 60,
			job_id="error-log-keep-7-days",
			deduplicate=True,
			doctype="Error Log",
			days=DAYS,
		)
	except Exception:
		frappe.log_error(title="Error Log clean-up retry could not be queued")
