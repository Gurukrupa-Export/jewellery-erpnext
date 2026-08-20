# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold rate service.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, Gold Rates rows are
``frappe._dict`` fakes and every DB read is patched. Nothing is written, and no Gold Rates
document is ever modified.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from jewellery_erpnext.customer_subcontracting.customer_gold_rate import (
	convert_gold_rate_to_per_gram,
	resolve_customer_gold_rate_for_date,
)

MOD = "jewellery_erpnext.customer_subcontracting.customer_gold_rate"

SOURCE = "Jain Jewels"
OTHER_SOURCE = "Arihant"
DATE_OLD = "2026-08-15"
DATE_NEW = "2026-08-21"
REF_OLD = "R-2026-08-15"
REF_NEW = "R-2026-08-21"

#: raw values per date, per source -- deliberately different so a date mix-up is visible
RATES = {
	REF_OLD: {SOURCE: {"live_rate": 72000.0, "9_am": 71500.0}},
	REF_NEW: {SOURCE: {"live_rate": 75000.0, "9_am": 74500.0}},
}


def _settings(**overrides):
	doc = frappe._dict(
		gold_rate_source=SOURCE,
		gold_rate_field="live_rate",
		gold_rate_unit="Per 10 Gram",
	)
	doc.update(overrides)
	return doc


def _db_exists(doctype, name=None):
	return True


def _db_get_value(doctype, filters, fieldname=None, **kwargs):
	if doctype == "Gold Rates":
		date = str(filters.get("date"))
		for ref in (REF_OLD, REF_NEW):
			if ref.endswith(date):
				return ref
		return None
	return None


def _get_all(doctype, filters=None, fields=None, **kwargs):
	"""Stand-in for frappe.get_all over Gold Rates branchs."""
	filters = filters or {}
	rates = RATES.get(filters.get("parent"), {})
	row = rates.get(filters.get("particulars"))
	if row is None:
		return []
	return [frappe._dict(row)]


@patch(f"{MOD}.frappe.get_all", side_effect=_get_all)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.frappe.db.exists", side_effect=_db_exists)
class TestCustomerGoldRateService(IntegrationTestCase):
	"""Happy-path resolution and unit conversion."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_resolves_exact_posting_date(self, *_mocks):
		res = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		self.assertEqual(res.gold_rate_reference, REF_OLD)
		self.assertEqual(res.gold_rate_date, getdate(DATE_OLD))

	def test_result_carries_full_derivation(self, *_mocks):
		res = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		for key in (
			"gold_rate_reference",
			"gold_rate_date",
			"rate_source",
			"rate_field",
			"raw_rate",
			"rate_unit",
			"per_gram_rate",
		):
			self.assertIn(key, res)
		self.assertEqual(res.rate_source, SOURCE)
		self.assertEqual(res.rate_field, "live_rate")
		self.assertEqual(res.raw_rate, 72000.0)
		self.assertEqual(res.rate_unit, "Per 10 Gram")

	def test_per_10_gram_conversion(self, *_mocks):
		res = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		self.assertEqual(res.per_gram_rate, 7200.0)

	def test_per_gram_conversion_is_identity(self, *_mocks):
		res = resolve_customer_gold_rate_for_date(
			DATE_OLD, _settings(gold_rate_unit="Per Gram")
		)
		self.assertEqual(res.per_gram_rate, 72000.0)

	def test_configured_field_is_used(self, *_mocks):
		"""Proves the Settings field really drives the read, not a hardcoded live_rate."""
		res = resolve_customer_gold_rate_for_date(
			DATE_OLD, _settings(gold_rate_field="9_am")
		)
		self.assertEqual(res.raw_rate, 71500.0)
		self.assertEqual(res.per_gram_rate, 7150.0)

	def test_non_round_precision_is_preserved(self, *_mocks):
		"""71,648.30 per 10 g must resolve to 7,164.83 per gram, not 7,164.8."""
		rates = {REF_OLD: {SOURCE: {"live_rate": 71648.30}}}
		with patch(
			f"{MOD}.frappe.get_all",
			side_effect=lambda dt, filters=None, fields=None, **kw: (
				[frappe._dict(rates[filters["parent"]][filters["particulars"]])]
				if filters.get("parent") in rates
				else []
			),
		):
			res = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		self.assertEqual(res.raw_rate, 71648.30)
		self.assertEqual(res.per_gram_rate, 7164.83)

	def test_different_dates_resolve_different_rates(self, *_mocks):
		"""The posting date drives the result -- never a single 'current' rate."""
		old = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		new = resolve_customer_gold_rate_for_date(DATE_NEW, _settings())
		self.assertEqual(old.per_gram_rate, 7200.0)
		self.assertEqual(new.per_gram_rate, 7500.0)

	def test_conversion_helper_is_pure(self, *_mocks):
		self.assertEqual(convert_gold_rate_to_per_gram(75000, "Per 10 Gram"), 7500.0)
		self.assertEqual(convert_gold_rate_to_per_gram(7500, "Per Gram"), 7500.0)

	def test_conversion_helper_rejects_unknown_unit(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			convert_gold_rate_to_per_gram(75000, "Per Ounce")


@patch(f"{MOD}.frappe.get_all", side_effect=_get_all)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.frappe.db.exists", side_effect=_db_exists)
class TestCustomerGoldRateMissing(IntegrationTestCase):
	"""Every missing / unusable-rate case must block with a ValidationError."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_missing_posting_date_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(None, _settings())

	def test_missing_gold_rates_document_blocks(self, *_mocks):
		"""EXACT date policy: a date with no Gold Rates blocks, never falls back."""
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date("2026-08-10", _settings())

	def test_missing_source_row_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_source=OTHER_SOURCE)
			)

	def test_duplicate_source_rows_block(self, *_mocks):
		def _dupes(doctype, filters=None, fields=None, **kwargs):
			return [frappe._dict(live_rate=72000.0), frappe._dict(live_rate=73000.0)]

		with (
			patch(f"{MOD}.frappe.get_all", side_effect=_dupes),
			self.assertRaises(frappe.ValidationError),
		):
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())

	def test_null_rate_blocks(self, *_mocks):
		def _null(doctype, filters=None, fields=None, **kwargs):
			return [frappe._dict(live_rate=None)]

		with (
			patch(f"{MOD}.frappe.get_all", side_effect=_null),
			self.assertRaises(frappe.ValidationError),
		):
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())

	def test_zero_rate_blocks(self, *_mocks):
		def _zero(doctype, filters=None, fields=None, **kwargs):
			return [frappe._dict(live_rate=0)]

		with (
			patch(f"{MOD}.frappe.get_all", side_effect=_zero),
			self.assertRaises(frappe.ValidationError),
		):
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())

	def test_negative_rate_blocks(self, *_mocks):
		def _negative(doctype, filters=None, fields=None, **kwargs):
			return [frappe._dict(live_rate=-1)]

		with (
			patch(f"{MOD}.frappe.get_all", side_effect=_negative),
			self.assertRaises(frappe.ValidationError),
		):
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())

	def test_blank_source_in_settings_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_source=None)
			)

	def test_blank_field_in_settings_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_field=None)
			)

	def test_arbitrary_field_in_settings_blocks(self, *_mocks):
		"""Defence in depth: the Select constrains this, the service re-checks it."""
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_field="__dict__")
			)

	def test_blank_unit_in_settings_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_unit=None)
			)

	def test_invalid_unit_in_settings_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_unit="Per Ounce")
			)

	def test_missing_gold_rates_doctype_blocks(self, *_mocks):
		with (
			patch(f"{MOD}.frappe.db.exists", return_value=False),
			self.assertRaises(frappe.ValidationError),
		):
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
