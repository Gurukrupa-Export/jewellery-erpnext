import frappe


def create_mould(self, row):
	if row.no_of_moulds > 0:
		mould_doc = frappe.new_doc("Mould")
		mould_doc.company = self.company
		mould_doc.item_code = frappe.db.get_value(
			"Manufacturing Work Order", row.manufacturing_work_order, "item_code"
		)
		mould_doc.no_of_moulds = row.no_of_moulds
		mould_doc.mould_wtin_gram = row.mould_wtin_gram
		# mould_doc.rake = row.rake
		# mould_doc.tray_no = row.tray_no
		# mould_doc.box_no = row.box_no
		mould_doc.flags.ignore_permission = True
		mould_doc.save()
