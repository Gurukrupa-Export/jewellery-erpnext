# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold block on Subcontracting Settings.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, docs are
``frappe._dict`` fakes and every DB read is patched. Nothing is written.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	ENABLE_FLAG,
	SETTINGS_DOCTYPE,
	get_allowed_customer_gold_items,
	is_customer_gold_enabled,
	validate_customer_gold_settings,
)

MOD = "jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings"

ITEM = "M-G-24KT-99.9-Y"
SE_TYPE = "Customer Goods Received"
RETURN_SE_TYPE = "Customer Goods Issue"
#: A second purity a customer may hand over, alongside the 99.9 primary.
SECOND_ITEM = "M-G-24KT-99.5-Y"
#: Same, but not batch controlled -- the negative case for the shared item gates.
SECOND_ITEM_NO_BATCH = "M-G-24KT-99.5-N"
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
}


def _db_get_value(doctype, name, fieldname=None, as_dict=False):
	"""Stand-in for frappe.db.get_value covering the three masters read by validate."""
	if doctype == "Item":
		return {
			ITEM: _item(),
			SECOND_ITEM: _item(),
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

	def test_cogs_account_root_type_is_not_enforced(self, _mock):
		"""Classification is pending Finance approval, so any non-group company account passes."""
		validate_customer_gold_settings(
			_settings(company_accounts=[_row(1, cogs=COGS_A)])
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

	# ------------------------------------------------- more than one customer purity
	def test_additional_items_are_held_to_the_same_standard(self, _mock):
		"""An extra purity that is not batch controlled breaks custody the same way.

		The point of extracting ``_validate_receipt_item`` was that the secondary list must not
		become the lenient one. This is what pins that.
		"""
		with self._blocks("must be batch controlled"):
			validate_customer_gold_settings(
				_settings(
					customer_gold_items=[
						frappe._dict(idx=1, item=SECOND_ITEM_NO_BATCH)
					]
				)
			)

	def test_an_additional_item_names_its_own_row_in_the_error(self, _mock):
		"""With several items configured, "Customer 24KT Item is disabled" points at the wrong one."""
		with self._blocks("Row #1"):
			validate_customer_gold_settings(
				_settings(
					customer_gold_items=[
						frappe._dict(idx=1, item=SECOND_ITEM_NO_BATCH)
					]
				)
			)

	def test_a_second_purity_passes(self, _mock):
		validate_customer_gold_settings(
			_settings(customer_gold_items=[frappe._dict(idx=1, item=SECOND_ITEM)])
		)

	def test_repeating_the_primary_item_blocks(self, _mock):
		"""Listing the 24KT item again is a configuration mistake, not a no-op."""
		with self._blocks("already accepted"):
			validate_customer_gold_settings(
				_settings(customer_gold_items=[frappe._dict(idx=1, item=ITEM)])
			)

	def test_a_duplicated_additional_item_blocks(self, _mock):
		with self._blocks("already accepted"):
			validate_customer_gold_settings(
				_settings(
					customer_gold_items=[
						frappe._dict(idx=1, item=SECOND_ITEM),
						frappe._dict(idx=2, item=SECOND_ITEM),
					]
				)
			)

	def test_no_additional_items_is_still_valid(self, _mock):
		"""Every existing site. The list is empty and only the 24KT item is accepted."""
		validate_customer_gold_settings(_settings(customer_gold_items=[]))

	def test_the_allowed_list_puts_the_primary_item_first(self, _mock):
		"""Order is load-bearing: the primary item is what the Gold Rate is quoted against."""
		self.assertEqual(
			get_allowed_customer_gold_items(
				_settings(customer_gold_items=[frappe._dict(idx=1, item=SECOND_ITEM)])
			),
			[ITEM, SECOND_ITEM],
		)

	def test_the_allowed_list_is_just_the_primary_when_unconfigured(self, _mock):
		self.assertEqual(get_allowed_customer_gold_items(_settings()), [ITEM])

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
