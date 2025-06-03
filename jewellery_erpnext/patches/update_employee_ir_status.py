import frappe

def execute():
    employee_ir_list = frappe.db.get_all("Employee IR", {"docstatus":1, "type": "Receive"}, pluck="name")
    for ir in employee_ir_list:
        frappe.db.set_value("Employee IR", ir, "workflow_state", "IR Received", update_modified=False)

    frappe.db.commit()