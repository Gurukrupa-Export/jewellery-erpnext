"""Parent Manufacturing Order Hold, Open and Close.

The PMO workflow (patches/create_pmo_close_open_workflow) moves the PMO; this module moves
what belongs to it, from the PMO's on_update (a draft's transitions) and
on_update_after_submit (a submitted PMO's):

* Hold -- its open work orders go to Hold, each one's current operation to Hold, and its
  Material Requests' workflow state to Hold. What each one was is kept in the PMO's
  hold_snapshot. Nothing is cancelled, so reserved stock stays where it is.
* Open -- puts back whatever is still on Hold, from that snapshot.
* Close -- final: work orders Closed, their waiting operations Finished, Material Requests
  Closed. A request whose Stock Entries moved stock can only be closed once those entries are
  cancelled: enqueue_close puts the PMO on Hold and cancels them in a background job first.

Hold is refused only while an operation is in progress or in transit (a held operation could
not be received); its work orders may be in any department. Close also needs every open
work order back in planning -- the department a Split is allowed in
(SPLIT_ALLOWED_DEPARTMENT), whatever the company suffix -- with no material on it or its
current operation and no draft transaction for them. The Close / Hold Request Departments
of MOP Settings are only where a requested PMO's work orders may be transferred; they do not
count as planning.

Close / Hold requests. When the only reason a Close or Hold is refused is that the work
orders are not back yet (elsewhere, in progress or in transit, still carrying metal, draft
transactions), the PMO can be put in Close Requested / Hold Requested instead: nothing of
it is written, the departments holding those work orders are alerted, and until the request
is done or withdrawn its work orders can only be transferred to a Close / Hold Request
Department or planning -- never issued to an employee -- and no raw material moves to them (Material
Requests and the Stock Entry types MOP Settings lists). The normal Close / Hold then runs,
with all of its checks, once the work is back.

Rows are locked NOWAIT before anything is decided (lock_order RULE D/E): the PMO's Material
Requests, then the operations to write, then the work orders. The decisions come from those
locking reads and the writes touch only rows already held, so nothing here waits while it
holds them. The transaction already holds the PMO row (the workflow saved it), and an IR that
holds a work order writes the PMO row too (Manufacturing Operation on_update ->
set_pmo_weight_details_in_bulk): a waiting lock here could close a deadlock cycle. A busy row
refuses the action and the user tries again.
"""

import json

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import (
	enqueue_create_notification,
)
from frappe.model.workflow import apply_workflow, get_transitions
from frappe.utils import cint, comma_or, flt, now_datetime, strip_html
from frappe.utils.background_jobs import is_job_enqueued

from jewellery_erpnext.jewellery_erpnext.lock_order import (
	MANUFACTURING_OPERATION_LOCK_FIELDS,
	WORK_ORDER_LOCK_FIELDS,
	lock_manufacturing_operations,
	lock_material_requests,
	lock_work_orders,
)
from jewellery_erpnext.utils import set_values_in_bulk

PMO = "Parent Manufacturing Order"
MWO = "Manufacturing Work Order"
MOP = "Manufacturing Operation"
MR = "Material Request"

# The PMO workflow's states and actions.
SUBMITTED = "Submitted"
HOLD_STATE = "Hold"
HOLD_DRAFT_STATE = "Hold (Draft)"
CLOSED_STATE = "Closed"
CLOSED_DRAFT_STATE = "Closed (Draft)"
HOLD_STATES = (HOLD_STATE, HOLD_DRAFT_STATE)
CLOSED_STATES = (CLOSED_STATE, CLOSED_DRAFT_STATE)
BLOCKING_STATES = (*HOLD_STATES, *CLOSED_STATES)
HOLD, OPEN, CLOSE = "Hold", "Open", "Close"

# The request states: submitted, not blocked -- the work orders keep moving, but only back.
CLOSE_REQUESTED = "Close Requested"
HOLD_REQUESTED = "Hold Requested"
REQUESTED_STATES = (CLOSE_REQUESTED, HOLD_REQUESTED)
REQUEST_CLOSE, REQUEST_HOLD, WITHDRAW = (
	"Request Close",
	"Request Hold",
	"Withdraw Request",
)
REQUEST_ACTIONS = (REQUEST_CLOSE, REQUEST_HOLD)
# The request a refused Close / Hold offers, and the action a request waits to do.
REQUEST_OF = {CLOSE: REQUEST_CLOSE, HOLD: REQUEST_HOLD}
TARGET_OF = {REQUEST_CLOSE: CLOSE, REQUEST_HOLD: HOLD}
# A Hold (or Hold request) made while a Close request is pending keeps that Close request:
# the key in hold_snapshot sends Open (and Withdraw Request) back to Close Requested. The
# workflow's conditional transitions read it (patches/create_pmo_close_open_workflow.py).
RESUME_CLOSE_KEY = "resume_close_request"
STATUS_REASONS_DOCTYPE = "PMO Status Reason"
# Stock Entry validate -> on_submit: the requested PMOs the entry moves stock for.
REQUESTED_PMOS_FLAG = "pmo_requested_pmos"

# What the PMO's documents are set to: the work order and operation status, and the Material
# Request's workflow state.
HOLD_STATUS = "Hold"
CLOSED_STATUS = "Closed"
FINISHED = "Finished"

# Work orders already finished one way or the other: Hold and Close leave them alone.
UNTOUCHED_MWO_STATUSES = ("Completed", "Closed", "Cancelled")
UNTOUCHED_MR_STATES = ("Cancelled", CLOSED_STATUS)
# An operation in one of these statuses is not in progress, unless it is In-Transit. Only a
# PMO Hold puts an operation on Hold, and only one that was not in progress.
SETTLED_OPERATION_STATUSES = ("Finished", "Not Started", "Revert", HOLD_STATUS)
IN_TRANSIT = "In-Transit"
REVERT = "Revert"
TRANSFER_PENDING = "Pending"

# cancel_all.enqueue_cancel_all's job id: a Hybrid Cancel and a Close never run together.
CANCEL_ALL_JOB_ID = "pmo_cancel_all::{0}"

# The actions that take a reason, the reasons MOP Settings offers for each (its table of status
# and reason), and the flags that carry the chosen reason through apply_workflow. The internal
# flag marks enqueue_close's own Hold, which is part of the Close.
REASON_ACTIONS = (HOLD, OPEN, CLOSE, REQUEST_CLOSE, REQUEST_HOLD, WITHDRAW)
REASONS_DOCTYPE = "PMO Action Reason"
REASONS_SETTINGS = "MOP Settings"
REASONS_FIELD = "pmo_action_reasons"
REASON_FLAG = "pmo_action_reason"
INTERNAL_FLAG = "pmo_internal_action"
# MOP Settings keeps reasons per Hold / Open / Close: a request takes those of the action it
# waits for, and a withdrawal those of Open (normal work resumes).
REASON_LIST_OF = {REQUEST_CLOSE: CLOSE, REQUEST_HOLD: HOLD, WITHDRAW: OPEN}

# MOP Settings: where a requested PMO's work orders may go, and which Stock Entry types move
# raw material to them.
CLOSE_HOLD_DEPARTMENTS_DOCTYPE = "PMO Close Hold Department"
CLOSE_HOLD_DEPARTMENTS_FIELD = "pmo_close_hold_departments"
BLOCKED_TYPES_DOCTYPE = "PMO Request Blocked Stock Entry Type"
BLOCKED_TYPES_FIELD = "pmo_request_blocked_stock_entry_types"

# Why a Hold / Close cannot go ahead, by kind. The work-order kinds clear once the work
# orders are back with no metal and no drafts -- what a request waits for; the others refuse.
PROBLEM_DEPARTMENT = "department"
PROBLEM_IN_PROGRESS = "in_progress"
PROBLEM_TRANSFER = "transfer_pending"
PROBLEM_WEIGHT = "weight"
PROBLEM_DRAFTS = "drafts"
PROBLEM_STOCK = "stock"
REQUESTABLE_PROBLEMS = (
	PROBLEM_DEPARTMENT,
	PROBLEM_IN_PROGRESS,
	PROBLEM_WEIGHT,
	PROBLEM_DRAFTS,
)


def on_workflow_state_change(pmo):
	"""Hold / Open / Close what belongs to the PMO when the workflow moves it.

	Called from on_update (a draft's transitions) and on_update_after_submit (a submitted
	PMO's); the state field already holds the new state. An insert has no earlier version and
	nothing to move.

	Every Hold, Open and Close carries the reason chosen for it (apply_action / the close job
	put it in ``pmo.flags``, which apply_workflow keeps through its reload) and adds a row to
	the PMO's Hold / Open / Close Reasons -- here, in the same transaction as the action, so a
	refused action leaves no row.
	"""
	before = pmo.get_doc_before_save()
	if not before:
		return
	previous, current = before.get("workflow_state"), pmo.get("workflow_state")
	if previous == current:
		return
	action = _action_of(previous, current)
	reason = _action_reason(pmo, action)
	if current in HOLD_STATES:
		_hold(pmo, reason, previous)
	elif current in CLOSED_STATES:
		_close(pmo, reason)
	elif previous in HOLD_STATES and current == CLOSE_REQUESTED:
		# Open of a Hold made during a Close request: the Close request carries on.
		_open(pmo, reason, resumed=True)
	elif previous == HOLD_REQUESTED and current == CLOSE_REQUESTED:
		# Withdraw Request of a Hold request made during a Close request.
		_withdraw(pmo, previous, reason, resumed=True)
	elif current in REQUESTED_STATES:
		_request(pmo, action, reason, previous)
	elif previous in REQUESTED_STATES:
		# Withdraw Request (Close Requested / Hold Requested -> Submitted).
		_withdraw(pmo, previous, reason)
	elif previous in HOLD_STATES:
		# Open (Hold -> Submitted), or the submit of a draft that was held.
		_open(pmo, reason)
	if action and reason:
		_record_reason(pmo, action, reason)


def _action_of(previous, current):
	"""The action a transition is: Hold, Open, Close, a request or its withdrawal, or None
	(a Submit, even from Hold (Draft), takes no reason)."""
	if current in HOLD_STATES:
		return HOLD
	if current in CLOSED_STATES:
		return CLOSE
	if previous == HOLD_STATE and current == CLOSE_REQUESTED:
		return OPEN
	if previous == HOLD_REQUESTED and current == CLOSE_REQUESTED:
		return WITHDRAW
	if current == CLOSE_REQUESTED:
		return REQUEST_CLOSE
	if current == HOLD_REQUESTED:
		return REQUEST_HOLD
	if previous in REQUESTED_STATES and current == SUBMITTED:
		return WITHDRAW
	if previous == HOLD_STATE and current == SUBMITTED:
		return OPEN
	return None


def _action_reason(pmo, action):
	"""The reason ``action`` was done with, or None for a transition that takes none.

	A Hold / Open / Close needs one. The only exception is enqueue_close's own Hold, which is
	part of the Close and gets no row of its own.
	"""
	if not action:
		return None
	reason = (pmo.flags.get(REASON_FLAG) or "").strip()
	if reason or pmo.flags.get(INTERNAL_FLAG):
		return reason or None
	frappe.throw(
		_(
			"A reason is needed to {0} a Parent Manufacturing Order: use its {0} action, which asks for one."
		).format(_(action))
	)


def _record_reason(pmo, action, reason):
	"""Add the action's row to the PMO's Hold / Open / Close Reasons, written directly: the
	PMO itself is already saved by now."""
	row = pmo.append(
		"status_reasons",
		{
			"status": action,
			"reason": reason,
			"action_by": frappe.session.user,
			"action_on": now_datetime(),
		},
	)
	row.docstatus = pmo.docstatus
	row.db_insert()


def _with_reason(message, reason):
	return f"{message} {_('Reason: {0}').format(reason)}" if reason else message


def _request(pmo, action, reason=None, previous=None):
	"""Close Requested / Hold Requested: the Close / Hold has to wait for the work orders.

	Nothing of the PMO's is written -- the work orders keep moving, but only back (see the
	refuse_*_while_requested guards). The departments holding the ones that are not back yet
	are alerted, and the requester is told which they are. A Hold request made during a Close
	request keeps the Close request (RESUME_CLOSE_KEY).
	"""
	plan = _request_plan(pmo, action)
	if previous == CLOSE_REQUESTED:
		_set_resume_close(pmo, True)
	departments, waiting = plan["departments"], plan["pending"]
	_notify_request(pmo, action, reason, departments, waiting)
	where = ", ".join(
		sorted({w.department or _("no department") for w in waiting})
	) or _("none outstanding")
	if action == REQUEST_CLOSE:
		message = _(
			"{0}: {1} work orders have to come back to {2} with no metal first ({3})."
		).format(_(CLOSE_REQUESTED), len(waiting), _planning_label(), where)
	else:
		message = _(
			"{0}: {1} work orders are still in progress or in transit ({2})."
		).format(_(HOLD_REQUESTED), len(waiting), where)
	message += " " + _("Until then they can be transferred only to {0}.").format(
		comma_or(_transfer_targets(departments), add_quotes=False)
	)
	pmo.add_comment("Info", _with_reason(message, reason))


def _withdraw(pmo, previous, reason=None, resumed=False):
	_set_resume_close(pmo, False)
	if resumed:
		message = _(
			"{0} withdrawn: the Close request continues, so the work orders still only come back."
		).format(_(previous))
	else:
		message = _("{0} withdrawn: the work orders can move freely again.").format(
			_(previous)
		)
	pmo.add_comment("Info", _with_reason(message, reason))
	_notify_pmo(
		pmo,
		_("Parent Manufacturing Order {0}: {1}").format(pmo.name, message),
		_holding_departments(_pmo_documents(pmo.name, HOLD, lock=False)),
		reason,
	)


def _set_resume_close(pmo, resume):
	"""Remember (or forget) a pending Close request in the PMO's hold_snapshot."""
	snapshot = _snapshot(pmo)
	if bool(snapshot.get(RESUME_CLOSE_KEY)) == resume:
		return
	if resume:
		snapshot[RESUME_CLOSE_KEY] = 1
	else:
		snapshot.pop(RESUME_CLOSE_KEY, None)
	_store_snapshot(pmo, snapshot)


def _work_orders_to_return(target, docs):
	"""The submitted work orders that keep ``target`` (Hold / Close) from going ahead: in
	progress or in transit, or -- for a Close -- not in planning or still carrying metal.
	Where each one is: its current operation's department."""
	submitted = [r for r in docs.mwos.values() if cint(r.docstatus) == 1]
	allowed = (
		_planning_departments({r.department for r in submitted if r.department})
		if target == CLOSE
		else set()
	)
	waiting = []
	for r in submitted:
		operation = docs.mops.get(r.manufacturing_operation)
		weight = flt(r.gross_wt, 3) or (flt(operation.gross_wt, 3) if operation else 0)
		if not (operation and _in_progress(operation)) and (
			target != CLOSE or (r.department in allowed and not weight)
		):
			continue
		waiting.append(
			frappe._dict(
				work_order=r.name,
				department=(operation.department if operation else None)
				or r.department,
				status=operation.status if operation else None,
				in_transit=bool(
					operation and operation.department_ir_status == IN_TRANSIT
				),
				employee=operation.get("employee") if operation else None,
				weight=weight,
			)
		)
	return sorted(waiting, key=lambda w: (w.department or "", w.work_order))


def _notify_request(pmo, action, reason, departments, waiting):
	"""Alert only the departments holding the work orders that are not back yet (their active
	Employees with a login), and tell the requester which departments were alerted."""
	destination = comma_or(_transfer_targets(departments), add_quotes=False)
	by_department = {}
	for w in waiting:
		if w.department:
			by_department.setdefault(w.department, []).append(w)
	users = _department_users(list(by_department))
	what = _("closing") if action == REQUEST_CLOSE else _("Hold")
	requester = frappe.session.user
	alerted = []
	for dept, rows in sorted(by_department.items()):
		recipients = sorted(users.get(dept, set()) - {requester})
		if not recipients:
			continue
		alerted.append(dept)
		enqueue_create_notification(
			recipients,
			{
				"type": "Alert",
				"document_type": PMO,
				"document_name": pmo.name,
				"from_user": requester,
				"subject": _(
					"{0}: Parent Manufacturing Order {1} has been requested for {2}. Transfer its work orders in {3} to {4}."
				).format(_(action), pmo.name, what, dept, destination),
				"email_content": _with_reason(
					_(
						"Work orders in {0}: {1}. Review their production status, reconcile the metal and transfer them to {2}; until then they cannot go to any other department or employee, and no raw material can be transferred to them."
					).format(
						dept,
						", ".join(f"{w.work_order} ({w.weight} g)" for w in rows),
						destination,
					),
					reason,
				),
			},
		)
	enqueue_create_notification(
		requester,
		{
			"type": "Alert",
			"document_type": PMO,
			"document_name": pmo.name,
			"subject": _(
				"{0} for {1}: {2} work orders to come back; alerted {3}."
			).format(
				_(action),
				pmo.name,
				len(waiting),
				", ".join(alerted)
				or _("no department (nothing outstanding, or no users)"),
			),
		},
	)


def _department_users(departments):
	"""{department: {user}}: the enabled login users of each department's active Employees."""
	if not departments:
		return {}
	rows = frappe.get_all(
		"Employee",
		filters={
			"department": ["in", sorted(departments)],
			"status": "Active",
			"user_id": ["is", "set"],
		},
		fields=["department", "user_id"],
	)
	enabled = set(
		frappe.get_all(
			"User",
			filters={"name": ["in", sorted({r.user_id for r in rows})], "enabled": 1},
			pluck="name",
		)
		if rows
		else []
	)
	users = {}
	for r in rows:
		if r.user_id in enabled:
			users.setdefault(r.department, set()).add(r.user_id)
	return users


def _holding_departments(docs):
	"""Where the PMO's open work orders are: each one's current operation's department."""
	departments = set()
	for r in docs.mwos.values():
		if cint(r.docstatus) == 2:
			continue
		operation = docs.mops.get(r.manufacturing_operation)
		departments.add((operation.department if operation else None) or r.department)
	return sorted(d for d in departments if d)


def _active_requests(pmo_name):
	"""{Request Close / Request Hold: its row (who asked and when)} for the PMO's requests still
	pending, replayed from its Hold / Open / Close Reasons in order.

	The latest request row is not enough: a Hold request made during a Close request and then
	withdrawn (or held and opened) leaves the Close request pending under a later Hold row. A
	request is pending from its row until it is withdrawn (the newest one goes), a Hold
	fulfils a pending Hold request, or a Close ends them all."""
	rows = frappe.get_all(
		STATUS_REASONS_DOCTYPE,
		filters={
			"parenttype": PMO,
			"parent": pmo_name,
			"parentfield": "status_reasons",
		},
		fields=["status", "action_by", "action_on"],
		order_by="idx asc",
	)
	pending = []
	for row in rows:
		if row.status in REQUEST_ACTIONS:
			pending = [r for r in pending if r.status != row.status] + [row]
		elif row.status == WITHDRAW:
			pending = pending[:-1]
		elif row.status == HOLD:
			if pending and pending[-1].status == REQUEST_HOLD:
				pending.pop()
		elif row.status == CLOSE:
			pending = []
	return {r.status: r for r in pending}


def _notify_pmo(pmo, subject, departments, reason=None):
	"""Alert the departments holding the PMO's work orders and whoever raised its pending
	Close / Hold requests about a Hold, Open, Close or withdrawal -- never the user who did
	it. Called before the action's own row is recorded, so a request the action ends or
	withdraws still counts. Queued after commit, so an action that is rolled back alerts
	nobody."""
	actor = frappe.session.user
	users = set()
	for department_users in _department_users(departments).values():
		users |= department_users
	users.update(
		r.action_by for r in _active_requests(pmo.name).values() if r.action_by
	)
	users.discard(actor)
	if not users:
		return
	enqueue_create_notification(
		sorted(users),
		{
			"type": "Alert",
			"document_type": PMO,
			"document_name": pmo.name,
			"from_user": actor,
			"subject": _with_reason(subject, reason),
		},
	)


def _hold(pmo, reason=None, previous=None):
	snapshot = _snapshot(pmo)
	internal = bool(pmo.flags.get(INTERNAL_FLAG))
	# A Hold during a Close request keeps it -- not enqueue_close's own Hold, which is the
	# Close itself.
	if internal:
		snapshot.pop(RESUME_CLOSE_KEY, None)
	elif previous == CLOSE_REQUESTED:
		snapshot[RESUME_CLOSE_KEY] = 1
	docs = _pmo_documents(pmo.name, HOLD)
	_refuse_unless_ready(pmo.name, HOLD, docs)

	# From the locked rows: one that finished since the plain read is left alone.
	mwos = {
		n: r.status
		for n, r in docs.mwos.items()
		if r.status not in (*UNTOUCHED_MWO_STATUSES, HOLD_STATUS)
	}
	mops = {
		n: r.status
		for n, r in docs.mops.items()
		if n in docs.pointers and r.status != HOLD_STATUS and not _is_reverted(r)
	}
	mrs = {
		n: r.workflow_state
		for n, r in docs.mrs.items()
		if "workflow_state" in r
		and r.workflow_state not in (*UNTOUCHED_MR_STATES, HOLD_STATUS)
	}
	_set(MWO, mwos, "status", HOLD_STATUS)
	_set(MOP, mops, "status", HOLD_STATUS)
	_set(MR, mrs, "workflow_state", HOLD_STATUS)
	snapshot.update({"mwo": mwos, "mop": mops, "mr": mrs})
	_store_snapshot(pmo, snapshot)
	message = _(
		"Put on Hold: {0} work orders, {1} operations and {2} Material Requests set to Hold."
	).format(len(mwos), len(mops), len(mrs))
	if snapshot.get(RESUME_CLOSE_KEY):
		message += " " + _("The Close request carries on once it is opened.")
	pmo.add_comment("Info", _with_reason(message, reason))
	if not internal:
		_notify_pmo(
			pmo,
			_(
				"Parent Manufacturing Order {0} has been put on Hold: its work orders are frozen until it is opened."
			).format(pmo.name),
			_holding_departments(docs),
			reason,
		)


def _open(pmo, reason=None, resumed=False):
	snapshot = _snapshot(pmo)
	_refuse_open_after_close_started(pmo.name, snapshot)
	docs = _pmo_documents(pmo.name, OPEN, extra_operations=snapshot.get("mop"))

	held = snapshot.get("mwo") or {}
	mwos = {
		n: held[n]
		for n, r in docs.mwos.items()
		if n in held and r.status == HOLD_STATUS and cint(r.docstatus) < 2
	}
	held = snapshot.get("mop") or {}
	mops = {
		n: held[n]
		for n, r in docs.mops.items()
		if n in held and r.status == HOLD_STATUS
	}
	held = snapshot.get("mr") or {}
	mrs = {
		n: held[n]
		for n, r in docs.mrs.items()
		if n in held
		and r.get("workflow_state") == HOLD_STATUS
		and cint(r.docstatus) < 2
	}
	_restore(MWO, mwos, "status")
	_restore(MOP, mops, "status")
	_restore(MR, mrs, "workflow_state")
	_store_snapshot(pmo, None)
	resumed_note = (
		" " + _("The Close request carries on: its work orders still only come back.")
		if resumed
		else ""
	)
	if mwos or mops or mrs or reason or resumed:
		pmo.add_comment(
			"Info",
			_with_reason(
				_(
					"Opened: {0} work orders, {1} operations and {2} Material Requests put back as they were."
				).format(len(mwos), len(mops), len(mrs))
				+ resumed_note,
				reason,
			),
		)
	# An Open always carries a reason; the submit of a held draft takes none and alerts nobody.
	if reason or resumed:
		_notify_pmo(
			pmo,
			_(
				"Parent Manufacturing Order {0} has been opened: its work orders can move again."
			).format(pmo.name)
			+ resumed_note,
			_holding_departments(docs),
			reason,
		)


def _close(pmo, reason=None):
	docs = _pmo_documents(pmo.name, CLOSE)
	_refuse_unless_ready(pmo.name, CLOSE, docs)

	mwos = {
		n: r.status
		for n, r in docs.mwos.items()
		if r.status not in UNTOUCHED_MWO_STATUSES
	}
	mops = {
		n: r.status
		for n, r in docs.mops.items()
		if r.manufacturing_work_order in mwos
		and r.status != FINISHED
		and not _is_reverted(r)
	}
	mrs = {
		n: r.workflow_state
		for n, r in docs.mrs.items()
		if "workflow_state" in r and r.workflow_state not in UNTOUCHED_MR_STATES
	}
	_set(MWO, mwos, "status", CLOSED_STATUS)
	_set(MOP, mops, "status", FINISHED)
	_set(MR, mrs, "workflow_state", CLOSED_STATUS)
	_store_snapshot(pmo, None)
	pmo.add_comment(
		"Info",
		_with_reason(
			_(
				"Closed: {0} work orders closed, {1} operations finished and {2} Material Requests closed."
			).format(len(mwos), len(mops), len(mrs)),
			reason,
		),
	)
	_notify_pmo(
		pmo,
		_(
			"Parent Manufacturing Order {0} has been closed: {1} work orders closed."
		).format(pmo.name, len(mwos)),
		_holding_departments(docs),
		reason,
	)


def _pmo_documents(pmo_name, action, lock=True, extra_operations=None):
	"""The PMO's open work orders, the operations ``action`` writes and its Material Requests.

	With ``lock`` they are taken NOWAIT -- Material Requests, then operations, then work
	orders -- and their state comes from those locking reads; without it, plain reads with
	the same columns (the dry run). The operations are each work order's current one, and for
	Close also every other operation still waiting (as a Split finishes them).
	"""
	mwo_rows = frappe.get_all(
		MWO,
		filters={
			"manufacturing_order": pmo_name,
			"docstatus": ["<", 2],
			"status": ["not in", UNTOUCHED_MWO_STATUSES],
		},
		fields=["name", "manufacturing_operation"],
	)
	mwos = [m.name for m in mwo_rows]
	mops = {m.manufacturing_operation for m in mwo_rows if m.manufacturing_operation}
	mops.update(extra_operations or ())
	if action == CLOSE and mwos:
		mops.update(
			frappe.get_all(
				MOP,
				filters={
					"manufacturing_work_order": ["in", mwos],
					"status": ["in", ("Not Started", HOLD_STATUS)],
					"department_ir_status": ["!=", REVERT],
				},
				pluck="name",
			)
		)
	mrs = frappe.get_all(
		MR,
		filters={"manufacturing_order": pmo_name, "docstatus": ["<", 2]},
		pluck="name",
	)
	mr_fields = _material_request_fields()

	if not lock:
		return _documents(
			_rows(MR, mrs, mr_fields),
			_rows(MOP, mops, MANUFACTURING_OPERATION_LOCK_FIELDS),
			_rows(MWO, mwos, WORK_ORDER_LOCK_FIELDS),
		)
	try:
		locked_mrs = lock_material_requests(mrs, wait=0, fields=mr_fields)
		locked_mops = lock_manufacturing_operations(mops, wait=0)
		locked_mwos = lock_work_orders(mwos, wait=0)
		# A current operation that moved since the read above is taken too. NOWAIT, so taking
		# it after its work order cannot make this transaction wait.
		moved = {
			r.manufacturing_operation
			for r in locked_mwos.values()
			if r.manufacturing_operation
		} - set(locked_mops)
		if moved:
			locked_mops.update(lock_manufacturing_operations(moved, wait=0))
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		frappe.throw(
			_(
				"{0} is being updated by another user right now. Try again in a moment."
			).format(frappe.bold(getattr(exc, "lock_row_name", None) or pmo_name)),
			title=_("Busy"),
		)
	return _documents(locked_mrs, locked_mops, locked_mwos)


def _documents(mrs, mops, mwos):
	return frappe._dict(
		mrs=mrs,
		mops=mops,
		mwos=mwos,
		# The current operations of the submitted work orders.
		pointers={
			r.manufacturing_operation
			for r in mwos.values()
			if cint(r.docstatus) == 1 and r.manufacturing_operation
		},
	)


def _rows(doctype, names, fields):
	if not names:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			doctype,
			filters={"name": ["in", sorted(names)]},
			fields=list(fields),
			limit_page_length=0,
		)
	}


def _material_request_fields():
	meta = frappe.get_meta(MR)
	return (
		"name",
		"docstatus",
		*(
			f
			for f in ("workflow_state", "custom_transfer_se_state")
			if meta.has_field(f)
		),
	)


def _refuse_unless_ready(pmo_name, action, docs, stock=True):
	problems = _problems(pmo_name, action, docs, stock=stock)
	if problems:
		frappe.throw(
			"<br><br>".join(problems),
			title=_("Hold Refused") if action == HOLD else _("Close Refused"),
		)


def _problems(pmo_name, action, docs, stock=True):
	"""Why ``action`` (Hold / Close) cannot go ahead, one paragraph per rule."""
	return [
		message for _kind, message in _problem_list(pmo_name, action, docs, stock=stock)
	]


def _problem_list(pmo_name, action, docs, stock=True):
	"""``_problems`` with the kind of each (PROBLEM_*): a request is offered only when every
	one of them is a kind the work orders' return clears (REQUESTABLE_PROBLEMS)."""
	verb = _("placed on Hold") if action == HOLD else _("closed")
	submitted = [r for r in docs.mwos.values() if cint(r.docstatus) == 1]
	problems = []

	# Only a Close needs the work orders back in planning; a Hold may freeze them anywhere.
	elsewhere = []
	if action == CLOSE:
		allowed = _planning_departments(
			{r.department for r in submitted if r.department}
		)
		elsewhere = [r for r in submitted if r.department not in allowed]
	if elsewhere:
		message = _(
			"PMO cannot be closed because all linked Manufacturing Work Orders must be in the Manufacturing Plan and Management Department."
		)
		problems.append(
			(
				PROBLEM_DEPARTMENT,
				message
				+ _bullets(
					f"{r.name} ({r.department or _('no department')})"
					for r in elsewhere
				),
			)
		)

	busy = _operations_in_progress([r.name for r in submitted], docs.mops)
	if busy:
		problems.append(
			(
				PROBLEM_IN_PROGRESS,
				_(
					"PMO cannot be {0} while these operations are in progress or in transit:"
				).format(verb)
				+ _bullets(f"{o.name} ({o.manufacturing_work_order})" for o in busy),
			)
		)

	pending = sorted(
		n
		for n, r in docs.mrs.items()
		if r.get("custom_transfer_se_state") == TRANSFER_PENDING
	)
	if pending:
		problems.append(
			(
				PROBLEM_TRANSFER,
				_(
					"PMO cannot be {0} while these Material Requests are still transferring their stock from reserve. Try again once they are done:"
				).format(verb)
				+ _bullets(pending),
			)
		)

	if action == CLOSE:
		heavy = []
		for r in submitted:
			# The header gross_wt is only written for FG work orders; the material on any
			# other one sits on its current operation (see _validate_split_basics).
			operation = docs.mops.get(r.manufacturing_operation)
			weight = flt(r.gross_wt, 3) or (
				flt(operation.gross_wt, 3) if operation else 0
			)
			if weight:
				heavy.append((r.name, weight))
		if heavy:
			problems.append(
				(
					PROBLEM_WEIGHT,
					_(
						"PMO cannot be closed because the Gross Weight of every linked Manufacturing Work Order must be 0."
					)
					+ _bullets(f"{name} ({weight})" for name, weight in heavy)
					+ "<br>"
					+ _(
						"Outstanding metal: {0} g. Transfer it back or adjust it first."
					).format(flt(sum(weight for _name, weight in heavy), 3)),
				)
			)
		drafts = _draft_transactions(pmo_name, sorted(docs.mwos))
		if drafts:
			problems.append(
				(
					PROBLEM_DRAFTS,
					_(
						"PMO cannot be closed while these transactions of its work orders are still drafts. Submit or delete them first:"
					)
					+ _bullets(drafts),
				)
			)
		if stock:
			entries = _close_stock_plan(list(docs.mrs)).entries
			if entries:
				problems.append(
					(
						PROBLEM_STOCK,
						_(
							"PMO cannot be closed while its Material Requests still have submitted Stock Entries. Close it from the Close action, which cancels them first:"
						)
						+ _bullets(entries),
					)
				)
	return problems


def _planning_departments(departments):
	"""Those of ``departments`` that are the planning department (by its name, whatever the
	company suffix): where a Close needs the work orders. The Close / Hold Request
	Departments of MOP Settings do not count -- they are transfer destinations only."""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
		SPLIT_ALLOWED_DEPARTMENT,
	)

	departments = sorted(d for d in departments if d)
	allowed = set()
	if departments:
		names = dict(
			frappe.get_all(
				"Department",
				filters={"name": ["in", departments]},
				fields=["name", "department_name"],
				as_list=True,
			)
		)
		allowed = {d for d in departments if names.get(d) == SPLIT_ALLOWED_DEPARTMENT}
	return allowed


def _is_planning_department(department):
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
		SPLIT_ALLOWED_DEPARTMENT,
	)

	return (
		frappe.db.get_value("Department", department, "department_name")
		== SPLIT_ALLOWED_DEPARTMENT
	)


def _draft_transactions(pmo_name, mwos):
	"""Draft Stock Entries, Department IRs and Employee IRs of the PMO or its work orders:
	a Close needs every stock transaction of the order submitted (or deleted) first.

	Read from the draft parents: the child tables are large and Employee IR Operation has no
	index on its work order."""
	found = set(
		frappe.get_all(
			"Stock Entry",
			filters={"docstatus": 0, "manufacturing_order": pmo_name},
			pluck="name",
		)
	)
	if mwos:
		found.update(
			frappe.get_all(
				"Stock Entry",
				filters={"docstatus": 0, "manufacturing_work_order": ["in", mwos]},
				pluck="name",
			)
		)
	labels = [f"{name} ({_('Stock Entry')})" for name in sorted(found)]
	if mwos:
		for parent, child in (
			("Department IR", "Department IR Operation"),
			("Employee IR", "Employee IR Operation"),
		):
			names = frappe.db.sql(
				f"""SELECT DISTINCT p.name FROM `tab{parent}` p
				JOIN `tab{child}` r ON r.parent = p.name AND r.parenttype = %(parent)s
				WHERE p.docstatus = 0 AND r.manufacturing_work_order IN %(mwos)s
				ORDER BY p.name""",
				{"parent": parent, "mwos": tuple(mwos)},
				pluck=True,
			)
			labels.extend(f"{name} ({_(parent)})" for name in names)
	return labels


def _operations_in_progress(mwos, fresh_operations):
	"""Same definition of "in progress" as Split Work Order: past Not Started and not Finished
	/ Revert, or still In-Transit between departments.

	The plain read covers every operation of the work orders; ``fresh_operations`` (the locked
	rows) are the latest committed state, so they override what the plain read -- this
	transaction's snapshot -- says about the same operations.
	"""
	if not mwos:
		return []
	busy = {
		o.name: o
		for o in frappe.get_all(
			MOP,
			filters={"manufacturing_work_order": ["in", mwos]},
			or_filters={
				"status": ["not in", list(SETTLED_OPERATION_STATUSES)],
				"department_ir_status": IN_TRANSIT,
			},
			fields=["name", "manufacturing_work_order"],
		)
	}
	mwos = set(mwos)
	for row in fresh_operations.values():
		if row.manufacturing_work_order not in mwos:
			continue
		if _in_progress(row):
			busy[row.name] = row
		else:
			busy.pop(row.name, None)
	return sorted(busy.values(), key=lambda o: o.name)


def _in_progress(row):
	return (
		row.status or ""
	) not in SETTLED_OPERATION_STATUSES or row.department_ir_status == IN_TRANSIT


def _is_reverted(row):
	return row.status == REVERT or row.department_ir_status == REVERT


def _bullets(items):
	return "<br>" + "<br>".join(f"&bull; {frappe.bold(item)}" for item in items)


def _set(doctype, rows, field, value):
	if rows:
		set_values_in_bulk(doctype, sorted(rows), {field: value})


def _restore(doctype, rows, field):
	"""Put each row back to its own value, one write per value."""
	by_value = {}
	for name, value in rows.items():
		by_value.setdefault(value, []).append(name)
	for value, names in by_value.items():
		set_values_in_bulk(doctype, sorted(names), {field: value})


def _snapshot(pmo):
	value = pmo.get("hold_snapshot")
	if isinstance(value, str):
		value = json.loads(value) if value.strip() else None
	return dict(value or {})


def _store_snapshot(pmo, snapshot):
	pmo.db_set(
		"hold_snapshot",
		json.dumps(snapshot, sort_keys=True) if snapshot else None,
		update_modified=False,
	)


# --------------------------------------------------------------------------------------------
# the form's calls
# --------------------------------------------------------------------------------------------


@frappe.whitelist()
def check_action(pmo, action):
	"""Dry run of ``action``'s checks, called before its dialog so a refusal comes first.

	Returns the reasons MOP Settings offers for the action (the dialog's dropdown). For Close
	it also returns the Stock Entries that have to be cancelled first (latest first) and the
	Stopped requests that have to be re-opened for that; with any entries the form offers
	enqueue_close instead of closing straight away.

	A Close / Hold that is refused only because the work orders are not back yet returns a
	``request`` instead (Request Close / Request Hold, with its reasons and the work orders it
	waits for) when the user may make that request here. A request action itself returns the
	Close / Hold Request Departments and the work orders to come back (``pending``).
	"""
	doc = frappe.get_doc(PMO, pmo)
	_require_action(doc, action)
	if action not in REASON_ACTIONS:
		return {}
	reasons = _configured_reasons(action)
	if action == OPEN:
		_refuse_open_after_close_started(doc.name, _snapshot(doc))
		return {"reasons": reasons}
	if action == WITHDRAW:
		# A Hold request made during a Close request goes back to Close Requested.
		return {
			"reasons": reasons,
			"resumes_close": bool(_snapshot(doc).get(RESUME_CLOSE_KEY)),
		}
	_refuse_while_job_runs(doc.name)
	if action in REQUEST_ACTIONS:
		return {"reasons": reasons, **_request_plan(doc, action)}
	docs = _pmo_documents(doc.name, action, lock=False)
	offer = _request_offer(doc, action, docs)
	if offer:
		return {"reasons": reasons, "request": offer}
	_refuse_unless_ready(doc.name, action, docs, stock=False)
	if action != CLOSE:
		return {"reasons": reasons}
	plan = _close_stock_plan(list(docs.mrs))
	details = (
		{
			row.name: row
			for row in frappe.get_all(
				"Stock Entry",
				filters={"name": ["in", plan.entries]},
				fields=["name", "stock_entry_type", "posting_date"],
			)
		}
		if plan.entries
		else {}
	)
	return {
		"reasons": reasons,
		"entries": [details[se] for se in plan.entries if se in details],
		"stopped": plan.stopped,
	}


def _request_plan(doc, action, docs=None):
	"""What a Request Close / Request Hold waits for: refused while a transfer-from-reserve
	job runs (try again) or without a Close / Hold Request Department."""
	target = TARGET_OF[action]
	departments = _required_close_hold_departments()
	docs = docs or _pmo_documents(doc.name, target, lock=False)
	transfers = [
		message
		for kind, message in _problem_list(doc.name, target, docs, stock=False)
		if kind == PROBLEM_TRANSFER
	]
	if transfers:
		frappe.throw(
			"<br><br>".join(transfers), title=_("{0} Refused").format(_(action))
		)
	return {"departments": departments, "pending": _work_orders_to_return(target, docs)}


def _request_offer(doc, action, docs):
	"""The request to offer instead of refusing ``action`` (Close / Hold), or None.

	Offered only when the user may make that request in the PMO's state, a Close / Hold
	Request Department is set, and every problem is one the work orders' return clears; a
	pending transfer, Stock Entries to cancel and the like keep the normal refusal."""
	request = REQUEST_OF.get(action)
	if not request or request not in _available_actions(doc):
		return None
	departments = close_hold_departments()
	if not departments:
		return None
	problems = _problem_list(doc.name, action, docs, stock=False)
	if not problems or any(kind not in REQUESTABLE_PROBLEMS for kind, _m in problems):
		return None
	return {
		"action": request,
		"problems": [message for _kind, message in problems],
		"reasons": _configured_reasons(request),
		"departments": departments,
		"pending": _work_orders_to_return(action, docs),
	}


def _available_actions(doc):
	return {t.get("action") for t in get_transitions(doc)}


@frappe.whitelist()
def apply_action(pmo, action, reason):
	"""Do the workflow ``action`` (Hold / Open / Close, Request Close / Request Hold,
	Withdraw Request) with the reason chosen for it.

	The reason travels with the transition: apply_workflow reloads the PMO from the database
	but keeps its flags, and the action's own handler adds the reason row in the same
	transaction (on_workflow_state_change). A refused action therefore leaves no row.
	"""
	if action not in REASON_ACTIONS:
		frappe.throw(_("{0} is not done with a reason.").format(frappe.bold(_(action))))
	doc = frappe.get_doc(PMO, pmo)
	_require_action(doc, action)
	reason = _valid_reason(action, reason)
	_refuse_while_job_runs(doc.name)
	doc.flags[REASON_FLAG] = reason
	apply_workflow(doc, action)


def action_reasons(action):
	"""The reasons MOP Settings offers for ``action``, in its table's order. A request takes
	the reasons of the action it waits for, a withdrawal those of Open (REASON_LIST_OF)."""
	return frappe.get_all(
		REASONS_DOCTYPE,
		filters={
			"parenttype": REASONS_SETTINGS,
			"parent": REASONS_SETTINGS,
			"parentfield": REASONS_FIELD,
			"status": REASON_LIST_OF.get(action, action),
		},
		pluck="reason",
		order_by="idx asc",
	)


def _configured_reasons(action):
	reasons = action_reasons(action)
	if not reasons:
		frappe.throw(
			_(
				"There are no reasons for {0} yet: add them in MOP Settings, under PMO Hold / Open / Close Reasons."
			).format(frappe.bold(_(REASON_LIST_OF.get(action, action))))
		)
	return reasons


def _valid_reason(action, reason):
	"""``reason`` when it is one MOP Settings offers for ``action``; refused otherwise."""
	reason = (reason or "").strip()
	if reason not in _configured_reasons(action):
		frappe.throw(
			_("Choose one of the reasons MOP Settings lists for {0}.").format(
				frappe.bold(_(action))
			)
		)
	return reason


@frappe.whitelist()
def enqueue_close(pmo, reason):
	"""Close a PMO whose Material Requests moved stock: cancel those entries first.

	The PMO is put on Hold first, so for as long as the job runs nothing else can move for it
	(every PMO guard reads Hold). Then a background job re-opens the Stopped requests, cancels
	the entries latest first, one per transaction -- a cancel takes the better part of a
	minute on a busy item and holds its tabBin rows until it commits -- and applies the Close.
	If anything fails the PMO stays on Hold with what was done recorded, and Close again
	carries on from there.

	The reason chosen for the Close is kept with the job and recorded when the PMO closes;
	the Hold in between is part of the Close and gets no reason row of its own.
	"""
	reason = _valid_reason(CLOSE, reason)
	doc = frappe.get_doc(PMO, pmo)
	_require_action(doc, CLOSE)
	_refuse_while_job_runs(doc.name)
	docs = _pmo_documents(doc.name, CLOSE, lock=False)
	_refuse_unless_ready(doc.name, CLOSE, docs, stock=False)
	plan = _close_stock_plan(list(docs.mrs))
	if not plan.entries:
		frappe.throw(
			_(
				"{0} has no Stock Entries to cancel: close it with the Close action."
			).format(doc.name)
		)
	_check_close_rights(doc, plan)

	if doc.workflow_state != HOLD_STATE:
		_hold_for_close(doc)
	snapshot = _snapshot(doc)
	snapshot["close"] = {
		"requested_by": frappe.session.user,
		"reason": reason,
		"entries": plan.entries,
		"reopen": plan.stopped,
		"cancelled": (snapshot.get("close") or {}).get("cancelled", []),
		"reopened": (snapshot.get("close") or {}).get("reopened", []),
	}
	_store_snapshot(doc, snapshot)
	frappe.enqueue(
		_close_with_stock,
		queue="long",
		timeout=4500,
		job_id=_close_job_id(doc.name),
		deduplicate=True,
		enqueue_after_commit=True,
		pmo=doc.name,
		requested_by=frappe.session.user,
	)
	message = _(
		"{0} is on Hold while {1} Stock Entries are cancelled in the background, latest first: {2}."
	).format(doc.name, len(plan.entries), ", ".join(plan.entries))
	if plan.stopped:
		message += " " + _("Material Request {0} will be re-opened first.").format(
			", ".join(plan.stopped)
		)
	message += " " + _("It closes once they are all cancelled; you will be notified.")
	doc.add_comment("Info", _with_reason(message, reason))
	frappe.msgprint(message, title=_("Close Queued"), indicator="blue")


def _hold_for_close(doc):
	"""The Hold a queued Close starts with -- part of the Close, so it takes no reason row.

	Through the workflow when the user may Hold the PMO in its state, as before. The Order
	Department may Close but need not hold the Hold Team role, so for them the same Hold is
	applied directly: the state written as the workflow would, then _hold with all its checks
	and writes (enqueue_close has already found the PMO ready to Close, which covers Hold).
	"""
	doc.flags[INTERNAL_FLAG] = True
	try:
		if HOLD in _available_actions(doc):
			apply_workflow(doc, HOLD)
		else:
			doc.db_set("workflow_state", HOLD_STATE)
			_hold(doc)
			doc.add_comment("Workflow", _(HOLD_STATE))
	finally:
		doc.flags.pop(INTERNAL_FLAG, None)
	doc.reload()


def _require_action(doc, action):
	"""The user may apply ``action`` to the PMO in its current state.

	The workflow's own transitions and roles decide, so a role changed on the Workflow form
	applies here as well.
	"""
	if action not in {t.get("action") for t in get_transitions(doc)}:
		frappe.throw(
			_("{0} is not available for {1} in state {2}.").format(
				frappe.bold(_(action)),
				doc.name,
				frappe.bold(doc.get("workflow_state") or _("none")),
			),
			frappe.PermissionError,
		)


def _refuse_while_job_runs(pmo_name):
	if is_job_enqueued(_close_job_id(pmo_name)):
		frappe.throw(
			_(
				"{0} is already being closed in the background. Reload in a minute."
			).format(pmo_name)
		)
	if is_job_enqueued(CANCEL_ALL_JOB_ID.format(pmo_name)):
		frappe.throw(
			_("{0} is being cancelled (Hybrid Cancel) in the background.").format(
				pmo_name
			)
		)


def _refuse_open_after_close_started(pmo_name, snapshot):
	close = snapshot.get("close") or {}
	done = [*close.get("reopened", []), *close.get("cancelled", [])]
	if done:
		frappe.throw(
			_(
				"A Close of {0} has already changed {1}, so its Material Requests no longer match what was on Hold. Fix what stopped the Close and Close it again."
			).format(pmo_name, ", ".join(done))
		)
	if is_job_enqueued(_close_job_id(pmo_name)):
		frappe.throw(_("{0} is being closed in the background.").format(pmo_name))


def _close_job_id(pmo_name):
	return f"pmo_close::{pmo_name}"


def _close_stock_plan(material_requests):
	"""The submitted Stock Entries that moved stock for these requests, latest first, plus
	their End Transit receipts; and the Stopped requests among their owners.

	Ownership is read from the Stock Entry rows, not the requests' custom_*_se links: those are
	copied between requests (copy_doc, desk Duplicate).
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
		_entries_in_cancel_order,
		_stopped_requests,
	)

	moved = (
		frappe.get_all(
			"Stock Entry Detail",
			filters={
				"material_request": ["in", sorted(material_requests)],
				"docstatus": 1,
			},
			fields=["material_request", "parent"],
		)
		if material_requests
		else []
	)
	return frappe._dict(
		entries=_entries_in_cancel_order({r.parent for r in moved}) if moved else [],
		stopped=_stopped_requests(sorted({r.material_request for r in moved})),
	)


def _check_close_rights(doc, plan):
	"""Everything the job will need, before the first change: a missing right must not
	surface half-way through the chain. Frappe's cancel needs both write and cancel.

	PMO write first: the final apply_workflow(Close) saves the PMO and checks it, but the
	Hold before it (written directly for a user without the Hold transition) and the
	cancellations would already be done by then."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
		validate_not_eod_sync_locked,
	)

	doc.check_permission("write")
	for se in plan.entries:
		for ptype in ("write", "cancel"):
			frappe.has_permission("Stock Entry", ptype, se, throw=True)
	for mr in plan.stopped:
		frappe.has_permission(MR, "write", mr, throw=True)
	# The doc-event hook ignores its document; it throws the standard EOD message.
	validate_not_eod_sync_locked(None)


# --------------------------------------------------------------------------------------------
# the queued half of enqueue_close
# --------------------------------------------------------------------------------------------


def _close_with_stock(pmo, requested_by):
	"""Re-open, cancel one entry per transaction, then apply the Close.

	Every step starts by locking the PMO row (the parent comes first in lock_order) and
	checking it is still on Hold with this Close pending. Each cancel leaves a valid ledger on
	its own and is recorded in the PMO's hold_snapshot, so a failure keeps the PMO on Hold
	with what is done, and Close again carries on from there.
	"""
	# Frappe re-runs a job that hit 1205/1213 without its user -- as Administrator, which would
	# skip the permission checks every cancel makes.
	frappe.set_user(requested_by)
	done, current = [], pmo
	try:
		doc = frappe.get_doc(PMO, pmo, for_update=True)
		if doc.workflow_state in CLOSED_STATES:
			_report_close(
				pmo, requested_by, _("{0} is already Closed.").format(pmo), ok=True
			)
			return
		# Re-checked for the job: a right taken away since the Close was queued stops it before
		# the first reopen or cancel.
		doc.check_permission("write")
		_refuse_unless_ready(
			pmo, CLOSE, _pmo_documents(pmo, CLOSE, lock=False), stock=False
		)
		while True:
			step = _next_close_step(doc)
			if not step:
				break
			kind, current = step
			if kind == "reopen":
				request = frappe.get_doc(MR, current)
				# update_status is ERPNext's controller method -- the write check lives in its
				# whitelisted wrapper -- so it is made here, at the moment of the change.
				request.check_permission("write")
				request.update_status("Submitted")
			else:
				frappe.get_doc("Stock Entry", current).cancel()
			_record_close_step(doc, kind, current)
			frappe.db.commit()
			done.append(current)
			doc = frappe.get_doc(PMO, pmo, for_update=True)
		current = pmo
		# The reason chosen when the Close was queued: the Close records it as it happens.
		doc.flags[REASON_FLAG] = (_snapshot(doc).get("close") or {}).get("reason")
		apply_workflow(doc, CLOSE)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(
			title=_("Closing Parent Manufacturing Order {0} failed").format(pmo),
			reference_doctype=PMO,
			reference_name=pmo,
		)
		# ERPNext's stock messages carry markup and hard line breaks; one line reads better
		# on the timeline and in the bell.
		reason = " ".join(strip_html(str(e)).split()).rstrip(".")
		if done:
			message = _(
				"Close of {0} stopped at {1}: {2}. Already done: {3}. It stays on Hold; fix this and Close again to carry on."
			).format(pmo, current, reason, ", ".join(done))
		else:
			message = _(
				"Close of {0} stopped before changing anything: {1}. It is on Hold: Open it to go back, or fix this and Close again."
			).format(pmo, reason)
		_report_close(pmo, requested_by, message, ok=False)
		return
	_report_close(
		pmo,
		requested_by,
		_("{0} is Closed. Cancelled: {1}.").format(
			pmo, ", ".join(done) or _("nothing")
		),
		ok=True,
	)


def _next_close_step(doc):
	"""("reopen", request), ("cancel", entry), or None once only the Close is left."""
	close = _snapshot(doc).get("close")
	if doc.workflow_state != HOLD_STATE or not close:
		frappe.throw(
			_("{0} is no longer on Hold with a Close pending.").format(doc.name)
		)
	plan = _close_stock_plan(
		frappe.get_all(
			MR,
			filters={"manufacturing_order": doc.name, "docstatus": ["<", 2]},
			pluck="name",
		)
	)
	for mr in plan.stopped:
		if mr not in close.get("reopen", []):
			frappe.throw(
				_(
					"Material Request {0} was stopped after the Close was confirmed."
				).format(mr)
			)
		return ("reopen", mr)
	for se in plan.entries:
		if se not in close.get("entries", []):
			frappe.throw(
				_(
					"Stock Entry {0} was made after the Close was confirmed. Close again to review it."
				).format(se)
			)
		return ("cancel", se)
	return None


def _record_close_step(doc, kind, name):
	snapshot = _snapshot(doc)
	close = snapshot.setdefault("close", {})
	close.setdefault("reopened" if kind == "reopen" else "cancelled", []).append(name)
	_store_snapshot(doc, snapshot)


def _report_close(pmo, user, message, ok):
	"""How the job ended: on the PMO's timeline, in the bell and, if live, a popup."""
	frappe.get_doc(PMO, pmo).add_comment("Comment", message)
	enqueue_create_notification(
		frappe.db.get_value("User", user, "email") or user,
		{
			"type": "Alert",
			"document_type": PMO,
			"document_name": pmo,
			"subject": message,
		},
	)
	frappe.publish_realtime(
		"msgprint",
		{
			"message": message,
			"title": _("Close Parent Manufacturing Order"),
			"indicator": "green" if ok else "red",
		},
		user=user,
		after_commit=True,
	)


# --------------------------------------------------------------------------------------------
# guards: nothing moves for a PMO on Hold or Closed
# --------------------------------------------------------------------------------------------


def pmo_blocking_state(pmo_name):
	"""The PMO's Hold / Closed state as stored, or None.

	Read from the database because a form opened before the Hold or Close still carries the
	old values: the PMO flow writes its documents without touching ``modified``. The
	workflow_state field is created with the Workflow, so a site without it has no held or
	closed PMO.
	"""
	if not pmo_name or not _has_workflow_state():
		return None
	state = frappe.db.get_value(PMO, pmo_name, "workflow_state")
	return state if state in BLOCKING_STATES else None


def blocked_pmos(pmo_names):
	"""{pmo: state} for those of ``pmo_names`` that are on Hold or Closed."""
	return _pmo_states(pmo_names, BLOCKING_STATES)


def requested_pmos(pmo_names):
	"""{pmo: state} for those of ``pmo_names`` that have a Close or Hold request."""
	return _pmo_states(pmo_names, REQUESTED_STATES)


def _pmo_states(pmo_names, states):
	"""{pmo: state} for those of ``pmo_names`` whose stored state is one of ``states``."""
	names = sorted({n for n in pmo_names if n})
	if not names or not _has_workflow_state():
		return {}
	return dict(
		frappe.get_all(
			PMO,
			filters={"name": ["in", names], "workflow_state": ["in", states]},
			fields=["name", "workflow_state"],
			as_list=True,
		)
	)


def blocking_message(pmo_name, state):
	if state in HOLD_STATES:
		return _("Parent Manufacturing Order {0} is on Hold.").format(
			frappe.bold(pmo_name)
		)
	return _("Parent Manufacturing Order {0} is Closed.").format(frappe.bold(pmo_name))


def refuse_while_blocked(pmo_name, what):
	"""Throw when the PMO is on Hold or Closed; ``what`` says what was refused."""
	state = pmo_blocking_state(pmo_name)
	if state:
		frappe.throw(
			f"{blocking_message(pmo_name, state)} {what}",
			title=_("Parent Manufacturing Order Blocked"),
		)


def refuse_material_request_while_blocked(doc, method=None):
	"""A Material Request of a PMO on Hold or Closed cannot be saved or moved on.

	before_validate / before_update_after_submit hook. The PMO flow writes the requests'
	state directly, and the Stock Entry cancels of a Close release their stamps with
	set_value, so neither passes through here.
	"""
	pmo = doc.get("manufacturing_order")
	refuse_while_blocked(
		pmo,
		_("Material Request {0} cannot be changed.").format(doc.name or _("New")),
	)
	# A Close / Hold request: no raw material moves to its work orders any more.
	state = pmo_request_state(pmo)
	if state:
		frappe.throw(
			f"{request_message(pmo, state)} "
			+ _(
				"Material Request {0} cannot be changed: no raw material can be transferred for it until the request is done or withdrawn."
			).format(doc.name or _("New")),
			title=_("Parent Manufacturing Order Restricted"),
		)


def refuse_stock_entry_while_blocked(doc, method=None):
	"""No stock moves for a PMO on Hold or Closed.

	Stock Entry validate hook, so it never sees a cancel: the cancels of a Close go through.
	The PMO is found from the header (order, work order, operation), the rows' Material
	Request and operation, and for an End Transit receipt the requests of the entry it
	receives.
	"""
	if not _has_workflow_state():
		return
	rows = doc.get("items") or []
	requests = {r.get("material_request") for r in rows}
	operations = {r.get("manufacturing_operation") for r in rows}
	operations.add(doc.get("manufacturing_operation"))
	if doc.get("outgoing_stock_entry"):
		requests.update(
			frappe.get_all(
				"Stock Entry Detail",
				filters={
					"parent": doc.outgoing_stock_entry,
					"parenttype": "Stock Entry",
				},
				pluck="material_request",
			)
		)
	requests.discard(None)
	requests.discard("")
	operations.discard(None)
	operations.discard("")

	pmos = {doc.get("manufacturing_order")}
	if doc.get("manufacturing_work_order"):
		pmos.add(
			frappe.db.get_value(
				MWO, doc.manufacturing_work_order, "manufacturing_order"
			)
		)
	if requests:
		pmos.update(
			frappe.get_all(
				MR,
				filters={"name": ["in", sorted(requests)]},
				pluck="manufacturing_order",
			)
		)
	if operations:
		pmos.update(
			frappe.get_all(
				MOP,
				filters={"name": ["in", sorted(operations)]},
				pluck="manufacturing_order",
			)
		)
	# One read for both: blocked (nothing moves) and requested (no raw material moves).
	states = _pmo_states(pmos, (*BLOCKING_STATES, *REQUESTED_STATES))
	blocked = {p: s for p, s in states.items() if s in BLOCKING_STATES}
	if blocked:
		frappe.throw(
			" ".join(
				blocking_message(pmo, state) for pmo, state in sorted(blocked.items())
			)
			+ " "
			+ _("No stock can move for it."),
			title=_("Parent Manufacturing Order Blocked"),
		)
	requested = {p: s for p, s in states.items() if s in REQUESTED_STATES}
	# Validate runs on submit too: on_submit (queue_ready_check_after_stock_entry) reuses it.
	doc.flags[REQUESTED_PMOS_FLAG] = sorted(requested)
	# An End Transit receipt completes a transfer issued before the request: it goes through.
	if (
		requested
		and not doc.get("outgoing_stock_entry")
		and doc.get("stock_entry_type") in blocked_request_stock_entry_types()
	):
		frappe.throw(
			" ".join(
				request_message(pmo, state) for pmo, state in sorted(requested.items())
			)
			+ " "
			+ _(
				"No raw material can be transferred to its work orders: {0} Stock Entries are blocked until the request is done or withdrawn."
			).format(frappe.bold(doc.stock_entry_type)),
			title=_("Parent Manufacturing Order Restricted"),
		)


# --------------------------------------------------------------------------------------------
# guards: a PMO with a Close or Hold request -- its work orders only come back
# --------------------------------------------------------------------------------------------


def pmo_request_state(pmo_name):
	"""The PMO's Close Requested / Hold Requested state as stored, or None."""
	if not pmo_name or not _has_workflow_state():
		return None
	state = frappe.db.get_value(PMO, pmo_name, "workflow_state")
	return state if state in REQUESTED_STATES else None


def request_message(pmo_name, state):
	if state == HOLD_REQUESTED:
		return _("Parent Manufacturing Order {0} has a Hold request.").format(
			frappe.bold(pmo_name)
		)
	return _("Parent Manufacturing Order {0} has a Close request.").format(
		frappe.bold(pmo_name)
	)


def close_hold_departments():
	"""MOP Settings > Close / Hold Request Departments, in their order there: where a requested
	PMO's work orders may be transferred (besides planning). Not a readiness rule: a Close
	needs them in planning, a Hold anywhere."""
	return frappe.get_all(
		CLOSE_HOLD_DEPARTMENTS_DOCTYPE,
		filters={
			"parenttype": REASONS_SETTINGS,
			"parent": REASONS_SETTINGS,
			"parentfield": CLOSE_HOLD_DEPARTMENTS_FIELD,
		},
		order_by="idx asc",
		pluck="department",
	)


def _required_close_hold_departments():
	departments = close_hold_departments()
	if not departments:
		frappe.throw(
			_(
				"Set the Close / Hold Request Departments in MOP Settings (PMO Hold / Open / Close Reasons) first: a request sends the work orders there."
			),
			title=_("Close / Hold Request Departments Missing"),
		)
	return departments


def _planning_label():
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
		SPLIT_ALLOWED_DEPARTMENT,
	)

	return SPLIT_ALLOWED_DEPARTMENT


def _transfer_targets(departments):
	"""Where a requested PMO's work orders may be transferred, for a message: the request
	departments, and the planning department when none of them is it (a Close needs the work
	orders there, so it is always allowed)."""
	departments = list(departments or ())
	if _planning_departments(set(departments)):
		return departments
	return [*departments, _planning_label()]


def _destination(departments):
	"""The transfer destinations for a message: "A, B or C" in bold."""
	return comma_or(
		[frappe.bold(d) for d in _transfer_targets(departments)], add_quotes=False
	)


def blocked_request_stock_entry_types():
	"""MOP Settings > Stock Entry Types Blocked During a Request."""
	return set(
		frappe.get_all(
			BLOCKED_TYPES_DOCTYPE,
			filters={
				"parenttype": REASONS_SETTINGS,
				"parent": REASONS_SETTINGS,
				"parentfield": BLOCKED_TYPES_FIELD,
			},
			pluck="stock_entry_type",
		)
	)


def _row_pmos(doc, table):
	"""The PMOs of an IR's rows: each row's operation's order, plus a row's own PMO link."""
	rows = doc.get(table) or []
	pmos = {r.get("parent_manufacturing_order") for r in rows}
	operations = sorted({r.get("manufacturing_operation") for r in rows} - {None, ""})
	if operations:
		pmos.update(
			frappe.get_all(
				MOP,
				filters={"name": ["in", operations]},
				pluck="manufacturing_order",
			)
		)
	return pmos


def refuse_department_ir_while_requested(doc, table="department_ir_operation"):
	"""Department IR validate: a requested PMO's work orders are issued only to a Close /
	Hold Request Department or the planning department. A Receive (taking them in) always
	goes through.

	The cutoff is the request's commit: an Issue that passed validation before it is not
	refused afterwards (like an End Transit receipt of a transfer issued before it). Nothing
	is frozen by a request, so that work order simply counts as not back yet --
	notify_ready_requests waits for it -- and has to come back like any other. No PMO lock is
	taken here: IR submits lock MOP -> MWO first, and a late PMO lock would invert the order."""
	if doc.get("type") != "Issue" or cint(doc.docstatus) == 2:
		return
	requested = requested_pmos(_row_pmos(doc, table))
	if not requested:
		return
	departments = close_hold_departments()
	next_department = doc.get("next_department")
	# Planning is always allowed: a Close needs the work orders there.
	if next_department and (
		next_department in departments or _planning_departments({next_department})
	):
		return
	frappe.throw(
		" ".join(request_message(p, s) for p, s in sorted(requested.items()))
		+ " "
		+ _("Its work orders can be transferred only to {0}, not to {1}.").format(
			_destination(departments),
			frappe.bold(doc.get("next_department") or _("(blank)")),
		),
		title=_("Transfer Restricted"),
	)


def refuse_employee_ir_while_requested(doc, table="employee_ir_operations"):
	"""Employee IR validate: a requested PMO's work orders are not issued to an employee. A
	Receive (handing them back) always goes through."""
	if doc.get("type") != "Issue" or cint(doc.docstatus) == 2:
		return
	requested = requested_pmos(_row_pmos(doc, table))
	if not requested:
		return
	frappe.throw(
		" ".join(request_message(p, s) for p, s in sorted(requested.items()))
		+ " "
		+ _(
			"Its work orders cannot be issued to an employee; transfer them to {0} with a Department IR."
		).format(_destination(close_hold_departments())),
		title=_("Transfer Restricted"),
	)


# --------------------------------------------------------------------------------------------
# a requested PMO whose work is back: tell the requester it can be closed / held now
# --------------------------------------------------------------------------------------------


def queue_ready_check_after_transfer(doc, table):
	"""Department IR / Employee IR on_submit: a transfer may have brought a requested PMO's
	last work order back (or its metal), so check its PMOs once the submit commits."""
	enqueue_ready_check(requested_pmos(_row_pmos(doc, table)))


def queue_ready_check_after_stock_entry(doc, method=None):
	"""Stock Entry on_submit: the requested PMOs refuse_stock_entry_while_blocked found at
	validate (a metal return or adjustment may have cleared the last of their metal)."""
	enqueue_ready_check(doc.flags.get(REQUESTED_PMOS_FLAG) or ())


def enqueue_ready_check(pmo_names):
	names = sorted({n for n in pmo_names if n})
	if not names:
		return
	frappe.enqueue(
		notify_ready_requests,
		queue="short",
		enqueue_after_commit=True,
		pmo_names=names,
	)


def notify_ready_requests(pmo_names):
	"""Background job: for each PMO still in Close Requested / Hold Requested with nothing
	left to wait for -- its work orders back, no metal or drafts for a Close -- alert the user
	who raised the request, once per request."""
	for name, state in sorted(requested_pmos(pmo_names).items()):
		target = CLOSE if state == CLOSE_REQUESTED else HOLD
		if _problem_list(
			name, target, _pmo_documents(name, target, lock=False), stock=False
		):
			continue
		# The request this state waits on: a Close request that resumed after a Hold request
		# belongs to whoever made the Close request, not the later Hold request.
		request = _active_requests(name).get(
			REQUEST_CLOSE if target == CLOSE else REQUEST_HOLD
		)
		if not request or not request.action_by:
			continue
		if target == CLOSE:
			message = _(
				"Parent Manufacturing Order {0} is ready to Close: its work orders are back with no metal and no draft transactions (Close request of {1})."
			)
		else:
			message = _(
				"Parent Manufacturing Order {0} is ready for Hold: its work orders are back (Hold request of {1})."
			)
		enqueue_create_notification(
			request.action_by,
			{
				"type": "Alert",
				"document_type": PMO,
				"document_name": name,
				"subject": message.format(name, request.action_on),
			},
			dedupe_on=["document_type", "document_name", "subject"],
		)


def validate_operation_hold(mop):
	"""Only a PMO Hold puts an operation on Hold, and only its Open or Close takes it off.

	Manufacturing Operation validate. The PMO flow writes the status directly, so it never
	passes through here; a desk or API save that sets or clears Hold is refused.
	"""
	before = mop.get_doc_before_save()
	was_held = bool(before) and before.get("status") == HOLD_STATUS
	if was_held != (mop.get("status") == HOLD_STATUS):
		frappe.throw(
			_(
				"Operation {0}: Hold is set and cleared by its Parent Manufacturing Order (Hold, Open, Close), not on the operation."
			).format(frappe.bold(mop.name or _("New")))
		)


def _has_workflow_state():
	return frappe.get_meta(PMO).has_field("workflow_state")
