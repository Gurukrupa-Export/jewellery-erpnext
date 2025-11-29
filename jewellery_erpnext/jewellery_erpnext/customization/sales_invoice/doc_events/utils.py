from datetime import datetime, timedelta
from frappe.utils import get_link_to_form
import frappe
from frappe import _


def validate_item_category_for_customer(self):
	customer = (
		self.party_name
		if self.doctype == "Quotation" and self.quotation_to == "Customer"
		else self.customer
	)

	allowed_category = frappe.db.get_value(
		"Customer", customer, "custom_allowed_item_category_for_invoice"
	)

	if allowed_category == "Mix":
		return

	separate_category = []
	if allowed_category == "Separate Selection":
		ict = frappe.qb.DocType("Item Category Multiselect")
		separate_category = (
			frappe.qb.from_(ict).select(ict.item_category).distinct().where(ict.parent == self.customer)
		).run(as_list=1, pluck=True)

	invoice_category = list({row.custom_item_sub_category for row in self.items})

	if allowed_category == "Unique":
		if len(invoice_category) != 1:
			frappe.throw(_("Not allowed Multiple Category in one invoice"))

	else:
		separate = sum(1 for row in separate_category if row in invoice_category)
		non_separate = len(separate_category) - separate

		if (separate > 1 and non_separate > 0) or (separate > 1):
			frappe.throw(_("Not allowed Multiple Category in one invoice"))


def create_branch_po(self):
	if self.sales_type != "Branch Sales":
		return
	branch = frappe.db.get_value("Branch", {"custom_customer": self.customer})
	if not branch:
		frappe.throw(_("Branch not available for selected customer"))

	branch_supplier = frappe.db.get_value("Branch", branch, "custom_supplier")

	if not branch_supplier:
		frappe.throw(_("Select supplier in Branch for PO generation."))

	po = create_po(self, branch, branch_supplier)

	frappe.msgprint(_("{0} generated as Branch PO").format(get_link_to_form("Purchase Order", po)))


def create_po(self, branch, branch_supplier):
	doc = frappe.new_doc("Purchase Order")
	doc.supplier = branch_supplier
	doc.company = self.company
	doc.branch = branch
	doc.custom_branch = branch
	doc.purchase_type = "Branch Purchase"
	format = "%Y-%m-%d"
	doc.schedule_date = (datetime.strptime(self.posting_date, format) + timedelta(days=1)).date()

	warehouse = frappe.db.get_value("Company", doc.company, "custom_default_purchase_warehouse")

	for row in self.items:
		doc.append(
			"items", {"item_code": row.item_code, "qty": row.qty, "rate": row.rate, "warehouse": warehouse}
		)

	doc.save()
	# doc.submit()

	return doc.name
