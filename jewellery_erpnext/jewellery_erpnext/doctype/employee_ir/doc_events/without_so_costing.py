# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Working time and worker cost for Finding Manufacturing - Without SO work orders.

Only work orders flagged ``is_without_so`` (raised from a Manufacturing Plan in that mode) are
touched; every other operation keeps the standard timer and costing untouched.

* **Working Hours** = Employee IR Receive To Time - Issue From Time, taken literally: the
  check-out / check-in deduction ``calculation_time_log`` applies elsewhere is not used. The
  operation's hours are the sum over its closed time logs, stored as ``total_working_hours``.
* **Worker Cost** = Working Hours x the Workstation Hour Rate of the receiving employee's
  Workstation (``Workstation.employee``, the same lookup the manufacturing-entry operating cost
  uses), stored on the operation as ``worker_cost``.
* **Batch Worker Cost** -- ``Batch.custom_worker_cost`` is the work order's worker cost, and
  ``Batch.custom_total_cost`` = Batch Rate (``custom_metal_rate``) + Worker Cost, as the spec
  states it. ``apply_worker_cost_to_batch`` stamps them on the batch the work order produces.

Employee IR saves the operation with ``flags.ignore_validation``, so the operation's own
``validate_time_logs`` never runs on that path; the values here are written explicitly.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import flt, time_diff, time_diff_in_hours


def is_without_so_mwo(mwo):
	return bool(
		mwo and frappe.db.get_value("Manufacturing Work Order", mwo, "is_without_so")
	)


def get_workstation_hour_rate(employee):
	"""Hour Rate of the employee's Workstation, or 0 when the employee has none."""
	if not employee:
		return 0
	return flt(frappe.db.get_value("Workstation", {"employee": employee}, "hour_rate"))


def apply_without_so_working_time(mop, row, employee):
	"""Recompute one closed time log and the operation's hours and worker cost.

	``mop`` is the Manufacturing Operation document being saved by Employee IR Receive and
	``row`` the time log it just closed. Called right after ``calculation_time_log`` so the
	literal To - From value replaces its check-in-adjusted one."""
	if not (row.from_time and row.to_time):
		return

	row.time_in_mins = time_diff_in_hours(row.to_time, row.from_time) * 60
	duration = time_diff(row.to_time, row.from_time)
	# H:MM, without the seconds and microseconds a raw timedelta string carries.
	row.time_in_hour = str(duration - timedelta(microseconds=duration.microseconds))[
		:-3
	]

	total_minutes = sum(flt(log.time_in_mins) for log in mop.time_logs if log.to_time)
	hours = flt(total_minutes / 60, 4)
	hour_rate = get_workstation_hour_rate(employee)
	if not hour_rate and employee:
		frappe.msgprint(
			_(
				"No Workstation Hour Rate found for employee {0}; Worker Cost is 0 for {1}."
			).format(frappe.bold(employee), mop.name),
			indicator="orange",
		)

	mop.total_minutes = total_minutes
	mop.total_working_hours = hours
	mop.workstation_hour_rate = hour_rate
	mop.worker_cost = flt(hours * hour_rate, 2)


def reset_without_so_costing_on_receive_cancel(eir):
	"""Undo what a cancelled Receive did to its operations' timer and worker cost.

	The time log that Receive closed is REOPENED -- its To Time and durations cleared, its From
	Time (the Issue time) kept -- so the amended Receive closes it again and the operation's
	working time still runs from the Issue, exactly as a Receive that was never cancelled.
	The standard Receive cancel leaves the log closed, which is what left an amended Receive
	with no open timer to close and the worker cost at 0."""
	for row in eir.employee_ir_operations:
		if not is_without_so_mwo(row.manufacturing_work_order):
			continue
		last_closed = frappe.db.get_value(
			"Manufacturing Operation Time Log",
			{"parent": row.manufacturing_operation, "to_time": ["is", "set"]},
			"name",
			order_by="idx desc",
		)
		if last_closed:
			frappe.db.set_value(
				"Manufacturing Operation Time Log",
				last_closed,
				{
					"to_time": None,
					"time_in_mins": 0,
					"time_in_hour": None,
					"time_in_days": 0,
				},
				update_modified=False,
			)
		frappe.db.set_value(
			"Manufacturing Operation",
			row.manufacturing_operation,
			{
				"total_minutes": _closed_minutes(row.manufacturing_operation),
				"total_working_hours": 0,
				"workstation_hour_rate": 0,
				"worker_cost": 0,
			},
			update_modified=False,
		)


def recompute_without_so_costing(mop, employee):
	"""Working hours and worker cost of an operation from its closed time logs.

	Called after every Receive as a safety net: whatever closed the timer, the operation's
	cost is re-derived from the logs rather than relying on the close having run here."""
	total_minutes = _closed_minutes(mop)
	hours = flt(total_minutes / 60, 4)
	hour_rate = get_workstation_hour_rate(employee)
	frappe.db.set_value(
		"Manufacturing Operation",
		mop,
		{
			"total_minutes": total_minutes,
			"total_working_hours": hours,
			"workstation_hour_rate": hour_rate,
			"worker_cost": flt(hours * hour_rate, 2),
		},
		update_modified=False,
	)


def _closed_minutes(mop):
	"""Minutes over an operation's closed time logs, each taken literally as To - From."""
	logs = frappe.get_all(
		"Manufacturing Operation Time Log",
		filters={"parent": mop, "to_time": ["is", "set"], "from_time": ["is", "set"]},
		fields=["from_time", "to_time"],
	)
	return flt(sum(time_diff_in_hours(log.to_time, log.from_time) * 60 for log in logs))


def get_mwo_worker_cost(mwo):
	"""Total worker cost booked on every operation of a work order."""
	return flt(
		frappe.db.sql(
			"""
			SELECT IFNULL(SUM(worker_cost), 0)
			FROM `tabManufacturing Operation`
			WHERE manufacturing_work_order = %s
			""",
			(mwo,),
		)[0][0],
		2,
	)


def apply_worker_cost_to_batch(batch_no, worker_cost):
	"""Stamp Worker Cost and Total Cost (= Batch Rate + Worker Cost) on a batch."""
	if not batch_no:
		return
	batch_rate = flt(frappe.db.get_value("Batch", batch_no, "custom_metal_rate"))
	worker_cost = flt(worker_cost, 2)
	frappe.db.set_value(
		"Batch",
		batch_no,
		{
			"custom_worker_cost": worker_cost,
			"custom_total_cost": flt(batch_rate + worker_cost, 2),
		},
	)
