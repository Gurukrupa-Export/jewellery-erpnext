import frappe


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_batch_details(doctype, txt, searchfield, start, page_len, filters):
	searchfield = "batch_no"
	ML = frappe.qb.DocType("MOP Log")

	query = frappe.qb.from_(ML).select(ML.batch_no).distinct()

	query = query.where(
		(ML.item_code == filters.get("item_code"))
		& (ML.manufacturing_operation == filters.get("manufacturing_operation"))
		& (ML.is_cancelled == 0)
	)

	query = (
		query.where((ML[searchfield].like(f"%{txt}%")))
		.orderby(ML.batch_no, order=frappe.qb.desc)
		.limit(page_len)
		.offset(start)
	)
	data = query.run()
	return data
