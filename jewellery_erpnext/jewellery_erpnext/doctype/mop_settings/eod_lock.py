# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

_SETTINGS = "MOP Settings"
_LOCK_HOURS = 2
_LOCK_MSG = (
	"EOD sync is in progress. You cannot proceed to make any transactions. "
	"Please contact your administrator or try again after 2 hours after the specified time."
)


def is_eod_sync_locked():
	"""Return True when EOD sync is running and the 2-hour lock window is still active.

	Uses a direct DB read (not cached doc) so the latest committed state is always seen.
	Returns False if the lock window has expired even when eod_sync_running is still 1
	(a crashed worker won't permanently lock the system).
	"""
	row = frappe.db.get_value(
		_SETTINGS,
		_SETTINGS,
		["eod_sync_running", "eod_sync_lock_until"],
		as_dict=True,
	)
	if not row or not cint(row.eod_sync_running):
		return False
	lock_until = row.eod_sync_lock_until
	if lock_until and get_datetime(lock_until) < now_datetime():
		return False
	return True


def set_eod_sync_running(sync_log_name=None):
	"""Mark EOD sync as running, set a 2-hour lock window, and commit immediately.

	The immediate commit ensures every concurrent DB connection sees the lock before
	the heavy processing starts. Optionally updates the MOP EOD Sync Log status.
	"""
	now = now_datetime()
	lock_until = add_to_date(now, hours=_LOCK_HOURS)
	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{
			"eod_sync_running": 1,
			"eod_sync_status": "Running",
			"eod_sync_started_on": now,
			"eod_sync_lock_until": lock_until,
			"eod_sync_message": (
				f"EOD sync is in progress. Transactions are blocked until "
				f"{lock_until.strftime('%Y-%m-%d %H:%M:%S')}."
			),
		},
	)
	if sync_log_name:
		frappe.db.set_value(
			"MOP EOD Sync Log",
			sync_log_name,
			{
				"status": "Running",
				"started_on": now,
			},
			update_modified=False,
		)
	frappe.db.commit()


def set_eod_sync_queued(sync_log_name=None):
	"""Mark EOD sync as queued (job enqueued but not yet started)."""
	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{
			"eod_sync_status": "Queued",
			"eod_sync_message": "EOD MOP Log Sync has been queued as a background job.",
		},
	)
	if sync_log_name:
		frappe.db.set_value(
			"MOP EOD Sync Log",
			sync_log_name,
			"status",
			"Queued",
			update_modified=False,
		)
	frappe.db.commit()


def release_eod_sync_lock(success=True, error_log_name=None, sync_log_name=None):
	"""Clear running state after EOD sync finishes (success or failure).

	Commits immediately so other connections stop seeing the lock.
	Optionally finalizes the MOP EOD Sync Log.
	"""
	now = now_datetime()
	values = {
		"eod_sync_running": 0,
		"eod_sync_status": "Completed" if success else "Failed",
		"eod_sync_lock_until": now,
	}
	if success:
		values["eod_sync_last_completed_on"] = now
		values[
			"eod_sync_message"
		] = f"EOD sync completed successfully on {now.strftime('%Y-%m-%d %H:%M:%S')}."
	else:
		values[
			"eod_sync_message"
		] = "EOD sync failed. Check the Last EOD Error Log for details."
		if error_log_name:
			values["eod_sync_last_error_log"] = error_log_name

	frappe.db.set_value(_SETTINGS, _SETTINGS, values)
	frappe.db.commit()


def release_expired_eod_sync_lock():
	"""Auto-release a stale lock when the 2-hour window has passed.

	Called every hour by the scheduler and at the top of each per-minute check.
	Safe to call when no lock is active — returns immediately.
	"""
	row = frappe.db.get_value(
		_SETTINGS,
		_SETTINGS,
		["eod_sync_running", "eod_sync_lock_until"],
		as_dict=True,
	)
	if not row or not cint(row.eod_sync_running):
		return
	lock_until = row.eod_sync_lock_until
	if not lock_until or get_datetime(lock_until) >= now_datetime():
		return

	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{
			"eod_sync_running": 0,
			"eod_sync_status": "Timeout Released",
			"eod_sync_message": (
				"EOD sync lock was automatically released after the configured 2-hour window. "
				"Please verify Error Log and pending draft Stock Entries before retrying."
			),
		},
	)
	frappe.db.commit()
	frappe.logger().warning(
		"MOP EOD Sync: expired lock auto-released (lock_until=%s)", lock_until
	)


def validate_not_eod_sync_locked(doc, method=None):
	"""Doc-event hook — blocks save/submit/cancel on protected doctypes during EOD sync.

	Bypassed when frappe.flags.in_eod_mop_sync is True so the sync process itself
	can create Stock Entries and update MOP Logs without being blocked.
	"""
	if getattr(frappe.flags, "in_eod_mop_sync", False):
		return
	if is_eod_sync_locked():
		frappe.throw(
			_(_LOCK_MSG),
			title=_("EOD Sync In Progress"),
		)
