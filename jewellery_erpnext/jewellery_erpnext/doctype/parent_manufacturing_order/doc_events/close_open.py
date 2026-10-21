import frappe
from frappe import _

from jewellery_erpnext.utils import set_values_in_bulk

CLOSED_STATE = "Closed"
OPEN_STATE = "Open"
# MWOs in these statuses are already finished one way or the other: closing the PMO leaves
# them alone, so reopening it has nothing to put back for them either.
UNTOUCHED_MWO_STATUSES = ("Completed", "Closed", "Cancelled")


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
		block_close_while_operations_in_progress(pmo.name)
		close_related_mwos(pmo.name)
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
	"""
	_require_system_manager()
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(
			_("Close Reason is required to close a Parent Manufacturing Order.")
		)
	frappe.db.set_value(
		"Parent Manufacturing Order", pmo, "close_reason", reason, update_modified=False
	)


def _require_system_manager():
	if "System Manager" not in frappe.get_roles():
		frappe.throw(
			_(
				"Only a System Manager can close or reopen a Parent Manufacturing Order."
			),
			frappe.PermissionError,
		)


def block_close_while_operations_in_progress(pmo_name):
	"""Same definition of "in progress" as Split Work Order: anything past Not Started that
	is not Finished / Revert, or an operation still In-Transit between departments."""
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
	open_operations = frappe.get_all(
		"Manufacturing Operation",
		filters={"manufacturing_work_order": ["in", mwos]},
		or_filters={
			"status": ["not in", ["Finished", "Not Started", "Revert"]],
			"department_ir_status": "In-Transit",
		},
		fields=["name", "manufacturing_work_order"],
	)
	if open_operations:
		frappe.throw(
			_(
				"Finish or revert these operations before closing the Parent Manufacturing Order: {0}"
			).format(
				", ".join(
					f"{o.name} ({o.manufacturing_work_order})" for o in open_operations
				)
			),
			title=_("Operations In Progress"),
		)


def close_related_mwos(pmo_name):
	mwos = frappe.get_all(
		"Manufacturing Work Order",
		filters={
			"manufacturing_order": pmo_name,
			"docstatus": 1,
			"status": ["not in", UNTOUCHED_MWO_STATUSES],
		},
		fields=["name", "status"],
	)
	for mwo in mwos:
		frappe.db.set_value(
			"Manufacturing Work Order",
			mwo.name,
			{"status_before_close": mwo.status, "status": "Closed"},
			update_modified=False,
		)


def reopen_related_mwos(pmo_name):
	mwos = frappe.get_all(
		"Manufacturing Work Order",
		filters={
			"manufacturing_order": pmo_name,
			"docstatus": 1,
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
