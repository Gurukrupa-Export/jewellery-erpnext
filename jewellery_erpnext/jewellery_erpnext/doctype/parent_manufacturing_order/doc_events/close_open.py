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

Hold and Close are refused unless every open work order is still in planning -- in the
department a Split is allowed in (SPLIT_ALLOWED_DEPARTMENT), with nothing in progress or in
transit -- and Close also needs no material on a work order or its current operation.

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
from frappe.utils import cint, flt, strip_html
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


def on_workflow_state_change(pmo):
	"""Hold / Open / Close what belongs to the PMO when the workflow moves it.

	Called from on_update (a draft's transitions) and on_update_after_submit (a submitted
	PMO's); the state field already holds the new state. An insert has no earlier version and
	nothing to move.
	"""
	before = pmo.get_doc_before_save()
	if not before:
		return
	previous, current = before.get("workflow_state"), pmo.get("workflow_state")
	if previous == current:
		return
	if current in HOLD_STATES:
		_hold(pmo)
	elif current in CLOSED_STATES:
		_close(pmo)
	elif previous in HOLD_STATES:
		# Open (Hold -> Submitted), or the submit of a draft that was held.
		_open(pmo)


def _hold(pmo):
	snapshot = _snapshot(pmo)
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
	pmo.add_comment(
		"Info",
		_(
			"Put on Hold: {0} work orders, {1} operations and {2} Material Requests set to Hold."
		).format(len(mwos), len(mops), len(mrs)),
	)


def _open(pmo):
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
	if mwos or mops or mrs:
		pmo.add_comment(
			"Info",
			_(
				"Opened: {0} work orders, {1} operations and {2} Material Requests put back as they were."
			).format(len(mwos), len(mops), len(mrs)),
		)


def _close(pmo):
	if not (pmo.close_reason or "").strip():
		frappe.throw(
			_("Close Reason is required to close a Parent Manufacturing Order.")
		)
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
		_(
			"Closed ({0}): {1} work orders closed, {2} operations finished and {3} Material Requests closed."
		).format(pmo.close_reason.strip(), len(mwos), len(mops), len(mrs)),
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
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
		SPLIT_ALLOWED_DEPARTMENT,
	)

	verb = _("placed on Hold") if action == HOLD else _("closed")
	submitted = [r for r in docs.mwos.values() if cint(r.docstatus) == 1]
	problems = []

	departments = sorted({r.department for r in submitted if r.department})
	department_names = (
		dict(
			frappe.get_all(
				"Department",
				filters={"name": ["in", departments]},
				fields=["name", "department_name"],
				as_list=True,
			)
		)
		if departments
		else {}
	)
	elsewhere = [
		r
		for r in submitted
		if department_names.get(r.department) != SPLIT_ALLOWED_DEPARTMENT
	]
	if elsewhere:
		problems.append(
			_(
				"PMO cannot be {0} because all linked Manufacturing Work Orders must be in the Manufacturing Plan and Management Department."
			).format(verb)
			+ _bullets(
				f"{r.name} ({r.department or _('no department')})" for r in elsewhere
			)
		)

	busy = _operations_in_progress([r.name for r in submitted], docs.mops)
	if busy:
		problems.append(
			_(
				"PMO cannot be {0} while these operations are in progress or in transit:"
			).format(verb)
			+ _bullets(f"{o.name} ({o.manufacturing_work_order})" for o in busy)
		)

	pending = sorted(
		n
		for n, r in docs.mrs.items()
		if r.get("custom_transfer_se_state") == TRANSFER_PENDING
	)
	if pending:
		problems.append(
			_(
				"PMO cannot be {0} while these Material Requests are still transferring their stock from reserve. Try again once they are done:"
			).format(verb)
			+ _bullets(pending)
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
				heavy.append(f"{r.name} ({weight})")
		if heavy:
			problems.append(
				_(
					"PMO cannot be closed because the Gross Weight of every linked Manufacturing Work Order must be 0."
				)
				+ _bullets(heavy)
			)
		if stock:
			entries = _close_stock_plan(list(docs.mrs)).entries
			if entries:
				problems.append(
					_(
						"PMO cannot be closed while its Material Requests still have submitted Stock Entries. Close it from the Close action, which cancels them first:"
					)
					+ _bullets(entries)
				)
	return problems


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

	For Close it also returns the Stock Entries that have to be cancelled first (latest
	first) and the Stopped requests that have to be re-opened for that; with any entries the
	form offers enqueue_close instead of closing straight away.
	"""
	doc = frappe.get_doc(PMO, pmo)
	_require_action(doc, action)
	if action == OPEN:
		_refuse_open_after_close_started(doc.name, _snapshot(doc))
		return {}
	if action not in (HOLD, CLOSE):
		return {}
	_refuse_while_job_runs(doc.name)
	docs = _pmo_documents(doc.name, action, lock=False)
	_refuse_unless_ready(doc.name, action, docs, stock=False)
	if action != CLOSE:
		return {}
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
		"entries": [details[se] for se in plan.entries if se in details],
		"stopped": plan.stopped,
	}


@frappe.whitelist()
def save_close_reason(pmo, reason):
	"""Store the reason ahead of the Close action.

	apply_workflow reloads the document from the database before it applies the transition,
	so a close_reason set on the open form never reaches it; it has to be in the database by
	then. Only a PMO the user may Close in its current state takes a reason, so the reason of
	a Closed PMO is never overwritten.
	"""
	reason = _required_reason(reason)
	doc = frappe.get_doc(PMO, pmo)
	_require_action(doc, CLOSE)
	frappe.db.set_value(PMO, doc.name, "close_reason", reason, update_modified=False)


@frappe.whitelist()
def enqueue_close(pmo, reason):
	"""Close a PMO whose Material Requests moved stock: cancel those entries first.

	The PMO is put on Hold first, so for as long as the job runs nothing else can move for it
	(every PMO guard reads Hold). Then a background job re-opens the Stopped requests, cancels
	the entries latest first, one per transaction -- a cancel takes the better part of a
	minute on a busy item and holds its tabBin rows until it commits -- and applies the Close.
	If anything fails the PMO stays on Hold with what was done recorded, and Close again
	carries on from there.
	"""
	reason = _required_reason(reason)
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
	_check_close_rights(plan)

	frappe.db.set_value(PMO, doc.name, "close_reason", reason, update_modified=False)
	if doc.workflow_state != HOLD_STATE:
		apply_workflow(doc, HOLD)
		doc.reload()
	snapshot = _snapshot(doc)
	snapshot["close"] = {
		"requested_by": frappe.session.user,
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
	doc.add_comment("Info", message)
	frappe.msgprint(message, title=_("Close Queued"), indicator="blue")


def _required_reason(reason):
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(
			_("Close Reason is required to close a Parent Manufacturing Order.")
		)
	return reason


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


def _check_close_rights(plan):
	"""Everything the job will need, before the first change: a missing right must not
	surface half-way through the chain. Frappe's cancel needs both write and cancel."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
		validate_not_eod_sync_locked,
	)

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
	names = sorted({n for n in pmo_names if n})
	if not names or not _has_workflow_state():
		return {}
	return dict(
		frappe.get_all(
			PMO,
			filters={"name": ["in", names], "workflow_state": ["in", BLOCKING_STATES]},
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
	refuse_while_blocked(
		doc.get("manufacturing_order"),
		_("Material Request {0} cannot be changed.").format(doc.name or _("New")),
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
	blocked = blocked_pmos(pmos)
	if blocked:
		frappe.throw(
			" ".join(
				blocking_message(pmo, state) for pmo, state in sorted(blocked.items())
			)
			+ " "
			+ _("No stock can move for it."),
			title=_("Parent Manufacturing Order Blocked"),
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
