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
#: Returning custody metal takes it OUT of the warehouse, so the configured type must be an
#: issue. ``_build_return_entry`` builds a row with ``s_warehouse`` and no ``t_warehouse``; any
#: other purpose makes erpnext reject that row deep inside an ``ignore_permissions`` submit,
#: which surfaces as a framework traceback at return time instead of a readable message here.
RETURN_PURPOSE = "Material Issue"

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
	validate_customer_gold_return_config(doc)
	validate_customer_gold_rate_config(doc)
	validate_customer_gold_accounts(doc)


def validate_customer_gold_return_config(doc):
	"""Check the return type the way the receipt type is already checked.

	The receipt side gets a purpose check here AND a second one at runtime
	(``customer_gold_receipt._validate_receipt_purpose``). The return side had neither: the only
	guard anywhere was ``_build_return_entry`` throwing when the field is BLANK. A field pointing
	at a Material Transfer or Material Receipt type passed configuration cleanly and failed later,
	inside the return, as a raw framework error.

	Blank is still allowed. A site that never returns customer gold does not have to configure a
	return type, and ``_is_customer_gold_return`` already answers ``False`` rather than raising
	for that case -- "no returns configured" is a legitimate state, "returns configured wrongly"
	is not.
	"""
	configured = doc.get("customer_gold_return_stock_entry_type")
	if not configured:
		return

	purpose = frappe.db.get_value("Stock Entry Type", configured, "purpose")
	if not purpose:
		frappe.throw(
			_("Stock Entry Type {0} does not exist.").format(frappe.bold(configured)),
			title=_("Customer Gold Configuration Incomplete"),
		)
	if purpose != RETURN_PURPOSE:
		frappe.throw(
			_(
				"Customer Gold Return Stock Entry Type {0} has purpose {1}, but returning "
				"customer gold requires {2}."
			).format(
				frappe.bold(configured),
				frappe.bold(purpose),
				frappe.bold(RETURN_PURPOSE),
			),
			title=_("Invalid Stock Entry Type"),
		)


def validate_customer_gold_receipt_config(doc):
	if not doc.get("customer_24kt_item"):
		frappe.throw(
			_("Customer Gold Flow is enabled, but {0} is not configured.").format(
				frappe.bold(_("Customer 24KT Item"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)

	_validate_receipt_item(doc.customer_24kt_item, _("Customer 24KT Item"))

	# Additional purities the customer may hand over -- 99.5 alongside 99.9, say. Held to
	# EXACTLY the same standard as the primary item: an extra item that is not batch controlled
	# or not stocked in grams breaks custody tracking and rate arithmetic the same way, and
	# there is no reason for the secondary list to be the lenient one.
	seen_items = {doc.customer_24kt_item}
	for row in doc.get("customer_gold_items") or []:
		if not row.item:
			frappe.throw(
				_(
					"Row #{0}: Item is mandatory in Additional Customer Gold Items."
				).format(row.idx)
			)
		if row.item in seen_items:
			frappe.throw(
				_(
					"Row #{0}: Item {1} is already accepted -- it is either the Customer 24KT "
					"Item or a duplicate row."
				).format(row.idx, frappe.bold(row.item)),
				title=_("Duplicate Customer Gold Item"),
			)
		seen_items.add(row.item)
		_validate_receipt_item(
			row.item, _("Additional Customer Gold Item (Row #{0})").format(row.idx)
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


def _validate_receipt_item(item_code, label):
	"""Every item a customer may hand over must clear the same four gates.

	Extracted so the additional-purity rows cannot drift into a weaker standard than the
	primary item. ``label`` names which field is at fault, because with several items
	configured "Customer 24KT Item is disabled" would point at the wrong row.
	"""
	item = frappe.db.get_value(
		"Item",
		item_code,
		["disabled", "is_stock_item", "has_batch_no", "stock_uom"],
		as_dict=True,
	)
	if not item:
		frappe.throw(_("{0} {1} does not exist.").format(label, frappe.bold(item_code)))
	if item.disabled:
		frappe.throw(_("{0} {1} is disabled.").format(label, frappe.bold(item_code)))
	if not item.is_stock_item:
		frappe.throw(
			_("{0} {1} must be a Stock Item.").format(label, frappe.bold(item_code))
		)
	if not item.has_batch_no:
		frappe.throw(
			_(
				"{0} {1} must be batch controlled, because customer gold is tracked per batch."
			).format(label, frappe.bold(item_code))
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
				"{0} {1} has Stock UOM {2}, but the Customer Gold rate basis is per {3}."
			).format(
				label,
				frappe.bold(item_code),
				frappe.bold(item.stock_uom or _("not set")),
				frappe.bold(RECEIPT_STOCK_UOM),
			),
			title=_("Unsupported Stock UOM"),
		)


def get_allowed_customer_gold_items(settings=None):
	"""Every item a customer may hand over, primary first.

	The primary ``customer_24kt_item`` is always included, so a site that configures no
	additional purities behaves exactly as it did when the receipt tested one item for equality.
	Returns a list rather than a set: the primary item's position is meaningful -- it is the item
	the configured Gold Rate is quoted against, and every other purity is priced relative to it.
	"""
	settings = settings or get_customer_gold_settings()

	allowed = []
	primary = settings.get("customer_24kt_item")
	if primary:
		allowed.append(primary)
	for row in settings.get("customer_gold_items") or []:
		item = row.get("item") if isinstance(row, dict) else row.item
		if item and item not in allowed:
			allowed.append(item)
	return allowed


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

		validate_settlement_accounts(
			row.company,
			row.customer_gold_liability_account,
			row.customer_gold_cogs_adjustment_account,
			where=_("Row #{0}").format(row.idx),
		)


def validate_settlement_accounts(
	company, liability_account, cogs_adjustment_account, where
):
	"""The rules for the two settlement legs, run at settings save AND again at posting (F4).

	The settlement Journal Entry debits the liability account and credits the COGS Adjustment
	account. Both must be enabled ledger accounts of ``company``; the liability must be root type
	Liability; the adjustment must NOT be, and must not be the same account.

	WHY TWICE
	---------
	Save-time validation runs only while the feature flag is on, and a configuration can change
	after it was saved -- the KGJPL row that posted ``KGJPL-JE-JE-26-00018`` had never been
	validated at all. So ``_build_settlement_entry`` calls this again before it inserts anything.
	``where`` says which: "Row #1" on the settings form, the document being posted otherwise.

	WHY THE ADJUSTMENT ACCOUNT MAY NOT BE A LIABILITY
	-------------------------------------------------
	``KGJPL-JE-JE-26-00018`` posted Dr Customer Goods Receive / Cr Advances from Customers:
	liability to liability. It passed every earlier check because the adjustment account had no
	root-type rule. The customer's obligation did not fall; it moved to a party-less advance.
	Which root type the adjustment SHOULD be (Expense, as a COGS adjustment, or Income) is
	Finance's decision and is not pinned here. Liability is the one that is certainly wrong.
	"""
	_validate_account(
		liability_account,
		company,
		where,
		_("Customer Gold Liability Account"),
		expected_root_type="Liability",
	)
	_reject_identical_accounts(liability_account, cogs_adjustment_account, where)
	_validate_account(
		cogs_adjustment_account,
		company,
		where,
		_("Customer Gold COGS Adjustment Account"),
		forbidden_root_type="Liability",
	)


def _reject_identical_accounts(liability, cogs, where):
	"""The two settlement legs must land on different ledgers.

	``_build_settlement_entry`` debits the liability account and credits the COGS adjustment
	account. Point both at one account and the Journal Entry becomes ``Dr X / Cr X``: balanced,
	so erpnext accepts it -- its only same-account rule is PER ROW
	(``journal_entry.py:959-961``, ``if d.debit and d.credit``) and these are two separate rows.
	It inserts, submits, and moves the balance by nothing.

	WHY THIS HAS TO BLOCK AT SAVE RATHER THAN BE DETECTED LATER
	-----------------------------------------------------------
	The failure is silent AND irreversible through the normal path. ``settle_customer_gold_liability``
	stamps ``cg_settlement_voucher`` on every event it settles and only ever selects events where
	that field is unset. Once a no-op Journal Entry has claimed them, correcting the configuration
	does not re-settle anything -- the next run finds nothing to do and returns. The custody ledger
	then reads "settled" while the general ledger still carries the whole obligation, and nothing in
	the app reconciles the two. Recovery means cancelling every affected Delivery Note.

	Found on a real configuration: both fields set to ``Customer Goods Receive - KGJPL - KGJPL``.
	It passed every existing check, because the liability gate wants root type ``Liability`` --
	which that account is -- and the COGS gate has no root-type rule to fail.
	"""
	if not liability or not cogs or liability != cogs:
		return

	frappe.throw(
		_(
			"{0}: Customer Gold Liability Account and Customer Gold COGS Adjustment "
			"Account are both set to {1}. The settlement Journal Entry debits the first and "
			"credits the second, so a single account would post {2} against itself and the "
			"liability would never reduce. Configure a separate account -- typically an "
			"Expense account -- for the COGS Adjustment."
		).format(where, frappe.bold(liability), frappe.bold(liability)),
		title=_("Customer Gold Accounts Must Differ"),
	)


def _validate_account(
	account, company, where, label, expected_root_type=None, forbidden_root_type=None
):
	if not account:
		frappe.throw(
			_("{0}: {1} is mandatory when Customer Gold Flow is enabled.").format(
				where, frappe.bold(label)
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
			_("{0}: Account {1} does not exist.").format(where, frappe.bold(account))
		)

	if details.is_group:
		frappe.throw(
			_("{0}: {1} {2} is a group account. Select a ledger account.").format(
				where, label, frappe.bold(account)
			)
		)

	if details.disabled:
		# erpnext itself never checks ``disabled`` in ``gl_entry.validate_account_details``,
		# so without this a disabled ledger is accepted here and only surfaces as a failed
		# posting later. Deliberately going beyond core.
		frappe.throw(
			_("{0}: {1} {2} is disabled.").format(where, label, frappe.bold(account)),
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
				"{0}: {1} {2} is a {3} account, which requires a Party on every entry. "
				"Customer Gold postings do not carry one."
			).format(
				where, label, frappe.bold(account), frappe.bold(details.account_type)
			),
			title=_("Unsupported Account Type"),
		)

	if details.account_type == STOCK_ACCOUNT_TYPE:
		# ``StockEntry.validate_difference_account`` (``stock_entry.py:925-932``) hard-throws
		# when the difference account is of type Stock.
		frappe.throw(
			_(
				"{0}: {1} {2} is a Stock account and cannot be used as the Customer Gold contra account."
			).format(where, label, frappe.bold(account)),
			title=_("Unsupported Account Type"),
		)

	if details.company != company:
		frappe.throw(
			_(
				"{0}: {1} {2} belongs to Company {3}, but the Customer Gold configuration is for Company {4}."
			).format(
				where,
				label,
				frappe.bold(account),
				frappe.bold(details.company),
				frappe.bold(company),
			)
		)

	if expected_root_type and details.root_type != expected_root_type:
		frappe.throw(
			_("{0}: {1} {2} is a {3} account, but it must be of root type {4}.").format(
				where,
				label,
				frappe.bold(account),
				frappe.bold(details.root_type),
				frappe.bold(expected_root_type),
			)
		)

	if forbidden_root_type and details.root_type == forbidden_root_type:
		frappe.throw(
			_(
				"{0}: {1} {2} is a {3} account. The settlement credits it to release the "
				"customer's gold liability, so a {3} account would move the obligation instead "
				"of discharging it. Select an Expense (COGS adjustment) account."
			).format(
				where,
				label,
				frappe.bold(account),
				frappe.bold(details.root_type),
			),
			title=_("Invalid Customer Gold Adjustment Account"),
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
