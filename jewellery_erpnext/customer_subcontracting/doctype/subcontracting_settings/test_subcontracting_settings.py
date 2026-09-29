# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold block on Subcontracting Settings.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, docs are
``frappe._dict`` fakes and every DB read is patched. Nothing is written. That includes the
read-only ``audit_customer_gold_items_removal`` patch, whose ``frappe.db`` is a fake.
"""

import json
import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.customer_goods_eligibility import (
	CUSTOMER_GOODS_FLAG,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	ENABLE_FLAG,
	RECEIPT_ITEM_FIELDS,
	SETTINGS_DOCTYPE,
	_validate_receipt_item,
	get_customer_gold_receipt_type,
	is_customer_gold_enabled,
	validate_customer_gold_settings,
)
from jewellery_erpnext.patches.audit_customer_gold_items_removal import (
	MATCH,
	MISMATCH,
	MISSING,
	collect,
	format_report,
)

MOD = "jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings"
AUDIT = "jewellery_erpnext.patches.audit_customer_gold_items_removal"

ITEM = "M-G-24KT-99.9-Y"
SE_TYPE = "Customer Goods Received"
RETURN_SE_TYPE = "Customer Goods Issue"
#: Not batch controlled -- a row the retired Additional Customer Gold Items table held and the
#: old validator refused. Kept to prove that table no longer gates a Settings save.
SECOND_ITEM_NO_BATCH = "M-G-24KT-99.5-N"
#: A second purity on the old list; flagged on kg-gk, so the audit's expected MATCH.
LEGACY_ITEM = "M-G-22KT-91.75-Y"
#: A stone. Never read from the Item master here -- the receipt-item gates get a prefetched dict.
STONE_ITEM = "D-TEST-CARAT"
#: A real type on this bench with the WRONG purpose for a return -- not an invented name, so the
#: test fails the way a misconfiguration actually would.
TRANSFER_SE_TYPE = "Material Transfer"
COMPANY_A = "Company A"
COMPANY_B = "Company B"
LIAB_A = "Customer Gold Liability - A"
COGS_A = "Customer Gold COGS Adjustment - A"


def _row(idx, company=COMPANY_A, liability=LIAB_A, cogs=COGS_A):
	return frappe._dict(
		idx=idx,
		company=company,
		customer_gold_liability_account=liability,
		customer_gold_cogs_adjustment_account=cogs,
	)


def _settings(**overrides):
	doc = frappe._dict(
		enable_customer_gold_flow=1,
		customer_24kt_item=ITEM,
		customer_goods_stock_entry_type=SE_TYPE,
		gold_rate_source="Jain Jewels",
		gold_rate_field="live_rate",
		gold_rate_unit="Per 10 Gram",
		company_accounts=[_row(1)],
	)
	doc.update(overrides)
	return doc


def _item(**overrides):
	"""An Item row as ``validate_customer_gold_receipt_config`` now reads it.

	Every field the validator asks for must be present. ``frappe._dict`` returns ``None``
	for a missing key rather than raising, so an absent field would not error -- it would
	silently fail whichever check reads it, and the negative tests below would still pass
	for the wrong reason. That is why this builder exists instead of inline dicts.
	"""
	row = frappe._dict(disabled=0, is_stock_item=1, has_batch_no=1, stock_uom="Gram")
	row.update(overrides)
	return row


def _account(**overrides):
	"""An Account row as ``_validate_account`` now reads it. Same reasoning as ``_item``."""
	row = frappe._dict(
		company=COMPANY_A,
		root_type="Liability",
		is_group=0,
		disabled=0,
		account_type="",
	)
	row.update(overrides)
	return row


_ACCOUNTS = {
	LIAB_A: _account(),
	COGS_A: _account(root_type="Expense"),
	"Group Liability - A": _account(is_group=1),
	"Expense - A": _account(root_type="Expense"),
	"Liability - B": _account(company=COMPANY_B),
	"Disabled Liability - A": _account(disabled=1),
	"Payable - A": _account(account_type="Payable"),
	"Receivable - A": _account(account_type="Receivable"),
	"Stock Type - A": _account(account_type="Stock"),
	#: The KGJPL shape behind KGJPL-JE-JE-26-00018: a Liability chosen as the adjustment account.
	"Advances from Customers - A": _account(),
	"Group Expense - A": _account(root_type="Expense", is_group=1),
	"Disabled Expense - A": _account(root_type="Expense", disabled=1),
	"Expense - B": _account(root_type="Expense", company=COMPANY_B),
	"Income - A": _account(root_type="Income"),
}


def _db_get_value(doctype, name, fieldname=None, as_dict=False):
	"""Stand-in for frappe.db.get_value covering the three masters read by validate."""
	if doctype == "Item":
		return {
			ITEM: _item(),
			SECOND_ITEM_NO_BATCH: _item(has_batch_no=0),
		}.get(name)
	if doctype == "Stock Entry Type":
		return {
			SE_TYPE: "Material Receipt",
			RETURN_SE_TYPE: "Material Issue",
			TRANSFER_SE_TYPE: "Material Transfer",
		}.get(name)
	if doctype == "Account":
		return _ACCOUNTS.get(name)
	return None


@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
class TestCustomerGoldSettings(IntegrationTestCase):
	"""Configuration guards for the Customer Gold block."""

	@classmethod
	def setUpClass(cls):
		pass

	def _blocks(self, fragment):
		"""Assert a ValidationError whose message contains ``fragment``.

		A bare ``assertRaises(frappe.ValidationError)`` is not falsifiable here. Every check
		in this validator raises the same class, so adding a new check that fires EARLIER
		would keep all of these green while silently testing something else. Each case now
		names the message it is actually about.
		"""
		case = self

		class _Ctx:
			def __enter__(self):
				self.ctx = case.assertRaises(frappe.ValidationError)
				self.raised = self.ctx.__enter__()
				return self.raised

			def __exit__(self, exc_type, exc, tb):
				handled = self.ctx.__exit__(exc_type, exc, tb)
				if handled:
					message = str(self.raised.exception)
					case.assertIn(
						fragment, message, f"blocked for the wrong reason: {message!r}"
					)
				return handled

		return _Ctx()

	def test_correct_setup_passes(self, _mock):
		validate_customer_gold_settings(_settings())

	def test_disabled_allows_incomplete_configuration(self, _mock):
		doc = _settings(
			enable_customer_gold_flow=0,
			customer_24kt_item=None,
			customer_goods_stock_entry_type=None,
			gold_rate_source=None,
			gold_rate_field=None,
			gold_rate_unit=None,
			company_accounts=[],
		)
		validate_customer_gold_settings(doc)

	def test_missing_24kt_item_blocks(self, _mock):
		with self._blocks("Customer 24KT Item"):
			validate_customer_gold_settings(_settings(customer_24kt_item=None))

	def test_unknown_24kt_item_blocks(self, _mock):
		with self._blocks("does not exist"):
			validate_customer_gold_settings(
				_settings(customer_24kt_item="NO-SUCH-ITEM")
			)

	def test_non_batch_item_blocks(self, _mock):
		def _no_batch(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return _item(has_batch_no=0)
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_no_batch),
			self._blocks("must be batch controlled"),
		):
			validate_customer_gold_settings(_settings())

	def test_non_stock_item_blocks(self, _mock):
		def _non_stock(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return _item(is_stock_item=0)
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_non_stock),
			self._blocks("must be a Stock Item"),
		):
			validate_customer_gold_settings(_settings())

	def test_missing_stock_entry_type_blocks(self, _mock):
		with self._blocks("Customer Goods Stock Entry Type"):
			validate_customer_gold_settings(
				_settings(customer_goods_stock_entry_type=None)
			)

	def test_stock_entry_type_with_wrong_purpose_blocks(self, _mock):
		def _wrong_purpose(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Stock Entry Type":
				return "Material Issue"
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_wrong_purpose),
			self._blocks("has purpose"),
		):
			validate_customer_gold_settings(_settings())

	def test_missing_gold_rate_source_blocks(self, _mock):
		with self._blocks("Gold Rate Source"):
			validate_customer_gold_settings(_settings(gold_rate_source=None))

	def test_missing_gold_rate_field_blocks(self, _mock):
		with self._blocks("Gold Rate Field"):
			validate_customer_gold_settings(_settings(gold_rate_field=None))

	def test_arbitrary_gold_rate_field_blocks(self, _mock):
		with self._blocks("is not a rate column"):
			validate_customer_gold_settings(_settings(gold_rate_field="__dict__"))

	def test_invalid_gold_rate_unit_blocks(self, _mock):
		with self._blocks("is not supported"):
			validate_customer_gold_settings(_settings(gold_rate_unit="Per Ounce"))

	def test_no_company_rows_blocks(self, _mock):
		with self._blocks("no Company Accounts are configured"):
			validate_customer_gold_settings(_settings(company_accounts=[]))

	def test_missing_liability_account_blocks(self, _mock):
		with self._blocks("is mandatory"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability=None)])
			)

	def test_missing_cogs_adjustment_account_blocks(self, _mock):
		with self._blocks("is mandatory"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, cogs=None)])
			)

	def test_wrong_company_account_blocks(self, _mock):
		with self._blocks("belongs to Company"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Liability - B")])
			)

	def test_liability_with_wrong_root_type_blocks(self, _mock):
		with self._blocks("must be of root type"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Expense - A")])
			)

	def test_group_liability_account_blocks(self, _mock):
		with self._blocks("is a group account"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Group Liability - A")])
			)

	def test_duplicate_company_row_blocks(self, _mock):
		with self._blocks("already exists for Company"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1), _row(2)])
			)

	# -- C02: stock UOM ------------------------------------------------------------
	def test_item_with_non_gram_stock_uom_blocks(self, _mock):
		"""The rate basis is per gram, and ``basic_rate`` is "as per Stock UOM".

		If the configured item is stocked in anything else, the booked nominal value is
		wrong by the conversion factor and nothing downstream would notice.
		"""

		def _nos(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return _item(stock_uom="Nos")
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_nos),
			self._blocks("Stock UOM"),
		):
			validate_customer_gold_settings(_settings())

	def test_item_with_blank_stock_uom_blocks(self, _mock):
		def _blank(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return _item(stock_uom=None)
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_blank),
			self._blocks("Stock UOM"),
		):
			validate_customer_gold_settings(_settings())

	def test_gram_stock_uom_passes(self, _mock):
		validate_customer_gold_settings(_settings())

	# -- C02: account preconditions --------------------------------------------------
	def test_disabled_account_blocks(self, _mock):
		"""erpnext never checks ``disabled`` in gl_entry; this goes beyond core."""
		with self._blocks("is disabled"):
			validate_customer_gold_settings(
				_settings(
					company_accounts=[_row(1, liability="Disabled Liability - A")]
				)
			)

	def test_payable_account_blocks(self, _mock):
		"""A Receivable/Payable account requires a Party on every GL Entry.

		A Stock Entry supplies none, so the posting would throw "Supplier is required
		against Payable account" -- at submit, long after the settings were saved.
		"""
		with self._blocks("requires a Party"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Payable - A")])
			)

	def test_receivable_account_blocks(self, _mock):
		with self._blocks("requires a Party"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Receivable - A")])
			)

	def test_stock_type_account_blocks(self, _mock):
		"""``StockEntry.validate_difference_account`` hard-throws on account_type Stock."""
		with self._blocks("Stock account"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Stock Type - A")])
			)

	def test_ordinary_liability_account_still_passes(self, _mock):
		"""Guard the guards -- the normal configuration must remain valid."""
		validate_customer_gold_settings(_settings())

	def test_an_expense_adjustment_account_passes(self, _mock):
		validate_customer_gold_settings(
			_settings(company_accounts=[_row(1, cogs=COGS_A)])
		)

	def test_an_income_adjustment_account_is_left_to_finance(self, _mock):
		"""Only Liability is certainly wrong. Expense vs Income is Finance's call, not pinned here."""
		validate_customer_gold_settings(
			_settings(company_accounts=[_row(1, cogs="Income - A")])
		)

	# ------------------------------------------------ F4: the adjustment account's own rules
	def test_a_liability_adjustment_account_blocks(self, _mock):
		"""F4. KGJPL-JE-JE-26-00018 posted Dr Customer Goods Receive / Cr Advances from
		Customers -- two liabilities. The obligation moved to a party-less advance and nothing
		was discharged. Every earlier check passed it: the adjustment account had no root-type rule.
		"""
		with self._blocks("would move the obligation"):
			validate_customer_gold_settings(
				_settings(
					company_accounts=[
						_row(1, liability=LIAB_A, cogs="Advances from Customers - A")
					]
				)
			)

	def test_a_group_adjustment_account_blocks(self, _mock):
		with self._blocks("is a group account"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, cogs="Group Expense - A")])
			)

	def test_a_disabled_adjustment_account_blocks(self, _mock):
		with self._blocks("is disabled"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, cogs="Disabled Expense - A")])
			)

	def test_another_companys_adjustment_account_blocks(self, _mock):
		with self._blocks("belongs to Company"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, cogs="Expense - B")])
			)

	def test_a_nonexistent_adjustment_account_blocks(self, _mock):
		with self._blocks("does not exist"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, cogs="No Such Account - A")])
			)

	def test_posting_time_messages_name_the_document_not_a_row(self, _mock):
		"""The same rules run at posting, where there is no settings row to point at."""
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			validate_settlement_accounts,
		)

		with self._blocks("Delivery Note DN-1:"):
			validate_settlement_accounts(
				COMPANY_A,
				LIAB_A,
				"Advances from Customers - A",
				where="Delivery Note DN-1",
			)

	# ------------------------------------------------------- the two legs must differ
	def test_identical_liability_and_cogs_accounts_block(self, _mock):
		"""A real production configuration, and every other check passes it.

		Found on the live KGJPL settings: both fields set to one account. It cleared the
		liability gate because that account IS root type Liability, and cleared the COGS gate
		because that one has no root-type rule. The settlement it would produce is
		``Dr X / Cr X`` -- balanced, accepted by erpnext, and worth nothing.
		"""
		with self._blocks("are both set to"):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability=LIAB_A, cogs=LIAB_A)])
			)

	def test_an_identical_expense_pair_blocks_on_root_type_first(self, _mock):
		"""Pins the ORDER of the two rules, because it decides what the user is told.

		The root-type gate runs before the sameness check, so an identical pair of EXPENSE
		accounts never reaches the sameness rule -- it is rejected for not being a liability.
		Which means the only identical pair that can reach the sameness check is a Liability
		one, and that is precisely the real-world case above.

		Asserted rather than assumed: the first draft of this test expected the sameness
		message here and was wrong. The message a user sees when they misconfigure is worth
		knowing exactly.
		"""
		with self._blocks("root type"):
			validate_customer_gold_settings(
				_settings(
					company_accounts=[
						_row(1, liability="Expense - A", cogs="Expense - A")
					]
				)
			)

	def test_distinct_accounts_still_pass(self, _mock):
		"""Guard the guard. The normal two-account configuration must stay valid."""
		validate_customer_gold_settings(
			_settings(company_accounts=[_row(1, liability=LIAB_A, cogs=COGS_A)])
		)

	# ------------------------------------------------------- the return entry type
	def test_return_stock_entry_type_with_wrong_purpose_blocks(self, _mock):
		"""The receipt type had this check in two places; the return type had it in none."""
		with self._blocks("requires"):
			validate_customer_gold_settings(
				_settings(customer_gold_return_stock_entry_type=TRANSFER_SE_TYPE)
			)

	def test_missing_return_stock_entry_type_blocks(self, _mock):
		with self._blocks("does not exist"):
			validate_customer_gold_settings(
				_settings(customer_gold_return_stock_entry_type="No Such Type")
			)

	def test_material_issue_return_type_passes(self, _mock):
		validate_customer_gold_settings(
			_settings(customer_gold_return_stock_entry_type=RETURN_SE_TYPE)
		)

	# ----------------------------- the retired Additional Customer Gold Items table
	def test_a_legacy_customer_gold_items_key_is_ignored(self, get_value):
		"""The retired table no longer gates a Settings save, and none of its rows is read.

		Both rows would have failed the old validator: the first is not batch controlled, the
		second repeats the Customer 24KT Item. A doc from a site that has not migrated yet can
		still carry the key. Eligibility is the Item's own flag now, so the only Item read is
		the anchor's.
		"""
		validate_customer_gold_settings(
			_settings(
				customer_gold_items=[
					frappe._dict(idx=1, item=SECOND_ITEM_NO_BATCH),
					frappe._dict(idx=2, item=ITEM),
				]
			)
		)
		item_reads = [
			c.args[1] for c in get_value.call_args_list if c.args[0] == "Item"
		]
		self.assertEqual(item_reads, [ITEM])

	def test_the_anchor_needs_no_customer_goods_flag(self, get_value):
		"""Saving Settings never asks for the anchor's own flag. That flag is audit-only, by decision.

		The Customer 24KT Item is the rate reference now, not an eligibility list. A site whose
		anchor is unflagged (prod's 24KT item on the day of the deploy) must still be able to
		save its Settings. Receipts of that item are refused at the receipt until the flag is
		ticked. ``_item()`` carries no flag key at all; an explicit 0 must pass as well.
		"""
		validate_customer_gold_settings(_settings())
		for call in get_value.call_args_list:
			self.assertNotIn(CUSTOMER_GOODS_FLAG, str(call))

		def _unflagged(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return _item(**{CUSTOMER_GOODS_FLAG: 0})
			return _db_get_value(doctype, name, fieldname, as_dict)

		with patch(f"{MOD}.frappe.db.get_value", side_effect=_unflagged):
			validate_customer_gold_settings(_settings())

	# ------------------------------------------------ the shared receipt-item gates
	def test_the_gates_read_the_item_when_none_is_prefetched(self, get_value):
		"""The Settings-save path: one read of exactly the fields the gates check."""
		_validate_receipt_item(ITEM, "Customer 24KT Item")
		get_value.assert_called_once_with(
			"Item", ITEM, list(RECEIPT_ITEM_FIELDS), as_dict=True
		)

	def test_a_carat_stone_passes_without_the_gram_gate(self, get_value):
		"""A stone is received in its own UOM at a typed rate, so the per-gram rule does not apply.

		The receipt hands every row its prefetched Item dict, so nothing is read here.
		"""
		_validate_receipt_item(
			STONE_ITEM,
			"Row #1: Item",
			item=_item(stock_uom="Carat"),
			require_gram=False,
		)
		get_value.assert_not_called()

	def test_a_carat_item_priced_from_the_gold_rate_blocks(self, get_value):
		"""Gold and findings are priced per gram, so the same Carat item is refused on that path."""
		with self._blocks("Stock UOM"):
			_validate_receipt_item(
				ITEM, "Row #1: Item", item=_item(stock_uom="Carat"), require_gram=True
			)
		get_value.assert_not_called()

	def test_a_stone_is_still_held_to_the_batch_gate(self, get_value):
		"""``require_gram=False`` lifts only the UOM rule. Custody is per batch for stones too."""
		with self._blocks(
			"must be batch controlled, because customer goods are tracked per batch"
		):
			_validate_receipt_item(
				STONE_ITEM,
				"Row #1: Item",
				item=_item(stock_uom="Carat", has_batch_no=0),
				require_gram=False,
			)
		get_value.assert_not_called()

	def test_an_empty_prefetched_item_does_not_exist(self, get_value):
		"""The receipt passes ``frappe._dict()`` for a code its one query did not find.

		That must read as "does not exist". It must not fall back to a per-row fetch: only
		``None`` means "not prefetched".
		"""
		with self._blocks("does not exist"):
			_validate_receipt_item("NO-SUCH-ITEM", "Row #1: Item", item=frappe._dict())
		get_value.assert_not_called()

	def test_a_blank_return_type_is_allowed(self, _mock):
		"""A site that never returns customer gold does not have to configure returns.

		This is the case the new check must NOT break: ``_is_customer_gold_return`` already
		answers "not a return" for an unconfigured site rather than raising, so blank is a
		supported state. Only a configured-but-wrong value is an error.
		"""
		validate_customer_gold_settings(
			_settings(customer_gold_return_stock_entry_type=None)
		)


class TestCustomerGoldFlagFailsClosed(IntegrationTestCase):
	"""``is_customer_gold_enabled`` must never abort an unrelated Stock Entry.

	It is called from ``_pure_qty_excluded_types``, which
	``doc_events.stock_entry.before_validate`` reaches on every save carrying an M or
	F row. A raise here does not merely disable customer gold — it blocks ordinary
	company stock movements, on every site.

	The docstring has always promised tolerance of "a site whose doctype has not been
	reloaded yet", but the guard caught only ``InvalidColumnName``.
	``get_single_value`` resolves the field through ``frappe.get_meta``, which raises
	``DoesNotExistError`` when the DocType row itself is absent — a different class,
	which fell straight through.
	"""

	@classmethod
	def setUpClass(cls):
		# ERPNext's global bootstrap cannot run on a bench carrying gke_customization.
		pass

	def test_a_missing_doctype_fails_closed(self):
		"""No DocType -> False, and the Singles read is never attempted.

		Rewritten. The previous version did
		``patch.object(frappe.db, "get_single_value", side_effect=frappe.DoesNotExistError(...))``
		-- it hand-injected the exception class the code named, so it could only ever confirm
		that ``except`` catches what it says it catches. It proved nothing about what
		``get_single_value`` actually raises, which is the entire substance of the defect.

		The guard now ASKS before calling, so the assertion is the stronger one: the readiness
		check fails and ``get_single_value`` is never reached at all.
		"""
		with patch.object(
			frappe.db, "exists", return_value=None
		) as exists, patch.object(frappe.db, "get_single_value") as get_single_value:
			self.assertFalse(is_customer_gold_enabled())

		exists.assert_called_with("DocType", SETTINGS_DOCTYPE)
		get_single_value.assert_not_called()

	def test_a_missing_field_fails_closed(self):
		"""DocType present but the field absent -> False, still without a Singles read.

		This is the case the old guard genuinely let through, and the reason is an upstream
		Frappe defect rather than a misreading: ``database.py:917-921`` passes
		``self.InvalidColumnName`` as the THIRD positional argument to ``str.format()`` on a
		format string with only ``{0}`` and ``{1}``, so the class is discarded and
		``frappe.throw`` raises its default ``frappe.ValidationError``. ``InvalidColumnName`` is
		a SUBCLASS of that, and ``except SubClass`` cannot catch a raised superclass -- so the
		old ``except (InvalidColumnName, DoesNotExistError)`` never fired for a missing field.
		Reproduced on ``gk`` and ``alfarsi``.
		"""
		meta = MagicMock()
		meta.has_field.return_value = False

		with patch.object(
			frappe.db, "exists", return_value=SETTINGS_DOCTYPE
		), patch.object(frappe, "get_meta", return_value=meta), patch.object(
			frappe.db, "get_single_value"
		) as get_single_value:
			self.assertFalse(is_customer_gold_enabled())

		meta.has_field.assert_called_with(ENABLE_FLAG)
		get_single_value.assert_not_called()

	def test_the_flag_is_read_only_once_the_field_is_present(self):
		"""The positive control: when capability holds, the value IS read."""
		meta = MagicMock()
		meta.has_field.return_value = True

		with patch.object(
			frappe.db, "exists", return_value=SETTINGS_DOCTYPE
		), patch.object(frappe, "get_meta", return_value=meta), patch.object(
			frappe.db, "get_single_value", return_value=1
		) as get_single_value:
			self.assertTrue(is_customer_gold_enabled())

		get_single_value.assert_called_once_with(SETTINGS_DOCTYPE, ENABLE_FLAG)

	def test_an_unrelated_error_is_not_swallowed(self):
		"""Failing closed is for a site that has not migrated, not for real faults.

		Swallowing everything here would hide a genuine database problem behind a
		silently disabled feature.
		"""
		with patch.object(
			frappe.db, "get_single_value", side_effect=ValueError("something real")
		):
			with self.assertRaises(ValueError):
				is_customer_gold_enabled()

	def test_a_set_flag_reads_true(self):
		with patch.object(frappe.db, "get_single_value", return_value=1):
			self.assertTrue(is_customer_gold_enabled())

	def test_an_unset_flag_reads_false(self):
		for value in (0, None, ""):
			with patch.object(frappe.db, "get_single_value", return_value=value):
				self.assertFalse(is_customer_gold_enabled())


class TestCustomerGoldItemsFieldIsRetired(IntegrationTestCase):
	"""The Settings DocType no longer carries the Additional Customer Gold Items table.

	Read from the JSON on disk rather than the site's meta, so it holds before a migrate as well.
	The ``Customer Gold Item`` child DocType itself is kept for this release on purpose.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _schema(self):
		path = os.path.join(
			os.path.dirname(os.path.abspath(__file__)), "subcontracting_settings.json"
		)
		with open(path) as f:
			return json.load(f)

	def test_the_doctype_has_no_customer_gold_items_field(self):
		schema = self._schema()
		fieldnames = [field.get("fieldname") for field in schema["fields"]]

		self.assertNotIn("customer_gold_items", fieldnames)
		self.assertNotIn("customer_gold_items", schema["field_order"])
		self.assertEqual(
			[
				f["fieldname"]
				for f in schema["fields"]
				if f.get("options") == "Customer Gold Item"
			],
			[],
		)
		# Guard the guard: the rate reference stays, and the two lists still agree.
		self.assertIn("customer_24kt_item", fieldnames)
		self.assertEqual(set(schema["field_order"]), set(fieldnames))


class TestCustomerGoldReceiptType(IntegrationTestCase):
	"""``get_customer_gold_receipt_type`` -- the one answer the Stock Entry form and batch minting share.

	``None`` means "the flow is off". The form then keeps its ordinary item picker, and batch
	minting falls back to the 24KT token alone. That is exactly how gk and alfarsi behave today.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _call(self, enabled, settings):
		with (
			patch(f"{MOD}.is_customer_gold_enabled", return_value=enabled),
			patch(
				f"{MOD}.get_customer_gold_settings", return_value=settings
			) as get_settings,
		):
			return get_customer_gold_receipt_type(), get_settings

	def test_an_enabled_flow_returns_the_configured_type(self):
		receipt_type, _get_settings = self._call(True, _settings())
		self.assertEqual(receipt_type, SE_TYPE)

	def test_a_disabled_flow_returns_none_even_with_a_type_configured(self):
		"""A switched-off site keeps its old type in Settings; it must not revive the filter."""
		receipt_type, get_settings = self._call(False, _settings())
		self.assertIsNone(receipt_type)
		get_settings.assert_not_called()

	def test_a_blank_type_returns_none_not_an_empty_string(self):
		receipt_type, _get_settings = self._call(
			True, _settings(customer_goods_stock_entry_type="")
		)
		self.assertIsNone(receipt_type)

	def test_it_is_whitelisted_for_the_form_but_not_for_guests(self):
		"""The Stock Entry form calls it; the Single is readable by System Managers alone."""
		self.assertIn(get_customer_gold_receipt_type, frappe.whitelisted)
		self.assertNotIn(get_customer_gold_receipt_type, frappe.guest_methods)


def _audit_db(
	anchor=ITEM,
	legacy=(),
	flags=None,
	table_exists=True,
	flag_exists=True,
	new_eligible=(),
):
	"""A fake ``frappe.db`` for the audit, answering each of its SELECTs by the query text.

	``flags`` maps item code -> flag value; a code absent from it is absent from ``tabItem``.
	Legacy rows are served even when ``table_exists`` is False, so a guard that stopped
	checking would show up as an extra row rather than pass on an empty answer. An unknown
	query raises, so a new statement cannot slip past the read-only assertion unseen.
	"""
	flags = {} if flags is None else flags
	db = MagicMock()
	db.table_exists.return_value = table_exists
	db.has_column.return_value = flag_exists
	db.escape.side_effect = lambda value: f"'{value}'"

	def _sql(query, values=None, as_dict=False):
		if "`tabSingles`" in query:
			return [(anchor,)] if anchor else []
		if "`tabCustomer Gold Item`" in query:
			return [(item,) for item in legacy]
		if "GROUP BY" in query:
			return [frappe._dict(row) for row in new_eligible]
		if "FROM `tabItem` WHERE `name` IN" in query:
			return [(code, flags[code]) for code in values[0] if code in flags]
		raise AssertionError(f"unexpected audit query: {query}")

	db.sql.side_effect = _sql
	return db


class TestCustomerGoldItemsAudit(IntegrationTestCase):
	"""``patches.audit_customer_gold_items_removal.collect`` -- report the difference, change nothing.

	The old code accepted the Customer 24KT Item whatever its flag said, plus every listed
	purity. The audit compares that with the Item flag, so each MISMATCH is known before
	someone meets it at a receipt. On prod the anchor is expected to be the MISMATCH.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _collect(self, **db_kwargs):
		db = _audit_db(**db_kwargs)
		with patch(f"{AUDIT}.frappe.db", new=db):
			report = collect()
		return report, db

	def _row(self, report, item):
		return next(row for row in report.rows if row.item == item)

	def _notices(self, lines, prefix):
		"""The report lines that open with ``prefix`` -- "ACTION:" or "NOTE:"."""
		return [line for line in lines if line.lstrip().startswith(prefix)]

	def _group_by(self, db):
		"""The new-eligible statement, whitespace-collapsed, with the values it was sent."""
		(call,) = [c for c in db.sql.call_args_list if "GROUP BY" in c.args[0]]
		return " ".join(call.args[0].split()), call.args[1]

	def test_without_the_legacy_table_only_the_anchor_is_compared(self):
		report, db = self._collect(
			legacy=(LEGACY_ITEM,), flags={ITEM: 1, LEGACY_ITEM: 1}, table_exists=False
		)

		self.assertFalse(report.table_exists)
		self.assertEqual([row.item for row in report.rows], [ITEM])
		queries = [call.args[0] for call in db.sql.call_args_list]
		self.assertFalse([q for q in queries if "Customer Gold Item" in q])

	def test_a_flagged_legacy_row_matches(self):
		report, _db = self._collect(
			legacy=(LEGACY_ITEM,), flags={ITEM: 1, LEGACY_ITEM: 1}
		)

		row = self._row(report, LEGACY_ITEM)
		self.assertEqual(
			(row.role, row.flag, row.result),
			("Additional Customer Gold Item", 1, MATCH),
		)

	def test_an_unflagged_anchor_is_a_mismatch(self):
		"""The expected prod finding: the old code accepted the anchor unconditionally."""
		report, _db = self._collect(flags={ITEM: 0})

		row = self._row(report, ITEM)
		self.assertEqual(
			(row.role, row.flag, row.result), ("Customer 24KT Item", 0, MISMATCH)
		)

	def test_a_legacy_item_missing_from_the_item_master(self):
		report, _db = self._collect(legacy=("NO-SUCH-ITEM",), flags={ITEM: 1})

		row = self._row(report, "NO-SUCH-ITEM")
		self.assertEqual((row.flag, row.result), (None, "ITEM MISSING"))

	def test_a_legacy_row_repeating_the_anchor_is_listed_once(self):
		report, _db = self._collect(
			legacy=(ITEM, LEGACY_ITEM), flags={ITEM: 1, LEGACY_ITEM: 1}
		)

		self.assertEqual(
			[(row.item, row.role) for row in report.rows],
			[
				(ITEM, "Customer 24KT Item"),
				(LEGACY_ITEM, "Additional Customer Gold Item"),
			],
		)

	def test_without_the_flag_column_rows_are_reported_without_flags(self):
		"""No Item field means nothing is eligible, and the column must not be queried at all."""
		report, db = self._collect(legacy=(LEGACY_ITEM,), flag_exists=False)

		self.assertFalse(report.flag_exists)
		self.assertEqual([row.item for row in report.rows], [ITEM, LEGACY_ITEM])
		self.assertEqual({row.flag for row in report.rows}, {None})
		self.assertEqual({row.result for row in report.rows}, {MISMATCH})
		self.assertEqual(report.new_eligible, [])
		queries = [call.args[0] for call in db.sql.call_args_list]
		self.assertFalse([q for q in queries if CUSTOMER_GOODS_FLAG in q])
		self.assertIn("WARNING", "\n".join(format_report(report)))

	def test_new_eligible_items_exclude_the_old_list(self):
		"""The group-by counts only flagged items the old list never named."""
		eligible = [
			{"item_group": "Diamond", "is_gold": 0, "passes_gates": 1, "items": 5}
		]
		report, db = self._collect(
			legacy=(LEGACY_ITEM,),
			flags={ITEM: 1, LEGACY_ITEM: 1},
			new_eligible=eligible,
		)

		self.assertEqual(report.new_eligible, eligible)
		query, values = self._group_by(db)
		self.assertEqual(values, ("Gram", [ITEM, LEGACY_ITEM]))
		# The fake answers any GROUP BY, so the filter itself is pinned on the statement text.
		self.assertIn(f"WHERE `{CUSTOMER_GOODS_FLAG}` = 1", query)
		self.assertIn("AND `name` NOT IN %s", query)
		self.assertEqual(query.count("%s"), len(values))

	def test_new_eligible_without_an_old_list_excludes_nothing(self):
		"""No anchor and no legacy rows: no ``name`` exclusion, and no empty ``IN ()`` sent.

		The statement always carries ``NOT IN ('M', 'F')`` for the gold gate, so the
		exclusion is looked for as ``name NOT IN %s``, not as a bare NOT IN.
		"""
		report, db = self._collect(anchor=None, new_eligible=[])

		self.assertEqual(report.rows, [])
		query, values = self._group_by(db)
		self.assertEqual(values, ("Gram",))
		self.assertIn(f"WHERE `{CUSTOMER_GOODS_FLAG}` = 1", query)
		self.assertNotIn("`name` NOT IN", query)
		self.assertNotIn("NOT IN %s", query)
		self.assertEqual(query.count("%s"), len(values))

	def test_the_audit_only_reads(self):
		"""Every statement is a SELECT and no write API is touched, on the fullest path."""
		report, db = self._collect(
			legacy=(LEGACY_ITEM, "NO-SUCH-ITEM"),
			flags={ITEM: 0, LEGACY_ITEM: 1},
			new_eligible=[
				{"item_group": "Diamond", "is_gold": 0, "passes_gates": 1, "items": 5}
			],
		)

		# Four statements: anchor, legacy rows, flags, new-eligible. Not vacuously "all SELECT".
		self.assertEqual(db.sql.call_count, 4)
		for call in db.sql.call_args_list:
			self.assertTrue(
				call.args[0].strip().upper().startswith("SELECT"), call.args[0]
			)
		for write in (
			"set_value",
			"set_single_value",
			"delete",
			"insert",
			"sql_ddl",
			"commit",
		):
			getattr(db, write).assert_not_called()
		self.assertLessEqual(
			{name for name, _args, _kwargs in db.method_calls},
			{"table_exists", "has_column", "sql", "escape"},
		)
		self.assertEqual(len(report.rows), 3)

	def test_the_report_names_the_mismatch_and_asks_for_action(self):
		report, _db = self._collect(
			legacy=(LEGACY_ITEM,),
			flags={ITEM: 0, LEGACY_ITEM: 1},
			new_eligible=[
				{"item_group": "Diamond", "is_gold": 0, "passes_gates": 1, "items": 5},
				{"item_group": "Metal", "is_gold": 1, "passes_gates": 0, "items": 2},
			],
		)
		lines = format_report(report)
		text = "\n".join(lines)

		self.assertIn("OLD SETTING", text)
		(anchor_line,) = [line for line in lines if ITEM in line]
		self.assertIn("MISMATCH", anchor_line)
		self.assertIn("[Customer 24KT Item]", anchor_line)
		# One ACTION, naming the Item checkbox by the label a person sees on the form.
		(action,) = self._notices(lines, "ACTION:")
		self.assertIn("MISMATCH items", action)
		self.assertIn(
			"'Inventory Type Can be Customer Goods' is ticked on the Item", action
		)
		self.assertEqual(self._notices(lines, "NOTE:"), [])
		self.assertIn(
			"5 pass every receipt gate, 2 are flagged but blocked by a gate", text
		)

	def test_an_item_missing_only_report_notes_it_and_asks_for_nothing(self):
		"""A dead old entry has no Item to tick, so it gets the NOTE and not the ACTION."""
		report, _db = self._collect(anchor=None, legacy=("NO-SUCH-ITEM",), flags={})
		self.assertEqual([row.result for row in report.rows], [MISSING])
		lines = format_report(report)

		(note,) = self._notices(lines, "NOTE:")
		self.assertIn("ITEM MISSING rows name an item that no longer exists", note)
		self.assertEqual(self._notices(lines, "ACTION:"), [])
		self.assertNotIn("is ticked on the Item", "\n".join(lines))

	def test_a_mismatch_and_a_missing_item_get_both_lines(self):
		"""The two notices are independent: one kind of row must not suppress the other's."""
		report, _db = self._collect(legacy=("NO-SUCH-ITEM",), flags={ITEM: 0})
		self.assertEqual([row.result for row in report.rows], [MISMATCH, MISSING])
		lines = format_report(report)

		self.assertEqual(len(self._notices(lines, "ACTION:")), 1)
		self.assertEqual(len(self._notices(lines, "NOTE:")), 1)

	def test_a_clean_audit_asks_for_nothing(self):
		"""Guard the guard: with every old item flagged there is neither ACTION nor NOTE."""
		report, _db = self._collect(
			legacy=(LEGACY_ITEM,), flags={ITEM: 1, LEGACY_ITEM: 1}
		)
		self.assertEqual({row.result for row in report.rows}, {MATCH})
		lines = format_report(report)
		text = "\n".join(lines)

		self.assertIn("MATCH", text)
		self.assertNotIn("MISMATCH", text)
		self.assertEqual(self._notices(lines, "ACTION:"), [])
		self.assertEqual(self._notices(lines, "NOTE:"), [])
		self.assertNotIn("ACTION", text)
		self.assertNotIn("NOTE", text)
