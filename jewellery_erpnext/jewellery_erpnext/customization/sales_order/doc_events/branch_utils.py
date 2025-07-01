from datetime import datetime, timedelta

import frappe
from frappe import _


def create_branch_so(self):
	if self.items[0].get("custom_customer_approval"):
		return

	central_branch = frappe.db.get_value("Branch", {"custom_is_central_branch":1}, "name")
	# central_branch = frappe.db.get_value("Company", self.company, "custom_central_branch")

	if not self.branch or self.branch == central_branch:
		return

	if self.branch and not central_branch:
		frappe.throw(_("Central branch is not mentioned in Company"))

	branch_customer = frappe.db.get_value("Branch", self.branch, "custom_customer")

	if not branch_customer:
		frappe.throw(_("Branch does not have any customer attached"))

	so = create_so(self, branch_customer, central_branch)

	frappe.msgprint(_("{0} has been generated as Branch SO").format(so))


def create_so(self, branch_customer, central_branch):
	doc = frappe.copy_doc(self)
	doc.company = self.company
	doc.customer = branch_customer
	doc.branch = central_branch
	doc.sales_type = "Branch"
	doc.save()
	doc.submit()

	return doc.name
