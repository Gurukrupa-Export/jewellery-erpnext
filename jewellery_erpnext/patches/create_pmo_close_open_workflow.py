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

Close requests (2026-10-29, simplified 2026-10-30). The buttons are only Hold, Open and
Close. A Close refused because the work orders are not back yet (elsewhere, in progress or
in transit, carrying metal, drafts) offers to put the PMO in Close Requested instead: the
form's Close dialog calls close_open.apply_action("Request Close"), which writes the state
directly -- there is no Request Close button. While Close Requested the work orders only
come back (doc_events/close_open.py), and Close closes it once they are back:

    Submitted       -- (Close, not ready)   --> Close Requested
    Close Requested -- Close                --> Closed
    Close Requested -- Hold                 --> Hold   (the Close request is kept)
    Close Requested -- Open                 --> Submitted   (the Close request is dropped)
    Hold            -- Open                 --> Close Requested   (Close request pending)

Close Requested -> Hold is also the Hold a queued Close (enqueue_close) applies before it
cancels Stock Entries. close_open keeps RESUME_KEY in hold_snapshot while a Close request is
pending under a Hold; the conditional Open rows read it, and only one of each pair passes
its condition, so the form shows a single Open.

The earlier Request Hold / Hold Requested and the Request Close / Withdraw Request buttons
are gone: a PMO still in Hold Requested goes back to Close Requested when a Close request
was pending under it, otherwise to Submitted.

The Order Department does the Close (and drops a Close request with Open), the Hold Team the
Hold and Open; System Manager keeps every transition. A transition row names one role, so a
transition open to several roles has one row per role.
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
)
# A state of the earlier request workflow, moved off by execute().
HOLD_REQUESTED = "Hold Requested"

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
	("Close Requested", "Close", "Closed", SM),
	("Close Requested", "Close", "Closed", ORDER_DEPARTMENT),
	("Close Requested", "Hold", "Hold", SM),
	("Close Requested", "Hold", "Hold", HOLD_TEAM),
	("Close Requested", "Open", "Submitted", SM),
	("Close Requested", "Open", "Submitted", ORDER_DEPARTMENT),
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
	# Hold Requested is gone: back to the Close request pending under it, else Submitted, and
	# the pending-Close mark it carried is dropped with it.
	frappe.db.sql(
		"""UPDATE `tabParent Manufacturing Order`
		SET workflow_state = IF(
				IFNULL(hold_snapshot, '') LIKE %(resume)s, 'Close Requested', 'Submitted'
			),
			hold_snapshot = NULL
		WHERE docstatus = 1 AND workflow_state = %(state)s""",
		{"resume": f"%{RESUME_KEY}%", "state": HOLD_REQUESTED},
	)
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
