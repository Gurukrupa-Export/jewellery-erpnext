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

PR #1236 review findings addressed here (see doc_events.material_request for F-01, F-04, F-05,
F-06, which live with the code they're about rather than the patch):

* F-02/F-03 -- both helpers used to return False silently when their target row was missing
  (no workflow named "Material Request", or no System Manager Custom DocPerm row), with no
  visible signal the restriction hadn't actually been applied. They still don't throw --
  every sibling patch to this same DB-only workflow (add_mr_transfer_to_department_workflow,
  update_mr_department_transfer_visibility, add_mr_department_to_mop_transition) is built on
  the same silent-skip contract, because a fresh site genuinely has no such workflow until
  someone sets it up by hand, and throwing here would break `bench migrate` on every one of
  them. Instead, the missing-row case now writes to the Error Log, so a site where this
  matters (one already running the Material Request workflow) surfaces it instead of the
  outcome looking identical to "already correct".
* F-07 -- the explicit frappe.db.commit() is intentional, not an oversight: every one of the
  three sibling patches above ends with the same explicit commit, so this matches established
  convention for this DB-only workflow rather than introducing a new pattern.
* F-08 -- was five separate update_permission_property() calls, each its own
  frappe.get_doc()+.save(). Replaced with one update_custom_docperm() call carrying every
  changed property, so a real fix is one read and one write instead of up to five of each.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.lock_material_request_edit_to_system_manager.execute

Idempotent: both writes are fixed-value assignments, skipped once already applied.
"""

import frappe
from frappe.core.doctype.custom_docperm.custom_docperm import update_custom_docperm

WORKFLOW = "Material Request"
DOCTYPE = "Material Request"
EDIT_ROLE = "System Manager"
OPEN_STATE = "Draft"
PERM_PTYPES = ("write", "create", "submit", "cancel", "amend")


def _lock_workflow_states():
	if not frappe.db.exists("Workflow", WORKFLOW):
		frappe.log_error(
			title="lock_material_request_edit_to_system_manager",
			message=(
				f'Workflow "{WORKFLOW}" does not exist on this site -- the System-Manager-only '
				"edit restriction was NOT applied. Expected wherever the Material Request "
				"workflow is already in use; harmless on a site that has never set it up."
			),
		)
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
	docperm_name = frappe.db.exists(
		"Custom DocPerm", {"parent": DOCTYPE, "role": EDIT_ROLE, "permlevel": 0}
	)
	if not docperm_name:
		frappe.log_error(
			title="lock_material_request_edit_to_system_manager",
			message=(
				f'No Custom DocPerm row for ("{DOCTYPE}", "{EDIT_ROLE}", permlevel 0) on this '
				"site -- System Manager's write/create/submit/cancel/amend access was NOT "
				"corrected."
			),
		)
		return False

	current = frappe.db.get_value(
		"Custom DocPerm", docperm_name, PERM_PTYPES, as_dict=True
	)
	missing = {ptype: 1 for ptype in PERM_PTYPES if not current.get(ptype)}
	if not missing:
		return False

	update_custom_docperm(docperm_name, missing)
	frappe.clear_cache(doctype=DOCTYPE)
	return True


def execute():
	states_locked = _lock_workflow_states()
	perms_fixed = _grant_system_manager_full_access()

	frappe.db.commit()
	frappe.logger().info(
		"lock_material_request_edit_to_system_manager: "
		f"workflow states {'updated' if states_locked else 'already locked or missing'}; "
		f"System Manager perms {'fixed' if perms_fixed else 'already correct or missing row'}"
	)
