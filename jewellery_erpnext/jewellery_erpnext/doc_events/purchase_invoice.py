import frappe
from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import (
	PurchaseInvoice as ERPNextPurchaseInvoice,
)
from frappe.utils import flt


class CustomPurchaseInvoice(ERPNextPurchaseInvoice):
	def validate(self):
		pass


def before_validate(self):
	update_expense_account(self)


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


def update_expense_account(self):
	if self.is_opening == "No":
		expense_account = frappe.db.get_value(
			"Account",
			{"company": self.company, "custom_purchase_type": self.purchase_type},
			"name",
		)
		if expense_account:
			for row in self.items:
				row.expense_account = expense_account


def update_effective_tax_rate(self, method=None):
	# pass
	if not self.net_total:
		return

	for tax in self.taxes:
		if tax.charge_type == "On Net Total" and tax.tax_amount:
			tax.rate = flt(tax.tax_amount / self.net_total * 100, tax.precision("rate"))
