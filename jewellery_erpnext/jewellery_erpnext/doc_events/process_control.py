# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Process control for manufacturing cells: the shared rules behind Process Transfer.

A cell department (switched on by ``Department.custom_enable_process_control``) is split into
processes (``Department Process``). Each Department Operation of the cell belongs to exactly one
process (``Department Operation.department_process``). Three ideas stay separate:

* **Worker eligibility**: what a karigar may do. That is the existing
  ``Employee.custom_operation``; its process is that operation's ``department_process``. Nothing
  new is stored on Employee.
* **User access**: which processes a logged-in user may transact in. These are
  ``User Process Access`` rows, granted by a Process Access Manager. A default only pre-selects;
  it never grants.
* **Job state**: which process a work order is in. ``Manufacturing Operation.department_process``
  on the work order's active operation is the authoritative value.
  ``Manufacturing Work Order.current_process`` mirrors it, and ``process_transition`` points at
  the job's latest change (a Process Transition Log row). All three change only in
  :func:`apply_transition`, under the same MOP-then-MWO row locks Employee IR and Department IR
  take. It writes with ``frappe.db.set_value`` on purpose (a full operation save rewrites Parent
  Manufacturing Order totals); a document save or REST update of these fields is refused by
  :func:`guard_process_field`.

Day 1 added configuration, read helpers and the field guard. Day 2 adds the transition service
(Process Transfer submit / cancel), the Department IR destination process and the Receive-cancel
block. The pilot switch (MOP Settings › Enable Process Control) keeps Process Transfer and the
Department IR assignment off until the Day 3 checks on Employee IR, Department IR, Stock Entry
and Material Request exist. Departments without process control behave exactly as before.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Coalesce
from frappe.utils import cint, cstr, escape_html, now_datetime

from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
	LOCK_DEADLINE_FLAG,
	OPEN_STATUSES,
	_confirm_drafts,
	_lock_block,
	_refuse_if_snapshot_older,
	is_open_operation,
	is_reverted,
	outstanding_issue_drafts,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import _lock_rows_by_name

DEPARTMENT_FLAG = "custom_enable_process_control"
TRANSITION_FLAG = "process_transition"

# Job-state field per DocType. Only the transition service may change these.
JOB_STATE_FIELDS = {
	"Manufacturing Operation": ("department_process",),
	"Manufacturing Work Order": ("current_process", "process_transition"),
}

INACTIVE_WORK_ORDER_STATUSES = ("Completed", "Closed", "Cancelled", "Stopped")
MESSAGE_LIST_LIMIT = 5


class ProcessControlError(frappe.ValidationError):
	pass


class ProcessStateError(ProcessControlError):
	"""A job-state field was changed outside the transition service."""


class ActiveOperationError(ProcessControlError):
	"""The work order has no usable active operation."""


def bold(value: object) -> str:
	"""``frappe.bold`` of an escaped value: names and labels may hold user-entered text."""
	return frappe.bold(escape_html(cstr(value)))


# --------------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------------


def department_flag_installed() -> bool:
	return frappe.get_meta("Department").has_field(DEPARTMENT_FLAG)


def is_process_control_enabled(department: str | None) -> bool:
	"""True when ``department`` is a cell with process control switched on.

	False when the flag field is not installed yet (the patch has not run), so the feature stays
	off rather than failing."""
	if not department or not department_flag_installed():
		return False
	return bool(frappe.db.get_value("Department", department, DEPARTMENT_FLAG))


def get_process(process: str | None) -> frappe._dict | None:
	if not process:
		return None
	return frappe.db.get_value(
		"Department Process",
		process,
		["name", "process_name", "department", "company", "manufacturer", "disabled"],
		as_dict=True,
	)


def get_operation_processes(operations: Iterable[str]) -> dict[str, str | None]:
	"""Process of many Department Operations in one query; unmapped operations map to None. One
	operation has exactly one ``department_process`` link, so it never resolves to two."""
	names = sorted({o for o in operations or () if o})
	if not names:
		return {}
	rows = frappe.get_all(
		"Department Operation",
		filters={"name": ["in", names]},
		fields=["name", "department_process"],
		limit_page_length=0,
	)
	found = {r.name: r.department_process or None for r in rows}
	return {o: found.get(o) for o in names}


def get_worker_process(employee: str | None) -> str | None:
	"""Process of the worker's existing Employee operation (``custom_operation``).

	Worker eligibility: it says what the karigar may do, never which process a job is in."""
	if not employee or not frappe.get_meta("Employee").has_field("custom_operation"):
		return None
	operation = frappe.db.get_value("Employee", employee, "custom_operation")
	return get_operation_processes([operation]).get(operation) if operation else None


def open_operations(
	*,
	department_process: str | None = None,
	operation: str | None = None,
	with_process: bool = False,
	limit: int = MESSAGE_LIST_LIMIT,
) -> list[str]:
	"""Names of open, non-reverted Manufacturing Operations matching the filters, at most
	``limit`` (for error messages; the table is large, so the scan is bounded in SQL)."""
	if not department_process and not operation:
		return []
	MOP = frappe.qb.DocType("Manufacturing Operation")
	query = (
		frappe.qb.from_(MOP)
		.select(MOP.name)
		.where(MOP.status.isin(OPEN_STATUSES))
		.where(Coalesce(MOP.department_ir_status, "") != "Revert")
		.orderby(MOP.name)
		.limit(limit)
	)
	if department_process:
		query = query.where(MOP.department_process == department_process)
	if operation:
		query = query.where(MOP.operation == operation)
	if with_process:
		query = query.where(Coalesce(MOP.department_process, "") != "")
	return [r[0] for r in query.run()]


def validate_department(doc: Document, method: str | None = None) -> None:
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
			).format(bold(doc.name)),
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
			).format(bold(doc.name), ", ".join(bold(o) for o in unmapped)),
			ProcessControlError,
		)

	# Work already in the department has no process. It is shown, not guessed (GK-DEC-0004):
	# those work orders get their first process through an Initial Process Assignment.
	unassigned = [
		r.manufacturing_work_order for r in report_active_jobs_without_process(doc.name)
	]
	if unassigned:
		shown = ", ".join(escape_html(m) for m in unassigned[:10])
		if len(unassigned) > 10:
			shown += _(" and {0} more").format(len(unassigned) - 10)
		frappe.msgprint(
			_(
				"{0} active work orders in Department {1} have no process yet: {2}. Give them their "
				"first process with a Process Transfer marked Initial Process Assignment."
			).format(len(unassigned), bold(doc.name), shown),
			title=_("Work Orders Without a Process"),
			indicator="orange",
		)


# --------------------------------------------------------------------------------------------
# user access (operator authority; separate from worker eligibility and job state)
# --------------------------------------------------------------------------------------------


def get_allowed_processes(
	user: str | None = None, department: str | None = None
) -> list[str]:
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


def get_default_process(
	user: str | None = None, department: str | None = None
) -> str | None:
	"""The user's default process for the department, only if it is also allowed."""
	user = user or frappe.session.user
	filters = {"user": user, "enabled": 1, "is_default": 1}
	if department:
		filters["department"] = department
	default = frappe.db.get_value("User Process Access", filters, "department_process")
	return default if default in get_allowed_processes(user, department) else None


# --------------------------------------------------------------------------------------------
# job state
# --------------------------------------------------------------------------------------------

_MWO_FIELDS = (
	"name",
	"docstatus",
	"status",
	"company",
	"manufacturer",
	"manufacturing_operation",
	"current_process",
	"process_transition",
)
_MOP_FIELDS = (
	"name",
	"manufacturing_work_order",
	"department",
	"operation",
	"department_process",
	"status",
	"department_ir_status",
	"employee",
	"subcontractor",
	"previous_mop",
)


def get_active_operations(work_orders: Iterable[str]) -> dict[str, frappe._dict | str]:
	"""Active Manufacturing Operation of each work order, in three queries whatever the count.

	Each value is the operation as a ``frappe._dict`` (with the work order under ``work_order``)
	or, when the work order has no usable active operation, the reason as a translated string.
	The active operation is the work order's ``manufacturing_operation`` pointer, the same
	definition ``current_operation_guard`` uses: it must belong to the work order, be open, not
	reverted, and have no live successor. The newest operation by timestamp is NOT used.
	Successors are found through ``manufacturing_work_order`` (indexed), not ``previous_mop``.
	"""
	names = sorted({cstr(w).strip() for w in work_orders or () if cstr(w).strip()})
	if not names:
		return {}
	mwos = {
		r.name: r
		for r in frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", names]},
			fields=list(_MWO_FIELDS),
			limit_page_length=0,
		)
	}
	family = frappe.get_all(
		"Manufacturing Operation",
		filters={"manufacturing_work_order": ["in", list(mwos)]}
		if mwos
		else {"name": ""},
		fields=list(_MOP_FIELDS),
		limit_page_length=0,
	)
	by_name = {r.name: r for r in family}
	live_successor_of = {
		r.previous_mop
		for r in family
		if r.previous_mop and not is_reverted(r.status, r.department_ir_status)
	}

	result = {}
	for name in names:
		mwo = mwos.get(name)
		if not mwo:
			result[name] = _("Manufacturing Work Order {0} does not exist.").format(
				bold(name)
			)
			continue
		mop = (
			by_name.get(mwo.manufacturing_operation)
			if mwo.manufacturing_operation
			else None
		)
		if not mwo.manufacturing_operation:
			result[name] = _(
				"Manufacturing Work Order {0} has no current Manufacturing Operation."
			).format(bold(name))
		elif not mop:
			result[name] = _(
				"The current operation {0} of Manufacturing Work Order {1} does not belong to it."
			).format(bold(mwo.manufacturing_operation), bold(name))
		elif not is_open_operation(mop.status, mop.department_ir_status):
			result[name] = _(
				"The current operation {0} of Manufacturing Work Order {1} is {2}, not open."
			).format(
				bold(mop.name), bold(name), _(mop.department_ir_status or mop.status)
			)
		elif mop.name in live_successor_of:
			result[name] = _(
				"The current operation {0} of Manufacturing Work Order {1} already has a successor, so "
				"it is not the active operation."
			).format(bold(mop.name), bold(name))
		else:
			mop.work_order = mwo
			result[name] = mop
	return result


def get_active_operation(work_order: str) -> frappe._dict:
	""":func:`get_active_operations` for one work order; raises :class:`ActiveOperationError`."""
	found = get_active_operations([work_order]).get(cstr(work_order).strip())
	if not isinstance(found, dict):
		frappe.throw(
			found or _("Select a Manufacturing Work Order."),
			ActiveOperationError,
		)
	return found


@contextmanager
def process_transition() -> Iterator[None]:
	"""The context in which the job-state fields may change. :func:`apply_transition` opens it
	around its writes; ``frappe.db.set_value`` itself bypasses validation, so the flag matters for
	any document save that happens inside it (the guard lets that save through) and documents
	that every job-state write belongs to a transition. It is a server flag, not a client one."""
	previous = frappe.flags.get(TRANSITION_FLAG)
	frappe.flags[TRANSITION_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[TRANSITION_FLAG] = previous


def guard_process_field(doc: Document, method: str | None = None) -> None:
	"""``validate`` hook on Manufacturing Operation and Manufacturing Work Order, and
	``before_update_after_submit`` on the (submittable) work order.

	The job's process may change only inside :func:`process_transition`. An ordinary save, an
	update after submit, a REST update or a Data Import cannot set or change it. A new
	Manufacturing Operation may carry the value of the operation it was copied from, the way
	Employee IR Receive creates the next operation. A new work order may not start with one."""
	if frappe.flags.get(TRANSITION_FLAG):
		return
	for fieldname in JOB_STATE_FIELDS.get(doc.doctype, ()):
		_guard_one_field(doc, fieldname)


def _guard_one_field(doc: Document, fieldname: str) -> None:
	value = cstr(doc.get(fieldname))
	if doc.is_new():
		if not value:
			return
		if fieldname == "department_process" and doc.get("previous_mop"):
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


def _refuse_state_change(doc: Document, fieldname: str, old: str, new: str) -> None:
	frappe.throw(
		_(
			"{0} {1}: the {2} can only be changed by a Process Transfer or the manufacturing flow "
			"(attempted: {3} to {4})."
		).format(
			_(doc.doctype),
			bold(doc.name or _("(new)")),
			_(doc.meta.get_label(fieldname)),
			bold(old or _("blank")),
			bold(new or _("blank")),
		),
		ProcessStateError,
	)


def report_active_jobs_without_process(department: str) -> list[frappe._dict]:
	"""Read-only audit: the department's active work orders whose current operation has no process.

	Run it before switching process control on, or for a cell that is already on. It fills in
	nothing: assigning a first process is a business decision (GK-DEC-0004), and department name,
	employee or last operation are never used to guess one.

	    bench --site <site> execute jewellery_erpnext.jewellery_erpnext.doc_events.process_control.report_active_jobs_without_process --kwargs "{'department': '<Department>'}"
	"""
	if not department:
		return []
	MWO = frappe.qb.DocType("Manufacturing Work Order")
	MOP = frappe.qb.DocType("Manufacturing Operation")
	return (
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
			& (Coalesce(MOP.department_ir_status, "") != "Revert")
			& (Coalesce(MOP.department_process, "") == "")
		)
		.orderby(MWO.name)
	).run(as_dict=True)


# --------------------------------------------------------------------------------------------
# Day 2: pilot gate, readiness, authority, locked state and the one write path
# --------------------------------------------------------------------------------------------

PILOT_DOCTYPE = "MOP Settings"
PILOT_FIELD = "enable_process_control"
TRANSFER_EVENTS = ("Process Transfer", "Initial Assignment")
BLOCKED_WORK_ORDER_STATUSES = ("Hold", "Stopped", "Closed", "Completed", "Cancelled")
WORK_ORDER_TABLE = "tabManufacturing Work Order"
OPERATION_TABLE = "tabManufacturing Operation"


class ProcessTransitionConflictError(ProcessControlError):
	"""The job changed since the document was prepared, or a later change depends on it."""


def is_pilot_enabled() -> bool:
	"""MOP Settings › Enable Process Control. Off (the default, or a site that has not migrated
	the field yet) means no process change is applied anywhere."""
	if not frappe.get_meta(PILOT_DOCTYPE).has_field(PILOT_FIELD):
		return False
	return bool(cint(frappe.db.get_single_value(PILOT_DOCTYPE, PILOT_FIELD)))


def readiness_problem(mop: frappe._dict, mwo: frappe._dict) -> str | None:
	"""Why this job cannot change process now (GK-DEC-0003), or None when it is at rest:
	submitted and not on hold or closed, its active operation Not Started, nobody holding it,
	not in transit between departments."""
	if cint(mwo.docstatus) != 1:
		return _("is not submitted")
	if mwo.status in BLOCKED_WORK_ORDER_STATUSES:
		return _("is {0}").format(_(mwo.status))
	if mop.department_ir_status in ("In-Transit", "Revert"):
		return _("is in transit between departments")
	if mop.employee:
		return _("is with employee {0}").format(bold(mop.employee))
	if mop.subcontractor:
		return _("is with subcontractor {0}").format(bold(mop.subcontractor))
	if mop.status != "Not Started":
		return _("is {0}").format(_(mop.status))
	return None


def open_issue_drafts(
	work_orders: Iterable[str], *, locked: bool = False
) -> dict[str, str]:
	"""``{work order: draft Employee IR Issue}`` for work orders an unsubmitted Issue names. On a
	locking path the snapshot hits are confirmed by current reads (``_confirm_drafts``)."""
	mwos = sorted({w for w in work_orders if w})
	if not mwos:
		return {}
	drafts = outstanding_issue_drafts(mwos)
	if locked and drafts:
		drafts = _confirm_drafts(drafts, mwos)
	return {d["mwo"]: d["draft"] for d in drafts or ()}


def require_process_access(process: str, user: str | None = None) -> None:
	"""The user needs an enabled User Process Access row for ``process`` (GK-DEC-0002).
	No role is exempt (GK-DEC-0007)."""
	user = user or frappe.session.user
	if process not in get_allowed_processes(user):
		frappe.throw(
			_(
				"{0} has no access to process {1}. Ask a Process Access Manager for it."
			).format(bold(user), bold(process)),
			frappe.PermissionError,
		)


def lock_jobs(
	doc: Document, operations: Iterable[str], work_orders: Iterable[str]
) -> None:
	"""Take the same MOP-then-MWO row locks Employee IR and Department IR take
	(``current_operation_guard._lock_block``, ``lock_order`` RULE D/E), with a fresh wait budget
	for this attempt. A busy row raises ``WorkOrderBusyError``.

	Then refuse as busy when an operation changed while this transaction waited for it
	(``_refuse_if_snapshot_older``): the transaction's REPEATABLE READ snapshot predates the wait,
	so its plain reads (open Employee IR drafts, Stock Entries) would miss what the other
	transaction committed. The retry reads it afresh."""
	doc.flags.pop(LOCK_DEADLINE_FLAG, None)
	operations = [m for m in operations if m]
	mop_rows, _mwo_rows = _lock_block(operations, [w for w in work_orders if w], doc)
	_refuse_if_snapshot_older(mop_rows, operations)


def effective_transitions(log_names: Iterable[str]) -> dict[str, str | None]:
	"""``{log: the change it leaves in force}``: a Cancellation is replaced by the transition
	before the change it reversed, repeatedly, so undone pairs (a Department IR Issue that was
	cancelled, say) do not count as a later change. Other logs map to themselves. Batched, one
	query per level of nesting."""
	result = {cstr(n): cstr(n) for n in log_names if n}
	pending = dict(result)
	for _level in range(50):
		if not pending:
			break
		rows = {
			cstr(r.name): r
			for r in frappe.get_all(
				"Process Transition Log",
				filters={"name": ["in", list(set(pending.values()))]},
				fields=["name", "event", "reverses"],
			)
		}
		reversed_names = [
			cstr(r.reverses)
			for r in rows.values()
			if r.event == "Cancellation" and r.reverses
		]
		previous = (
			{
				cstr(r.name): cstr(r.previous_transition)
				if r.previous_transition
				else None
				for r in frappe.get_all(
					"Process Transition Log",
					filters={"name": ["in", reversed_names]},
					fields=["name", "previous_transition"],
				)
			}
			if reversed_names
			else {}
		)
		next_pending = {}
		for start, current in pending.items():
			row = rows.get(current)
			if row and row.event == "Cancellation" and row.reverses:
				before = previous.get(cstr(row.reverses))
				result[start] = before
				if before:
					next_pending[start] = before
		pending = next_pending
	return result


def read_locked_state(expected: dict[str, str]) -> dict[str, frappe._dict | str]:
	"""Current state of each work order in ``expected`` (``{work order: operation it should be
	at}``), from locking reads of rows the caller already holds, so nothing waits and nothing is
	read from an older REPEATABLE READ snapshot.

	Each value is the operation (``frappe._dict`` with the work order under ``work_order``) or,
	when the work order is gone or no longer at that operation, the reason as a string."""
	if not expected:
		return {}
	mwos = _lock_rows_by_name(WORK_ORDER_TABLE, _MWO_FIELDS, list(expected), None)
	held = [
		mop
		for mwo, mop in expected.items()
		if mwo in mwos and mwos[mwo].manufacturing_operation == mop
	]
	mops = _lock_rows_by_name(OPERATION_TABLE, _MOP_FIELDS, held, None)
	result = {}
	for mwo_name, mop_name in expected.items():
		mwo = mwos.get(mwo_name)
		if not mwo:
			result[mwo_name] = _("Manufacturing Work Order {0} does not exist.").format(
				bold(mwo_name)
			)
		elif mwo.manufacturing_operation != mop_name or mop_name not in mops:
			result[mwo_name] = _(
				"Manufacturing Work Order {0} has moved on from operation {1} to {2}."
			).format(
				bold(mwo_name),
				bold(mop_name),
				bold(mwo.manufacturing_operation or _("none")),
			)
		else:
			mop = frappe._dict(mops[mop_name])
			mop.work_order = frappe._dict(mwo)
			result[mwo_name] = mop
	return result


def apply_transition(
	entries: list[frappe._dict], *, event: str, voucher_type: str, voucher_no: str
) -> list[str]:
	"""The one place a job's process changes. Returns the new Process Transition Log names.

	Each entry: ``mwo``, ``mop``, ``department``, ``from_process``, ``to_process``,
	``previous_transition`` and optionally ``reverses`` and ``set_operation`` (default True).
	The caller holds the rows' locks and has validated them. For every entry this writes one log
	row, the operation's ``department_process`` (unless ``set_operation`` is False) and the work
	order's ``current_process`` + ``process_transition``. It never commits: a failure anywhere
	rolls the whole document back with it.

	Written with ``frappe.db.set_value`` on purpose, as every other job-state write in this app
	is: a full Manufacturing Operation save would also rewrite Parent Manufacturing Order weight
	totals and, in CAD/CAM departments, the Item. Nothing else on the job is touched (material,
	weights, warehouses, custody, employee, status).
	"""
	logs = []
	when = now_datetime()
	with process_transition():
		for entry in sorted(entries, key=lambda e: e.mwo):
			# The log is the service's own record; users cannot create one (no create permission),
			# so the insert runs without the user's permissions, after the caller's checks.
			log = frappe.get_doc(
				{
					"doctype": "Process Transition Log",
					"manufacturing_work_order": entry.mwo,
					"manufacturing_operation": entry.mop,
					"department": entry.department,
					"event": event,
					"from_process": entry.from_process or None,
					"to_process": entry.to_process or None,
					"voucher_type": voucher_type,
					"voucher_no": voucher_no,
					"previous_transition": entry.previous_transition or None,
					"reverses": entry.get("reverses"),
					"user": frappe.session.user,
					"transition_time": when,
				}
			).insert(ignore_permissions=True)
			if entry.get("set_operation", True) and entry.mop:
				frappe.db.set_value(
					"Manufacturing Operation",
					entry.mop,
					"department_process",
					entry.to_process or None,
				)
			frappe.db.set_value(
				"Manufacturing Work Order",
				entry.mwo,
				{
					"current_process": entry.to_process or None,
					"process_transition": log.name,
				},
			)
			logs.append(log.name)
	return logs


def active_transfers_on(operations: Iterable[str]) -> list[frappe._dict]:
	"""Process Transfer / Initial Assignment logs on these operations that no Cancellation has
	reversed: the changes a cancellation of the document that minted the operation would undo."""
	mops = sorted({m for m in operations if m})
	if not mops:
		return []
	PTL = frappe.qb.DocType("Process Transition Log")
	REV = frappe.qb.DocType("Process Transition Log").as_("rev")
	return (
		frappe.qb.from_(PTL)
		.left_join(REV)
		.on(REV.reverses == PTL.name)
		.select(PTL.name, PTL.voucher_type, PTL.voucher_no, PTL.manufacturing_operation)
		.where(
			PTL.manufacturing_operation.isin(mops)
			& PTL.event.isin(TRANSFER_EVENTS)
			& REV.name.isnull()
		)
		.orderby(PTL.name)
	).run(as_dict=True)


def _refuse_if_transferred(doc: Document, operations: list[str]) -> None:
	blocking = active_transfers_on(operations)
	if blocking:
		vouchers = sorted({b.voucher_no for b in blocking})
		frappe.throw(
			_(
				"{0} {1} cannot be cancelled: the work order has since changed process with {2}. Cancel {2} "
				"first."
			).format(
				_(doc.doctype), bold(doc.name), ", ".join(bold(v) for v in vouchers)
			),
			ProcessTransitionConflictError,
		)


# --------------------------------------------------------------------------------------------
# Employee IR / Department IR hooks
# --------------------------------------------------------------------------------------------


def block_receive_cancel_after_transfer(
	doc: Document, method: str | None = None
) -> None:
	"""``before_cancel`` on Employee IR and Department IR.

	Cancelling a Receive moves the work order back to an earlier operation. If a Process Transfer
	has since changed the process of the operation that Receive produced (Employee IR: the
	successor it minted; Department IR: the operation it received), the cancel would leave the
	work order showing a process its active operation no longer has. Refuse and name the
	transfer to cancel first."""
	if doc.get("type") != "Receive":
		return
	rows = (
		doc.get(
			"employee_ir_operations"
			if doc.doctype == "Employee IR"
			else "department_ir_operation"
		)
		or ()
	)
	work_orders = sorted(
		{r.manufacturing_work_order for r in rows if r.manufacturing_work_order}
	)
	# Cheap exit first (indexed): no transfer was ever made on these work orders.
	if not work_orders or not frappe.db.exists(
		"Process Transition Log",
		{
			"manufacturing_work_order": ["in", work_orders],
			"event": ["in", TRANSFER_EVENTS],
		},
	):
		return
	if doc.doctype == "Employee IR":
		row_mops = [
			r.manufacturing_operation
			for r in doc.get("employee_ir_operations") or ()
			if r.manufacturing_operation
		]
		if not row_mops:
			return
		operations = frappe.get_all(
			"Manufacturing Operation",
			filters={
				"manufacturing_work_order": ["in", work_orders],
				"employee_ir": doc.name,
				"previous_mop": ["in", row_mops],
			},
			pluck="name",
		)
	else:
		operations = [
			r.manufacturing_operation for r in doc.get("department_ir_operation") or ()
		]
	_refuse_if_transferred(doc, operations)


def validate_department_ir_destination(
	doc: Document, method: str | None = None
) -> None:
	"""``validate`` on Department IR: an Issue into a department that uses process control names
	the process the work orders start in (GK-DEC-0005); any other Issue leaves it blank."""
	destination = doc.get("destination_process")
	if doc.get("type") != "Issue":
		if destination:
			doc.destination_process = None
		return
	controlled = is_process_control_enabled(doc.next_department)
	if not controlled:
		if destination:
			frappe.throw(
				_(
					"Destination Process applies only to departments that use process control; {0} does not."
				).format(bold(doc.next_department)),
				ProcessControlError,
			)
		return
	if not is_pilot_enabled():
		if destination:
			frappe.throw(
				_(
					"Process control is not active on this site yet (MOP Settings › Enable Process Control), "
					"so leave Destination Process empty."
				),
				ProcessControlError,
			)
		return
	if not destination:
		frappe.throw(
			_(
				"Department {0} uses process control: select the Destination Process."
			).format(bold(doc.next_department)),
			ProcessControlError,
		)
	process = get_process(destination)
	if not process or process.department != doc.next_department:
		frappe.throw(
			_("Destination Process {0} is not a process of Department {1}.").format(
				bold(destination), bold(doc.next_department)
			),
			ProcessControlError,
		)
	if process.disabled:
		frappe.throw(
			_("Destination Process {0} is disabled.").format(bold(destination)),
			ProcessControlError,
		)


def assign_destination_process(doc: Document, method: str | None = None) -> None:
	"""``on_submit`` on Department IR Issue, after the controller minted the In-Transit operations
	in the next department. A controlled next department: set the destination process. Any other
	department: clear a process the copy carried over from the cell it left (GK-GAP-0925)."""
	if doc.get("type") != "Issue":
		return
	rows = doc.get("department_ir_operation") or ()
	work_orders = sorted(
		{r.manufacturing_work_order for r in rows if r.manufacturing_work_order}
	)
	if not work_orders:
		return
	controlled = is_pilot_enabled() and is_process_control_enabled(doc.next_department)
	if not controlled and not frappe.db.exists(
		"Manufacturing Operation",
		{
			"name": [
				"in",
				[r.manufacturing_operation for r in rows if r.manufacturing_operation],
			],
			"department_process": ["is", "set"],
		},
	):
		return  # nothing to assign and nothing carried over: today's Department IR, untouched
	minted = frappe.get_all(
		"Manufacturing Operation",
		filters={
			"manufacturing_work_order": ["in", work_orders],
			"department_issue_id": doc.name,
		},
		fields=["name", "manufacturing_work_order", "department", "department_process"],
	)
	target = doc.destination_process if controlled else None
	minted = [
		m
		for m in minted
		if m.manufacturing_work_order and (controlled or m.department_process)
	]
	if not minted:
		return
	# The Department IR's own lock block holds these work orders (its minted operations are its
	# own inserts), so the locking read below never waits on another document.
	state = _lock_rows_by_name(
		WORK_ORDER_TABLE,
		_MWO_FIELDS,
		[m.manufacturing_work_order for m in minted],
		None,
	)
	apply_transition(
		[
			frappe._dict(
				mwo=m.manufacturing_work_order,
				mop=m.name,
				department=m.department,
				from_process=m.department_process,
				to_process=target,
				previous_transition=(state.get(m.manufacturing_work_order) or {}).get(
					"process_transition"
				),
			)
			for m in minted
		],
		event="Department IR Issue",
		voucher_type=doc.doctype,
		voucher_no=doc.name,
	)


def restore_after_department_ir_cancel(
	doc: Document, method: str | None = None
) -> None:
	"""``on_cancel`` on Department IR Issue, after the controller moved each work order back to its
	source operation and marked the minted one Revert: the work order's process goes back to the
	source operation's (the cancelled operation keeps its stamp as history)."""
	if doc.get("type") != "Issue":
		return
	issued = frappe.get_all(
		"Process Transition Log",
		filters={
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"event": "Department IR Issue",
		},
		fields=["name", "manufacturing_work_order", "from_process", "to_process"],
	)
	if not issued:
		return
	state = _lock_rows_by_name(
		WORK_ORDER_TABLE,
		_MWO_FIELDS,
		[r.manufacturing_work_order for r in issued],
		None,
	)
	entries = []
	for row in issued:
		mwo = state.get(row.manufacturing_work_order) or {}
		entries.append(
			frappe._dict(
				mwo=row.manufacturing_work_order,
				mop=mwo.get("manufacturing_operation"),
				department=None,
				from_process=row.to_process,
				to_process=row.from_process,
				previous_transition=mwo.get("process_transition"),
				reverses=row.name,
				set_operation=False,
			)
		)
	apply_transition(
		entries, event="Cancellation", voucher_type=doc.doctype, voucher_no=doc.name
	)
