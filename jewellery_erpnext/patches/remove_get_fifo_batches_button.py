import frappe


def execute():
	"""Remove the obsolete "Get FIFO Batches" button on Stock Entry.

	FIFO batch allocation (update_batches) now runs automatically during
	before_validate for every draft Stock Entry, so the manual button field is
	no longer needed. Delete the Custom Field on existing sites.
	"""
	name = "Stock Entry-get_fifo_batches"
	if frappe.db.exists("Custom Field", name):
		frappe.delete_doc("Custom Field", name, force=True)
		print(f"Deleted Custom Field: {name}")
