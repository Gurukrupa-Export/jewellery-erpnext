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


CORAL_FACETED_ROW = frappe._dict(
	parent="Coral", item_attribute="Cut or Cab", attribute_value="Faceted"
)
SELECTION = {
	"Gemstone Type": ["Coral", "Amethyst"],
	"Cut or Cab": ["Faceted", "Cabochon"],
}


class TestMultipleVariantCombinations(IntegrationTestCase):
	"""Multiple Variants: blocked combinations are reported and skipped, the rest created."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		p = patch.object(
			item_events, "get_not_allowed_rows", return_value=[CORAL_FACETED_ROW]
		)
		self.get_rows = p.start()
		self.addCleanup(p.stop)

	def test_split_blocks_only_the_restricted_combination(self):
		allowed, blocked = item_events.split_variant_combinations(SELECTION)
		self.assertEqual(len(allowed), 3)
		self.assertEqual(
			[combination for combination, _message in blocked],
			[{"Gemstone Type": "Coral", "Cut or Cab": "Faceted"}],
		)
		self.assertIn("Coral", blocked[0][1])
		self.get_rows.assert_called_once()

	def test_value_blocked_by_an_unselected_value_is_allowed(self):
		allowed, blocked = item_events.split_variant_combinations(
			{"Gemstone Type": ["Amethyst"], "Cut or Cab": ["Faceted"]}
		)
		self.assertEqual(len(allowed), 1)
		self.assertEqual(blocked, [])

	def test_pre_check_reports_counts_and_reasons(self):
		result = item_events.get_blocked_combinations(
			'{"Gemstone Type": ["Coral", "Amethyst"], "Cut or Cab": ["Faceted", "Cabochon"], "Gemstone PR": []}'
		)
		self.assertEqual(result["total"], 4)
		self.assertEqual(result["blocked"], 1)
		self.assertEqual(len(result["reasons"]), 1)

	def test_create_multiple_variants_skips_blocked_combinations(self):
		created = []
		with (
			patch.object(
				item_events.frappe, "get_doc", return_value=SimpleNamespace(image=None)
			),
			patch("erpnext.controllers.item_variant.get_variant", return_value=None),
			patch(
				"erpnext.controllers.item_variant.create_variant",
				side_effect=lambda item, values: created.append(values)
				or SimpleNamespace(save=lambda: None),
			),
		):
			count = item_events.create_multiple_variants("G", SELECTION)
		self.assertEqual(count, 3)
		self.assertNotIn({"Gemstone Type": "Coral", "Cut or Cab": "Faceted"}, created)

	def test_existing_variants_are_not_recreated(self):
		with (
			patch.object(
				item_events.frappe, "get_doc", return_value=SimpleNamespace(image=None)
			),
			patch(
				"erpnext.controllers.item_variant.get_variant",
				return_value="G-EXISTING",
			),
			patch("erpnext.controllers.item_variant.create_variant") as create_variant,
		):
			self.assertEqual(item_events.create_multiple_variants("G", SELECTION), 0)
		create_variant.assert_not_called()


class TestEnqueueMultipleVariantCreation(IntegrationTestCase):
	"""enqueue override keeps ERPNext's limits and routes creation through ours."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		p = patch.object(item_events.frappe, "has_permission", return_value=True)
		p.start()
		self.addCleanup(p.stop)

	def test_small_selection_is_created_immediately(self):
		with patch.object(
			item_events, "create_multiple_variants", return_value=3
		) as create:
			self.assertEqual(
				item_events.enqueue_multiple_variant_creation("G", SELECTION), 3
			)
		create.assert_called_once()

	def test_large_selection_is_queued_to_our_creator(self):
		args = {
			"Gemstone Type": [f"T{i}" for i in range(5)],
			"Cut or Cab": ["Faceted", "Cabochon"],
		}
		with patch.object(item_events.frappe, "enqueue") as enqueue:
			self.assertEqual(
				item_events.enqueue_multiple_variant_creation("G", args), "queued"
			)
		self.assertEqual(
			enqueue.call_args.args[0],
			"jewellery_erpnext.jewellery_erpnext.doc_events.item.create_multiple_variants",
		)

	def test_empty_selection_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			item_events.enqueue_multiple_variant_creation("G", {"Gemstone Type": []})

	def test_more_than_500_combinations_is_rejected(self):
		args = {"A": [str(i) for i in range(30)], "B": [str(i) for i in range(20)]}
		with self.assertRaises(frappe.ValidationError):
			item_events.enqueue_multiple_variant_creation("G", args)
