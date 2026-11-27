# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Day 3 of process control: Employee IR, Department IR, Stock Entry and Material Request respect
the job's process (rules GK-PROC-0011..0015, decisions GK-DEC-0009..0012).

Every check here acts only while the site's pilot switch is on (MOP Settings › Enable Process
Control) and only for jobs whose operation sits in a department with process control on. Other
departments, and every department while the pilot is off, behave exactly as before.

The checks are action-specific, never one "process equals process" test:

* **Employee IR Issue**: the header operation's process equals every row's job process; the
  selected worker holds exactly that operation (``Employee.custom_operation``, the picker's rule)
  or the subcontractor offers it; the acting user has access to that process.
* **Employee IR Receive**: judged against the Issue that gave the job out (employee or
  subcontractor, operation), not against today's Employee master; the acting user has access to
  the operation's process.
* **Department IR Issue out of a controlled department**: the acting user has access to each
  row's source process (hand-off authority). The destination process is Day 2's check.
* **Department IR Receive into a controlled department**: the acting user has access to some
  process of that department (accepting custody, not doing a process's work).
* **Stock Entry, new material movement**: under the MOP-then-MWO lock, every job row is at its
  work order's current operation, all rows share one process, a selected process equals it,
  and the acting user has access to it. The process is stamped on the entry.
* **Stock Entry generated inside a checked Employee IR / Department IR**, or **posted by the EOD
  sync for earlier events**: verified by server-side context a client cannot set (a request-local
  registry; the sync's own flag plus its log), never by ``auto_created`` or a source string.
* **Material Request › Transfer to MOP**: re-resolved and locked when the transfer executes.

The acting user is always ``frappe.session.user``; a submitted employee, owner or user field
never decides authority. A default process only pre-fills; it never grants.

On this branch (gurukrupa-uat) Employee IR and Department IR take no MOP-then-MWO lock block of
their own (GK-GAP-0930), so the submit / cancel checks here take it (``lock_jobs``, built on
``process_locks``) for jobs in controlled departments, and also make the current-operation,
work-order and source-department checks the kggk_uat guard makes for every department.
"""

from collections.abc import Iterable

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	_MOP_FIELDS,
	_MWO_FIELDS,
	DEPARTMENT_FLAG,
	MESSAGE_LIST_LIMIT,
	OPERATION_TABLE,
	WORK_ORDER_TABLE,
	ProcessControlError,
	bold,
	department_flag_installed,
	get_allowed_processes,
	is_pilot_enabled,
	lock_jobs,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import _lock_rows_by_name

REGISTRY_ATTR = "process_control_checked_parents"
STOCK_ENTRY_PROCESS_FIELD = "custom_department_process"
EOD_RUNNING_STATUS = "Running"
QTY_TOLERANCE = 1e-6

_OPERATION_FIELDS = (*_MOP_FIELDS, "company", "manufacturing_order")
_WORK_ORDER_FIELDS = (*_MWO_FIELDS, "manufacturing_order")


class ProcessEnforcementError(ProcessControlError):
	"""A transaction does not fit the job's process."""


# --------------------------------------------------------------------------------------------
# shared reads (batched: a fixed number of queries per document, whatever the row count)
# --------------------------------------------------------------------------------------------


def _names(values: Iterable) -> list[str]:
	return sorted({cstr(v).strip() for v in values or () if cstr(v).strip()})


def controlled_departments(departments: Iterable[str]) -> set[str]:
	"""The departments among ``departments`` that have process control on (one query)."""
	names = _names(departments)
	if not names or not department_flag_installed():
		return set()
	return set(
		frappe.get_all(
			"Department",
			filters={"name": ["in", names], DEPARTMENT_FLAG: 1},
			pluck="name",
		)
	)


def read_operations(names: Iterable[str], *, locked: bool) -> dict[str, frappe._dict]:
	"""Manufacturing Operations by name. ``locked``: locking reads (latest committed values,
	rows already held by the caller's lock block), otherwise one plain query."""
	names = _names(names)
	if not names:
		return {}
	if locked:
		return {
			k: frappe._dict(v)
			for k, v in _lock_rows_by_name(
				OPERATION_TABLE, _OPERATION_FIELDS, names, None
			).items()
		}
	return {
		r.name: r
		for r in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", names]},
			fields=list(_OPERATION_FIELDS),
			limit_page_length=0,
		)
	}


def read_work_orders(names: Iterable[str], *, locked: bool) -> dict[str, frappe._dict]:
	names = _names(names)
	if not names:
		return {}
	if locked:
		return {
			k: frappe._dict(v)
			for k, v in _lock_rows_by_name(
				WORK_ORDER_TABLE, _WORK_ORDER_FIELDS, names, None
			).items()
		}
	return {
		r.name: r
		for r in frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", names]},
			fields=list(_WORK_ORDER_FIELDS),
			limit_page_length=0,
		)
	}


def _refuse(problems: list[str], exc=ProcessEnforcementError, title=None) -> None:
	if not problems:
		return
	shown = problems[:MESSAGE_LIST_LIMIT]
	more = len(problems) - len(shown)
	lines = "".join(f"<li>{p}</li>" for p in shown)
	if more:
		lines += "<li>" + _("and {0} more").format(more) + "</li>"
	frappe.throw(f"<ul>{lines}</ul>", exc, title=title or _("Process Control"))


def _refuse_access(user: str, processes: Iterable[str]) -> None:
	missing = _names(processes)
	if missing:
		frappe.throw(
			_(
				"{0} has no access to process {1}. Ask a Process Access Manager for it."
			).format(bold(user), ", ".join(bold(p) for p in missing)),
			frappe.PermissionError,
		)


def _row_label(row) -> str:
	return _("Row {0}").format(row.idx)


def _no_process_message(row, mop) -> str:
	return _(
		"{0}: work order {1} has no process yet. Give it its first process with a Process "
		"Transfer marked Initial Process Assignment."
	).format(_row_label(row), bold(mop.manufacturing_work_order))


# --------------------------------------------------------------------------------------------
# request-local registry of checked parent documents
# --------------------------------------------------------------------------------------------


def _registry() -> dict:
	registry = getattr(frappe.local, REGISTRY_ATTR, None)
	if registry is None:
		registry = {}
		setattr(frappe.local, REGISTRY_ATTR, registry)
	return registry


def _register(doc: Document, rows) -> None:
	"""Remember, for this request only, that ``doc`` passed its Day 3 checks and which work orders
	it covers. ``frappe.local`` lives in this worker's memory; no request payload can write it."""
	_registry()[(doc.doctype, doc.name)] = set(
		_names(r.manufacturing_work_order for r in rows)
	)


def release_parent(doc: Document, method: str | None = None) -> None:
	"""``on_submit`` / ``on_cancel``: the parent's own Stock Entries are made by then."""
	_registry().pop((doc.doctype, doc.name), None)


def _registered_work_orders(doctype: str, name: str | None) -> set[str] | None:
	if not name:
		return None
	return _registry().get((doctype, name))


# --------------------------------------------------------------------------------------------
# Employee IR
# --------------------------------------------------------------------------------------------


def check_employee_ir(doc: Document, method: str | None = None) -> None:
	"""``validate`` (plain reads, an early message on save), ``before_submit`` and ``before_cancel``
	(locking reads under the MOP-then-MWO block) on Employee IR."""
	if not is_pilot_enabled():
		return
	locked = method in ("before_submit", "before_cancel")
	rows = list(doc.get("employee_ir_operations") or ())
	if not controlled_departments([doc.department]):
		if locked:
			# Nothing to check; the Stock Entries it makes are still its business.
			_register(doc, rows)
		return
	operations = [r.manufacturing_operation for r in rows]
	if locked:
		lock_jobs(doc, operations, [r.manufacturing_work_order for r in rows])
	mops = read_operations(operations, locked=locked)
	mwos = read_work_orders(
		[m.manufacturing_work_order for m in mops.values()], locked=locked
	)

	operation = frappe.db.get_value(
		"Department Operation",
		doc.operation,
		["name", "department", "department_process"],
		as_dict=True,
	)
	if not operation:
		frappe.throw(
			_("Operation {0} does not exist.").format(bold(doc.operation)),
			ProcessEnforcementError,
		)
	if operation.department != doc.department:
		frappe.throw(
			_("Operation {0} belongs to Department {1}, not {2}.").format(
				bold(operation.name), bold(operation.department), bold(doc.department)
			),
			ProcessEnforcementError,
		)
	process = operation.department_process
	if not process:
		frappe.throw(
			_(
				"Operation {0} has no process. Department {1} uses process control, so every one of its "
				"operations needs one (Department Operation › Process)."
			).format(bold(operation.name), bold(doc.department)),
			ProcessEnforcementError,
		)

	user = frappe.session.user
	if process not in set(get_allowed_processes(user)):
		_refuse_access(user, [process])
	if method == "before_cancel":
		_register(doc, rows)
		return

	problems = []
	issues = _issues_holding(operations) if doc.type == "Receive" else {}
	for row in rows:
		mop = mops.get(cstr(row.manufacturing_operation).strip())
		if not mop:
			problems.append(
				_("{0}: Manufacturing Operation {1} does not exist.").format(
					_row_label(row), bold(row.manufacturing_operation)
				)
			)
			continue
		if (
			row.manufacturing_work_order
			and mop.manufacturing_work_order != row.manufacturing_work_order
		):
			problems.append(
				_("{0}: operation {1} belongs to work order {2}, not {3}.").format(
					_row_label(row),
					bold(mop.name),
					bold(mop.manufacturing_work_order),
					bold(row.manufacturing_work_order),
				)
			)
			continue
		if doc.type == "Issue":
			problems.extend(_issue_row_problems(row, mop, mwos, process))
		else:
			problems.extend(_receive_row_problems(doc, row, mop, process, issues))
	if doc.type == "Issue":
		problems.extend(_worker_problems(doc, operation))
	_refuse(problems, title=_("Employee IR: process check"))
	if locked:
		_register(doc, rows)


def _issue_row_problems(row, mop, mwos, process) -> list[str]:
	mwo = mwos.get(mop.manufacturing_work_order)
	if not mwo or mwo.manufacturing_operation != mop.name:
		return [
			_(
				"{0}: operation {1} is not the current operation of work order {2}."
			).format(
				_row_label(row), bold(mop.name), bold(mop.manufacturing_work_order)
			)
		]
	if not mop.department_process:
		return [_no_process_message(row, mop)]
	if mop.department_process != process:
		return [
			_(
				"{0}: work order {1} is in process {2}; the selected operation belongs to {3}. Move the "
				"job with a Process Transfer first, or pick an operation of {2}."
			).format(
				_row_label(row),
				bold(mop.manufacturing_work_order),
				bold(mop.department_process),
				bold(process),
			)
		]
	return []


def _issues_holding(operations: Iterable[str]) -> dict[str, list[frappe._dict]]:
	"""``{operation: [submitted Employee IR Issues listing it]}`` in two queries."""
	names = _names(operations)
	if not names:
		return {}
	rows = frappe.get_all(
		"Employee IR Operation",
		filters={
			"manufacturing_operation": ["in", names],
			"parenttype": "Employee IR",
			"docstatus": 1,
		},
		fields=["parent", "manufacturing_operation"],
		limit_page_length=0,
	)
	headers = (
		{
			h.name: h
			for h in frappe.get_all(
				"Employee IR",
				filters={
					"name": ["in", _names(r.parent for r in rows)],
					"type": "Issue",
					"docstatus": 1,
				},
				fields=[
					"name",
					"employee",
					"subcontractor",
					"subcontracting",
					"operation",
				],
				limit_page_length=0,
			)
		}
		if rows
		else {}
	)
	found = {}
	for r in rows:
		if r.parent in headers:
			found.setdefault(r.manufacturing_operation, []).append(headers[r.parent])
	return found


def _receive_row_problems(doc, row, mop, process, issues) -> list[str]:
	"""A Receive is judged by the Issue that gave the job out: today's Employee master (a changed
	``custom_operation``) never rewrites an earlier valid Issue."""
	held = issues.get(mop.name) or []
	if not held:
		return [
			_("{0}: no submitted Employee IR Issue gave out operation {1}.").format(
				_row_label(row), bold(mop.name)
			)
		]
	if len({h.name for h in held}) > 1:
		return [
			_(
				"{0}: operation {1} appears on more than one submitted Issue ({2}); it cannot be told "
				"which one this Receive closes."
			).format(
				_row_label(row),
				bold(mop.name),
				", ".join(bold(h.name) for h in held),
			)
		]
	issue = held[0]
	problems = []
	if issue.operation != doc.operation:
		problems.append(
			_("{0}: Issue {1} gave the job out for operation {2}, not {3}.").format(
				_row_label(row),
				bold(issue.name),
				bold(issue.operation),
				bold(doc.operation),
			)
		)
	if (doc.subcontracting == "Yes") != (issue.subcontracting == "Yes"):
		problems.append(
			_("{0}: Issue {1} and this Receive differ on subcontracting.").format(
				_row_label(row), bold(issue.name)
			)
		)
	elif doc.subcontracting == "Yes":
		if issue.subcontractor != doc.subcontractor:
			problems.append(
				_("{0}: Issue {1} gave the job to subcontractor {2}, not {3}.").format(
					_row_label(row),
					bold(issue.name),
					bold(issue.subcontractor),
					bold(doc.subcontractor),
				)
			)
	elif issue.employee != doc.employee:
		problems.append(
			_("{0}: Issue {1} gave the job to employee {2}, not {3}.").format(
				_row_label(row),
				bold(issue.name),
				bold(issue.employee),
				bold(doc.employee),
			)
		)
	# A job issued before process control reached it has no process: its Receive completes the
	# pair (an Initial Process Assignment needs the job at rest). A stamped one must agree.
	if mop.department_process and mop.department_process != process:
		problems.append(
			_(
				"{0}: work order {1} is in process {2}; operation {3} belongs to {4}."
			).format(
				_row_label(row),
				bold(mop.manufacturing_work_order),
				bold(mop.department_process),
				bold(doc.operation),
				bold(process),
			)
		)
	return problems


def _worker_problems(doc, operation) -> list[str]:
	"""The worker must be eligible for this exact operation: being eligible for another operation
	of the same process is not enough (the Employee IR picker's rule, now on the server)."""
	if doc.subcontracting == "Yes":
		if not doc.subcontractor:
			return []
		if frappe.db.exists(
			"Operation MultiSelect",
			{
				"parenttype": "Supplier",
				"parent": doc.subcontractor,
				"operation": operation.name,
			},
		):
			return []
		return [
			_("Subcontractor {0} does not offer operation {1}.").format(
				bold(doc.subcontractor), bold(operation.name)
			)
		]
	if not doc.employee:
		return []
	has_operation = frappe.get_meta("Employee").has_field("custom_operation")
	fields = ["name", "department"] + (["custom_operation"] if has_operation else [])
	employee = frappe.db.get_value("Employee", doc.employee, fields, as_dict=True)
	if not employee:
		return [_("Employee {0} does not exist.").format(bold(doc.employee))]
	problems = []
	if employee.department != doc.department:
		problems.append(
			_("Employee {0} belongs to Department {1}, not {2}.").format(
				bold(employee.name), bold(employee.department), bold(doc.department)
			)
		)
	# ``custom_operation`` belongs to gke_customization; a site without it has no operation
	# assignment to enforce (the same rule as process_control.get_worker_process).
	if has_operation and employee.custom_operation != operation.name:
		problems.append(
			_(
				"Employee {0} is assigned to operation {1}, not {2}. Being in the same process does not "
				"make a worker eligible for another operation."
			).format(
				bold(employee.name),
				bold(employee.custom_operation or _("none")),
				bold(operation.name),
			)
		)
	return problems


# --------------------------------------------------------------------------------------------
# Department IR
# --------------------------------------------------------------------------------------------


def check_department_ir(doc: Document, method: str | None = None) -> None:
	"""``validate``, ``before_submit`` and ``before_cancel`` on Department IR.

	Issue out of a controlled department: per row, the job is at its current operation in that
	department, at rest, and the acting user has access to the job's process (hand-off
	authority, GK-DEC-0002). A Receive into a controlled department needs access to some process
	of it. Authority to work inside the destination process is not asked for here.

	Once the checks pass at submit / cancel, every Department IR is registered, also one with
	nothing to check: an Issue INTO a cell from an uncontrolled department makes Stock Entries on
	the operation it minted in the cell, which has no process until on_submit stamps it."""
	if not is_pilot_enabled():
		return
	rows = list(doc.get("department_ir_operation") or ())
	locked = method in ("before_submit", "before_cancel")
	if doc.type == "Issue":
		_check_department_issue(doc, rows, method, locked)
	else:
		_check_department_receive(doc)
	if locked:
		_register(doc, rows)


def _check_department_issue(doc, rows, method, locked) -> None:
	if not controlled_departments([doc.current_department]):
		return
	user = frappe.session.user
	operations = [r.manufacturing_operation for r in rows]
	if locked:
		lock_jobs(doc, operations, [r.manufacturing_work_order for r in rows])
	mops = read_operations(operations, locked=locked)
	mwos = read_work_orders(
		[m.manufacturing_work_order for m in mops.values()], locked=locked
	)
	problems, needed, unassigned = [], set(), False
	for row in rows:
		mop = mops.get(cstr(row.manufacturing_operation).strip())
		if not mop:
			problems.append(
				_("{0}: Manufacturing Operation {1} does not exist.").format(
					_row_label(row), bold(row.manufacturing_operation)
				)
			)
			continue
		if method != "before_cancel":
			problems.extend(_department_issue_row_problems(doc, row, mop, mwos))
		if mop.department_process:
			needed.add(mop.department_process)
		else:
			unassigned = True
	_refuse(problems, title=_("Department IR: process check"))
	_refuse_access(user, needed - set(get_allowed_processes(user)))
	if unassigned and not get_allowed_processes(user, doc.current_department):
		frappe.throw(
			_(
				"{0} has no access to any process of Department {1}, so cannot send its work orders on."
			).format(bold(user), bold(doc.current_department)),
			frappe.PermissionError,
		)


def _check_department_receive(doc) -> None:
	if not controlled_departments([doc.current_department]):
		return
	user = frappe.session.user
	if not get_allowed_processes(user, doc.current_department):
		frappe.throw(
			_(
				"{0} has no access to any process of Department {1}, so cannot receive work orders "
				"into it."
			).format(bold(user), bold(doc.current_department)),
			frappe.PermissionError,
		)


def _department_issue_row_problems(doc, row, mop, mwos) -> list[str]:
	if (
		row.manufacturing_work_order
		and mop.manufacturing_work_order != row.manufacturing_work_order
	):
		return [
			_("{0}: operation {1} belongs to work order {2}, not {3}.").format(
				_row_label(row),
				bold(mop.name),
				bold(mop.manufacturing_work_order),
				bold(row.manufacturing_work_order),
			)
		]
	mwo = mwos.get(mop.manufacturing_work_order)
	if not mwo or mwo.manufacturing_operation != mop.name:
		return [
			_(
				"{0}: operation {1} is not the current operation of work order {2}."
			).format(
				_row_label(row), bold(mop.name), bold(mop.manufacturing_work_order)
			)
		]
	if mop.department != doc.current_department:
		return [
			_("{0}: operation {1} is in Department {2}, not {3}.").format(
				_row_label(row),
				bold(mop.name),
				bold(mop.department),
				bold(doc.current_department),
			)
		]
	if mop.employee or mop.subcontractor:
		return [
			_("{0}: work order {1} is still with {2}.").format(
				_row_label(row),
				bold(mop.manufacturing_work_order),
				bold(mop.employee or mop.subcontractor),
			)
		]
	return []


# --------------------------------------------------------------------------------------------
# Stock Entry
# --------------------------------------------------------------------------------------------


def _stock_entry_job_rows(doc: Document) -> list[tuple[object, str, str | None]]:
	"""``(row or header, operation, work order named with it)`` for every job reference."""
	refs = []
	header_mwo = cstr(doc.get("manufacturing_work_order")).strip() or None
	if cstr(doc.get("manufacturing_operation")).strip():
		refs.append((doc, cstr(doc.manufacturing_operation).strip(), header_mwo))
	for row in doc.get("items") or ():
		operation = cstr(row.get("manufacturing_operation")).strip()
		if operation:
			refs.append(
				(
					row,
					operation,
					cstr(row.get("custom_manufacturing_work_order")).strip()
					or header_mwo,
				)
			)
	return refs


def check_stock_entry(doc: Document, method: str | None = None) -> None:
	"""``validate``, ``before_submit`` (after ``prelock_bins``: Bin, then MOP, then MWO) and
	``before_cancel`` on Stock Entry."""
	if not is_pilot_enabled():
		return
	refs = _stock_entry_job_rows(doc)
	if not refs:
		return
	mops = read_operations([op for _r, op, _w in refs], locked=False)
	controlled = controlled_departments(m.department for m in mops.values())
	in_scope = [
		(row, op, mwo)
		for row, op, mwo in refs
		if op in mops and mops[op].department in controlled
	]
	if not in_scope:
		return
	work_orders = {mops[op].manufacturing_work_order for _r, op, _w in in_scope}

	if _is_generated(doc, work_orders) or _is_deferred_posting(doc, in_scope):
		return
	user = frappe.session.user
	if method == "before_cancel":
		recorded = doc.get(STOCK_ENTRY_PROCESS_FIELD)
		if recorded and recorded not in set(get_allowed_processes(user)):
			_refuse_access(user, [recorded])
		return

	locked = method == "before_submit"
	if locked:
		lock_jobs(doc, [op for _r, op, _w in in_scope], work_orders)
		mops = read_operations([op for _r, op, _w in in_scope], locked=True)
	mwos = read_work_orders(work_orders, locked=locked)

	problems, processes = [], set()
	for row, op, named_mwo in in_scope:
		mop = mops.get(op)
		label = _row_label(row) if row is not doc else _("Header")
		if not mop:
			problems.append(
				_("{0}: Manufacturing Operation {1} does not exist.").format(
					label, bold(op)
				)
			)
			continue
		if named_mwo and mop.manufacturing_work_order != named_mwo:
			problems.append(
				_("{0}: operation {1} belongs to work order {2}, not {3}.").format(
					label, bold(op), bold(mop.manufacturing_work_order), bold(named_mwo)
				)
			)
			continue
		mwo = mwos.get(mop.manufacturing_work_order)
		if not mwo or mwo.manufacturing_operation != op:
			problems.append(
				_(
					"{0}: operation {1} is not the current operation of work order {2}, so material "
					"cannot move against it."
				).format(label, bold(op), bold(mop.manufacturing_work_order))
			)
			continue
		if not mop.department_process:
			problems.append(
				_(
					"{0}: work order {1} has no process yet. Give it its first process with a Process "
					"Transfer marked Initial Process Assignment."
				).format(label, bold(mop.manufacturing_work_order))
			)
			continue
		processes.add(mop.department_process)
	if len(processes) > 1:
		problems.append(
			_(
				"The work orders on this entry are in different processes ({0}). Make one entry per "
				"process."
			).format(", ".join(bold(p) for p in sorted(processes)))
		)
	_refuse(problems, title=_("Stock Entry: process check"))
	if not processes:
		return
	process = processes.pop()
	selected = doc.get(STOCK_ENTRY_PROCESS_FIELD)
	if selected and selected != process:
		frappe.throw(
			_(
				"You selected process {0}, but the work order is in process {1}. Material can move only "
				"in the process the job is in."
			).format(bold(selected), bold(process)),
			ProcessEnforcementError,
		)
	if process not in set(get_allowed_processes(user)):
		_refuse_access(user, [process])
	if doc.meta.has_field(STOCK_ENTRY_PROCESS_FIELD):
		doc.set(STOCK_ENTRY_PROCESS_FIELD, process)


def _is_generated(doc: Document, work_orders: set[str]) -> bool:
	"""Made (or cancelled) by an Employee IR / Department IR that passed its own Day 3 checks in
	this same request, for that document's work orders only. A link to any other document,
	``auto_created`` or a source string proves nothing."""
	for doctype, field in (
		("Employee IR", "employee_ir"),
		("Department IR", "department_ir"),
	):
		covered = _registered_work_orders(doctype, doc.get(field))
		if covered is not None and work_orders <= covered:
			return True
	return False


def _is_deferred_posting(doc: Document, in_scope) -> bool:
	"""An EOD sync posting of events already recorded in MOP Logs. The job may have moved on
	since, so its current process is not asked for; what is checked is that the posting is
	genuine:

	* inside the sync run: the sync's own in-memory flag, the entry's sync log Running, and an
	  unsynced MOP Log for every (work order, operation) the entry names;
	* a draft the sync saved and a user submits later: the sync log lists this exact entry, and
	  no row carries more than the log recorded for that work order, operation, item and batch.

	Fields a client can write (``custom_is_eod_sync_stock_entry``, the log link) alone never
	qualify."""
	log = doc.get("custom_eod_sync_log")
	if not cint(doc.get("custom_is_eod_sync_stock_entry")) or not log:
		return False
	pairs = {
		(cstr(row.get("custom_manufacturing_work_order")).strip() or None, op)
		for row, op, _mwo in in_scope
		if row is not doc
	}
	if not pairs:
		return False
	if frappe.flags.in_eod_mop_sync:
		if frappe.db.get_value("MOP EOD Sync Log", log, "status") != EOD_RUNNING_STATUS:
			return False
		pending = {
			(r.manufacturing_work_order, r.manufacturing_operation)
			for r in frappe.get_all(
				"MOP Log",
				filters={
					"manufacturing_operation": ["in", _names(op for _w, op in pairs)],
					"is_synced": 0,
					"is_cancelled": 0,
				},
				fields=["manufacturing_work_order", "manufacturing_operation"],
				limit_page_length=0,
			)
		}
		return pairs <= pending
	if doc.is_new():
		return False
	recorded = {}
	for item in frappe.get_all(
		"MOP EOD Sync Log Item",
		filters={"parent": log, "draft_stock_entry": doc.name},
		fields=[
			"manufacturing_work_order",
			"manufacturing_operation",
			"item_code",
			"batch_no",
			"qty",
		],
		limit_page_length=0,
	):
		key = (
			item.manufacturing_work_order,
			item.manufacturing_operation,
			item.item_code,
			item.batch_no or None,
		)
		recorded[key] = recorded.get(key, 0) + flt(item.qty)
	if not recorded:
		return False
	claimed = {}
	for row in doc.get("items") or ():
		operation = cstr(row.get("manufacturing_operation")).strip()
		if not operation:
			continue
		key = (
			cstr(row.get("custom_manufacturing_work_order")).strip() or None,
			operation,
			row.item_code,
			row.batch_no or None,
		)
		claimed[key] = claimed.get(key, 0) + flt(row.qty)
	return all(
		key in recorded and qty <= recorded[key] + QTY_TOLERANCE
		for key, qty in claimed.items()
	)


# --------------------------------------------------------------------------------------------
# Material Request › Transfer to MOP
# --------------------------------------------------------------------------------------------


def check_material_request_transfer(doc: Document, operation: str) -> None:
	"""Called by the Transfer to MOP step (``before_update_after_submit``) just before it builds
	the work-order Stock Entry. An old request is judged by the job's state now, under the same
	locks a competing transfer takes; the Stock Entry it builds is checked again on submit."""
	if not is_pilot_enabled():
		return
	mop = read_operations([operation], locked=False).get(cstr(operation).strip())
	if not mop or not controlled_departments([mop.department]):
		return
	lock_jobs(doc, [mop.name], [mop.manufacturing_work_order])
	mop = read_operations([mop.name], locked=True).get(mop.name)
	mwo = read_work_orders([mop.manufacturing_work_order], locked=True).get(
		mop.manufacturing_work_order
	)
	problems = []
	if (
		doc.get("manufacturing_order")
		and mop.manufacturing_order != doc.manufacturing_order
	):
		problems.append(
			_(
				"Operation {0} belongs to Parent Manufacturing Order {1}, not this request's {2}."
			).format(
				bold(mop.name),
				bold(mop.manufacturing_order),
				bold(doc.manufacturing_order),
			)
		)
	named = doc.get("custom_manufacturing_work_order")
	if named and mop.manufacturing_work_order != named:
		problems.append(
			_(
				"Operation {0} belongs to work order {1}, not this request's {2}."
			).format(bold(mop.name), bold(mop.manufacturing_work_order), bold(named))
		)
	if not mwo or mwo.manufacturing_operation != mop.name:
		problems.append(
			_(
				"Operation {0} is no longer the current operation of work order {1}. Pick its current "
				"operation."
			).format(bold(mop.name), bold(mop.manufacturing_work_order))
		)
	elif not mop.department_process:
		problems.append(
			_(
				"Work order {0} has no process yet. Give it its first process with a Process Transfer "
				"marked Initial Process Assignment."
			).format(bold(mop.manufacturing_work_order))
		)
	_refuse(problems, title=_("Material Request: process check"))
	user = frappe.session.user
	if mop.department_process not in set(get_allowed_processes(user)):
		_refuse_access(user, [mop.department_process])


# --------------------------------------------------------------------------------------------
# UI helper
# --------------------------------------------------------------------------------------------


@frappe.whitelist(methods=["GET", "POST"])
def my_processes(department: str | None = None) -> dict:
	"""The caller's own processes (never another user's) for pickers and defaults; usability only,
	the server checks above decide."""
	from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
		get_default_process,
	)

	return {
		"controlled": bool(is_pilot_enabled() and controlled_departments([department])),
		"allowed": get_allowed_processes(frappe.session.user, department or None),
		"default": get_default_process(frappe.session.user, department or None),
	}
