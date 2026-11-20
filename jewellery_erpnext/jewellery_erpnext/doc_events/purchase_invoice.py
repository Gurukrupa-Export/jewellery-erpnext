import frappe
from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import (
	PurchaseInvoice as ERPNextPurchaseInvoice,
)
from frappe.utils import flt


class CustomPurchaseInvoice(ERPNextPurchaseInvoice):
	def validate(self):
		pass


def before_validate(doc, method=None):
	"""Job-work invoices only: rows billed against a Purchase Order raised by an Employee IR
	(KGGK subcontracting) take KGGK's Sales Invoice amount and the purchase-type expense
	account. Every other Purchase Invoice is left exactly as before."""
	if doc.docstatus != 0 or doc.is_return:
		return
	job_work_pos = _get_job_work_purchase_orders(doc)
	if not job_work_pos:
		return
	update_expense_account(doc)
	update_rate_from_sales_invoice(doc, job_work_pos)


def validate(self, method=None):
	sync_tax_row_rate_with_item(self)


def sync_tax_row_rate_with_item(self):
	# core ERPNext leaves the Purchase Taxes and Charges row's `rate` as
	# whatever the Purchase Taxes and Charges Template set (or 0, if the
	# row was auto-added for an item tax template account head) -- the
	# actual tax_amount is always computed from each item's own Item Tax
	# Template regardless of this field, so the row's displayed rate can
	# be misleading. Overwrite it with the (first) item's real rate so
	# the row reflects what is actually being charged.
	if not self.get("items") or not self.get("taxes"):
		return

	first_item = self.items[0]
	item_tax_template = first_item.get("item_tax_template")
	if not item_tax_template:
		return

	# Read the item's real Item Tax Template directly instead of
	# first_item.item_tax_rate: that field is computed by core ERPNext's
	# update_item_tax_map(), which silently backfills any account head the
	# template doesn't define with whatever rate the tax row already had
	# (i.e. the Purchase Taxes and Charges Template default) -- so an
	# account head can appear "resolved" there while actually still just
	# carrying the stale PTCT value (this is exactly what happens for RCM
	# heads, since Item Tax Templates only define the plain, non-RCM heads).
	template_rates = {
		row.tax_type: row.tax_rate
		for row in frappe.get_all(
			"Item Tax Template Detail",
			filters={"parent": item_tax_template},
			fields=["tax_type", "tax_rate"],
		)
	}
	if not template_rates:
		return

	resolved_rates = {}
	for tax in self.taxes:
		if tax.account_head in template_rates:
			tax.rate = flt(template_rates[tax.account_head], tax.precision("rate"))
			resolved_rates[tax.account_head] = tax.rate

	# RCM (Reverse Charge) account heads (e.g. "Input Tax IGST RCM - KGJPL")
	# have no entry of their own in the item's Item Tax Template -- only the
	# plain "Input Tax IGST - KGJPL" head does. By GST rules the RCM rate is
	# always the same as the corresponding normal rate, so mirror it instead
	# of leaving the row on the Purchase Taxes and Charges Template default.
	for tax in self.taxes:
		if tax.account_head in template_rates:
			continue
		base_account_head = _get_rcm_base_account_head(tax.account_head)
		if base_account_head and base_account_head in resolved_rates:
			tax.rate = resolved_rates[base_account_head]


def _get_rcm_base_account_head(account_head):
	if not account_head or " - " not in account_head:
		return None

	account_name, abbr = account_head.rsplit(" - ", 1)
	if not account_name.upper().endswith(" RCM"):
		return None

	return f"{account_name[: -len(' RCM')]} - {abbr}"


def _get_job_work_purchase_orders(doc):
	po_names = {row.purchase_order for row in doc.items if row.get("purchase_order")}
	if not po_names:
		return set()
	return set(
		frappe.get_all(
			"Purchase Order",
			filters={"name": ["in", list(po_names)], "employee_ir": ["is", "set"]},
			pluck="name",
		)
	)


def update_rate_from_sales_invoice(doc, job_work_pos):
	"""Bill each job-work row at the amount KGGK invoiced for it.

	The chain is one-to-one per Purchase Order row: make_quotation stamps the PO row name on
	the Quotation row as ``custom_po_details``, it rides to the Subcontracting Income Sales
	Order row, and that row is invoiced once. The row's *amount* is taken (not its rate)
	because the PO row is qty 1 per operation while the Sales Order row carries the PMO qty.
	Only submitted Sales Invoices count, so a KGGK draft never changes what Gurukrupa pays.
	"""
	for row in doc.items:
		if row.get("purchase_order") not in job_work_pos or not row.get("po_detail"):
			continue
		si_amount = frappe.db.sql(
			"""
			SELECT sii.amount
			FROM `tabSales Invoice Item` sii
			JOIN `tabSales Invoice` si ON si.name = sii.parent
			JOIN `tabSales Order Item` soi ON soi.name = sii.so_detail
			WHERE soi.custom_po_details = %s
				AND soi.item_code = 'Subcontracting Income'
				AND si.docstatus = 1
				AND si.is_return = 0
			ORDER BY si.posting_date DESC, si.creation DESC
			LIMIT 1
			""",
			(row.po_detail,),
		)
		if si_amount and flt(row.qty):
			row.rate = flt(flt(si_amount[0][0]) / flt(row.qty), row.precision("rate"))
			row.amount = flt(row.rate * flt(row.qty), row.precision("amount"))


def update_expense_account(doc):
	if doc.is_opening == "No":
		expense_account = frappe.db.get_value(
			"Account",
			{"company": doc.company, "custom_purchase_type": doc.purchase_type},
			"name",
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
