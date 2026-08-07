# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

_SETTINGS = "MOP Settings"

# THE single source of truth for how long an EOD run may hold the lock.
#
# This one number drives three things that used to be three separate literals: the lock
# window written to eod_sync_lock_until, the RQ job timeout at every enqueue site, and the
# wording of the two user-facing messages below. They drifted apart before -- worse, three
# real JobTimeoutExceptions in worker.log fired at 1500s (the `long` queue default) because
# one invocation path never passed a timeout at all.
#
# 4 hours is a CEILING, not an expectation. The lock blocks before_save/before_submit/
# before_cancel on Employee IR, Department IR, MOP Log, Stock Entry and Stock Reconciliation
# for its whole duration, so a run that stops making progress must release it early rather
# than sit on it -- that is what the soft deadline in mop_eod_sync does. Keep the scheduled
# run off-hours.
_LOCK_HOURS = 4
_LOCK_SECONDS = _LOCK_HOURS * 60 * 60

# Soft deadline: stop STARTING new work this many minutes into a run, then finish cleanly
# and release the lock. Must stay comfortably inside _LOCK_HOURS so the graceful stop always
# wins the race against the hard RQ kill.
_SOFT_DEADLINE_MINUTES = 200

_LOCK_MSG = (
	"EOD sync is in progress. You cannot proceed to make any transactions. "
	f"Please contact your administrator or try again after {_LOCK_HOURS} hours after the "
	"specified time."
)


def is_eod_sync_locked():
	"""Return True when EOD sync is running and the lock window is still active.

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
	"""Mark EOD sync as running, open the lock window, and commit immediately.

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


def ensure_eod_sync_lock_released(sync_log_name=None):
	"""Last-resort net: clear the lock if it is somehow STILL held as the run unwinds.

	Called from ``sync_mop_logs``' ``finally``, so it covers every exit path -- including
	the one that actually bit on 2026-08-05, where the failure handler itself raised on a
	dead connection and skipped its own ``release_eod_sync_lock``, and the documented case
	of a ``JobTimeoutException`` firing *inside* the recovery handler.

	Checks committed state rather than tracking a flag through three release sites, so it
	is self-correcting and a no-op on the normal success path (which already set
	``eod_sync_running=0``). Deliberately does NOT stamp ``eod_sync_last_completed_on``:
	reaching here means the run did not finish cleanly, and marking the day complete would
	make the scheduler skip it.

	Returns True when it had to intervene.
	"""

	def _still_locked():
		return cint(frappe.db.get_value(_SETTINGS, _SETTINGS, "eod_sync_running"))

	try:
		if not _still_locked():
			return False
	except Exception:
		# The connection is probably still dead. Try once more with a fresh one -- a
		# restarted MariaDB is usually back within seconds, and by the time the run
		# unwinds it often accepts connections again.
		try:
			frappe.db.connect()
			if not _still_locked():
				return False
		except Exception:
			frappe.logger().exception(
				"MOP EOD Sync: could not read lock state during final cleanup, even after "
				"reconnecting. The hourly release_expired_eod_sync_lock job is the backstop."
			)
			return False

	message = (
		"EOD sync ended without releasing its lock (the run was cut short before its "
		"handler finished). The lock was cleared during final cleanup — verify the Error "
		"Log and any pending draft Stock Entries before retrying."
	)
	try:
		frappe.db.set_value(
			_SETTINGS,
			_SETTINGS,
			{
				"eod_sync_running": 0,
				"eod_sync_status": "Failed",
				"eod_sync_lock_until": now_datetime(),
				"eod_sync_message": message,
			},
		)
		if sync_log_name and frappe.db.get_value(
			"MOP EOD Sync Log", sync_log_name, "status"
		) in ("Queued", "Running"):
			frappe.db.set_value(
				"MOP EOD Sync Log",
				sync_log_name,
				{
					"status": "Failed",
					"completed_on": now_datetime(),
					"progress_message": message,
				},
				update_modified=False,
			)
		frappe.db.commit()
	except Exception:
		frappe.logger().exception(
			"MOP EOD Sync: FINAL cleanup could not release the EOD sync lock. It will be "
			"cleared by release_expired_eod_sync_lock once eod_sync_lock_until passes."
		)
		return False

	frappe.logger().warning(
		"MOP EOD Sync: lock released by final cleanup — the run exited without releasing "
		"it (sync_log=%s).",
		sync_log_name,
	)
	return True


def release_expired_eod_sync_lock():
	"""Auto-release a stale lock when the lock window has passed.

	Called every hour by the scheduler and at the top of each per-minute check.
	Safe to call when no lock is active — returns immediately.
	"""
	row = frappe.db.get_value(
		_SETTINGS,
		_SETTINGS,
		["eod_sync_running", "eod_sync_lock_until", "eod_sync_last_sync_log"],
		as_dict=True,
	)
	if not row or not cint(row.eod_sync_running):
		return
	lock_until = row.eod_sync_lock_until
	if not lock_until or get_datetime(lock_until) >= now_datetime():
		return

	message = (
		f"EOD sync lock was automatically released after the configured {_LOCK_HOURS}-hour "
		"window. "
		"Please verify Error Log and pending draft Stock Entries before retrying."
	)
	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{
			"eod_sync_running": 0,
			"eod_sync_status": "Timeout Released",
			"eod_sync_message": message,
		},
	)

	# Carry the timeout onto the run's own log. Without this the Sync Log stays at
	# "Running" forever and mop_eod_sync_log.js keeps polling it every 5 seconds.
	sync_log = row.eod_sync_last_sync_log
	if sync_log and frappe.db.get_value("MOP EOD Sync Log", sync_log, "status") in (
		"Queued",
		"Running",
	):
		frappe.db.set_value(
			"MOP EOD Sync Log",
			sync_log,
			{
				"status": "Timeout Released",
				"completed_on": now_datetime(),
				"progress_message": message,
			},
			update_modified=False,
		)

	frappe.db.commit()
	frappe.logger().warning(
		"MOP EOD Sync: expired lock auto-released (lock_until=%s, sync_log=%s)",
		lock_until,
		sync_log,
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
