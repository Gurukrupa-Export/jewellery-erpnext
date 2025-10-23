import frappe

def execute():
    for fieldname in [
        "custom_manufacturing_work_order",
        "custom_parent_manufacturing_order",
        "manufacturing_operation",
    ]:
        if docname:= frappe.db.exists("Custom Field", {"dt": "Stock Entry Detail", "fieldtype":"Small Text", "fieldname": fieldname}):
            frappe.delete_doc(
                "Custom Field", docname, force=True
            )
            print(f"Deleted Custom Field: {fieldname}")
