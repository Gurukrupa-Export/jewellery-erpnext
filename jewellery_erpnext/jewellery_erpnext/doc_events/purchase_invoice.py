import frappe
from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import (
	PurchaseInvoice as ERPNextPurchaseInvoice,
)
from frappe.utils import flt


class CustomPurchaseInvoice(ERPNextPurchaseInvoice):
	def validate(self):
		pass


def before_validate(self, method=None):
	update_expense_account(self)
	assign_zero_tax_template_for_untaxed_items(self)


def validate(self, method=None):
	update_effective_tax_rate(self)


ZERO_TAX_TEMPLATE_TITLE = "Zero Tax (Auto)"


def assign_zero_tax_template_for_untaxed_items(self):
	# Each item should be taxed independently at its own Item Tax Template
	# rate. Core ERPNext's per-item tax engine (get_current_tax_and_net_amount)
	# already does this correctly when an item has a template -- but an item
	# with NO item_tax_template doesn't fall back to 0: core's get_item_tax_map()
	# seeds every account head from the Purchase Taxes and Charges row's own
	# `rate`, so a template-less item would silently get taxed at that shared
	# rate instead of being left untaxed. Give it an explicit zero-rated Item
	# Tax Template (one per company, auto-provisioned and grown as needed) so
	# core's own tested engine treats it as genuinely not applicable -- this
	# avoids hand-computing tax_amount/grand_total/rounding ourselves.
	if not self.get("items") or not self.get("taxes"):
		return

	account_heads = sorted({tax.account_head for tax in self.taxes if tax.account_head})
	if not account_heads:
		return

	untaxed_items = [
		item
		for item in self.items
		if item.get("item_code") and not item.get("item_tax_template")
	]
	if not untaxed_items:
		return

	zero_tax_template = get_or_create_zero_tax_template(self.company, account_heads)
	for item in untaxed_items:
		item.item_tax_template = zero_tax_template


def get_or_create_zero_tax_template(company, account_heads):
	name = frappe.db.get_value(
		"Item Tax Template",
		{"company": company, "title": ZERO_TAX_TEMPLATE_TITLE},
		"name",
	)
	template = (
		frappe.get_doc("Item Tax Template", name)
		if name
		else frappe.new_doc("Item Tax Template")
	)
	if not name:
		template.title = ZERO_TAX_TEMPLATE_TITLE
		template.company = company

	# India Compliance's Item Tax Template validate hook throws "GST Rate
	# cannot be zero for Taxable GST Treatment" unless gst_treatment is
	# explicitly non-Taxable. "Non-GST" is used (rather than "Nil-Rated")
	# because these items never had any tax info asserted for them, and it
	# keeps them out of the Nil-Rated/Exempted GSTR-3B bucket.
	needs_treatment_fix = template.get("gst_treatment") != "Non-GST"
	if needs_treatment_fix:
		template.gst_treatment = "Non-GST"

	existing_heads = {row.tax_type for row in template.taxes}
	missing_heads = [head for head in account_heads if head not in existing_heads]
	if not missing_heads and not needs_treatment_fix and name:
		return template.name

	for account_head in missing_heads:
		template.append(
			"taxes", {"tax_type": account_head, "tax_rate": 0, "not_applicable": 1}
		)

	template.flags.ignore_permissions = True
	template.save()
	return template.name


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
	# Each item is now taxed at its own Item Tax Template rate (see
	# assign_zero_tax_template_for_untaxed_items), so a single row's `rate`
	# no longer reflects one flat percentage -- display the row's real
	# blended rate (tax_amount / net_total) instead of the stale Purchase
	# Taxes and Charges Template default.
	if not self.get("taxes") or not self.net_total:
		return

	for tax in self.taxes:
		if tax.charge_type == "On Net Total":
			tax.rate = flt(
				flt(tax.tax_amount) / self.net_total * 100, tax.precision("rate")
			)
