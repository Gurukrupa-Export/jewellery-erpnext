# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Subcontracting Settings, including the Customer Gold configuration.

The Customer Gold block lives here rather than on a new Single because this doctype
already owns customer-gold policy (the repack-days table) and already sits in the
``Customer Subcontracting`` module. Adding a sibling Single would repeat the existing
``Jewellery Settings`` / ``Manufacturing Setting`` duplication.

Every check is gated on ``enable_customer_gold_flow`` so that a site can migrate and
save an incomplete configuration while the feature is off. Nothing here changes stock
valuation or GL posting; it is configuration and validation only.

Custody warehouse mapping is deliberately NOT modelled yet -- it is scheduled with the
custody-warehouse work and can be added as a further section without breaking this
schema.
"""

import frappe
from frappe import _
from frappe.model.document import Document

SETTINGS_DOCTYPE = "Subcontracting Settings"
ENABLE_FLAG = "enable_customer_gold_flow"

GOLD_RATE_FIELDS = ("live_rate", "9_am", "3_pm", "11_pm")
GOLD_RATE_UNITS = ("Per Gram", "Per 10 Gram")

RECEIPT_PURPOSE = "Material Receipt"


class SubcontractingSettings(Document):
	def validate(self):
		validate_customer_gold_settings(self)


def validate_customer_gold_settings(doc):
	"""Validate the Customer Gold block. A no-op while the feature is disabled."""
	if not doc.get(ENABLE_FLAG):
		return

	validate_customer_gold_receipt_config(doc)
	validate_customer_gold_rate_config(doc)
	validate_customer_gold_accounts(doc)


def validate_customer_gold_receipt_config(doc):
	if not doc.get("customer_24kt_item"):
		frappe.throw(
			_("Customer Gold Flow is enabled, but {0} is not configured.").format(
				frappe.bold(_("Customer 24KT Item"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)

	item = frappe.db.get_value(
		"Item",
		doc.customer_24kt_item,
		["disabled", "is_stock_item", "has_batch_no"],
		as_dict=True,
	)
	if not item:
		frappe.throw(
			_("Customer 24KT Item {0} does not exist.").format(
				frappe.bold(doc.customer_24kt_item)
			)
		)
	if item.disabled:
		frappe.throw(
			_("Customer 24KT Item {0} is disabled.").format(
				frappe.bold(doc.customer_24kt_item)
			)
		)
	if not item.is_stock_item:
		frappe.throw(
			_("Customer 24KT Item {0} must be a Stock Item.").format(
				frappe.bold(doc.customer_24kt_item)
			)
		)
	if not item.has_batch_no:
		frappe.throw(
			_(
				"Customer 24KT Item {0} must be batch controlled, because customer gold is tracked per batch."
			).format(frappe.bold(doc.customer_24kt_item))
		)

	if not doc.get("customer_goods_stock_entry_type"):
		frappe.throw(
			_("Customer Gold Flow is enabled, but {0} is not configured.").format(
				frappe.bold(_("Customer Goods Stock Entry Type"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)

	purpose = frappe.db.get_value(
		"Stock Entry Type", doc.customer_goods_stock_entry_type, "purpose"
	)
	if not purpose:
		frappe.throw(
			_("Stock Entry Type {0} does not exist.").format(
				frappe.bold(doc.customer_goods_stock_entry_type)
			)
		)
	if purpose != RECEIPT_PURPOSE:
		frappe.throw(
			_(
				"Customer Goods Stock Entry Type {0} has purpose {1}, but a Customer Gold receipt requires {2}."
			).format(
				frappe.bold(doc.customer_goods_stock_entry_type),
				frappe.bold(purpose),
				frappe.bold(RECEIPT_PURPOSE),
			)
		)


def validate_customer_gold_rate_config(doc):
	if not doc.get("gold_rate_source"):
		frappe.throw(
			_("Customer Gold Flow is enabled, but {0} is not configured.").format(
				frappe.bold(_("Gold Rate Source"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)

	if not doc.get("gold_rate_field"):
		frappe.throw(
			_("Customer Gold Flow is enabled, but {0} is not configured.").format(
				frappe.bold(_("Gold Rate Field"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)
	if doc.gold_rate_field not in GOLD_RATE_FIELDS:
		frappe.throw(
			_(
				"Gold Rate Field {0} is not a rate column on Gold Rates branchs. Allowed: {1}."
			).format(frappe.bold(doc.gold_rate_field), ", ".join(GOLD_RATE_FIELDS))
		)

	if not doc.get("gold_rate_unit"):
		frappe.throw(
			_("Customer Gold Flow is enabled, but {0} is not configured.").format(
				frappe.bold(_("Gold Rate Unit"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)
	if doc.gold_rate_unit not in GOLD_RATE_UNITS:
		frappe.throw(
			_("Gold Rate Unit {0} is not supported. Allowed: {1}.").format(
				frappe.bold(doc.gold_rate_unit), ", ".join(GOLD_RATE_UNITS)
			)
		)


def validate_customer_gold_accounts(doc):
	rows = doc.get("company_accounts") or []
	if not rows:
		frappe.throw(
			_(
				"Customer Gold Flow is enabled, but no Company Accounts are configured. Add one row per company that receives customer gold."
			),
			title=_("Customer Gold Configuration Incomplete"),
		)

	seen = {}
	for row in rows:
		if not row.company:
			frappe.throw(_("Row #{0}: Company is mandatory.").format(row.idx))

		first_idx = seen.get(row.company)
		if first_idx:
			frappe.throw(
				_(
					"Row #{0}: Customer Gold account configuration already exists for Company {1} in Row #{2}."
				).format(row.idx, frappe.bold(row.company), first_idx),
				title=_("Duplicate Company"),
			)
		seen[row.company] = row.idx

		_validate_account(
			row.customer_gold_liability_account,
			row.company,
			row.idx,
			_("Customer Gold Liability Account"),
			expected_root_type="Liability",
		)
		# Root type is deliberately not enforced for the COGS Adjustment account --
		# its classification is pending Finance approval. Company / non-group / existence
		# are still enforced so it cannot point at another company's ledger.
		_validate_account(
			row.customer_gold_cogs_adjustment_account,
			row.company,
			row.idx,
			_("Customer Gold COGS Adjustment Account"),
		)


def _validate_account(account, company, idx, label, expected_root_type=None):
	if not account:
		frappe.throw(
			_("Row #{0}: {1} is mandatory when Customer Gold Flow is enabled.").format(
				idx, frappe.bold(label)
			),
			title=_("Customer Gold Configuration Incomplete"),
		)

	details = frappe.db.get_value(
		"Account", account, ["company", "root_type", "is_group"], as_dict=True
	)
	if not details:
		frappe.throw(
			_("Row #{0}: Account {1} does not exist.").format(idx, frappe.bold(account))
		)

	if details.is_group:
		frappe.throw(
			_("Row #{0}: {1} {2} is a group account. Select a ledger account.").format(
				idx, label, frappe.bold(account)
			)
		)

	if details.company != company:
		frappe.throw(
			_(
				"Row #{0}: {1} {2} belongs to Company {3}, but the Customer Gold configuration is for Company {4}."
			).format(
				idx,
				label,
				frappe.bold(account),
				frappe.bold(details.company),
				frappe.bold(company),
			)
		)

	if expected_root_type and details.root_type != expected_root_type:
		frappe.throw(
			_(
				"Row #{0}: {1} {2} is a {3} account, but it must be of root type {4}."
			).format(
				idx,
				label,
				frappe.bold(account),
				frappe.bold(details.root_type),
				frappe.bold(expected_root_type),
			)
		)


def is_customer_gold_enabled():
	"""True when the master switch is on (default OFF).

	Reads the value rather than a cached doc so the latest committed configuration is
	always seen, matching ``jewellery_erpnext.stock_recon_window``. The field ships with
	the app doctype, so it reaches a site through ``bench migrate``; the guard below
	still tolerates a site whose doctype has not been reloaded yet.
	"""
	try:
		return bool(frappe.db.get_single_value(SETTINGS_DOCTYPE, ENABLE_FLAG))
	except frappe.db.InvalidColumnName:
		# A site whose doctype has not been reloaded yet has no such column, and
		# get_single_value raises rather than returning None. Fail CLOSED -- the flag
		# also ships as 0, so such a site keeps exactly its previous behaviour.
		return False


def get_customer_gold_settings():
	"""Return the Customer Gold configuration once per request."""
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def get_customer_gold_company_settings(company):
	"""Return the configured accounts for ``company``.

	Throws when the feature is enabled but the company has no row, so callers never
	silently post to the wrong ledger.
	"""
	settings = get_customer_gold_settings()
	for row in settings.get("company_accounts") or []:
		if row.company == company:
			return frappe._dict(
				liability_account=row.customer_gold_liability_account,
				cogs_adjustment_account=row.customer_gold_cogs_adjustment_account,
			)

	frappe.throw(
		_(
			"No Customer Gold account configuration found for Company {0}. Please configure it in {1}."
		).format(frappe.bold(company), frappe.bold(_(SETTINGS_DOCTYPE))),
		title=_("Customer Gold Configuration Missing"),
	)
