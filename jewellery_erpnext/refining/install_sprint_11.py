import frappe


def execute():
	frappe.flags.in_install = True
	create_reports()
	create_number_cards()
	create_dashboard()
	create_workflow_states()
	create_workflow_actions()

	frappe.db.commit()
	print("Sprint 11 Reporting & Dashboard setup complete.")


def create_workflow_states():
	states = [
		{"name": "Draft", "style": "Primary"},
		{"name": "Submitted", "style": "Success"},
		{"name": "Received", "style": "Primary"},
		{"name": "Classified", "style": "Warning"},
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


def create_workflow_actions():
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


def create_reports():
	reports = [
		{
			"name": "Daily Dust and Loss",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Weekly Refining Summary",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Weekly Recovery Efficiency",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Weekly Work Order Refining",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Weekly Serial Number Refining",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Monthly Refining Performance",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Monthly Dust Reprocessing",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
		{
			"name": "Monthly Department Loss Analysis",
			"ref_doctype": "Refining Entry",
			"type": "Script Report",
		},
	]

	for rep in reports:
		if not frappe.db.exists("Report", rep["name"]):
			doc = frappe.new_doc("Report")
			doc.report_name = rep["name"]
			doc.ref_doctype = rep["ref_doctype"]
			doc.report_type = rep["type"]
			doc.module = "Refining"
			doc.is_standard = "Yes"
			doc.insert(ignore_permissions=True)
			print(f"Created Report: {rep['name']}")


def create_number_cards():
	cards = [
		{
			"name": "Physical vs System Match %",
			"function": "Average",
			"document_type": "Refining Entry",
			"aggregate_function_based_on": "difference_quantity",
		},
		{
			"name": "Entry Completion Time",
			"function": "Count",
			"document_type": "Refining Entry",
		},
		{
			"name": "Refining Error Rate",
			"function": "Count",
			"document_type": "Refining Entry",
		},
		{
			"name": "Refining Processing Time",
			"function": "Count",
			"document_type": "Refining Entry",
		},
		{
			"name": "Average Recovery Efficiency",
			"function": "Average",
			"document_type": "Refining Entry",
			"aggregate_function_based_on": "recovery_percentage",
		},
		{
			"name": "Refining Transfer Time",
			"function": "Count",
			"document_type": "Refining Entry",
		},
		{
			"name": "Dust Conversion Accuracy",
			"function": "Average",
			"document_type": "Refining Entry",
			"aggregate_function_based_on": "actual_recovery",
		},
		{
			"name": "Work Order Closure %",
			"function": "Count",
			"document_type": "Refining Entry",
		},
		{
			"name": "Serial Update %",
			"function": "Count",
			"document_type": "Refining Entry",
		},
		{
			"name": "Batch Traceability %",
			"function": "Count",
			"document_type": "Refining Entry",
		},
	]

	for c in cards:
		if not frappe.db.exists("Number Card", c["name"]):
			doc = frappe.new_doc("Number Card")
			doc.name = c["name"]
			doc.label = c["name"]
			doc.document_type = c["document_type"]
			doc.function = c["function"]
			if "aggregate_function_based_on" in c:
				doc.aggregate_function_based_on = c["aggregate_function_based_on"]
			doc.is_standard = 1
			doc.module = "Refining"
			doc.insert(ignore_permissions=True)
			print(f"Created Number Card: {c['name']}")


def create_dashboard():
	if not frappe.db.exists("Dashboard Chart", "Refining Activity"):
		chart = frappe.new_doc("Dashboard Chart")
		chart.chart_name = "Refining Activity"
		chart.document_type = "Refining Entry"
		chart.chart_type = "Count"
		chart.timeseries = 1
		chart.time_interval = "Daily"
		chart.based_on = "creation"
		chart.filters_json = "{}"
		chart.is_standard = 1
		chart.module = "Refining"
		chart.insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Dashboard Chart", "Refining Activity", "is_standard", 1)
		frappe.db.set_value(
			"Dashboard Chart", "Refining Activity", "module", "Refining"
		)

	if not frappe.db.exists("Dashboard", "Refining Management Dashboard"):
		doc = frappe.new_doc("Dashboard")
		doc.dashboard_name = "Refining Management Dashboard"
		doc.module = "Refining"
		doc.is_standard = 1

		cards = frappe.get_all("Number Card", filters={"module": "Refining"})
		for c in cards:
			doc.append("cards", {"card": c.name})

		doc.append("charts", {"chart": "Refining Activity"})

		doc.insert(ignore_permissions=True)
		print("Created Dashboard: Refining Management Dashboard")


if __name__ == "__main__":
	execute()
