# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Process control for manufacturing cells: the shared rules behind Process Transfer.

A cell department (switched on by ``Department.custom_enable_process_control``) is split into
processes (``Department Process``). Each Department Operation of the cell belongs to exactly one
process (``Department Operation.department_process``). Three ideas stay separate:

* **Worker eligibility**: what a karigar may do. That is the existing
  ``Employee.custom_operation``; its process is that operation's ``department_process``. Nothing new is
  stored on Employee.
* **User access**: which processes a logged-in user may transact in. These are
  ``User Process Access`` rows, granted by a Process Access Manager. A default only pre-selects;
  it never grants.
* **Job state**: which process a work order is in. ``Manufacturing Operation.department_process`` on the
  work order's active operation is the authoritative value.
  ``Manufacturing Work Order.current_process`` mirrors it, and both are written together, only
  inside :func:`process_transition`.

DAY 1 SCOPE: this module provides the configuration checks, the read helpers and the guard on the
two job-state fields. Nothing here blocks Employee IR, Department IR, Stock Entry or Material
Request yet, so departments without process control behave exactly as before.
"""

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
	OPEN_STATUSES,
	is_open_operation,
	is_reverted,
)

DEPARTMENT_FLAG = "custom_enable_process_control"
TRANSITION_FLAG = "process_transition"

# Job-state field per DocType. Only the transition service may change these.
JOB_STATE_FIELDS = {
	"Manufacturing Operation": "department_process",
	"Manufacturing Work Order": "current_process",
}

INACTIVE_WORK_ORDER_STATUSES = ("Completed", "Closed", "Cancelled", "Stopped")


class ProcessControlError(frappe.ValidationError):
	pass


class ProcessStateError(ProcessControlError):
	"""A job-state field was changed outside the transition service."""


class ActiveOperationError(ProcessControlError):
	"""The work order has no usable active operation."""


# --------------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------------


def department_flag_installed():
	return frappe.get_meta("Department").has_field(DEPARTMENT_FLAG)


def is_process_control_enabled(department):
	"""True when ``department`` is a cell with process control switched on.

	False when the flag field is not installed yet (the patch has not run), so the feature stays
	off rather than failing."""
	if not department or not department_flag_installed():
		return False
	return bool(frappe.db.get_value("Department", department, DEPARTMENT_FLAG))


def get_process(process):
	"""The Department Process row as a ``frappe._dict``, or None."""
	if not process:
		return None
	return frappe.db.get_value(
		"Department Process",
		process,
		["name", "process_name", "department", "company", "manufacturer", "disabled"],
		as_dict=True,
	)


def get_operation_processes(operations):
	"""``{Department Operation: process}`` for many operations in one query. Unmapped operations
	map to None. One operation has exactly one ``department_process`` link, so it can never resolve to two
	processes."""
	operations = sorted({o for o in operations or () if o})
	if not operations:
		return {}
	rows = frappe.get_all(
		"Department Operation",
		filters={"name": ["in", operations]},
		fields=["name", "department_process"],
		limit_page_length=0,
	)
	found = {r.name: r.department_process or None for r in rows}
	return {o: found.get(o) for o in operations}


def get_worker_process(employee):
	"""The process of the worker's existing Employee operation (``custom_operation``), or None.

	Worker eligibility: it says what the karigar may do, never which process a job is in."""
	if not employee or not frappe.get_meta("Employee").has_field("custom_operation"):
		return None
	operation = frappe.db.get_value("Employee", employee, "custom_operation")
	return get_operation_processes([operation]).get(operation) if operation else None


def validate_department(doc, method=None):
	"""Department ``validate`` hook: a department may switch process control on only when it is
	fully configured. That means at least one enabled process, and every Department Operation of
	the department mapped to one. A missing mapping is reported rather than guessed."""
	if not doc.get(DEPARTMENT_FLAG):
		return
	before = doc.get_doc_before_save()
	if before and before.get(DEPARTMENT_FLAG):
		return  # already on; mappings are guarded where they change

	if not frappe.db.exists(
		"Department Process", {"department": doc.name, "disabled": 0}
	):
		frappe.throw(
			_(
				"Process control cannot be enabled for Department {0}: it has no enabled Department "
				"Process. Create its processes first."
			).format(frappe.bold(doc.name)),
			ProcessControlError,
		)
	unmapped = frappe.get_all(
		"Department Operation",
		filters={"department": doc.name, "department_process": ["is", "not set"]},
		pluck="name",
		order_by="name asc",
	)
	if unmapped:
		frappe.throw(
			_(
				"Process control cannot be enabled for Department {0}: these Department Operations "
				"have no Process: {1}"
			).format(
				frappe.bold(doc.name), ", ".join(frappe.bold(o) for o in unmapped)
			),
			ProcessControlError,
		)

	# Work already in the department has no process. It is shown, not guessed (GK-DEC-0004):
	# those work orders get their first process through an Initial Process Assignment.
	unassigned = [
		r.manufacturing_work_order for r in report_active_jobs_without_process(doc.name)
	]
	if unassigned:
		shown = ", ".join(unassigned[:10]) + (
			_(" and {0} more").format(len(unassigned) - 10)
			if len(unassigned) > 10
			else ""
		)
		frappe.msgprint(
			_(
				"{0} active work orders in Department {1} have no process yet: {2}. Give them their "
				"first process with a Process Transfer marked Initial Process Assignment."
			).format(len(unassigned), frappe.bold(doc.name), shown),
			title=_("Work Orders Without a Process"),
			indicator="orange",
		)


# --------------------------------------------------------------------------------------------
# user access (operator authority; separate from worker eligibility and job state)
# --------------------------------------------------------------------------------------------


def get_allowed_processes(user=None, department=None):
	"""Processes the user may transact in: their enabled grant rows on enabled processes.

	A default flag can only sit on one of these rows, so it can never widen the list."""
	user = user or frappe.session.user
	UPA = frappe.qb.DocType("User Process Access")
	DP = frappe.qb.DocType("Department Process")
	query = (
		frappe.qb.from_(UPA)
		.join(DP)
		.on(DP.name == UPA.department_process)
		.select(UPA.department_process)
		.where((UPA.user == user) & (UPA.enabled == 1) & (DP.disabled == 0))
		.orderby(UPA.department_process)
	)
	if department:
		query = query.where(DP.department == department)
	return [r[0] for r in query.run()]


def get_default_process(user=None, department=None):
	"""The user's default process for the department. It must also be in
	:func:`get_allowed_processes`, otherwise None."""
	user = user or frappe.session.user
	filters = {"user": user, "enabled": 1, "is_default": 1}
	if department:
		filters["department"] = department
	default = frappe.db.get_value("User Process Access", filters, "department_process")
	return default if default in get_allowed_processes(user, department) else None


# --------------------------------------------------------------------------------------------
# job state
# --------------------------------------------------------------------------------------------


def get_active_operation(work_order):
	"""The work order's active Manufacturing Operation, as a ``frappe._dict``.

	This is the work order's ``manufacturing_operation`` pointer, the same definition
	``current_operation_guard`` uses. It must be open, not reverted, and have no live successor.
	Raises :class:`ActiveOperationError` with the reason otherwise. The newest operation by
	timestamp is NOT used.
	"""
	mwo = frappe.db.get_value(
		"Manufacturing Work Order",
		work_order,
		[
			"name",
			"docstatus",
			"status",
			"company",
			"manufacturer",
			"manufacturing_operation",
			"current_process",
		],
		as_dict=True,
	)
	if not mwo:
		frappe.throw(
			_("Manufacturing Work Order {0} does not exist.").format(
				frappe.bold(work_order)
			),
			ActiveOperationError,
		)
	if not mwo.manufacturing_operation:
		frappe.throw(
			_(
				"Manufacturing Work Order {0} has no current Manufacturing Operation."
			).format(frappe.bold(mwo.name)),
			ActiveOperationError,
		)
	mop = frappe.db.get_value(
		"Manufacturing Operation",
		mwo.manufacturing_operation,
		[
			"name",
			"manufacturing_work_order",
			"department",
			"operation",
			"department_process",
			"status",
			"department_ir_status",
			"employee",
			"subcontractor",
		],
		as_dict=True,
	)
	if not mop or cstr(mop.manufacturing_work_order) != cstr(mwo.name):
		frappe.throw(
			_(
				"The current operation of Manufacturing Work Order {0} does not belong to it."
			).format(frappe.bold(mwo.name)),
			ActiveOperationError,
		)
	if not is_open_operation(mop.status, mop.department_ir_status):
		frappe.throw(
			_(
				"The current operation {0} of Manufacturing Work Order {1} is {2}, not open."
			).format(
				frappe.bold(mop.name),
				frappe.bold(mwo.name),
				mop.department_ir_status or mop.status,
			),
			ActiveOperationError,
		)
	for successor in frappe.get_all(
		"Manufacturing Operation",
		filters={"previous_mop": mop.name},
		fields=["status", "department_ir_status"],
	):
		if not is_reverted(successor.status, successor.department_ir_status):
			frappe.throw(
				_(
					"The current operation {0} of Manufacturing Work Order {1} already has a "
					"successor, so it is not the active operation."
				).format(frappe.bold(mop.name), frappe.bold(mwo.name)),
				ActiveOperationError,
			)
	mop.work_order = mwo
	return mop


@contextmanager
def process_transition():
	"""The only context in which the job-state fields may change. The Day 2 transition service
	opens it after its own authority, scope and lock checks. It is a server flag, not a client
	one."""
	previous = frappe.flags.get(TRANSITION_FLAG)
	frappe.flags[TRANSITION_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[TRANSITION_FLAG] = previous


def guard_process_field(doc, method=None):
	"""``validate`` hook on Manufacturing Operation and Manufacturing Work Order.

	The job's process may change only inside :func:`process_transition`. An ordinary save, a REST
	update or a Data Import cannot set or change it. A new Manufacturing Operation may carry the
	value of the operation it was copied from, the way Employee IR Receive creates the next
	operation. A new work order may not start with one."""
	fieldname = JOB_STATE_FIELDS.get(doc.doctype)
	if not fieldname or frappe.flags.get(TRANSITION_FLAG):
		return
	value = cstr(doc.get(fieldname))
	if doc.is_new():
		if not value:
			return
		if doc.doctype == "Manufacturing Operation" and doc.get("previous_mop"):
			inherited = cstr(
				frappe.db.get_value(
					"Manufacturing Operation", doc.previous_mop, "department_process"
				)
			)
			if inherited == value:
				return
		_refuse_state_change(doc, fieldname, "", value)
		return
	before = doc.get_doc_before_save()
	old = (
		cstr(before.get(fieldname))
		if before
		else cstr(frappe.db.get_value(doc.doctype, doc.name, fieldname))
	)
	if old != value:
		_refuse_state_change(doc, fieldname, old, value)


def _refuse_state_change(doc, fieldname, old, new):
	frappe.throw(
		_(
			"{0} {1}: the {2} can only be changed by a Process Transfer or the manufacturing flow "
			"(attempted: {3} to {4})."
		).format(
			_(doc.doctype),
			frappe.bold(doc.name or _("(new)")),
			_(doc.meta.get_label(fieldname)),
			frappe.bold(old or _("blank")),
			frappe.bold(new or _("blank")),
		),
		ProcessStateError,
	)


def report_active_jobs_without_process(department):
	"""Read-only audit: the department's active work orders whose current operation has no process.

	Run it before switching process control on, or for a cell that is already on. It fills in
	nothing: assigning a first process is a business decision (see the Day 1 handover), and
	department name, employee or last operation are never used to guess one.

	    bench --site <site> execute jewellery_erpnext.jewellery_erpnext.doc_events.process_control.report_active_jobs_without_process --kwargs "{'department': '<Department>'}"
	"""
	MWO = frappe.qb.DocType("Manufacturing Work Order")
	MOP = frappe.qb.DocType("Manufacturing Operation")
	rows = (
		frappe.qb.from_(MWO)
		.join(MOP)
		.on(MOP.name == MWO.manufacturing_operation)
		.select(
			MWO.name.as_("manufacturing_work_order"),
			MOP.name.as_("manufacturing_operation"),
			MOP.status,
			MOP.department_ir_status,
			MOP.operation,
			MOP.employee,
		)
		.where(
			(MWO.docstatus == 1)
			& (MWO.status.notin(INACTIVE_WORK_ORDER_STATUSES))
			& (MOP.department == department)
			& (MOP.status.isin(OPEN_STATUSES))
			& ((MOP.department_process.isnull()) | (MOP.department_process == ""))
		)
		.orderby(MWO.name)
	).run(as_dict=True)
	return [r for r in rows if not is_reverted(r.status, r.department_ir_status)]
