import frappe
from frappe import _


def validate_po(self):
	allow_quotation = frappe.db.get_value(
		"Company", self.company, "custom_allow_quotation_from_po_only"
	)
	hallmarking_charge = (
		frappe.db.get_value("Customer Certification Price", self.party_name, "hallmarking_amount") or 0
	)
	po_data = frappe._dict()
	for row in self.items:
		update_customer_details(self, row)
		update_hallmarking_amount(hallmarking_charge, row)
		if allow_quotation and not row.po_no:
			frappe.throw(
				_("Row {0} : Quotation can be created from Purchase Order for this Company").format(row.idx)
			)
		elif row.po_no:
			if not po_data.get(row.po_no):
				po_data[row.po_no] = frappe.db.get_value("Purchase Order", row.po_no, "custom_quotation")
			if not po_data.get(row.po_no):
				frappe.db.set_value("Purchase Order", row.po_no, "custom_quotation", self.name)


def update_customer_details(self, row):
	if not row.custom_customer_gold:
		row.custom_customer_gold = self.custom_customer_gold
	if not row.custom_customer_diamond:
		row.custom_customer_diamond = self.custom_customer_diamond
	if not row.custom_customer_stone:
		row.custom_customer_stone = self.custom_customer_stone
	if not row.custom_customer_good:
		row.custom_customer_good = self.custom_customer_good
	if not row.custom_customer_finding:
		row.custom_customer_finding = self.custom_customer_finding


def update_hallmarking_amount(hallmarking_charge, row):
	row.custom_hallmarking_amount = hallmarking_charge * row.qty
