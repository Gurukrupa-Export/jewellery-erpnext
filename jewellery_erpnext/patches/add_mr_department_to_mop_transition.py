"""Let a department-staged Material Request go on to Transfer to MOP.

"Material Transferred to Department" shipped as a terminal state -- Cancel was the only way
out. Staging material into a department is not the end of the flow though: the natural next
step is handing it to an operation in that department, and there was no way to do it. An
operator who switched Operation Type back to "Transfer to MOP" got an empty Actions menu.

Two changes, both configuration:

* A new transition ``Material Transferred to Department --Transfer to MOP-->
  Material Transferred to MOP``, carrying the same ``MOP_CONDITION`` as the transition of
  that name out of "Material Transferred". Both states therefore offer exactly one of the
  two routes, chosen by ``custom_operation_type``.

* ``custom_manufacturing_operation`` gains "Material Transferred to Department" in its
  ``depends_on`` and ``mandatory_depends_on``, so the field is reachable from the new state.
  The ``!= "Transfer to Department"`` clause both conditions already carry keeps it hidden
  and optional while the request is still on the department route; it appears and becomes
  required only once the operator flips to the MOP route, which is what stops the action
  being fired without an operation.

The server side of the chain lives outside this patch:
``doc_events.material_request._current_material_warehouse`` resolves the department guard
against ``custom_destination_warehouse`` once a transfer has happened, and
``make_department_mop_stock_entry`` sources the Work Order Stock Entry from that same
warehouse rather than the now-stale Request Item one.

Why a separate patch: frappe records patches by module path, so
``add_mr_transfer_to_department_workflow`` and ``add_mr_department_transfer_fields`` never
run again on a site that has them -- and the former is create-only, skipping a
``(state, action)`` pair that already exists. Their constants ARE updated in step, so a fresh
site gets all of this directly and this patch is a no-op there. Every write here is an
assignment of a fixed value, so it is also safe whether or not
``update_mr_department_transfer_visibility`` has been migrated yet.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_mr_department_to_mop_transition.execute

Idempotent: the transition is guarded on its (state, action) pair, the field properties are
fixed-value writes.
"""

import frappe

from jewellery_erpnext.patches.add_mr_department_transfer_fields import PROPERTY_UPDATES
from jewellery_erpnext.patches.add_mr_transfer_to_department_workflow import (
	MOP_ACTION,
	MOP_CONDITION,
	MOP_STATE,
	NEW_STATE,
	WORKFLOW,
)

FIELD = "Material Request-custom_manufacturing_operation"


def _update_field():
	if not frappe.db.exists("Custom Field", FIELD):
		return False

	# db.set_value rather than a doc save: Custom Field.on_update re-runs insert_after
	# resolution for the whole DocType, a far larger blast radius than two condition strings.
	frappe.db.set_value(
		"Custom Field", FIELD, PROPERTY_UPDATES[FIELD], update_modified=False
	)
	frappe.clear_cache(doctype="Material Request")
	return True


def _add_transition():
	if not frappe.db.exists("Workflow", WORKFLOW):
		return False

	doc = frappe.get_doc("Workflow", WORKFLOW)

	if any(
		row.state == NEW_STATE and row.action == MOP_ACTION for row in doc.transitions
	):
		return False

	if not any(row.state == NEW_STATE for row in doc.states):
		# add_mr_transfer_to_department_workflow has not run yet; it now creates this
		# transition itself, so there is nothing to add here.
		return False

	doc.append(
		"transitions",
		{
			"state": NEW_STATE,
			"action": MOP_ACTION,
			"next_state": MOP_STATE,
			"allowed": "All",
			"allow_self_approval": 1,
			"condition": MOP_CONDITION,
		},
	)
	doc.save(ignore_permissions=True)
	return True


def execute():
	field_updated = _update_field()
	transition_added = _add_transition()

	frappe.db.commit()
	frappe.logger().info(
		"add_mr_department_to_mop_transition: "
		f"custom_manufacturing_operation {'updated' if field_updated else 'absent'}; "
		f"transition {'added' if transition_added else 'already present or not applicable'}"
	)
