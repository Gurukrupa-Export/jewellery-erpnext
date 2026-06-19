# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
#
# Registered in hooks.py as:
#   scheduler_events = {"cron": {"* * * * *": ["...scheduler.check_and_enqueue_eod_sync"]}}
#
# Kept deliberately lightweight — no MOP Log reads; only decides whether to enqueue.

import frappe
from frappe.utils import cint, get_datetime, getdate, now_datetime, nowdate

from .eod_lock import release_expired_eod_sync_lock, set_eod_sync_queued

_SYNC_FUNC = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.sync_mop_logs"


def check_and_enqueue_eod_sync():
	"""Per-minute scheduler: enqueue EOD sync when the configured time window fires.

	Guards:
	- Already running or queued → skip.
	- Lock window still active → skip.
	- Already completed today → skip.
	- Configured time (HH:MM) does not match current time (HH:MM) → skip.
	"""
	release_expired_eod_sync_lock()

	row = frappe.db.get_value(
		"MOP Settings",
		"MOP Settings",
		[
			"eod_sync_time",
			"eod_sync_running",
			"eod_sync_lock_until",
			"eod_sync_last_completed_on",
			"eod_sync_status",
			"eod_sync_time",
		],
		as_dict=True,
	)
	if not row:
		return

	if cint(row.eod_sync_running):
		return

	if row.eod_sync_status == "Queued":
		return

	if row.eod_sync_lock_until:
		if get_datetime(row.eod_sync_lock_until) > now_datetime():
			return

	if row.eod_sync_last_completed_on:
		if getdate(row.eod_sync_last_completed_on) == getdate(now_datetime()):
			return

	configured_time = row.eod_sync_time
	if not configured_time:
		return

	now = now_datetime()
	configured_hhmm = str(configured_time)[:5]  # "HH:MM" from "HH:MM:SS"
	current_hhmm = now.strftime("%H:%M")

	if configured_hhmm != current_hhmm:
		return

	# Create a MOP EOD Sync Log for this scheduler-triggered run
	try:
		sync_log = frappe.new_doc("MOP EOD Sync Log")
		sync_log.status = "Queued"
		sync_log.trigger_type = "Scheduler"
		sync_log.started_by = "Administrator"
		sync_log.posting_date = nowdate()
		sync_log.eod_sync_time = configured_time
		sync_log.mop_settings = "MOP Settings"
		sync_log.flags.ignore_permissions = True
		sync_log.insert()
		sync_log_name = sync_log.name

		frappe.db.set_value(
			"MOP Settings",
			"MOP Settings",
			"eod_sync_last_sync_log",
			sync_log_name,
		)
	except Exception:
		frappe.logger().exception(
			"MOP EOD Sync: failed to create MOP EOD Sync Log in scheduler"
		)
		sync_log_name = None

	set_eod_sync_queued(sync_log_name=sync_log_name)
	frappe.enqueue(
		_SYNC_FUNC,
		queue="long",
		timeout=7200,
		enqueue_after_commit=True,
		job_id="eod_sync",
		deduplicate=True,
		sync_log_name=sync_log_name,
	)
	frappe.logger().info(
		"MOP EOD Sync: enqueued at %s (configured=%s), sync_log=%s",
		current_hhmm,
		configured_hhmm,
		sync_log_name,
	)
