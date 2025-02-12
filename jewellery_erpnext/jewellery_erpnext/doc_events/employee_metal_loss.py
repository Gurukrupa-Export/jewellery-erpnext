import json

import frappe
from frappe import _


@frappe.whitelist()
def update_metal_loss(data):
	dates = json.loads(data)
	if not dates.get("from_date") or not dates.get("to_date"):
		return frappe.throw(_("Please Specify From Date and To Date"))

	emp_list = frappe.db.get_list("Employee", {}, "name")

	for oper in emp_list:

		EML = frappe.qb.DocType("Employee Metal Loss")
		query = (
			frappe.qb.from_(EML)
			.select(EML.name)
			.where(
				(EML.employee == oper.name)
				& (
					(EML.date_from.between(dates.get("from_date"), dates.get("to_date")))
					| (EML.date_to.between(dates.get("from_date"), dates.get("to_date")))
				)
			)
		)
		data = query.run()

		if data:
			frappe.msgprint(_("Record Already Exists Within Date Range"))
		else:
			ml_doc = frappe.new_doc("Employee Metal Loss")
			ml_doc.employee = oper.get("name")
			ml_doc.date_from = dates.get("from_date")
			ml_doc.date_to = dates.get("to_date")
			ml_doc.save()
			if ml_doc.total_metal_loss == 0:
				frappe.delete_doc("Employee Metal Loss", ml_doc.name)
