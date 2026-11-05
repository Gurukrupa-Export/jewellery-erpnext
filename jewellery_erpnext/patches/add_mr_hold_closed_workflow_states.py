"""Give the Material Request workflow the "Hold" and "Closed" states a PMO puts its requests in.

Holding a Parent Manufacturing Order sets its Material Requests to "Hold" and Open puts each
one back where it was; closing the PMO sets them to "Closed". The PMO flow writes the state
directly -- nothing transitions into or out of these states, so a held or closed request
offers no Actions at all.

Each state is appended twice, once per doc_status, because a PMO's requests are a mix of
drafts (Draft / Reservation Pending / Material Reserved) and submitted ones (Material
Transferred ...). Frappe matches a document against the (state, doc_status) pairs: without a
doc_status 1 row, set_workflow_state_on_action would move a submitted request off "Hold" on
its next update; without the state at all, validate_workflow refuses every save of it. The
rows are appended, so the first state per doc_status -- what Frappe defaults a blank state
to -- does not change.

Why a patch: the Material Request workflow only lives in the database (see
add_mr_transfer_to_department_workflow), so this appends to it rather than shipping it as a
fixture. A site without that workflow is left alone. The Workflow State masters are created
here too: fixtures import after patches.

Idempotent: every row is guarded on its (state, doc_status) pair already being there.
"""

import frappe
from frappe.utils import cstr

WORKFLOW = "Material Request"
# (state, style for a Workflow State created here)
STATES = (("Hold", "Warning"), ("Closed", "Danger"))
EDIT_ROLE = "System Manager"


def execute():
	if not frappe.db.exists("Workflow", WORKFLOW):
		return

	for state, style in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{
					"doctype": "Workflow State",
					"workflow_state_name": state,
					"style": style,
				}
			).insert(ignore_permissions=True)

	workflow = frappe.get_doc("Workflow", WORKFLOW)
	present = {(row.state, cstr(row.doc_status)) for row in workflow.states}
	changed = False
	for state, _style in STATES:
		for doc_status in ("0", "1"):
			if (state, doc_status) in present:
				continue
			workflow.append(
				"states",
				{"state": state, "doc_status": doc_status, "allow_edit": EDIT_ROLE},
			)
			changed = True
	if changed:
		workflow.save(ignore_permissions=True)
