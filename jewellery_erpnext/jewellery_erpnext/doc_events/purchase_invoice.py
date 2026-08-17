import frappe
from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice as ERPNextPurchaseInvoice
from frappe.utils import cint , flt

class CustomPurchaseInvoice(ERPNextPurchaseInvoice):
    def validate(self):
        pass


def before_validate(doc, method=None):
	update_expense_account(doc)
	update_rate_from_sales_invoice(doc)


def update_rate_from_sales_invoice(doc):
	"""
	Fetch the rate from the Sales Invoice corresponding to the Purchase Order.
	"""
	
	for row in doc.items:
		if row.purchase_order:
			# Get any Sales Order Item where po_no = purchase_order and custom_design_code is set
			so_item = frappe.db.sql("""
				SELECT parent
				FROM `tabSales Order Item`
				WHERE po_no = %s AND custom_design_code IS NOT NULL AND custom_design_code != ''
				LIMIT 1
			""", (row.purchase_order,), as_dict=True)
			
			if so_item:
				so_name = so_item[0].parent
				
				# Find the corresponding Sales Invoice Item rate for this specific row
				si_rate = frappe.db.sql("""
					SELECT sii.rate 
					FROM `tabSales Invoice Item` sii
					JOIN `tabSales Invoice` si ON si.name = sii.parent
					JOIN `tabSales Order Item` soi ON soi.name = sii.so_detail
					WHERE sii.sales_order = %s 
					  AND si.docstatus < 2
					  AND (soi.custom_po_details = %s OR soi.custom_design_code = %s)
					ORDER BY si.creation DESC
					LIMIT 1
				""", (so_name, row.po_detail, row.item_code))
				# frappe.throw(str(si_rate))
				if si_rate and si_rate[0][0] is not None:
					row.rate = frappe.utils.flt(si_rate[0][0])
					row.amount = frappe.utils.flt(row.qty) * row.rate


def update_expense_account(doc):
	if doc.is_opening == "No":
		expense_account = frappe.db.get_value(
			"Account", {"company": doc.company, "custom_purchase_type": doc.purchase_type}, "name"
		)
		if expense_account:
			for row in doc.items:
				row.expense_account = expense_account

def update_effective_tax_rate(self, method=None):
	# pass
	if not self.net_total:
		return

	for tax in self.taxes:
		if tax.charge_type == "On Net Total" and tax.tax_amount:
			tax.rate = flt(tax.tax_amount / self.net_total * 100, tax.precision("rate"))
