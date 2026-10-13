"""Put Metal Conversions under the Gemstone Conversion resize workflow.

Chain Subcontracting is a two-stage conversion -- the metal goes to a subcontractor on
**Issue** and comes back converted on **Receive** -- so it takes the same shape as the
Gemstone Conversion resize flow, whose workflow has only ever lived in the database::

    Draft --Submit--> Submitted                  every non-chain conversion, as today
    Draft --Issue---> Issue --Receive--> Receive --Submit--> Submitted
                                                 Chain Subcontracting only

* **Issue** keeps docstatus 0: the transition is a save, and ``MetalConversions.validate``
  books the Issue transfer and the draft PO (``chain_subcontracting.run_issue``).
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

Idempotent and create-only: an existing Metal Conversions workflow is left untouched, so
anything an administrator later configures in the desk survives migrate.
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

IS_CHAIN = "doc.is_chain_subcontracting == 1"
NOT_CHAIN = "doc.is_chain_subcontracting != 1"

# (state, doc_status) -- Submitted before Receive, see the module docstring.
STATES = [
	(DRAFT, "0"),
	(ISSUE, "0"),
	(SUBMITTED, "1"),
	(RECEIVE, "1"),
]

# (state, action, next_state, condition)
TRANSITIONS = [
	(DRAFT, SUBMIT_ACTION, SUBMITTED, NOT_CHAIN),
	(DRAFT, ISSUE_ACTION, ISSUE, IS_CHAIN),
	(ISSUE, RECEIVE_ACTION, RECEIVE, None),
	(RECEIVE, SUBMIT_ACTION, SUBMITTED, None),
]


def _ensure_masters():
	for state, _doc_status in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state}
			).insert(ignore_permissions=True)

	for action in {action for _state, action, _next, _cond in TRANSITIONS}:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)


def execute():
	_ensure_masters()

	if frappe.db.exists("Workflow", WORKFLOW) or frappe.db.exists(
		"Workflow", {"document_type": DOCTYPE}
	):
		frappe.logger().info(
			"add_metal_conversion_workflow: a Metal Conversions workflow already exists, "
			"left as it is"
		)
		return

	workflow = frappe.get_doc(
		{
			"doctype": "Workflow",
			"workflow_name": WORKFLOW,
			"document_type": DOCTYPE,
			"workflow_state_field": "workflow_state",
			"is_active": 1,
			"override_status": 0,
			"send_email_alert": 0,
			"states": [
				{"state": state, "doc_status": doc_status, "allow_edit": "All"}
				for state, doc_status in STATES
			],
			"transitions": [
				{
					"state": state,
					"action": action,
					"next_state": next_state,
					"allowed": "All",
					"allow_self_approval": 1,
					"condition": condition,
				}
				for state, action, next_state, condition in TRANSITIONS
			],
		}
	)
	# Workflow.on_update creates the hidden ``workflow_state`` Custom Field and back-fills
	# every existing document (see the module docstring for why the order is safe).
	workflow.insert(ignore_permissions=True)
	frappe.logger().info(
		"add_metal_conversion_workflow: created the Metal Conversions workflow"
	)
