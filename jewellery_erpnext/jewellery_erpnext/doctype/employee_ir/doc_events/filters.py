import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Locate


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_batch_details(doctype, txt, searchfield, start, page_len, filters):
	searchfield = "batch_no"
	MBT = frappe.qb.DocType("MOP Balance Table")

	query = frappe.qb.from_(MBT).select(MBT.batch_no)

	query = query.where(
		(MBT.item_code == filters.get("item_code"))
		& (MBT.parent == filters.get("manufacturing_operation"))
	)

	query = (
		query.where((MBT[searchfield].like(f"%{txt}%")))
		.orderby(MBT.batch_no, order=frappe.qb.desc)
		.limit(page_len)
		.offset(start)
	)
	data = query.run()
	return data
