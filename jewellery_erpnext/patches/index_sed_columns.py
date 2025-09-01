import frappe

def execute():
    query = """CREATE INDEX idx_sed_department ON `tabStock Entry Detail` (parent, t_warehouse, to_department)"""
    frappe.db.sql(query)
    print("Index idx_sed_department created on tabStock Entry Detail")