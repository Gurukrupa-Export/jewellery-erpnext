import frappe

WORKFLOW = "Parent Manufacturing Order"
DOCTYPE = "Parent Manufacturing Order"

STATES = (
	("Draft", "Primary"),
	("Submitted", "Success"),
	("Closed", "Danger"),
	("Open", "Info"),
	("Cancelled", "Danger"),
)
ACTIONS = ("Submit", "Close", "Open", "Cancel")


def execute():
	for name, style in STATES:
		if not frappe.db.exists("Workflow State", name):
			frappe.get_doc(
				{
					"doctype": "Workflow State",
					"workflow_state_name": name,
					"style": style,
				}
			).insert(ignore_permissions=True)

	for action in ACTIONS:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)

	if not frappe.db.exists("Workflow", WORKFLOW):
		sm = "System Manager"
		frappe.get_doc(
			{
				"doctype": "Workflow",
				"workflow_name": WORKFLOW,
				"document_type": DOCTYPE,
				"workflow_state_field": "workflow_state",
				"is_active": 1,
				"send_email_alert": 0,
				"states": [
					{"state": "Draft", "doc_status": "0", "allow_edit": "All"},
					{"state": "Submitted", "doc_status": "1", "allow_edit": "All"},
					# "Open" is only ever reached by reopening a Closed PMO, so it tells
					# those apart from PMOs that were never closed (Submitted).
					{"state": "Open", "doc_status": "1", "allow_edit": "All"},
					{"state": "Closed", "doc_status": "1", "allow_edit": sm},
					{"state": "Cancelled", "doc_status": "2", "allow_edit": "All"},
				],
				"transitions": [
					{
						"state": "Draft",
						"action": "Submit",
						"next_state": "Submitted",
						"allowed": "All",
					},
					{
						"state": "Submitted",
						"action": "Close",
						"next_state": "Closed",
						"allowed": sm,
					},
					{
						"state": "Open",
						"action": "Close",
						"next_state": "Closed",
						"allowed": sm,
					},
					{
						"state": "Closed",
						"action": "Open",
						"next_state": "Open",
						"allowed": sm,
					},
					{
						"state": "Submitted",
						"action": "Cancel",
						"next_state": "Cancelled",
						"allowed": "All",
					},
					{
						"state": "Open",
						"action": "Cancel",
						"next_state": "Cancelled",
						"allowed": "All",
					},
				],
			}
		).insert(ignore_permissions=True)

	# The workflow state field is created with the workflow. PMOs that predate it have no
	# state, so without one they would show no Close button and no workflow label.
	if frappe.db.has_column(DOCTYPE, "workflow_state"):
		frappe.db.sql(
			"""UPDATE `tabParent Manufacturing Order`
			SET workflow_state = CASE docstatus
				WHEN 0 THEN 'Draft' WHEN 1 THEN 'Submitted' ELSE 'Cancelled' END
			WHERE IFNULL(workflow_state, '') = ''"""
		)
