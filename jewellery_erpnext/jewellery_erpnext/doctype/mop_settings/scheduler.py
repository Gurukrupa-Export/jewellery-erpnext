# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
#
# Registered in hooks.py as:
#   scheduler_events = {"cron": {"* * * * *": ["...scheduler.check_and_enqueue_eod_sync"]}}
#
# Kept deliberately lightweight — no MOP Log reads; only decides whether to enqueue.

from datetime import datetime, time, timedelta

import frappe
from frappe.utils import cint, get_datetime, get_time, getdate, now_datetime, nowdate

from .eod_lock import _LOCK_SECONDS, release_expired_eod_sync_lock, set_eod_sync_queued

_SYNC_FUNC = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.sync_mop_logs"


def _parse_time_of_day(value):
	"""Return a datetime.time for the configured EOD time.

	``frappe.db.get_value`` hands a Time field back as a ``datetime.timedelta``;
	``str(timedelta(hours=9))`` is ``"9:00:00"`` (no leading zero), so the old
	``str(value)[:5]`` slice never matched strftime "09:00" for hours < 10. Parse
	robustly from timedelta / time / "HH:MM:SS" string instead.
	"""
	if isinstance(value, timedelta):
		total = int(value.total_seconds())
		return time(
			hour=(total // 3600) % 24, minute=(total % 3600) // 60, second=total % 60
		)
	return get_time(value)


# Statuses that mean "the run was cut short by something outside the sync" -- an OOM-killed
# or restarted DB server, an RQ job timeout, a stranded lock the hourly job had to clear.
# Retrying these the same day is worth a second attempt; anything else is not.
_RETRYABLE_SYNC_LOG_STATUSES = ("Timeout Released", "Failed")

# Hard ceiling on Scheduler attempts per day. The heavy job must never re-fire on every
# per-minute tick, so one retry is the whole allowance: attempt, retry, then wait for
# tomorrow (or the manual button).
_MAX_SCHEDULER_ATTEMPTS_PER_DAY = 2


def _scheduler_already_attempted_today(now):
	"""True when today's Scheduler run is done and must not be re-enqueued.

	The once-per-day attempt guard keys off the Sync Log (created at enqueue time) rather
	than ``eod_sync_last_completed_on`` (set only on success), so a run does not re-fire
	the heavy job on every subsequent tick. Manual runs are excluded, leaving the manual
	button free for same-day retries.

	**One retry is allowed for infrastructure faults.** The guard used to treat *any*
	Sync Log for today as "attempted", which could not tell "failed deterministically,
	retrying is pointless" from "the DB server was killed, retrying would work fine". On
	2026-08-05 MariaDB was OOM-killed mid-run at 19:06 and the day was simply forfeited --
	no retry ever fired. Now a log left in ``Timeout Released`` / ``Failed`` does not block
	a second attempt, while ``_MAX_SCHEDULER_ATTEMPTS_PER_DAY`` keeps the anti-thrash
	guarantee the original guard was written for.
	"""
	logs = frappe.get_all(
		"MOP EOD Sync Log",
		filters={"trigger_type": "Scheduler", "posting_date": getdate(now)},
		fields=["status"],
	)
	if not logs:
		return False
	if len(logs) >= _MAX_SCHEDULER_ATTEMPTS_PER_DAY:
		return True
	# Block while a run is still in flight, or once one has genuinely settled. Only a
	# cut-short run leaves the door open for the retry.
	return not all(log.status in _RETRYABLE_SYNC_LOG_STATUSES for log in logs)


def check_and_enqueue_eod_sync():
	"""Per-minute scheduler: enqueue EOD sync once per day at/after the configured time.

	Guards:
	- Already running or queued → skip.
	- Lock window still active → skip.
	- Already completed today → skip.
	- Current time has not yet reached the configured time today → skip.
	- A Scheduler run was already enqueued today → skip (once-per-day, with catch-up).
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

	# "Due" check with catch-up: fire on the first scheduler tick at or after the
	# configured time-of-day today, not only during the exact configured minute. A
	# missed minute (tick drift, worker lag, restart, downtime) no longer skips the
	# whole day.
	configured_today = datetime.combine(now.date(), _parse_time_of_day(configured_time))
	if now < configured_today:
		return

	# Once-per-day attempt guard: a Scheduler-triggered log for today means this day's
	# run was already enqueued — skip so a failed/timed-out run does not re-fire every tick.
	if _scheduler_already_attempted_today(now):
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
		timeout=_LOCK_SECONDS,
		enqueue_after_commit=True,
		job_id="eod_sync",
		deduplicate=True,
		sync_log_name=sync_log_name,
	)
	frappe.logger().info(
		"MOP EOD Sync: enqueued at %s (configured=%s), sync_log=%s",
		now.strftime("%H:%M"),
		configured_today.strftime("%H:%M"),
		sync_log_name,
	)
