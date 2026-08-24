import frappe
from frappe import _


def create_branch_so(self):
	if self.items[0].get("custom_customer_approval"):
		return

	central_branch = frappe.db.get_value(
		"Branch", {"custom_is_central_branch": 1}, "name"
	)
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
	# The mirror SO bills from the central branch to the originating branch (now
	# the customer), so the address pair must flip too - otherwise both ends keep
	# pointing at the originating branch's own address and India Compliance sees
	# an identical company/party GSTIN.
	central_branch_address = frappe.db.get_value(
		"Branch", central_branch, "branch_address"
	)
	source_branch_address = frappe.db.get_value("Branch", self.branch, "branch_address")

	if not central_branch_address or not source_branch_address:
		frappe.throw(_("Branch Address- Billing is not set for one of the branches"))

	doc = frappe.copy_doc(self)
	doc.company = self.company
	doc.customer = branch_customer
	doc.branch = central_branch
	doc.sales_type = "Branch"
	doc.company_address = central_branch_address
	doc.customer_address = source_branch_address
	doc.save()
	doc.submit()

	return doc.name
