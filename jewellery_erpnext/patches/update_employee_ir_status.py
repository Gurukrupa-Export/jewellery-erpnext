import frappe

def execute():
    employee_ir_list = frappe.db.get_all(
    "Employee IR", {
    "docstatus": 1
    }, ["name", "workflow_state", "type"])

    for ir in employee_ir_list:
        if ir.type == "Issue" and ir.workflow_state != "IR Issued":
            frappe.db.set_value("Employee IR", ir.name, "workflow_state", "IR Issued", update_modified=False)
        elif ir.type == "Receive" and ir.workflow_state != "IR Received":
            frappe.db.set_value("Employee IR", ir.name, "workflow_state", "IR Received", update_modified=False)

    frappe.db.commit()