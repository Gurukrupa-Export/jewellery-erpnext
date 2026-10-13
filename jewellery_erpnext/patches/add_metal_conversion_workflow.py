"""Put Metal Conversions under the Gemstone Conversion resize workflow.

Chain Subcontracting is a two-stage conversion -- the metal goes to a subcontractor on
**Issue** and comes back converted on **Receive** -- so it takes the same shape as the
Gemstone Conversion resize flow, whose workflow has only ever lived in the database::

    Draft --Submit--> Submitted                  every non-chain conversion, as today
    Draft --Issue---> Issue --Receive--> Receive --Submit--> Submitted
                      Issue --Cancel Issue--> Draft
                                                 Chain Subcontracting only

* **Issue** keeps docstatus 0: the transition is a save, and ``MetalConversions.validate``
  books the Issue transfer and the draft PO (``chain_subcontracting.run_issue``).
* **Cancel Issue** undoes an Issue that has not been received: the transfer is cancelled
  and the draft PO deleted (``chain_subcontracting.run_cancel_issue``). System Manager
  only -- the role that may cancel a Metal Conversion at all
  (``patches/metal_conversion_system_manager_cancel.py``).
* **Receive** is the submit: ``on_submit`` books the Repack, the Process Loss and the final
  transfer (``chain_subcontracting.run_receive``).
* **Receive -> Submitted** carries no stock effect; it is kept so the two doctypes read the
  same in the desk.

The two Draft transitions are mutually exclusive on ``is_chain_subcontracting``, so a
document never offers both. ``!= 1`` rather than ``== 0`` keeps a NULL flag -- every
document saved before the field existed -- on the plain Submit route.

**State order matters.** ``Workflow.update_default_workflow_status`` back-fills a blank
``workflow_state`` with the FIRST state per ``doc_status``, and
``set_workflow_state_on_action`` lands a programmatic ``.submit()`` on the first
docstatus-1 state. ``Submitted`` therefore comes before ``Receive``: the 788 conversions
already submitted on kggk-prod become "Submitted", and code that submits a conversion
directly (tests, SNC) still lands there. A chain conversion cannot be submitted that way:
``MetalConversions.before_submit`` refuses any chain submit that is not the Receive action.

There is no docstatus-2 state, as on Gemstone Conversion, so
``frappe.model.workflow.can_cancel_document`` keeps the standard Cancel button.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_metal_conversion_workflow.execute

Idempotent and additive: a missing workflow is created; an existing one only gets the
states and transitions it lacks (keyed on state, and on state + action), appended the way
``add_mr_transfer_to_department_workflow`` does it. No existing row is changed, so anything
an administrator later configures in the desk survives migrate.
"""

import frappe

WORKFLOW = "Metal Conversions"
DOCTYPE = "Metal Conversions"

DRAFT = "Draft"
ISSUE = "Issue"
RECEIVE = "Receive"
SUBMITTED = "Submitted"

ISSUE_ACTION = "Issue"
RECEIVE_ACTION = "Receive"
SUBMIT_ACTION = "Submit"
CANCEL_ISSUE_ACTION = "Cancel Issue"

ALL = "All"
SYSTEM_MANAGER = "System Manager"

IS_CHAIN = "doc.is_chain_subcontracting == 1"
NOT_CHAIN = "doc.is_chain_subcontracting != 1"

# (state, doc_status) -- Submitted before Receive, see the module docstring.
STATES = [
	(DRAFT, "0"),
	(ISSUE, "0"),
	(SUBMITTED, "1"),
	(RECEIVE, "1"),
]

# (state, action, next_state, condition, allowed)
TRANSITIONS = [
	(DRAFT, SUBMIT_ACTION, SUBMITTED, NOT_CHAIN, ALL),
	(DRAFT, ISSUE_ACTION, ISSUE, IS_CHAIN, ALL),
	(ISSUE, RECEIVE_ACTION, RECEIVE, None, ALL),
	(ISSUE, CANCEL_ISSUE_ACTION, DRAFT, None, SYSTEM_MANAGER),
	(RECEIVE, SUBMIT_ACTION, SUBMITTED, None, ALL),
]


def _ensure_masters():
	for state, _doc_status in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state}
			).insert(ignore_permissions=True)

	for action in {row[1] for row in TRANSITIONS}:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)


def _state(state, doc_status):
	return {"state": state, "doc_status": doc_status, "allow_edit": ALL}


def _transition(state, action, next_state, condition, allowed):
	return {
		"state": state,
		"action": action,
		"next_state": next_state,
		"allowed": allowed,
		"allow_self_approval": 1,
		"condition": condition,
	}


def execute():
	_ensure_masters()

	name = (
		WORKFLOW
		if frappe.db.exists("Workflow", WORKFLOW)
		else frappe.db.get_value("Workflow", {"document_type": DOCTYPE}, "name")
	)
	if name:
		_add_missing(frappe.get_doc("Workflow", name))
		return
	_create_workflow()


def _create_workflow():
	workflow = frappe.get_doc(
		{
			"doctype": "Workflow",
			"workflow_name": WORKFLOW,
			"document_type": DOCTYPE,
			"workflow_state_field": "workflow_state",
			"is_active": 1,
			"override_status": 0,
			"send_email_alert": 0,
			"states": [_state(*row) for row in STATES],
			"transitions": [_transition(*row) for row in TRANSITIONS],
		}
	)
	# Workflow.on_update creates the hidden ``workflow_state`` Custom Field and back-fills
	# every existing document (see the module docstring for why the order is safe).
	workflow.insert(ignore_permissions=True)
	frappe.logger().info(
		"add_metal_conversion_workflow: created the Metal Conversions workflow"
	)


def _add_missing(workflow):
	"""Append the states and transitions this patch defines that ``workflow`` lacks.

	Appended, never reordered: back-fill only ever reads the first state per doc_status,
	which an existing workflow already has. Saves only when something was added.
	"""
	added = []

	have_states = {row.state for row in workflow.states}
	for state, doc_status in STATES:
		if state not in have_states:
			workflow.append("states", _state(state, doc_status))
			added.append(f"state {state!r}")

	have_transitions = {(row.state, row.action) for row in workflow.transitions}
	for row in TRANSITIONS:
		if (row[0], row[1]) not in have_transitions:
			workflow.append("transitions", _transition(*row))
			added.append(f"{row[0]} --{row[1]}--> {row[2]}")

	if not added:
		frappe.logger().info(
			"add_metal_conversion_workflow: the Metal Conversions workflow is complete"
		)
		return
	workflow.save(ignore_permissions=True)
	frappe.logger().info(
		f"add_metal_conversion_workflow: added {', '.join(added)} to {workflow.name!r}"
	)
