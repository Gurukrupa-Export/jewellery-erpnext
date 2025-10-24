import frappe

def execute():
    fieldname = "manufacturing_operation"

    if docname:= frappe.db.exists("Custom Field", {"dt": "Stock Entry Detail", "fieldtype":"Small Text", "fieldname": fieldname}):
        frappe.delete_doc(
            "Custom Field", docname, force=True
        )
        print(f"Deleted Custom Field: {fieldname}")
