import frappe

def execute():
	department_ir_list = frappe.db.get_all(
		"Department IR", {
		"docstatus": 1
		}, ["name", "workflow_state", "type"])

	for ir in department_ir_list:
		if ir.type == "Issue" and ir.workflow_state != "IR Issued":
			frappe.db.set_value("Department IR", ir.name, "workflow_state", "IR Issued", update_modified=False)
		elif ir.type == "Receive" and ir.workflow_state != "IR Received":
			frappe.db.set_value("Department IR", ir.name, "workflow_state", "IR Received", update_modified=False)

	frappe.db.commit()