"""Only System Manager may edit Material Request field values once it has left Draft.

Every Workflow Document State for the (DB-only) "Material Request" workflow currently has
allow_edit = "All" -- the literal Role every user holds by default -- so nothing has ever
restricted who can type into a saved request's fields. allow_edit is enforced purely
client-side (frappe.workflow.is_read_only disables the form) and has no bearing on the
workflow action buttons themselves: apply_workflow()'s internal doc.save() is gated by base
doctype "write" permission, not allow_edit, so operational roles keep clicking Reserve
Material / Transfer Material / Transfer to MOP exactly as before in every state -- only
free-typing into fields directly is now blocked outside Draft for anyone without System
Manager. Draft itself is left at "All" so normal users keep filling in a request as they
create it.

Also corrects System Manager's own Custom DocPerm row, which -- unlike every operational
role -- currently has write=0/create=0/submit=0/cancel=0 (only amend=1), the opposite of what
"System Manager can edit everything" requires.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.lock_material_request_edit_to_system_manager.execute

Idempotent: both writes are fixed-value assignments, skipped once already applied.
"""

import frappe
from frappe.permissions import update_permission_property

WORKFLOW = "Material Request"
DOCTYPE = "Material Request"
EDIT_ROLE = "System Manager"
OPEN_STATE = "Draft"


def _lock_workflow_states():
	if not frappe.db.exists("Workflow", WORKFLOW):
		return False

	doc = frappe.get_doc("Workflow", WORKFLOW)
	changed = False
	for state in doc.states:
		target = "All" if state.state == OPEN_STATE else EDIT_ROLE
		if state.allow_edit != target:
			state.allow_edit = target
			changed = True

	if changed:
		doc.save(ignore_permissions=True)
	return changed


def _grant_system_manager_full_access():
	if not frappe.db.exists(
		"Custom DocPerm", {"parent": DOCTYPE, "role": EDIT_ROLE, "permlevel": 0}
	):
		return False

	changed = False
	for ptype in ("write", "create", "submit", "cancel", "amend"):
		current = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": DOCTYPE, "role": EDIT_ROLE, "permlevel": 0},
			ptype,
		)
		if not current:
			update_permission_property(DOCTYPE, EDIT_ROLE, 0, ptype, 1, validate=False)
			changed = True

	if changed:
		frappe.clear_cache(doctype=DOCTYPE)
	return changed


def execute():
	states_locked = _lock_workflow_states()
	perms_fixed = _grant_system_manager_full_access()

	frappe.db.commit()
	frappe.logger().info(
		"lock_material_request_edit_to_system_manager: "
		f"workflow states {'updated' if states_locked else 'already locked'}; "
		f"System Manager perms {'fixed' if perms_fixed else 'already correct'}"
	)
