# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext.utils import resolve_manufacturing_setting


class TestManufacturingSetting(UnitTestCase):
	"""Unit tests for resolve_manufacturing_setting — no fixtures, so the three fallback
	branches are exercised independently of whatever the site's real records look like.

	The branch that matters in production is #2: gk.site's records are named after the
	COMPANY with a `manufacturer` that is not a real Manufacturer, so a manufacturer-keyed
	lookup finds nothing and the company fallback is the only path that ever resolves.
	"""

	def test_exact_manufacturer_and_company_pair_wins(self):
		with patch("frappe.db.get_value", return_value="Shubh") as get_value:
			self.assertEqual(
				resolve_manufacturing_setting("Test_Company", "Shubh"), "Shubh"
			)
		# One call only: the exact (manufacturer, company) pair short-circuits.
		get_value.assert_called_once()
		self.assertEqual(
			get_value.call_args.args[1],
			{"manufacturer": "Shubh", "company": "Test_Company"},
		)

	def test_falls_back_to_manufacturer_alone(self):
		"""Behaviour the rest of the app relies on: no exact pair, but the manufacturer has
		a setting under another company. On gk.site this is GEPL entries stamped Labh, whose
		setting lives under KGJPL."""
		with patch("frappe.db.get_value", side_effect=[None, "Labh"]) as get_value:
			self.assertEqual(
				resolve_manufacturing_setting(
					"Gurukrupa Export Private Limited", "Labh"
				),
				"Labh",
			)
		self.assertEqual(get_value.call_count, 2)
		self.assertEqual(get_value.call_args.args[1], {"manufacturer": "Labh"})

	def test_falls_back_to_the_company_single_record(self):
		"""A real manufacturer matching nothing must still resolve — the gk.site case."""
		with (
			patch("frappe.db.get_value", return_value=None),
			patch(
				"frappe.get_all",
				return_value=[
					frappe._dict(name="Test_Company", manufacturer="Test_Company")
				],
			),
		):
			self.assertEqual(
				resolve_manufacturing_setting("Test_Company", "Labh"), "Test_Company"
			)

	def test_company_fallback_returns_none_when_siblings_exist(self):
		"""The live gk.site shape: each company has two manufacturer-specific settings and
		no company-wide one, so an entry with NO manufacturer resolves to None and the
		restriction feature fails open."""
		with (
			patch("frappe.db.get_value", return_value=None),
			patch(
				"frappe.get_all",
				return_value=[
					frappe._dict(name="Labh", manufacturer="Labh"),
					frappe._dict(name="Labh 1", manufacturer="Labh 1"),
				],
			),
		):
			self.assertIsNone(
				resolve_manufacturing_setting("KG GK Jewellers Private Limited")
			)

	def test_falls_back_to_the_company_wide_record(self):
		with (
			patch("frappe.db.get_value", return_value=None),
			patch(
				"frappe.get_all",
				return_value=[
					frappe._dict(name="A", manufacturer="Labh"),
					frappe._dict(name="B", manufacturer=None),
				],
			),
		):
			self.assertEqual(resolve_manufacturing_setting("Test_Company"), "B")

	def test_never_guesses_between_sibling_manufacturers(self):
		"""Two manufacturer-specific records and no company-wide one: return None rather
		than silently apply another manufacturer's configuration."""
		with (
			patch("frappe.db.get_value", return_value=None),
			patch(
				"frappe.get_all",
				return_value=[
					frappe._dict(name="A", manufacturer="Labh"),
					frappe._dict(name="B", manufacturer="Shubh"),
				],
			),
		):
			self.assertIsNone(resolve_manufacturing_setting("Test_Company"))

	def test_returns_none_quietly_with_nothing_to_go_on(self):
		self.assertIsNone(resolve_manufacturing_setting())

	def test_throws_only_when_asked(self):
		with (
			patch("frappe.db.get_value", return_value=None),
			patch("frappe.get_all", return_value=[]),
		):
			self.assertRaises(
				frappe.ValidationError,
				resolve_manufacturing_setting,
				"Test_Company",
				"Labh",
				True,
			)
