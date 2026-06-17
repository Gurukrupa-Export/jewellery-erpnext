import frappe


def execute():
	actions = [
		"Send for Verification",
		"Submit",
		"Receive Materials",
		"Classify & Generate Recovery",
		"Enter Yield",
		"Verify Recovery",
		"Complete Refining",
		"Transfer to Department",
		"Cancel",
		"Approve",
		"Reject",
	]

	for action in actions:
		if not frappe.db.exists("Workflow Action Master", action):
			doc = frappe.new_doc("Workflow Action Master")
			doc.workflow_action_name = action
			doc.insert(ignore_permissions=True)
			print(f"Created Workflow Action Master: {action}")
