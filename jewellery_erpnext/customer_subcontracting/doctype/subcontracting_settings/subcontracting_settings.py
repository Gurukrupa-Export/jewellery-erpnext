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

#: The Customer Gold rate basis is per gram, so the receipt item must be stocked in grams.
RECEIPT_STOCK_UOM = "Gram"

#: Account types that require a Party on every GL Entry (``gl_entry.py:139-152``).
PARTY_ACCOUNT_TYPES = ("Receivable", "Payable")

#: Rejected by ``StockEntry.validate_difference_account`` as a difference account.
STOCK_ACCOUNT_TYPE = "Stock"

VALUATION_POLICY_FIELD = "customer_gold_valuation_policy"

#: Book the metal at 0 and post no GL. What every existing site does today, and what
#: ``test_entered_metal_rate.py:95-104`` asserts.
VALUATION_ZERO = "Zero Value"

#: Book the frozen per-gram rate as stock value with an equal Customer Gold Liability -- the
#: supplied SOP's model. Changes an accounting policy; see D01 in docs-customer-gold/DECISIONS.md.
VALUATION_NOMINAL = "Nominal"


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
		["disabled", "is_stock_item", "has_batch_no", "stock_uom"],
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

	if item.stock_uom != RECEIPT_STOCK_UOM:
		# Load-bearing, not cosmetic. ``customer_gold_rate.convert_gold_rate_to_per_gram``
		# turns a "Per 10 Gram" quote into a per-gram rate by dividing by 10, and
		# ``apply_valuation_policy`` then books that figure as ``basic_rate`` -- which
		# erpnext labels "as per Stock UOM". If the item's stock UOM is not grams, the
		# booked value is wrong by the conversion factor and nothing downstream would
		# notice. Not a tautology either: on this bench 67 metal items are Gram and 2
		# are Nos.
		frappe.throw(
			_(
				"Customer 24KT Item {0} has Stock UOM {1}, but the Customer Gold rate basis is per {2}."
			).format(
				frappe.bold(doc.customer_24kt_item),
				frappe.bold(item.stock_uom or _("not set")),
				frappe.bold(RECEIPT_STOCK_UOM),
			),
			title=_("Unsupported Stock UOM"),
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
		"Account",
		account,
		["company", "root_type", "is_group", "disabled", "account_type"],
		as_dict=True,
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

	if details.disabled:
		# erpnext itself never checks ``disabled`` in ``gl_entry.validate_account_details``,
		# so without this a disabled ledger is accepted here and only surfaces as a failed
		# posting later. Deliberately going beyond core.
		frappe.throw(
			_("Row #{0}: {1} {2} is disabled.").format(
				idx, label, frappe.bold(account)
			),
			title=_("Account Disabled"),
		)

	# These two are not style preferences -- each is a posting that would fail, and the
	# Customer Gold receipt now uses this account as the Stock Entry contra
	# (``customer_gold_receipt.apply_valuation_policy``), so a bad choice breaks a receipt
	# rather than a report.
	if details.account_type in PARTY_ACCOUNT_TYPES:
		# ``gl_entry.py:139-152`` requires a party against a Receivable/Payable account, and
		# a Stock Entry supplies none -- "Supplier is required against Payable account".
		frappe.throw(
			_(
				"Row #{0}: {1} {2} is a {3} account, which requires a Party on every entry. "
				"Customer Gold postings do not carry one."
			).format(
				idx, label, frappe.bold(account), frappe.bold(details.account_type)
			),
			title=_("Unsupported Account Type"),
		)

	if details.account_type == STOCK_ACCOUNT_TYPE:
		# ``StockEntry.validate_difference_account`` (``stock_entry.py:925-932``) hard-throws
		# when the difference account is of type Stock.
		frappe.throw(
			_(
				"Row #{0}: {1} {2} is a Stock account and cannot be used as the Customer Gold contra account."
			).format(idx, label, frappe.bold(account)),
			title=_("Unsupported Account Type"),
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


def has_settings_capability(fieldname):
	"""True when ``Subcontracting Settings`` exists AND actually carries ``fieldname``.

	A NARROW CAPABILITY CHECK, NOT AN EXCEPTION CATCH -- and the distinction is the whole point.

	The previous implementation called ``get_single_value`` and caught
	``(InvalidColumnName, DoesNotExistError)``. That did not work, and the reason is an upstream
	Frappe defect rather than a misreading. ``frappe/database/database.py:917-921`` reads::

	    frappe.throw(
	        _("Field {0} does not exist on {1}").format(
	            frappe.bold(fieldname), frappe.bold(doctype), self.InvalidColumnName
	        )
	    )

	``self.InvalidColumnName`` is the THIRD positional argument to ``str.format()``. The format
	string has only ``{0}`` and ``{1}``, so the class is silently discarded and ``frappe.throw``
	falls back to its default, plain ``frappe.ValidationError``. ``InvalidColumnName`` is a
	SUBCLASS of ``ValidationError`` (``database.py:101``), and ``except SubClass`` cannot catch a
	raised superclass -- so the missing-FIELD case was never caught. Only the missing-DOCTYPE case
	was. Reproduced on ``gk`` and ``alfarsi``.

	Widening the catch to ``ValidationError`` is explicitly rejected by the recovery
	specification (§4.3): it would swallow every unrelated business validation raised anywhere
	beneath this call. So the question is asked BEFORE the call instead of rescued after it.

	``frappe.db.exists("DocType", ...)`` never throws for an absent DocType. ``Meta.has_field``
	is an in-memory dict lookup over ``_fields``, which ``Meta.process`` populates from DocFields
	AND Custom Fields -- so it sees this app's provisioned fields, which ``frappe.db.field_exists``
	(DocField-only) would miss. ``get_meta`` is cache-first, so on a warm cache this costs no
	query at all; the old path paid a real ``tabSingles`` round-trip before throwing.
	"""
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return False

	try:
		return bool(frappe.get_meta(SETTINGS_DOCTYPE).has_field(fieldname))
	except frappe.DoesNotExistError:
		# Narrow, and only for the race where the DocType row vanishes between the two calls.
		return False


def is_customer_gold_enabled():
	"""True when the master switch is on (default OFF).

	Reads the value rather than a cached doc so the latest committed configuration is always
	seen, matching ``jewellery_erpnext.stock_recon_window``.

	Fails CLOSED on a site whose schema has not caught up -- the flag also ships as 0, so such a
	site keeps exactly its previous behaviour. That matters well beyond this function:
	``_pure_qty_excluded_types`` calls it from ``doc_events.stock_entry.before_validate``, so a
	raise here would abort **every Stock Entry save carrying an M or F row**, not merely the
	customer-gold ones.

	The readiness question is asked through ``has_settings_capability`` rather than by catching
	what ``get_single_value`` raises; see that function for why the old catch could not work.
	"""
	if not has_settings_capability(ENABLE_FLAG):
		return False

	return bool(frappe.db.get_single_value(SETTINGS_DOCTYPE, ENABLE_FLAG))


def get_customer_gold_valuation_policy():
	"""Which valuation model a Customer Gold receipt should use.

	Defaults to ``VALUATION_ZERO`` and falls back to it on a site whose schema has not caught up,
	for the same reason ``is_customer_gold_enabled`` fails closed: zero-value is what every site
	does today, so a site that cannot answer the question keeps its current behaviour. Choosing
	nominal by accident would silently start booking stock value and posting to the general
	ledger.

	D01 is the open decision about whether nominal is the approved policy at all. This function
	reports the configured setting; it does not decide the policy.
	"""
	if not has_settings_capability(VALUATION_POLICY_FIELD):
		return VALUATION_ZERO

	policy = frappe.db.get_single_value(SETTINGS_DOCTYPE, VALUATION_POLICY_FIELD)
	return VALUATION_NOMINAL if policy == VALUATION_NOMINAL else VALUATION_ZERO


def is_nominal_valuation():
	"""True only when the configured policy is explicitly Nominal."""
	return get_customer_gold_valuation_policy() == VALUATION_NOMINAL


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
