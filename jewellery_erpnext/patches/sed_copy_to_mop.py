import frappe

def execute():
    se_list = frappe.db.get_all("Stock Entry", {"creation": ["<", "2025-03-30"], "creation": [">", "2025-03-01"]}, pluck="name")
    for se in se_list:
        sed_list = frappe.db.get_all("Stock Entry Detail", {"parent": se}, ["*"])
        for row in sed_list:
            se_mop_item = frappe.new_doc("Stock Entry MOP Item")
            se_mop_item.update(row)
            se_mop_item.save()

