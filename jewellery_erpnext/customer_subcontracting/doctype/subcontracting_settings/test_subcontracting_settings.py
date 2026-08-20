# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold block on Subcontracting Settings.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, docs are
``frappe._dict`` fakes and every DB read is patched. Nothing is written.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	validate_customer_gold_settings,
)

MOD = "jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings"

ITEM = "M-G-24KT-99.9-Y"
SE_TYPE = "Customer Goods Received"
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


def _db_get_value(doctype, name, fieldname=None, as_dict=False):
	"""Stand-in for frappe.db.get_value covering the three masters read by validate."""
	if doctype == "Item":
		if name != ITEM:
			return None
		return frappe._dict(disabled=0, is_stock_item=1, has_batch_no=1)
	if doctype == "Stock Entry Type":
		return "Material Receipt" if name == SE_TYPE else None
	if doctype == "Account":
		if name == LIAB_A:
			return frappe._dict(company=COMPANY_A, root_type="Liability", is_group=0)
		if name == COGS_A:
			return frappe._dict(company=COMPANY_A, root_type="Expense", is_group=0)
		if name == "Group Liability - A":
			return frappe._dict(company=COMPANY_A, root_type="Liability", is_group=1)
		if name == "Expense - A":
			return frappe._dict(company=COMPANY_A, root_type="Expense", is_group=0)
		if name == "Liability - B":
			return frappe._dict(company=COMPANY_B, root_type="Liability", is_group=0)
		return None
	return None


@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
class TestCustomerGoldSettings(IntegrationTestCase):
	"""Configuration guards for the Customer Gold block."""

	@classmethod
	def setUpClass(cls):
		pass

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
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(_settings(customer_24kt_item=None))

	def test_unknown_24kt_item_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(customer_24kt_item="NO-SUCH-ITEM")
			)

	def test_non_batch_item_blocks(self, _mock):
		def _no_batch(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return frappe._dict(disabled=0, is_stock_item=1, has_batch_no=0)
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_no_batch),
			self.assertRaises(frappe.ValidationError),
		):
			validate_customer_gold_settings(_settings())

	def test_non_stock_item_blocks(self, _mock):
		def _non_stock(doctype, name, fieldname=None, as_dict=False):
			if doctype == "Item":
				return frappe._dict(disabled=0, is_stock_item=0, has_batch_no=1)
			return _db_get_value(doctype, name, fieldname, as_dict)

		with (
			patch(f"{MOD}.frappe.db.get_value", side_effect=_non_stock),
			self.assertRaises(frappe.ValidationError),
		):
			validate_customer_gold_settings(_settings())

	def test_missing_stock_entry_type_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
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
			self.assertRaises(frappe.ValidationError),
		):
			validate_customer_gold_settings(_settings())

	def test_missing_gold_rate_source_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(_settings(gold_rate_source=None))

	def test_missing_gold_rate_field_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(_settings(gold_rate_field=None))

	def test_arbitrary_gold_rate_field_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(_settings(gold_rate_field="__dict__"))

	def test_invalid_gold_rate_unit_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(_settings(gold_rate_unit="Per Ounce"))

	def test_no_company_rows_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(_settings(company_accounts=[]))

	def test_missing_liability_account_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability=None)])
			)

	def test_missing_cogs_adjustment_account_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, cogs=None)])
			)

	def test_wrong_company_account_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Liability - B")])
			)

	def test_liability_with_wrong_root_type_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Expense - A")])
			)

	def test_group_liability_account_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1, liability="Group Liability - A")])
			)

	def test_duplicate_company_row_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_settings(
				_settings(company_accounts=[_row(1), _row(2)])
			)

	def test_cogs_account_root_type_is_not_enforced(self, _mock):
		"""Classification is pending Finance approval, so any non-group company account passes."""
		validate_customer_gold_settings(
			_settings(company_accounts=[_row(1, cogs=COGS_A)])
		)
