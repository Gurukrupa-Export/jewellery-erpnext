import json

import frappe
from frappe import _


@frappe.whitelist()
def update_metal_loss(data):
	dates = json.loads(data)
	if not dates.get("from_date") or not dates.get("to_date"):
		return frappe.throw(_("Please Specify From Date and To Date"))

	oper_list = frappe.db.get_list("Operation", {}, "name")

	for oper in oper_list:
		OML = frappe.qb.DocType("Operation Metal Loss")
		query = (
			frappe.qb.from_(OML)
			.select(OML.name)
			.where(
				(OML.operation == oper.name)
				& (
					(OML.date_from.between(dates.get("from_date"), dates.get("to_date")))
					| (OML.date_to.between(dates.get("from_date"), dates.get("to_date")))
				)
			)
		)
		data = query.run()
		if data:
			frappe.msgprint(_("Record Already Exists Within Date Range"))
		else:
			ml_doc = frappe.new_doc("Operation Metal Loss")
			ml_doc.operation = oper.get("name")
			ml_doc.date_from = dates.get("from_date")
			ml_doc.date_to = dates.get("to_date")
			ml_doc.save()
			if ml_doc.total_metal_loss == 0:
				frappe.delete_doc("Operation Metal Loss", ml_doc.name)
