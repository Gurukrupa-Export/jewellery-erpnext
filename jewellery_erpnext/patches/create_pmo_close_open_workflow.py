"""Parent Manufacturing Order workflow: Submit, Hold, Open and Close.

    Draft         -- Submit --> Submitted     Submitted -- Hold   --> Hold
    Draft         -- Hold   --> Hold (Draft)  Submitted -- Close  --> Closed
    Draft         -- Close  --> Closed (Draft) Submitted -- Cancel --> Cancelled
    Hold (Draft)  -- Submit --> Submitted     Hold      -- Open   --> Submitted
    Hold (Draft)  -- Close  --> Closed (Draft) Hold      -- Close  --> Closed

A state has exactly one doc_status, and Frappe forbids a draft from going straight to a
cancelled one, so holding or closing a draft needs states of its own: "Hold (Draft)" and
"Closed (Draft)" stay docstatus 0, so on_submit -- which creates the work orders and
Material Requests -- never runs for them. Submitting a held draft goes through the normal
submit. Close is final: it cancels the Material Requests' Stock Entries and finishes the
operations, so there is no way back to Open from it.

Draft, Submitted and Cancelled come first in their doc_status: Frappe's defaults (a blank
state, set_workflow_state_on_action) take the first state per doc_status.

This replaces the first version of this patch (Draft / Submitted / Open / Closed /
Cancelled, create-only): the workflow is rebuilt to the states and transitions below, and
PMOs are moved off states the new workflow does not have -- "Open" to Submitted, a legacy
"On Hold" to Hold / Hold (Draft), anything else by its docstatus.

The roles are a starting point; the server checks transitions, not roles, so they can be
changed on the Workflow form.

Close / Hold requests (2026-10-29). A submitted PMO whose work orders are still in other
departments, in progress, or carrying metal cannot be held or closed yet; it can be put in
a request state instead, which restricts what moves for it until the work comes back
(doc_events/close_open.py):

    Submitted       -- Request Close    --> Close Requested
    Submitted       -- Request Hold     --> Hold Requested
    Close Requested -- Close            --> Closed
    Close Requested -- Hold             --> Hold
    Close Requested -- Withdraw Request --> Submitted
    Hold Requested  -- Hold             --> Hold
    Hold Requested  -- Withdraw Request --> Submitted

Close Requested -> Hold is also the Hold a queued Close (enqueue_close) applies before it
cancels Stock Entries.

Hold during a Close request (2026-10-29, FRD 3.5). The Hold Team may Hold a Close Requested
PMO (once its work is back) or Request Hold for it (before). The Close request is kept:
close_open stores RESUME_KEY in hold_snapshot, and conditional transitions send Open and
Withdraw Request back to Close Requested instead of Submitted:

    Close Requested -- Request Hold     --> Hold Requested
    Hold            -- Open             --> Close Requested   (Close request pending)
    Hold Requested  -- Withdraw Request --> Close Requested   (Close request pending)

Only one of each pair passes its condition, so the form still shows a single button. The Order Department requests and does the Close, the Hold Team
the Hold and Open; System Manager keeps every transition. A transition row names one
role, so a transition open to several roles has one row per role.
"""

import frappe

WORKFLOW = "Parent Manufacturing Order"
DOCTYPE = "Parent Manufacturing Order"
SM = "System Manager"
ORDER_DEPARTMENT = "Order Department"
HOLD_TEAM = "Hold Team"
ROLES = (ORDER_DEPARTMENT, HOLD_TEAM)

# (state, doc_status, allow_edit, style for a Workflow State created here)
STATES = (
	("Draft", "0", "All", "Primary"),
	("Submitted", "1", "All", "Success"),
	("Cancelled", "2", "All", "Danger"),
	("Hold (Draft)", "0", "All", "Warning"),
	("Closed (Draft)", "0", SM, "Danger"),
	("Hold", "1", "All", "Warning"),
	("Closed", "1", SM, "Danger"),
	("Close Requested", "1", "All", "Info"),
	("Hold Requested", "1", "All", "Warning"),
)

# close_open.RESUME_CLOSE_KEY: hold_snapshot carries it while a Close request is pending.
RESUME_KEY = "resume_close_request"
RESUMES = f"'{RESUME_KEY}' in (doc.hold_snapshot or '')"
NO_RESUME = f"'{RESUME_KEY}' not in (doc.hold_snapshot or '')"

# (state, action, next_state, allowed[, condition])
TRANSITIONS = (
	("Draft", "Submit", "Submitted", "All"),
	("Draft", "Hold", "Hold (Draft)", SM),
	("Draft", "Close", "Closed (Draft)", SM),
	("Hold (Draft)", "Submit", "Submitted", SM),
	("Hold (Draft)", "Close", "Closed (Draft)", SM),
	("Submitted", "Hold", "Hold", SM),
	("Submitted", "Hold", "Hold", HOLD_TEAM),
	("Submitted", "Close", "Closed", SM),
	("Submitted", "Close", "Closed", ORDER_DEPARTMENT),
	("Submitted", "Cancel", "Cancelled", "All"),
	("Hold", "Open", "Submitted", SM, NO_RESUME),
	("Hold", "Open", "Submitted", HOLD_TEAM, NO_RESUME),
	("Hold", "Open", "Close Requested", SM, RESUMES),
	("Hold", "Open", "Close Requested", HOLD_TEAM, RESUMES),
	("Hold", "Close", "Closed", SM),
	("Hold", "Close", "Closed", ORDER_DEPARTMENT),
	("Submitted", "Request Close", "Close Requested", SM),
	("Submitted", "Request Close", "Close Requested", ORDER_DEPARTMENT),
	("Submitted", "Request Hold", "Hold Requested", SM),
	("Submitted", "Request Hold", "Hold Requested", HOLD_TEAM),
	("Close Requested", "Close", "Closed", SM),
	("Close Requested", "Close", "Closed", ORDER_DEPARTMENT),
	("Close Requested", "Hold", "Hold", SM),
	("Close Requested", "Hold", "Hold", HOLD_TEAM),
	("Close Requested", "Request Hold", "Hold Requested", SM),
	("Close Requested", "Request Hold", "Hold Requested", HOLD_TEAM),
	("Close Requested", "Withdraw Request", "Submitted", SM),
	("Close Requested", "Withdraw Request", "Submitted", ORDER_DEPARTMENT),
	("Hold Requested", "Hold", "Hold", SM),
	("Hold Requested", "Hold", "Hold", HOLD_TEAM),
	("Hold Requested", "Withdraw Request", "Submitted", SM, NO_RESUME),
	("Hold Requested", "Withdraw Request", "Submitted", HOLD_TEAM, NO_RESUME),
	("Hold Requested", "Withdraw Request", "Close Requested", SM, RESUMES),
	("Hold Requested", "Withdraw Request", "Close Requested", HOLD_TEAM, RESUMES),
)


def execute():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 1}
			).insert(ignore_permissions=True)

	for state, _doc_status, _allow_edit, style in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{
					"doctype": "Workflow State",
					"workflow_state_name": state,
					"style": style,
				}
			).insert(ignore_permissions=True)

	for action in sorted({t[1] for t in TRANSITIONS}):
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)

	if frappe.db.exists("Workflow", WORKFLOW):
		workflow = frappe.get_doc("Workflow", WORKFLOW)
	else:
		workflow = frappe.new_doc("Workflow")
		workflow.workflow_name = WORKFLOW
	workflow.document_type = DOCTYPE
	workflow.workflow_state_field = "workflow_state"
	workflow.is_active = 1
	workflow.send_email_alert = 0
	workflow.set(
		"states",
		[
			{"state": state, "doc_status": doc_status, "allow_edit": allow_edit}
			for state, doc_status, allow_edit, _style in STATES
		],
	)
	workflow.set(
		"transitions",
		[
			{
				"state": state,
				"action": action,
				"next_state": next_state,
				"allowed": allowed,
				"condition": condition[0] if condition else None,
			}
			for state, action, next_state, allowed, *condition in TRANSITIONS
		],
	)
	workflow.save(ignore_permissions=True)

	# The workflow state field is created with the workflow.
	if not frappe.db.has_column(DOCTYPE, "workflow_state"):
		return
	valid = {}
	for state, doc_status, _allow_edit, _style in STATES:
		valid[int(doc_status)] = (*valid.get(int(doc_status), ()), state)
	frappe.db.sql(
		"""UPDATE `tabParent Manufacturing Order`
		SET workflow_state = CASE
			WHEN workflow_state = 'On Hold' AND docstatus = 1 THEN 'Hold'
			WHEN workflow_state = 'On Hold' AND docstatus = 0 THEN 'Hold (Draft)'
			WHEN docstatus = 0 THEN 'Draft'
			WHEN docstatus = 1 THEN 'Submitted'
			ELSE 'Cancelled' END
		WHERE NOT (
			(docstatus = 0 AND IFNULL(workflow_state, '') IN %(draft)s)
			OR (docstatus = 1 AND IFNULL(workflow_state, '') IN %(submitted)s)
			OR (docstatus = 2 AND IFNULL(workflow_state, '') IN %(cancelled)s)
		)""",
		{"draft": valid[0], "submitted": valid[1], "cancelled": valid[2]},
	)
