import frappe


def execute():
	"""
	Patch to create default dummy finding item
	"""

	item_code = "F-PER-DUM-PRE-CC"

	if frappe.db.exists("Item", item_code):
		print(f"Item already exists: {item_code}")
		return

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Default Dummy Finding",
			"item_group": "Finding DNU",
			"stock_uom": "Gram",
			"is_stock_item": 0,
			"disabled": 0,
		}
	)

	item.insert(ignore_permissions=True)

	frappe.db.commit()

	print(f"Created Item: {item_code}")
