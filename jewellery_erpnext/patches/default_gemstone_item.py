import frappe


def execute():
	"""
	Patch to create default dummy gemstone item
	"""

	item_code = "G-PER-DUM-PRE-CC"

	if frappe.db.exists("Item", item_code):
		print(f"Item already exists: {item_code}")
		return

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Default Dummy Gemstone",
			"item_group": "Gemstone DNU",
			"stock_uom": "Carat",
			"is_stock_item": 0,
			"disabled": 0,
		}
	)

	item.insert(ignore_permissions=True)

	frappe.db.commit()

	print(f"Created Item: {item_code}")
