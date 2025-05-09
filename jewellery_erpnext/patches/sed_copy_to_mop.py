import frappe
import copy
from frappe.utils import now

def execute():
	se_list = frappe.db.get_all("Stock Entry", {"docstatus":1, "creation": [">", "2025-01-01"], "creation": ["<", "2025-05-05"]}, pluck="name", limit=10000)
	for se in se_list:
		se_doc = frappe.get_doc("Stock Entry", se)

		for row in se_doc.items:
			if (row.manufacturing_operation and "," in row.manufacturing_operation
			or row.custom_manufacturing_work_order and "," in row.custom_manufacturing_work_order
			or row.custom_parent_manufacturing_order and "," in row.custom_parent_manufacturing_order):
				# Skip if any of the fields contain a comma
				continue


			copy_row = copy.deepcopy(row)
			copy_row.doctype = "Stock Entry MOP Item"
			copy_row.name = None
			se_doc.append("custom_mop_items", copy_row)

		se_doc.update_child_table("custom_mop_items")
		se_doc.db_update_all()
		print(f"Updated {se_doc.name} with {len(se_doc.custom_mop_items)} MOP items")
		frappe.db.commit()