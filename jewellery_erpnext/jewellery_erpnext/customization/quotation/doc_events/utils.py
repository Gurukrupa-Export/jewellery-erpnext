import frappe
from frappe import _


def validate_po(self):
	allow_quotation = frappe.db.get_value(
		"Company", self.company, "custom_allow_quotation_from_po_only"
	)
	for row in self.items:
		if allow_quotation and not row.po_no:
			frappe.throw(
				_("Row {0} : Quotation can be created from Purchase Order for this Company").format(row.idx)
			)
		elif row.po_no:
			if not frappe.db.get_value("Purchase Order", row.po_no, "custom_quotation"):
				frappe.db.set_value("Purchase Order", row.po_no, "custom_quotation", self.name)
