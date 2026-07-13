import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	mould_exists_for_item,
)


def create_mould(self, row):
	if row.no_of_moulds > 0:
		item_code = frappe.db.get_value(
			"Manufacturing Work Order", row.manufacturing_work_order, "item_code"
		)
		if not item_code:
			return
		if mould_exists_for_item(item_code):
			return
		mould_doc = frappe.new_doc("Mould")
		mould_doc.company = self.company
		mould_doc.item_code = item_code
		mould_doc.no_of_moulds = row.no_of_moulds
		mould_doc.mould_wtin_gram = row.mould_wtin_gram
		# rake / tray_no / box_no are reqd on Mould but are filled in manually
		# later; bypass the mandatory check so the Mould is still created here.
		mould_doc.insert(ignore_mandatory=True, ignore_permissions=True)
