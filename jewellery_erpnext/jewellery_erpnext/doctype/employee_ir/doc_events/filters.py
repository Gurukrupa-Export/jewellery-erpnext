import frappe


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_batch_details(doctype, txt, searchfield, start, page_len, filters):
	# Returns batch_no candidates for Manually Book Loss Details. Filters by
	# item_code (always required) plus manufacturing_operation and
	# manufacturing_work_order when the row supplies them; missing filters are
	# treated as wildcards rather than literal NULL matches so the dropdown
	# isn't silently empty when only one of the two MOP keys is set.
	searchfield = "batch_no"
	ML = frappe.qb.DocType("MOP Log")

	query = (
		frappe.qb.from_(ML)
		.select(ML.batch_no)
		.distinct()
		.where((ML.item_code == filters.get("item_code")) & (ML.is_cancelled == 0))
	)

	if filters.get("manufacturing_operation"):
		query = query.where(
			ML.manufacturing_operation == filters.get("manufacturing_operation")
		)
	if filters.get("manufacturing_work_order"):
		query = query.where(
			ML.manufacturing_work_order == filters.get("manufacturing_work_order")
		)

	query = (
		query.where((ML[searchfield].like(f"%{txt}%")))
		.orderby(ML.batch_no, order=frappe.qb.desc)
		.limit(page_len)
		.offset(start)
	)
	data = query.run()
	return data
