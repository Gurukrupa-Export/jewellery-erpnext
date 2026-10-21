import frappe
from frappe import _
from frappe.utils import cint, cstr

from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_manufacturing_operations,
	lock_work_orders,
)
from jewellery_erpnext.utils import set_values_in_bulk

PMO = "Parent Manufacturing Order"
CLOSED_STATE = "Closed"
OPEN_STATE = "Open"
# MWOs in these statuses are already finished one way or the other: closing the PMO leaves
# them alone, so reopening it has nothing to put back for them either.
UNTOUCHED_MWO_STATUSES = ("Completed", "Closed", "Cancelled")
# An operation in one of these statuses is not in progress, unless it is In-Transit.
SETTLED_OPERATION_STATUSES = ("Finished", "Not Started", "Revert")
IN_TRANSIT = "In-Transit"


def on_workflow_state_change(pmo):
	"""Close / reopen the PMO's work orders when the workflow moves it to Closed / Open.

	Called from on_update_after_submit. The workflow's own role check is not enough on its
	own, since the same transition can be applied through the API.
	"""
	before = pmo.get_doc_before_save()
	previous = before.get("workflow_state") if before else None
	current = pmo.get("workflow_state")
	if previous == current:
		return

	if current == CLOSED_STATE:
		_require_system_manager()
		if not (pmo.close_reason or "").strip():
			frappe.throw(
				_("Close Reason is required to close a Parent Manufacturing Order.")
			)
		mwo_rows, mop_rows = _lock_for_close(pmo.name)
		block_close_while_operations_in_progress(pmo.name, fresh_operations=mop_rows)
		close_related_mwos(mwo_rows)
	elif previous == CLOSED_STATE and current == OPEN_STATE:
		_require_system_manager()
		reopen_related_mwos(pmo.name)
		pmo.db_set("close_reason", None, update_modified=False)
		pmo.close_reason = None


@frappe.whitelist()
def check_can_close(pmo):
	"""Dry run of the Close checks, called before the dialog so a refusal comes first."""
	_require_system_manager()
	block_close_while_operations_in_progress(pmo)


@frappe.whitelist()
def save_close_reason(pmo, reason):
	"""Store the reason ahead of the Close action.

	apply_workflow reloads the document from the database before it applies the transition,
	so a close_reason set on the open form never reaches it; it has to be in the database by
	then. The field stays hidden on the form until the PMO is actually Closed.

	Only a PMO the Close can start from (submitted and not Closed: the workflow's Submitted /
	Open) takes a reason, so the reason recorded on a Closed PMO is never overwritten.
	"""
	_require_system_manager()
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(
			_("Close Reason is required to close a Parent Manufacturing Order.")
		)
	stored = _stored_state(pmo)
	if not stored:
		frappe.throw(
			_("Parent Manufacturing Order {0} does not exist.").format(
				frappe.bold(pmo)
			),
			frappe.DoesNotExistError,
		)
	if cint(stored.docstatus) != 1 or stored.workflow_state == CLOSED_STATE:
		frappe.throw(
			_(
				"A Close Reason can only be saved on a submitted Parent Manufacturing Order that is not already Closed."
			)
		)
	frappe.db.set_value(PMO, pmo, "close_reason", reason, update_modified=False)


def is_pmo_closed(pmo_name):
	"""True while the PMO is in the workflow's Closed state, as stored.

	Read from the database because a form opened before the Close still carries the old
	values: the Close writes its work orders without touching ``modified``. The
	workflow_state field is created with the Workflow, so a site without it has no Closed PMO.
	"""
	if not pmo_name or not _has_workflow_state():
		return False
	stored = _stored_state(pmo_name)
	return bool(stored) and stored.workflow_state == CLOSED_STATE


def _has_workflow_state():
	return frappe.get_meta(PMO).has_field("workflow_state")


def _stored_state(pmo_name):
	return frappe.db.get_value(
		PMO, pmo_name, ["docstatus", "workflow_state"], as_dict=True
	)


def _require_system_manager():
	if "System Manager" not in frappe.get_roles():
		frappe.throw(
			_(
				"Only a System Manager can close or reopen a Parent Manufacturing Order."
			),
			frappe.PermissionError,
		)


def _closable_mwo_filters(pmo_name):
	return {
		"manufacturing_order": pmo_name,
		# Draft work orders count too: left alone they could still be submitted under
		# a closed PMO.
		"docstatus": ["<", 2],
		"status": ["not in", UNTOUCHED_MWO_STATUSES],
	}


def _lock_for_close(pmo_name):
	"""Lock the work orders the Close writes, then their current operations, and return the
	locked rows ``(mwo_rows, mop_rows)``.

	An Employee IR / Department IR submit holds its work orders' rows until it commits
	(lock_order RULE D). Taking them here first means the Close decides from the latest
	committed rows rather than from a snapshot taken while such a submit was in flight, and an
	IR that comes later waits for the Close and then reads the work order as Closed
	(current_operation_guard).

	Every lock is NOWAIT. This transaction already holds the PMO row (apply_workflow saved it),
	and an IR holding a work order writes the PMO row too (Manufacturing Operation on_update ->
	set_pmo_weight_details_in_bulk), so waiting here could close a deadlock cycle. Operations
	after work orders is the reverse of RULE D, safe only because nothing here waits (RULE E's
	non-waiting locking reads). A busy row refuses the Close and the user tries again.
	"""
	mwos = frappe.get_all(
		"Manufacturing Work Order",
		filters=_closable_mwo_filters(pmo_name),
		fields=["name", "manufacturing_operation"],
	)
	try:
		mwo_rows = lock_work_orders([m.name for m in mwos], wait=0)
		# The snapshot's pointer and the locked one: an IR committed since the snapshot may have
		# moved it, and either operation can be the one it left in progress.
		pointers = {m.manufacturing_operation for m in mwos}
		pointers.update(r.manufacturing_operation for r in mwo_rows.values())
		pointers.discard(None)
		mop_rows = lock_manufacturing_operations(pointers, wait=0)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		frappe.throw(
			_(
				"{0} is being updated by another user right now. Try closing again in a moment."
			).format(frappe.bold(getattr(exc, "lock_row_name", None) or pmo_name)),
			title=_("Work Order Busy"),
		)
	return mwo_rows, mop_rows


def _in_progress(status, department_ir_status):
	return (
		cstr(status) not in SETTLED_OPERATION_STATUSES
		or department_ir_status == IN_TRANSIT
	)


def block_close_while_operations_in_progress(pmo_name, fresh_operations=None):
	"""Same definition of "in progress" as Split Work Order: anything past Not Started that
	is not Finished / Revert, or an operation still In-Transit between departments.

	``fresh_operations``: the locked rows of the work orders' current operations
	(``_lock_for_close``). They are the latest committed state, so they override what the
	plain read -- this transaction's snapshot -- says about the same operations."""
	mwos = frappe.get_all(
		"Manufacturing Work Order",
		filters={
			"manufacturing_order": pmo_name,
			"docstatus": 1,
			"status": ["not in", UNTOUCHED_MWO_STATUSES],
		},
		pluck="name",
	)
	if not mwos:
		return
	open_operations = {
		o.name: o
		for o in frappe.get_all(
			"Manufacturing Operation",
			filters={"manufacturing_work_order": ["in", mwos]},
			or_filters={
				"status": ["not in", list(SETTLED_OPERATION_STATUSES)],
				"department_ir_status": IN_TRANSIT,
			},
			fields=["name", "manufacturing_work_order"],
		)
	}
	mwos = set(mwos)
	for row in (fresh_operations or {}).values():
		if row.manufacturing_work_order not in mwos:
			continue
		if _in_progress(row.status, row.department_ir_status):
			open_operations[row.name] = row
		else:
			open_operations.pop(row.name, None)
	if open_operations:
		frappe.throw(
			_(
				"Finish or revert these operations before closing the Parent Manufacturing Order: {0}"
			).format(
				", ".join(
					f"{o.name} ({o.manufacturing_work_order})"
					for o in sorted(open_operations.values(), key=lambda o: o.name)
				)
			),
			title=_("Operations In Progress"),
		)


def close_related_mwos(mwo_rows):
	"""Close the locked work orders (``_lock_for_close``) that are still open, keeping the
	status each one goes back to on reopen. The rows are the latest committed ones, so the kept
	status is the current one even when this transaction's snapshot is older."""
	for row in mwo_rows.values():
		if cint(row.docstatus) == 2 or row.status in UNTOUCHED_MWO_STATUSES:
			continue
		frappe.db.set_value(
			"Manufacturing Work Order",
			row.name,
			{"status_before_close": row.status, "status": "Closed"},
			update_modified=False,
		)


def reopen_related_mwos(pmo_name):
	mwos = frappe.get_all(
		"Manufacturing Work Order",
		filters={
			"manufacturing_order": pmo_name,
			"docstatus": ["<", 2],
			"status": "Closed",
			"status_before_close": ["is", "set"],
		},
		fields=["name", "status_before_close"],
	)
	by_status = {}
	for mwo in mwos:
		by_status.setdefault(mwo.status_before_close, []).append(mwo.name)
	for status, names in by_status.items():
		set_values_in_bulk(
			"Manufacturing Work Order",
			names,
			{"status": status, "status_before_close": None},
		)
