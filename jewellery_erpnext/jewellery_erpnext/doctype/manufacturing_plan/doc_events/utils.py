import json

import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Locate


@frappe.whitelist()
def get_mwo(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Manufacturing Plan")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))
	if not target_doc.get("manufacturing_work_order", {"manufacturing_work_order": source_name}):
		target_doc.append(
			"manufacturing_work_order",
			{"manufacturing_work_order": source_name},
		)
	return target_doc


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_mwo_details(doctype, txt, searchfield, start, page_len, filters):
	conditions = ""
	MWO = frappe.qb.DocType("Manufacturing Work Order")

	query = (
		frappe.qb.from_(MWO)
		.select(MWO.name, MWO.company, MWO.customer)
		.distinct()
		.where((MWO.docstatus == 1) & (MWO.is_finding_mwo == 1))
	)
	if filters.get("company"):
		# first_department = frappe.db.get_value(
		# 	"Manufacturing Setting", {"company": filters.get("company")}, "default_department"
		# )
		first_department = frappe.db.get_value(
			"Manufacturing Setting", {"manufacturer": filters.get("manufacturer")}, "default_department"
		)
		if first_department:
			query = query.where(MWO.department == first_department)

	query = (
		query.where(
			(MWO[searchfield].like(f"%{txt}%"))
			| (MWO.company.like(f"%{txt}%"))
			| (MWO.customer.like(f"%{txt}%"))
		)
		.orderby(Case().when(Locate(txt, MWO.name) > 0, Locate(txt, MWO.name)).else_(99999))
		.orderby(Case().when(Locate(txt, MWO.company) > 0, Locate(txt, MWO.company)).else_(99999))
		.orderby(Case().when(Locate(txt, MWO.customer) > 0, Locate(txt, MWO.customer)).else_(99999))
		.orderby(MWO.idx, order=frappe.qb.desc)
		.orderby(MWO.name)
		.orderby(MWO.company)
		.orderby(MWO.customer)
		.limit(page_len)
		.offset(start)
	)
	mwo_data = query.run(as_dict=True)

	return mwo_data
