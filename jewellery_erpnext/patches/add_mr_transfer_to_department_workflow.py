"""Split the Material Request workflow's final action on ``custom_operation_type``.

Until now a submitted Manufacture request had exactly one way forward: **Transfer to
MOP**, which hands the reserved material to a Manufacturing Operation. This patch adds
the sibling route -- **Transfer to Department**, which moves the material from
``set_warehouse`` to a chosen department warehouse -- and makes the two mutually
exclusive, so the Actions menu offers whichever one ``custom_operation_type`` selects::

    Material Transferred
      |-- custom_operation_type != "Transfer to Department"  -> Transfer to MOP
      |                                                         -> Material Transferred to MOP
      `-- custom_operation_type == "Transfer to Department"  -> Transfer to Department
                                                                -> Material Transferred to Department

The first three transitions (Send for Reservation, Reserve Material, Transfer Material)
are untouched: they are what physically lands the material in ``set_warehouse``, so both
routes share them and only the fourth step differs.

Why a patch rather than ``fixtures/workflow.json``: the Workflow fixture is filtered to
three Sketch/Refining workflows (``hooks.py``) and the Material Request workflow has only
ever lived in the database. Widening that filter is not the fix -- ``import_fixtures``
walks the whole ``fixtures/`` directory on every migrate and **deletes and re-creates**
each record it names, which for a Workflow would take its ``states`` and ``transitions``
child rows with it, discarding anything an administrator has since configured in the
desk. That is the same trap ``seed_stock_entry_types`` was written to escape. So this
appends to the live document instead, in the same create-only spirit.

The Workflow **State** master ``Material Transferred to Department`` is created here as
well as being added to ``fixtures/workflow_state.json``: fixtures import *after*
post_model_sync patches, so the transition rows below cannot rely on the fixture having
run yet. ``Transfer to Department`` already exists as a Workflow Action Master.

Two things this patch is deliberately careful about:

* The new state is **appended** to ``states``. ``Workflow.update_default_workflow_status``
  back-fills a blank ``workflow_state`` with the FIRST state it sees per ``doc_status``,
  and ``doc_status = 1`` is already claimed by ``Material Transferred`` at idx 5, so an
  appended row can never become the default for submitted documents.
* Both new conditions, and the rewritten MOP one, are phrased against
  ``!= "Transfer to Department"`` rather than ``== "Transfer to MOP"``. A row whose
  ``custom_operation_type`` is NULL -- possible for anything created before the field
  existed -- therefore keeps the Transfer to MOP behaviour it has today.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_mr_transfer_to_department_workflow.execute

Idempotent: every append is guarded on the (state, action) pair already being present,
and the condition rewrite is an assignment of a fixed string.
"""

import frappe

WORKFLOW = "Material Request"

FROM_STATE = "Material Transferred"
NEW_STATE = "Material Transferred to Department"
MOP_STATE = "Material Transferred to MOP"
CANCELLED = "Cancelled"

DEPARTMENT_ACTION = "Transfer to Department"
MOP_ACTION = "Transfer to MOP"
CANCEL_ACTION = "Cancel"

_MANUFACTURE = 'doc.material_request_type == "Manufacture"'
_IS_DEPARTMENT = 'doc.custom_operation_type == "Transfer to Department"'
_NOT_DEPARTMENT = 'doc.custom_operation_type != "Transfer to Department"'

DEPARTMENT_CONDITION = f"{_MANUFACTURE} and {_IS_DEPARTMENT}"
MOP_CONDITION = f"{_MANUFACTURE} and {_NOT_DEPARTMENT}"

# Cancel is deliberately NOT gated on custom_operation_type, unlike the two route-selecting
# transitions. That field is ``allow_on_submit``, so an operator can switch it back to
# "Transfer to MOP" after the transfer -- and a Cancel keyed on the department route would
# vanish from the Actions menu the moment they did, leaving the document with no available
# transition at all. Nothing leads back into the MOP branch from this state, so leaving
# Cancel open cannot cause an accidental MOP transfer.
CANCEL_CONDITION = _MANUFACTURE

# (state, action, next_state, condition)
NEW_TRANSITIONS = [
	(FROM_STATE, DEPARTMENT_ACTION, NEW_STATE, DEPARTMENT_CONDITION),
	# Staging material into a department is not the end of the line -- the natural next
	# step is handing it to an operation in that department. Same condition as the
	# Material Transferred -> Transfer to MOP transition, so flipping custom_operation_type
	# swaps which single action the Actions menu offers here too.
	(NEW_STATE, MOP_ACTION, MOP_STATE, MOP_CONDITION),
	# Without this the new state is a dead end: no transition out of it at all, not even
	# the Cancel every other terminal state on this workflow has.
	(NEW_STATE, CANCEL_ACTION, CANCELLED, CANCEL_CONDITION),
]


def _ensure_workflow_state():
	if frappe.db.exists("Workflow State", NEW_STATE):
		return False

	frappe.get_doc(
		{
			"doctype": "Workflow State",
			"workflow_state_name": NEW_STATE,
			# "Material Transferred to MOP", the sibling this mirrors, carries no style.
			"style": "",
		}
	).insert(ignore_permissions=True)
	return True


def execute():
	created_state = _ensure_workflow_state()

	if not frappe.db.exists("Workflow", WORKFLOW):
		# Fresh / CI site that has not had the workflow built yet. The state master above
		# is still worth leaving behind; the rest re-applies on the next run.
		frappe.db.commit()
		frappe.logger().info(
			"add_mr_transfer_to_department_workflow: no Material Request workflow on this "
			"site, nothing to amend"
		)
		return

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

	existing = {(row.state, row.action) for row in doc.transitions}
	for state, action, next_state, condition in NEW_TRANSITIONS:
		if (state, action) in existing:
			continue
		doc.append(
			"transitions",
			{
				"state": state,
				"action": action,
				"next_state": next_state,
				"allowed": "All",
				"allow_self_approval": 1,
				"condition": condition,
			},
		)
		changes.append(f"transition {state!r} -> {action!r}")

	for row in doc.transitions:
		if (
			row.state == FROM_STATE
			and row.action == MOP_ACTION
			and row.condition != MOP_CONDITION
		):
			row.condition = MOP_CONDITION
			changes.append(f"condition on {FROM_STATE!r} -> {MOP_ACTION!r}")

	if changes:
		doc.save(ignore_permissions=True)

	frappe.db.commit()
	frappe.logger().info(
		"add_mr_transfer_to_department_workflow: "
		f"{'created Workflow State, ' if created_state else ''}"
		f"applied {len(changes)} change(s): {', '.join(changes) or 'none'}"
	)
