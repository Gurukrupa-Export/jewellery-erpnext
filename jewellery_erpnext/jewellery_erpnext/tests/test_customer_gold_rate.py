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


#: Extra same-date ``Gold Rates`` parents, keyed by date string. Empty by default so the
#: ordinary fixture stays unambiguous; the ambiguity tests patch this in.
EXTRA_SAME_DATE_PARENTS = {}


def _get_all(doctype, filters=None, fields=None, **kwargs):
	"""Stand-in for frappe.get_all, dispatching on doctype.

	MUST dispatch: the resolver calls ``frappe.get_all`` for BOTH the ``Gold Rates``
	parent (exact-date ambiguity) and the ``Gold Rates branchs`` child (source row). A
	stand-in that only understood the child silently returned ``[]`` for the parent and
	made every test fail with "Gold Rates is not available".
	"""
	filters = filters or {}

	if doctype == "Gold Rates":
		date = str(filters.get("date"))
		names = [ref for ref in (REF_OLD, REF_NEW) if ref.endswith(date)]
		names += EXTRA_SAME_DATE_PARENTS.get(date, [])
		return [frappe._dict(name=n) for n in sorted(names)]

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

		def _rows(doctype, filters=None, fields=None, **kw):
			# Must dispatch on doctype -- the resolver queries the Gold Rates PARENT
			# (exact-date ambiguity) as well as the Gold Rates branchs child.
			if doctype == "Gold Rates":
				return _get_all(doctype, filters, fields, **kw)
			filters = filters or {}
			if filters.get("parent") not in rates:
				return []
			return [frappe._dict(rates[filters["parent"]][filters["particulars"]])]

		with patch(f"{MOD}.frappe.get_all", side_effect=_rows):
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


@patch(f"{MOD}.frappe.get_all", side_effect=_get_all)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.frappe.db.exists", side_effect=_db_exists)
class TestGoldRatesExactDateAmbiguity(IntegrationTestCase):
	"""CG-T025 -- two ``Gold Rates`` records sharing one date must block.

	Reachable in the real schema: ``Gold Rates`` autonames ``format:R-{date}`` but sets
	``allow_rename: 1`` and puts no ``unique`` constraint on ``date``, so renaming the
	first record frees its name for a second one carrying the same date.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_single_record_still_resolves(self, *_mocks):
		"""Guard the guard: the ordinary one-record case must not have regressed."""
		res = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		self.assertEqual(res.gold_rate_reference, REF_OLD)

	def test_duplicate_exact_date_records_block(self, *_mocks):
		with patch.dict(
			EXTRA_SAME_DATE_PARENTS, {DATE_OLD: ["R-2026-08-15-RENAMED"]}, clear=False
		):
			with self.assertRaises(frappe.ValidationError):
				resolve_customer_gold_rate_for_date(DATE_OLD, _settings())

	def test_duplicate_block_names_both_records(self, *_mocks):
		"""The operator must be told WHICH records collide, or they cannot fix it."""
		with patch.dict(
			EXTRA_SAME_DATE_PARENTS, {DATE_OLD: ["R-2026-08-15-RENAMED"]}, clear=False
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		message = str(ctx.exception)
		self.assertIn(REF_OLD, message)
		self.assertIn("R-2026-08-15-RENAMED", message)

	def test_ambiguity_does_not_silently_pick_first(self, *_mocks):
		"""The whole point: no result may come back when the date is ambiguous."""
		with patch.dict(
			EXTRA_SAME_DATE_PARENTS, {DATE_OLD: ["R-2026-08-15-RENAMED"]}, clear=False
		):
			try:
				res = resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
			except frappe.ValidationError:
				return
		self.fail(f"ambiguous date silently resolved to {res.gold_rate_reference}")

	def test_ambiguity_on_one_date_does_not_affect_another(self, *_mocks):
		with patch.dict(
			EXTRA_SAME_DATE_PARENTS, {DATE_OLD: ["R-2026-08-15-RENAMED"]}, clear=False
		):
			res = resolve_customer_gold_rate_for_date(DATE_NEW, _settings())
		self.assertEqual(res.gold_rate_reference, REF_NEW)


@patch(f"{MOD}.frappe.get_all", side_effect=_get_all)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.frappe.db.exists", side_effect=_db_exists)
class TestGoldRateNonFiniteQuotes(IntegrationTestCase):
	"""CG-T027 -- ``nan`` and ``inf`` must be rejected.

	Neither fails a bare positive test: ``float("nan") <= 0`` and ``float("inf") <= 0``
	are both ``False``. Without an explicit finiteness check a non-finite quote would be
	frozen onto the receipt, and ``nan / 10`` is still ``nan``, carrying it into the
	per-gram conversion.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _resolve_with_rate(self, raw):
		rates = {REF_OLD: {SOURCE: {"live_rate": raw}}}

		def _rows(doctype, filters=None, fields=None, **kwargs):
			if doctype == "Gold Rates":
				return _get_all(doctype, filters, fields, **kwargs)
			filters = filters or {}
			row = rates.get(filters.get("parent"), {}).get(filters.get("particulars"))
			return [] if row is None else [frappe._dict(row)]

		with patch(f"{MOD}.frappe.get_all", side_effect=_rows):
			return resolve_customer_gold_rate_for_date(DATE_OLD, _settings())

	def test_nan_rate_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			self._resolve_with_rate(float("nan"))

	def test_positive_infinity_rate_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			self._resolve_with_rate(float("inf"))

	def test_negative_infinity_rate_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			self._resolve_with_rate(float("-inf"))

	def test_nan_never_reaches_per_gram_conversion(self, *_mocks):
		"""Explicitly assert the value does not escape as a nan per-gram rate."""
		try:
			res = self._resolve_with_rate(float("nan"))
		except frappe.ValidationError:
			return
		self.fail(f"nan escaped resolution as per_gram_rate={res.per_gram_rate}")

	def test_finite_rate_still_resolves(self, *_mocks):
		"""Guard the guard -- the finiteness check must not reject ordinary rates."""
		res = self._resolve_with_rate(71648.30)
		self.assertAlmostEqual(res.per_gram_rate, 7164.83, places=2)


@patch(f"{MOD}.frappe.get_all", side_effect=_get_all)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.frappe.db.exists", side_effect=_db_exists)
class TestGoldRateOperatingCase(IntegrationTestCase):
	"""The rate-unit case named in the specification, both ways round.

	Rs 71,648.30 per 10 g and Rs 7,164.83 per g are the same money with different raw
	evidence, and the snapshot must keep the raw form and unit distinguishable.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _resolve(self, raw, unit):
		rates = {REF_OLD: {SOURCE: {"live_rate": raw}}}

		def _rows(doctype, filters=None, fields=None, **kwargs):
			if doctype == "Gold Rates":
				return _get_all(doctype, filters, fields, **kwargs)
			filters = filters or {}
			row = rates.get(filters.get("parent"), {}).get(filters.get("particulars"))
			return [] if row is None else [frappe._dict(row)]

		with patch(f"{MOD}.frappe.get_all", side_effect=_rows):
			return resolve_customer_gold_rate_for_date(
				DATE_OLD, _settings(gold_rate_unit=unit)
			)

	def test_per_10_gram_quote_gives_expected_per_gram(self, *_mocks):
		res = self._resolve(71648.30, "Per 10 Gram")
		self.assertAlmostEqual(res.per_gram_rate, 7164.83, places=2)

	def test_per_gram_quote_gives_the_same_per_gram(self, *_mocks):
		res = self._resolve(7164.83, "Per Gram")
		self.assertAlmostEqual(res.per_gram_rate, 7164.83, places=2)

	def test_both_units_value_ten_grams_identically(self, *_mocks):
		per_10 = self._resolve(71648.30, "Per 10 Gram")
		per_1 = self._resolve(7164.83, "Per Gram")
		self.assertAlmostEqual(
			per_10.per_gram_rate * 10, per_1.per_gram_rate * 10, places=2
		)
		self.assertAlmostEqual(per_10.per_gram_rate * 10, 71648.30, places=2)

	def test_raw_evidence_stays_distinguishable_between_the_two(self, *_mocks):
		"""Same money, different evidence -- the snapshot must not blur them."""
		per_10 = self._resolve(71648.30, "Per 10 Gram")
		per_1 = self._resolve(7164.83, "Per Gram")
		self.assertEqual(per_10.raw_rate, 71648.30)
		self.assertEqual(per_10.rate_unit, "Per 10 Gram")
		self.assertEqual(per_1.raw_rate, 7164.83)
		self.assertEqual(per_1.rate_unit, "Per Gram")


@patch(f"{MOD}.frappe.get_all", side_effect=_get_all)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.frappe.db.exists", side_effect=_db_exists)
class TestGoldRateResolutionIsReadOnly(IntegrationTestCase):
	"""C08 -- valuing a receipt must never invoke the Gold Rates updater.

	The ``Gold Rates`` controller makes external provider calls from ``validate``. The
	resolver therefore must never load or save the document -- only query it. This pins
	that property so a future refactor to ``frappe.get_doc`` is caught here.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_resolution_never_loads_the_gold_rates_document(self, *_mocks):
		with patch(f"{MOD}.frappe.get_doc") as get_doc:
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		get_doc.assert_not_called()

	def test_resolution_never_commits(self, *_mocks):
		with patch(f"{MOD}.frappe.db.commit") as commit:
			resolve_customer_gold_rate_for_date(DATE_OLD, _settings())
		commit.assert_not_called()
