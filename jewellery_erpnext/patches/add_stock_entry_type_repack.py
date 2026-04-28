import frappe


def execute():
	try:
		if not frappe.db.exists("Stock Entry Type", "Subcontracting Repack"):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = "Subcontracting Repack"
			doc.purpose = "Repack"
			doc.insert()
	except Exception as e:
		print(f"Error adding Stock Entry Type: {e}")
