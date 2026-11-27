"""Hold "Transfer Material" in a new state until its Stock Entry exists.

"Transfer Material" submits a Manufacture Material Request. Until now that submit moved it
straight to **Material Transferred**, but the "Material Transfer From Reserve" Stock Entry
is only *queued* by it (``doc_events.material_request.on_submit`` ->
``materialize_transfer_se``). So the form said "Material Transferred" while the entry did
not exist yet, and kept saying it when the job failed (gepl-prod, 2026-11-25: 5 requests
in that state with ``custom_transfer_se_state = Failed``)::

    Material Reserved --Transfer Material--> Material Transfer In Progress   (docstatus 1)
                                               |  job submits the transfer entry
                                               v
                                             Material Transferred

The job sets "Material Transferred" itself, once the entry is submitted. If it fails, the
request stays in progress with the error and the Retry Transfer From Reserve button.
Falling back to "Material Reserved" is not possible: that state is docstatus 0, and the
request is already submitted.

No transition leads out of the new state. Only the job moves it on, so the Actions menu
offers nothing until the material has really been transferred.

Same approach as ``add_mr_transfer_to_department_workflow``. The Material Request workflow
only lives in the database, so this patch edits the live document instead of using a
fixture. The new state row is **appended**. ``Workflow.update_default_workflow_status``
fills a blank ``workflow_state`` with the first state it finds for each ``doc_status``,
and for docstatus 1 that is still "Material Transferred".

Requests already waiting on their entry (state "Material Transferred", a reserve entry, no
transfer entry, transfer state Pending or Failed) are moved into the new state too. The
older requests with a blank transfer state are not touched: they come from before the
deferred job existed.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_mr_transfer_in_progress_state.execute

Idempotent: the state row is appended only once, the transition is repointed only while it
still leads to "Material Transferred", and the request update only matches requests that
are still in "Material Transferred".
"""

import frappe

WORKFLOW = "Material Request"

FROM_STATE = "Material Reserved"
ACTION = "Transfer Material"
OLD_NEXT_STATE = "Material Transferred"
NEW_STATE = "Material Transfer In Progress"


def _ensure_workflow_state():
	if frappe.db.exists("Workflow State", NEW_STATE):
		return False

	frappe.get_doc(
		{
			"doctype": "Workflow State",
			"workflow_state_name": NEW_STATE,
			"style": "Warning",
		}
	).insert(ignore_permissions=True)
	return True


def _amend_workflow():
	doc = frappe.get_doc("Workflow", WORKFLOW)
	changes = []

	if not any(row.state == NEW_STATE for row in doc.states):
		doc.append(
			"states",
			{
				"state": NEW_STATE,
				"doc_status": "1",
				"allow_edit": "All",
			},
		)
		changes.append(f"state {NEW_STATE!r}")

	for row in doc.transitions:
		if (
			row.state == FROM_STATE
			and row.action == ACTION
			and row.next_state == OLD_NEXT_STATE
		):
			row.next_state = NEW_STATE
			changes.append(f"{FROM_STATE!r} -> {ACTION!r} now leads to {NEW_STATE!r}")

	if changes:
		doc.save(ignore_permissions=True)
	return changes


def _move_waiting_requests():
	"""Move requests still waiting on their transfer entry into the new state."""
	if not frappe.db.has_column("Material Request", "custom_transfer_se_state"):
		return 0

	mr = frappe.qb.DocType("Material Request")
	waiting = (
		(mr.docstatus == 1)
		& (mr.workflow_state == OLD_NEXT_STATE)
		& mr.custom_reserve_se.isnotnull()
		& (mr.custom_reserve_se != "")
		& (mr.custom_transfer_se.isnull() | (mr.custom_transfer_se == ""))
		& mr.custom_transfer_se_state.isin(["Pending", "Failed"])
	)
	names = frappe.qb.from_(mr).select(mr.name).where(waiting).run(pluck=True)
	if names:
		# One UPDATE with the same conditions, so a job that finishes during the patch cannot
		# have its request moved back out of "Material Transferred".
		frappe.qb.update(mr).set(mr.workflow_state, NEW_STATE).where(
			waiting & mr.name.isin(names)
		).run()
	return len(names)


def execute():
	created_state = _ensure_workflow_state()

	if not frappe.db.exists("Workflow", WORKFLOW):
		# Fresh / CI site without the workflow. The state master is still worth leaving
		# behind; the rest applies on the next run.
		frappe.db.commit()
		frappe.logger().info(
			"add_mr_transfer_in_progress_state: no Material Request workflow on this site, "
			"nothing to amend"
		)
		return

	changes = _amend_workflow()

	# Only once "Transfer Material" leads to the new state, so a site whose transition was
	# changed by hand does not get requests parked in a state nothing leads to.
	transition_points_here = frappe.db.exists(
		"Workflow Transition",
		{
			"parent": WORKFLOW,
			"state": FROM_STATE,
			"action": ACTION,
			"next_state": NEW_STATE,
		},
	)
	moved = _move_waiting_requests() if transition_points_here else 0

	frappe.db.commit()
	frappe.logger().info(
		"add_mr_transfer_in_progress_state: "
		f"{'created Workflow State, ' if created_state else ''}"
		f"applied {len(changes)} change(s): {', '.join(changes) or 'none'}; "
		f"moved {moved} waiting request(s) to {NEW_STATE!r}"
	)
