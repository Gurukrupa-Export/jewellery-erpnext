import frappe


def execute():
	try:
		if frappe.db.exists("Stock Entry Type", "Process Loss"):
			if (
				frappe.db.get_value("Stock Entry Type", "Process Loss", "purpose")
				!= "Repack"
			):
				frappe.db.set_value(
					"Stock Entry Type", "Process Loss", "purpose", "Repack"
				)
			return
		doc = frappe.new_doc("Stock Entry Type")
		doc.name = "Process Loss"
		doc.purpose = "Repack"
		doc.insert(ignore_permissions=True)
	except Exception as e:
		print(f"Error creating/updating Stock Entry Type 'Process Loss': {e}")
