import frappe
from frappe.query_builder.functions import IfNull

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate import (
	get_blocked_loss_variants,
)


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


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_manual_loss_items(doctype, txt, searchfield, start, page_len, filters):
	# Returns item_code candidates for Manually Book Loss Details, restricted to
	# items that appear in the selected Manufacturing Work Order (via MOP Log,
	# the same ledger get_batch_details reads). Narrowed by manufacturing_operation
	# when the row supplies it. If no work order is set yet the dropdown is empty
	# on purpose so the operator selects the work order first.
	#
	# Also drops materials the Department Operation refuses loss on, so the
	# operator never picks an item that validate would only throw on. This is
	# prevention, not enforcement -- material_loss_gate remains the authority, and
	# a missing `operation` filter (a browser still running the old bundle) fails
	# open to the previous behaviour rather than erroring.
	searchfield = "item_code"
	if not filters.get("manufacturing_work_order"):
		return []

	ML = frappe.qb.DocType("MOP Log")

	query = (
		frappe.qb.from_(ML)
		.select(ML.item_code)
		.distinct()
		.where(
			(ML.manufacturing_work_order == filters.get("manufacturing_work_order"))
			& (ML.is_cancelled == 0)
		)
	)

	if filters.get("manufacturing_operation"):
		query = query.where(
			ML.manufacturing_operation == filters.get("manufacturing_operation")
		)

	blocked_variants = get_blocked_loss_variants(filters.get("operation"))
	if blocked_variants:
		# Joined on Item.variant_of rather than filtered on the item-code prefix,
		# so the dropdown and the server gate key on exactly the same signal.
		# IfNull keeps non-variant items (blank variant_of) visible -- SQL NOT IN
		# would drop them, and the gate lets them book loss.
		Item = frappe.qb.DocType("Item")
		query = query.left_join(Item).on(Item.name == ML.item_code)
		query = query.where(IfNull(Item.variant_of, "").notin(sorted(blocked_variants)))

	query = (
		query.where((ML[searchfield].like(f"%{txt}%")))
		.orderby(ML.item_code, order=frappe.qb.desc)
		.limit(page_len)
		.offset(start)
	)
	return query.run()
