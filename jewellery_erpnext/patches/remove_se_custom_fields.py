import frappe

def execute():
    for fieldname in [
        "custom_manufacturing_work_order",
        "custom_parent_manufacturing_order",
        "manufacturing_operation",
    ]:
        if frappe.db.exists("Custom Field", {"dt": "Stock Entry Detail", "fieldname": fieldname}):
            frappe.delete_doc(
                "Custom Field", {"dt": "Stock Entry Detail", "fieldname": fieldname}, force=True
            )