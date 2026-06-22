import frappe


def execute():
	states = [
		{"name": "Draft", "style": "Primary"},
		{"name": "Physical Verification", "style": "Warning"},
		{"name": "Submitted", "style": "Success"},
		{"name": "Received", "style": "Primary"},
		{"name": "Classified", "style": "Warning"},
		{"name": "Refining In Progress", "style": "Info"},
		{"name": "Recovery Entered", "style": "Info"},
		{"name": "Recovery Verified", "style": "Success"},
		{"name": "Completed", "style": "Success"},
		{"name": "Transferred", "style": "Primary"},
		{"name": "Cancelled", "style": "Danger"},
	]

	for state in states:
		if not frappe.db.exists("Workflow State", state["name"]):
			doc = frappe.new_doc("Workflow State")
			doc.workflow_state_name = state["name"]
			doc.style = state["style"]
			doc.insert(ignore_permissions=True)
			print(f"Created Workflow State: {state['name']}")
