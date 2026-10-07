# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Unit tests for the Attribute Value "Not Allowed Attribute Values" table.

An Attribute Value (e.g. Gemstone Type Coral) lists values of other Item
Attributes that must not be used with it (e.g. Cut or Cab Faceted):

* Attribute Value.validate -> rows are unique and belong to their Item Attribute.
* Item.validate -> validate_not_allowed_attribute_values blocks a new or
  changed variant that uses a blocked value; unchanged items are not rechecked.
* get_allowed_attribute_values / get_blocked_attributes -> Create Variant
  dialog helpers that hide and clear blocked values.

DB-free per the suite convention: setUpClass is neutralised and every frappe
lookup is mocked.

Run with:
  bench --site gk.localhost run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_not_allowed_attribute_values
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import item as item_events
from jewellery_erpnext.jewellery_erpnext.doctype.attribute_value import (
	attribute_value as attribute_value_module,
)

BLOCK_CORAL_FACETED = {"Cut or Cab": {"Faceted": "Coral"}}


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def _attr(attribute, value):
	return SimpleNamespace(attribute=attribute, attribute_value=value)


def _variant(gemstone_type="Coral", cut_or_cab="Faceted", before=None, variant_of="G"):
	doc = _Doc(
		variant_of=variant_of,
		attributes=[
			_attr("Gemstone Type", gemstone_type),
			_attr("Cut or Cab", cut_or_cab),
		],
	)
	doc.get_doc_before_save = lambda: before
	return doc


class TestGetNotAllowedAttributeValues(IntegrationTestCase):
	"""get_not_allowed_attribute_values(): reads the Not Allowed rows of the selected values."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_groups_blocked_values_by_attribute(self):
		rows = [
			frappe._dict(
				parent="Coral", item_attribute="Cut or Cab", attribute_value="Faceted"
			),
			frappe._dict(
				parent="Coral", item_attribute="Stone Shape", attribute_value="Oval"
			),
		]
		with patch.object(item_events.frappe, "get_all", return_value=rows) as get_all:
			result = item_events.get_not_allowed_attribute_values(["Coral", "Round"])
		self.assertEqual(
			result,
			{"Cut or Cab": {"Faceted": "Coral"}, "Stone Shape": {"Oval": "Coral"}},
		)
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["parent"], ("in", ["Coral", "Round"]))
		self.assertEqual(filters["parentfield"], "not_allowed_attribute_values")

	def test_no_selected_values_skips_the_query(self):
		with patch.object(item_events.frappe, "get_all") as get_all:
			self.assertEqual(
				item_events.get_not_allowed_attribute_values([None, ""]), {}
			)
		get_all.assert_not_called()


class TestValidateNotAllowedAttributeValues(IntegrationTestCase):
	"""Item.validate: a new or changed variant cannot use a blocked value."""

	@classmethod
	def setUpClass(cls):
		pass

	def _blocked(self, value):
		p = patch.object(
			item_events, "get_not_allowed_attribute_values", return_value=value
		)
		mock = p.start()
		self.addCleanup(p.stop)
		return mock

	def test_blocked_value_on_new_variant_is_rejected(self):
		self._blocked(BLOCK_CORAL_FACETED)
		with self.assertRaises(frappe.ValidationError):
			item_events.validate_not_allowed_attribute_values(
				_variant("Coral", "Faceted")
			)

	def test_allowed_value_passes(self):
		self._blocked(BLOCK_CORAL_FACETED)
		item_events.validate_not_allowed_attribute_values(_variant("Coral", "Cabochon"))

	def test_unrestricted_gemstone_type_passes(self):
		self._blocked({})
		item_events.validate_not_allowed_attribute_values(
			_variant("Amethyst", "Faceted")
		)

	def test_unchanged_existing_variant_is_not_rechecked(self):
		mock = self._blocked(BLOCK_CORAL_FACETED)
		before = _Doc(
			attributes=[_attr("Gemstone Type", "Coral"), _attr("Cut or Cab", "Faceted")]
		)
		item_events.validate_not_allowed_attribute_values(_variant(before=before))
		mock.assert_not_called()

	def test_changing_to_a_blocked_value_is_rejected(self):
		self._blocked(BLOCK_CORAL_FACETED)
		before = _Doc(
			attributes=[
				_attr("Gemstone Type", "Coral"),
				_attr("Cut or Cab", "Cabochon"),
			]
		)
		with self.assertRaises(frappe.ValidationError):
			item_events.validate_not_allowed_attribute_values(_variant(before=before))

	def test_template_is_skipped(self):
		mock = self._blocked(BLOCK_CORAL_FACETED)
		item_events.validate_not_allowed_attribute_values(_variant(variant_of=None))
		mock.assert_not_called()


class TestCreateVariantDialogHelpers(IntegrationTestCase):
	"""Dialog helpers: blocked values are hidden from suggestions and cleared."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		for target, kwargs in (
			("has_permission", {"return_value": True}),
			("get_all", {"return_value": ["Faceted", "Cabochon"]}),
		):
			p = patch.object(item_events.frappe, target, **kwargs)
			p.start()
			self.addCleanup(p.stop)
		p = patch.object(
			item_events,
			"get_not_allowed_attribute_values",
			side_effect=lambda values: BLOCK_CORAL_FACETED if "Coral" in values else {},
		)
		p.start()
		self.addCleanup(p.stop)

	def test_blocked_value_is_hidden_from_suggestions(self):
		values = item_events.get_allowed_attribute_values(
			"Cut or Cab", selected={"Gemstone Type": "Coral"}
		)
		self.assertEqual(values, ["Cabochon"])

	def test_unrestricted_selection_lists_every_value(self):
		values = item_events.get_allowed_attribute_values(
			"Cut or Cab", selected={"Gemstone Type": "Amethyst"}
		)
		self.assertEqual(values, ["Faceted", "Cabochon"])

	def test_blocked_selection_is_reported_for_clearing(self):
		blocked = item_events.get_blocked_attributes(
			selected={"Gemstone Type": "Coral", "Cut or Cab": "Faceted"}
		)
		self.assertEqual(blocked, ["Cut or Cab"])

	def test_allowed_selection_reports_nothing(self):
		blocked = item_events.get_blocked_attributes(
			selected={"Gemstone Type": "Coral", "Cut or Cab": "Cabochon"}
		)
		self.assertEqual(blocked, [])


class TestAttributeValueNotAllowedRows(IntegrationTestCase):
	"""Attribute Value.validate: Not Allowed rows are unique and belong to their attribute."""

	@classmethod
	def setUpClass(cls):
		pass

	def _validate(self, rows, exists=True):
		doc = SimpleNamespace(
			not_allowed_attribute_values=rows, get=lambda key: rows if key else None
		)
		with patch.object(
			attribute_value_module.frappe.db, "exists", return_value=exists
		):
			attribute_value_module.AttributeValue.validate_not_allowed_attribute_values(
				doc
			)

	def _row(self, idx, value="Faceted", attribute="Cut or Cab"):
		return SimpleNamespace(idx=idx, item_attribute=attribute, attribute_value=value)

	def test_valid_rows_pass(self):
		self._validate([self._row(1, "Faceted")])

	def test_duplicate_row_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate([self._row(1), self._row(2)])

	def test_value_outside_its_item_attribute_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate([self._row(1, "Round")], exists=False)


class TestGetVariantOverride(IntegrationTestCase):
	"""get_variant override: Create in the Single Variant dialog rejects blocked values."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		p = patch.object(
			item_events,
			"get_not_allowed_attribute_values",
			side_effect=lambda values: BLOCK_CORAL_FACETED if "Coral" in values else {},
		)
		p.start()
		self.addCleanup(p.stop)
		p = patch(
			"erpnext.controllers.item_variant.get_variant", return_value="G-EXISTING"
		)
		self.erpnext_get_variant = p.start()
		self.addCleanup(p.stop)

	def test_blocked_combination_is_rejected_before_lookup(self):
		with self.assertRaises(frappe.ValidationError):
			item_events.get_variant(
				"G",
				'{"Gemstone Type": "Coral", "Cut or Cab": "Faceted", "use_template_image": 0}',
			)
		self.erpnext_get_variant.assert_not_called()

	def test_allowed_combination_falls_through_to_erpnext(self):
		args = '{"Gemstone Type": "Coral", "Cut or Cab": "Cabochon"}'
		self.assertEqual(item_events.get_variant("G", args), "G-EXISTING")
		self.erpnext_get_variant.assert_called_once_with("G", args, None, None, None)

	def test_no_args_falls_through_to_erpnext(self):
		self.assertEqual(item_events.get_variant("G"), "G-EXISTING")
