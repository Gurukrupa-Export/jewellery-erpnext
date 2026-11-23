"""Chain Manufacturing's Receive submits the Metal Conversion straight to Submitted.

The first workflow (``add_metal_conversion_workflow``) landed Receive on a docstatus-1
"Receive" state and then asked for a second Submit to "Submitted" -- a step with no stock
effect that users had to click through::

    before:  Issue --Receive--> Receive --Submit--> Submitted
    after:   Issue --Receive--> Submitted

On the Metal Conversions workflow this patch:

* points the ``Issue --Receive-->`` transition at Submitted (only while it still leads to
  Receive -- a transition an administrator re-pointed is left alone);
* deletes the ``Receive --Submit--> Submitted`` transition;
* moves submitted conversions waiting in Receive to Submitted -- they are already
  docstatus 1, so only the state changes;
* drops the Receive state once no transition uses it.

Idempotent: a workflow already in the new shape, or a site without the workflow, is left
as it is. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.metal_conversion_receive_submits_directly.execute
"""

import frappe

WORKFLOW = "Metal Conversions"
DOCTYPE = "Metal Conversions"

ISSUE = "Issue"
LEGACY_RECEIVE = "Receive"
SUBMITTED = "Submitted"

RECEIVE_ACTION = "Receive"
SUBMIT_ACTION = "Submit"


def execute():
	name = (
		WORKFLOW
		if frappe.db.exists("Workflow", WORKFLOW)
		else frappe.db.get_value("Workflow", {"document_type": DOCTYPE}, "name")
	)
	if not name:
		return
	workflow = frappe.get_doc("Workflow", name)
	changes = submit_on_receive(workflow)
	if not changes:
		frappe.logger().info(
			"metal_conversion_receive_submits_directly: Receive already submits directly"
		)
		return
	workflow.save(ignore_permissions=True)
	frappe.logger().info(
		f"metal_conversion_receive_submits_directly: {', '.join(changes)} on {workflow.name!r}"
	)


def submit_on_receive(workflow):
	"""Rewrite ``workflow`` in memory; return what changed (empty when nothing did).

	The conversions in Receive are moved before the state is dropped, so none is left in a
	state the workflow no longer has.
	"""
	changes = []

	for row in workflow.transitions:
		if (
			row.state == ISSUE
			and row.action == RECEIVE_ACTION
			and row.next_state == LEGACY_RECEIVE
		):
			row.next_state = SUBMITTED
			changes.append(f"{ISSUE} --{RECEIVE_ACTION}--> {SUBMITTED}")

	stockless_submit = [
		row
		for row in workflow.transitions
		if row.state == LEGACY_RECEIVE
		and row.action == SUBMIT_ACTION
		and row.next_state == SUBMITTED
	]
	if stockless_submit:
		workflow.transitions = [
			row for row in workflow.transitions if row not in stockless_submit
		]
		for idx, row in enumerate(workflow.transitions, start=1):
			row.idx = idx
		changes.append(f"removed {LEGACY_RECEIVE} --{SUBMIT_ACTION}--> {SUBMITTED}")

	still_used = any(
		LEGACY_RECEIVE in (row.state, row.next_state) for row in workflow.transitions
	)
	has_state = any(row.state == LEGACY_RECEIVE for row in workflow.states)
	if has_state and not still_used:
		moved = frappe.db.count(
			DOCTYPE, {"workflow_state": LEGACY_RECEIVE, "docstatus": 1}
		)
		if moved:
			frappe.db.set_value(
				DOCTYPE,
				{"workflow_state": LEGACY_RECEIVE, "docstatus": 1},
				"workflow_state",
				SUBMITTED,
				update_modified=False,
			)
			changes.append(f"moved {moved} conversion(s) from {LEGACY_RECEIVE}")
		workflow.states = [
			row for row in workflow.states if row.state != LEGACY_RECEIVE
		]
		for idx, row in enumerate(workflow.states, start=1):
			row.idx = idx
		changes.append(f"removed state {LEGACY_RECEIVE}")

	return changes
