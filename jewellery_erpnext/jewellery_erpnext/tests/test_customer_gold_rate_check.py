# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""F1 -- a customer-gold receipt's rate is checked against an independent reference.

``KGJPL-SE-CGR-26-00011`` booked 10 g at Rs.1,57,655 per gram while the company was paying
Rs.15,504.85 per gram for the same item (``PR-26-00171``). The feed quotes per 10 g on some days
and per gram on others, so no ``gold_rate_unit`` is right every day, and nothing compared the
frozen rate with anything. Every receipt was also backdated a day, because the day's rate row
only appeared at 23:02.

Pure-logic per the suite convention: docs are ``frappe._dict`` fakes and every DB read is patched.
Exact-date resolution, zero, negative, NaN/Infinity and unknown-unit quotes are covered in
``test_customer_gold_rate``; the frozen-after-submit behaviour against real records is in
``test_customer_gold_integration``.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from jewellery_erpnext.customer_subcontracting import customer_gold_rate as cgrate
from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
	RATE_APPROVER_ROLE,
	validate_customer_gold_receipt,
)

from .test_customer_gold_receipt import MOD, RATE, SETTINGS, _db_get_value, _entry

_REAL_GET_VALUE = frappe.db.get_value


def _get_value(doctype, *args, **kwargs):
	"""The receipt suite's masters, faked; everything else -- System Settings for ``nowdate``
	among it -- read for real, so the framework keeps working under the patch."""
	if doctype in ("Stock Entry Type", "Batch", "Item", "Company"):
		return _db_get_value(doctype, *args, as_dict=kwargs.get("as_dict", False))
	return _REAL_GET_VALUE(doctype, *args, **kwargs)


#: What KGJPL-SE-CGR-26-00011 froze, and what the company paid for the same item.
BOOKED_PER_GRAM = 157655.0
PAID_PER_GRAM = 15504.85


class TestNormalisation(unittest.TestCase):
	"""§7.3-7.4: one contract -- raw rate, its unit, the factor, the per-gram rate."""

	def test_each_unit_carries_its_factor(self):
		self.assertEqual(cgrate.GRAMS_PER_UNIT[cgrate.PER_GRAM], 1.0)
		self.assertEqual(cgrate.GRAMS_PER_UNIT[cgrate.PER_10_GRAM], 10.0)

	def test_per_gram_is_raw_divided_by_the_factor(self):
		for unit, factor in cgrate.GRAMS_PER_UNIT.items():
			with self.subTest(unit=unit):
				self.assertEqual(
					cgrate.convert_gold_rate_to_per_gram(157655.0, unit),
					157655.0 / factor,
				)


class TestOutlierBand(unittest.TestCase):
	"""§7.5: block a scale error in either direction; let a normal day's movement through."""

	def _ratio(self, rate, reference):
		return cgrate.rate_ratio(rate, frappe._dict(rate=reference, source="ref"))

	def test_the_kgjpl_receipt_is_an_outlier(self):
		ratio = self._ratio(BOOKED_PER_GRAM, PAID_PER_GRAM)
		self.assertAlmostEqual(ratio, 10.17, places=2)
		self.assertTrue(cgrate.is_outlier(ratio))

	def test_a_tenth_of_the_reference_is_an_outlier(self):
		self.assertTrue(
			cgrate.is_outlier(self._ratio(PAID_PER_GRAM / 10, PAID_PER_GRAM))
		)

	def test_an_ordinary_day_is_not(self):
		"""15,765.50 (the 21 Sept feed, per gram) against 15,504.85 paid: 1.7% apart."""
		self.assertFalse(cgrate.is_outlier(self._ratio(15765.5, PAID_PER_GRAM)))

	def test_no_reference_is_not_an_outlier(self):
		self.assertIsNone(cgrate.rate_ratio(15765.5, None))
		self.assertFalse(cgrate.is_outlier(None))


class TestReferenceChoice(unittest.TestCase):
	"""A purchase first; the feed's own earlier day only when there is none."""

	def test_a_purchase_is_preferred_to_the_feed(self):
		purchase = frappe._dict(rate=PAID_PER_GRAM, source="Purchase Receipt PR-1")
		with (
			patch.object(cgrate, "_purchase_reference", return_value=purchase),
			patch.object(cgrate, "_feed_reference") as feed,
		):
			self.assertEqual(
				cgrate.reference_rate("M", "Co", nowdate(), SETTINGS), purchase
			)
		feed.assert_not_called()

	def test_the_feed_is_used_when_there_is_no_purchase(self):
		feed = frappe._dict(rate=15725.0, source="Gold Rates R-1")
		with (
			patch.object(cgrate, "_purchase_reference", return_value=None),
			patch.object(cgrate, "_feed_reference", return_value=feed),
		):
			self.assertEqual(
				cgrate.reference_rate("M", "Co", nowdate(), SETTINGS), feed
			)


class TestFeedReference(unittest.TestCase):
	"""The feed's earlier rate, normalised with the configured unit."""

	def _run(self, days):
		"""``days``: newest first, each ``(name, live_rate)``."""

		def get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype == cgrate.GOLD_RATES_DOCTYPE:
				return [frappe._dict(name=name, date=name[2:]) for name, _rate in days]
			rate = dict(days).get(filters["parent"])
			return [] if rate == "missing" else [frappe._dict(live_rate=rate)]

		with patch(f"{cgrate.__name__}.frappe.get_all", side_effect=get_all):
			return cgrate._feed_reference("2026-09-22", SETTINGS)

	def test_the_previous_day_is_normalised_with_the_configured_unit(self):
		"""SETTINGS says Per 10 Gram: 1,57,655 per 10 g is 15,765.50 per gram."""
		ref = self._run([("R-2026-09-21", 157655.0)])
		self.assertAlmostEqual(ref.rate, 15765.5, places=2)
		self.assertIn("R-2026-09-21", ref.source)

	def test_an_unusable_day_is_skipped_for_the_one_before(self):
		ref = self._run(
			[
				("R-2026-09-21", 0.0),
				("R-2026-09-20", "missing"),
				("R-2026-09-19", 157000.0),
			]
		)
		self.assertAlmostEqual(ref.rate, 15700.0, places=2)

	def test_no_earlier_day_means_no_reference(self):
		self.assertIsNone(self._run([]))


@patch(
	f"{MOD}.resolve_customer_gold_rate_for_date",
	return_value=frappe._dict(RATE, rate_factor=10.0),
)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
class TestReceiptRateCheck(unittest.TestCase):
	"""The receipt records the check on every validate and enforces it at submit."""

	def _validate(
		self, reference, action=None, reason=None, roles=(), posting_date=None
	):
		doc = _entry(posting_date=posting_date or nowdate(), _action=action)
		if reason:
			doc.custom_gold_rate_override_reason = reason
		with (
			patch(f"{MOD}.reference_rate", return_value=reference),
			patch(f"{MOD}.frappe.get_roles", return_value=list(roles)),
			patch(f"{MOD}.frappe.msgprint") as msgprint,
		):
			validate_customer_gold_receipt(doc)
		doc.msgprint = msgprint
		return doc

	#: RATE is 7,200 per gram; a reference of 720 makes it exactly ten times too high.
	OUTLIER = frappe._dict(rate=720.0, source="Purchase Receipt PR-1 (2026-09-10)")
	NORMAL = frappe._dict(rate=7000.0, source="Purchase Receipt PR-1 (2026-09-10)")

	def test_the_normalisation_is_frozen_with_the_rate(self, *_mocks):
		doc = self._validate(self.NORMAL)
		self.assertEqual(doc.custom_gold_rate_factor, 10.0)
		self.assertEqual(
			doc.custom_gold_rate_raw / doc.custom_gold_rate_factor,
			doc.custom_gold_rate_per_gram,
		)

	def test_the_check_evidence_is_recorded(self, *_mocks):
		doc = self._validate(self.NORMAL)
		self.assertEqual(doc.custom_gold_rate_check_reference, 7000.0)
		self.assertEqual(doc.custom_gold_rate_check_source, self.NORMAL.source)
		self.assertAlmostEqual(
			doc.custom_gold_rate_check_ratio, 7200.0 / 7000.0, places=6
		)

	def test_a_draft_with_an_outlier_saves_and_warns(self, *_mocks):
		doc = self._validate(self.OUTLIER)
		self.assertAlmostEqual(doc.custom_gold_rate_check_ratio, 10.0, places=6)
		doc.msgprint.assert_called_once()

	def test_submitting_an_outlier_is_refused(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate(self.OUTLIER, action="submit")
		self.assertIn("<strong>10.0</strong>x the reference", str(raised.exception))

	def test_a_reason_without_the_role_is_refused(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				self.OUTLIER,
				action="submit",
				reason="feed is per 10 g today",
				roles=["Stock User"],
			)

	def test_the_role_without_a_reason_is_refused(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			self._validate(self.OUTLIER, action="submit", roles=[RATE_APPROVER_ROLE])

	def test_an_approver_with_a_reason_may_submit_and_is_recorded(self, *_mocks):
		doc = self._validate(
			self.OUTLIER,
			action="submit",
			reason="confirmed with the dealer",
			roles=[RATE_APPROVER_ROLE],
		)
		self.assertEqual(doc.custom_gold_rate_override_by, frappe.session.user)

	def test_a_rate_inside_the_band_submits_without_an_approver(self, *_mocks):
		doc = self._validate(self.NORMAL, action="submit")
		self.assertIsNone(doc.custom_gold_rate_override_by)

	def test_no_reference_submits_and_says_so(self, *_mocks):
		doc = self._validate(None, action="submit")
		self.assertIsNone(doc.custom_gold_rate_check_ratio)
		self.assertEqual(
			doc.custom_gold_rate_check_source, "No reference rate available"
		)


@patch(f"{MOD}.reference_rate", return_value=None)
@patch(
	f"{MOD}.resolve_customer_gold_rate_for_date",
	return_value=frappe._dict(RATE, rate_factor=10.0),
)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
class TestNoBackdating(unittest.TestCase):
	"""§7.7 and the 2026-09-24 decision: block until today's rate exists; never backdate."""

	def test_a_backdated_receipt_cannot_be_submitted(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(
				_entry(posting_date=add_days(nowdate(), -1), _action="submit")
			)
		self.assertIn("cannot be backdated", str(raised.exception))

	def test_a_backdated_draft_can_still_be_saved_and_corrected(self, *_mocks):
		validate_customer_gold_receipt(
			_entry(posting_date=add_days(nowdate(), -1), _action="save")
		)

	def test_todays_receipt_submits(self, *_mocks):
		validate_customer_gold_receipt(_entry(posting_date=nowdate(), _action="submit"))
