"""Focused Item regressions; pure unittest tests mock DB boundaries explicitly.

These tests verify helper output and lookup counts, not real SQL timing, saving,
transaction behavior, or remote synchronization.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events import item as item_events


class ItemFixture(SimpleNamespace):
	def append(self, field, row):
		getattr(self, field).append(row)


def template(**overrides):
	values = {"has_variants": 1, "subcategory": "Test Subcategory", "attributes": []}
	values.update(overrides)
	return ItemFixture(**values)


class TestItemAttributeInitialization(unittest.TestCase):
	def setUp(self):
		self.names = ["Length", "Shape", "Length", "Missing"]
		self.metadata = {
			"Length": {
				"numeric_values": 1,
				"from_range": 0.001,
				"to_range": 25.5,
				"increment": 0.125,
			},
			"Shape": {
				"numeric_values": 0,
				"from_range": None,
				"to_range": 0,
				"increment": None,
			},
		}
		self.db = Mock()
		self.db.get_value.side_effect = self.get_value
		self.config = Mock(
			side_effect=lambda *args, **kwargs: [
				frappe._dict(item_attribute=name) for name in self.names
			]
		)
		self.patches = [
			patch.object(item_events.frappe, "db", self.db),
			patch.object(item_events.frappe, "get_all", self.config),
		]
		for patcher in self.patches:
			patcher.start()
			self.addCleanup(patcher.stop)

	def get_value(self, doctype, name, fields, **kwargs):
		self.assertEqual(doctype, "Item Attribute")
		metadata = self.metadata.get(name)
		if isinstance(fields, str):
			return metadata.get(fields) if metadata else None
		return (
			frappe._dict({field: metadata.get(field) for field in fields})
			if metadata
			else None
		)

	def test_order_duplicates_numeric_bounds_and_absence_are_preserved(self):
		doc = template()
		item_events.add_item_attributes(doc)
		self.assertEqual(
			doc.attributes,
			[
				{
					"attribute": "Length",
					"numeric_values": 1,
					"from_range": 0.001,
					"to_range": 25.5,
					"increment": 0.125,
				},
				{
					"attribute": "Shape",
					"numeric_values": 0,
					"from_range": None,
					"to_range": 0,
					"increment": None,
				},
				{
					"attribute": "Length",
					"numeric_values": 1,
					"from_range": 0.001,
					"to_range": 25.5,
					"increment": 0.125,
				},
				{
					"attribute": "Missing",
					"numeric_values": None,
					"from_range": None,
					"to_range": None,
					"increment": None,
				},
			],
		)
		self.config.assert_called_once_with(
			"Attribute Value Item Attribute Detail",
			{"parent": "Test Subcategory", "in_item_variant": 1},
			"item_attribute",
			order_by="idx asc",
		)

	def test_metadata_read_budget_scales_with_unique_attributes(self):
		self.names = ["Length", "Shape", "Missing"] * 100
		item_events.add_item_attributes(template())
		self.assertEqual(self.db.get_value.call_count, 3)

	def test_missing_metadata_is_reused_within_one_initialization(self):
		self.names = ["Missing"] * 4
		doc = template()
		item_events.add_item_attributes(doc)
		self.assertEqual(self.db.get_value.call_count, 1)
		self.assertEqual(len(doc.attributes), 4)
		self.assertTrue(all(row["numeric_values"] is None for row in doc.attributes))

	def test_metadata_is_reread_after_master_changes(self):
		first = template()
		item_events.add_item_attributes(first)
		self.metadata["Length"]["increment"] = 0.25
		second = template()
		item_events.add_item_attributes(second)
		self.assertEqual(first.attributes[0]["increment"], 0.125)
		self.assertEqual(second.attributes[0]["increment"], 0.25)

	def test_existing_attributes_are_untouched_and_no_lookups_run(self):
		original = [
			frappe._dict(attribute="Hand Entered", numeric_values=1, increment=0.001)
		]
		doc = template(attributes=original)
		item_events.add_item_attributes(doc)
		self.assertIs(doc.attributes, original)
		self.config.assert_not_called()
		self.db.get_value.assert_not_called()

	def test_non_template_and_absent_subcategory_gates_skip_work(self):
		for overrides in (
			{"has_variants": 0},
			{"subcategory": ""},
			{"subcategory": None},
		):
			with self.subTest(overrides=overrides):
				item_events.add_item_attributes(template(**overrides))
		self.config.assert_not_called()
		self.db.get_value.assert_not_called()

	def test_empty_configuration_does_not_read_metadata(self):
		self.names = []
		doc = template()
		item_events.add_item_attributes(doc)
		self.assertEqual(doc.attributes, [])
		self.db.get_value.assert_not_called()

	def test_blank_and_none_link_metadata_stays_missing(self):
		self.names = [None, ""]
		doc = template()
		item_events.add_item_attributes(doc)
		self.assertEqual([row["attribute"] for row in doc.attributes], [None, ""])
		self.assertTrue(all(row["increment"] is None for row in doc.attributes))


class TestItemCadValidation(unittest.TestCase):
	def setUp(self):
		self.settings = SimpleNamespace(
			cad_to_rpt=5,
			rpt_to_wax=2,
			wax_to_gold_10=10,
			wax_to_gold_14=11,
			wax_to_gold_18=12,
			wax_to_gold_22=13,
			wax_to_silver=14,
		)

		def throw(message, *args, **kwargs):
			raise frappe.ValidationError(message)

		self.db = Mock()
		self.db.get_value.return_value = 0.125
		for patcher in [
			patch.object(item_events.frappe, "get_doc", return_value=self.settings),
			patch.object(item_events.frappe, "db", self.db),
			patch.object(item_events.frappe, "throw", side_effect=throw),
			patch.object(item_events, "_", lambda message: message),
		]:
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_zero_cad_ratio_raises_validation_error_before_mutation(self):
		self.settings.cad_to_rpt = 0
		doc = {"cad_weight": 100}
		with self.assertRaisesRegex(frappe.ValidationError, "Cad to RPT Ratio"):
			item_events.calculate_item_wt_details(doc, bom="Test BOM")
		self.assertEqual(doc, {"cad_weight": 100})
		self.db.get_value.assert_not_called()

	def test_zero_rpt_ratio_raises_validation_error_before_mutation(self):
		self.settings.rpt_to_wax = 0
		doc = {"cad_weight": 100}
		with self.assertRaisesRegex(frappe.ValidationError, "RPT to Wax Ratio"):
			item_events.calculate_item_wt_details(doc, bom="Test BOM")
		self.assertEqual(doc, {"cad_weight": 100})
		self.db.get_value.assert_not_called()

	def test_missing_cad_weight_raises_validation_error(self):
		with self.assertRaisesRegex(frappe.ValidationError, "CAD Weight"):
			item_events.calculate_item_wt_details({}, bom="Test BOM")
		self.db.get_value.assert_not_called()

	def test_zero_cad_weight_remains_valid(self):
		result = item_events.calculate_item_wt_details(
			{"cad_weight": 0}, bom="Test BOM"
		)
		self.assertEqual(result["estimated_rpt_wt"], 0)
		self.assertEqual(result["estimated_wax_wt"], 0)
		self.assertEqual(result["estimated_18kt_gold_wt"], 0)

	def test_valid_json_payload_preserves_hand_calculated_weights(self):
		result = item_events.calculate_item_wt_details(
			'{"cad_weight": 100}', bom="Test BOM"
		)
		self.assertEqual(result["estimated_rpt_wt"], 20)
		self.assertEqual(result["estimated_wax_wt"], 10)
		self.assertEqual(result["estimated_10kt_gold_wt"], 100)
		self.assertEqual(result["estimated_14kt_gold_wt"], 110)
		self.assertEqual(result["estimated_18kt_gold_wt"], 120)
		self.assertEqual(result["estimated_22kt_gold_wt"], 130)
		self.assertEqual(result["estimated_silver_wt"], 140)
		self.assertEqual(result["estimated_finding_gold_wt_bom"], 0.125)


class TestWeightLookupPreservation(unittest.TestCase):
	def setUp(self):
		self.db = Mock()
		self.db.exists.return_value = True
		self.db.get_value.return_value = 0.05
		patcher = patch.object(item_events.frappe, "db", self.db)
		patcher.start()
		self.addCleanup(patcher.stop)

	def diamond(self, pairs=None):
		pairs = pairs or [
			("Diamond Type", "Natural"),
			("Stone Shape", "Round"),
			("Diamond Sieve Size", "1-2"),
		]
		return ItemFixture(
			attributes=[frappe._dict(attribute=a, attribute_value=v) for a, v in pairs]
		)

	def test_complete_diamond_filters_are_built_before_one_lookup(self):
		doc = self.diamond()
		weight = item_events.set_diamond_attribute_weight(
			doc, [row.attribute for row in doc.attributes]
		)
		self.assertEqual(weight, 0.05)
		self.db.exists.assert_called_once_with(
			"Diamond Weight",
			{
				"diamond_type": "Natural",
				"stone_shape": "Round",
				"diamond_sieve_size": "1-2",
			},
		)
		self.db.get_value.assert_called_once()

	def test_attribute_permutations_and_extra_attribute_do_not_change_filter(self):
		doc = self.diamond(
			[
				("Diamond Sieve Size", "1-2"),
				("Collection", "Extra"),
				("Stone Shape", "Round"),
				("Diamond Type", "Natural"),
			]
		)
		item_events.set_diamond_attribute_weight(
			doc, [row.attribute for row in doc.attributes]
		)
		self.assertEqual(
			self.db.exists.call_args.args[1],
			{
				"diamond_type": "Natural",
				"stone_shape": "Round",
				"diamond_sieve_size": "1-2",
			},
		)

	def test_missing_required_attribute_does_not_query_partial_key(self):
		doc = self.diamond([("Diamond Type", "Natural"), ("Stone Shape", "Round")])
		self.assertEqual(
			item_events.set_diamond_attribute_weight(
				doc, [row.attribute for row in doc.attributes]
			),
			0,
		)
		self.db.exists.assert_not_called()
		self.db.get_value.assert_not_called()

	def test_gemstone_uses_all_four_key_components(self):
		doc = self.diamond(
			[
				("Gemstone Type", "Ruby"),
				("Stone Shape", "Round"),
				("Gemstone Grade", "A"),
				("Gemstone Size", "2"),
			]
		)
		self.assertEqual(
			item_events.set_gemstone_attribute_weight(
				doc, [row.attribute for row in doc.attributes]
			),
			0.05,
		)
		self.db.exists.assert_called_once_with(
			"Gemstone Weight",
			{
				"gemstone_type": "Ruby",
				"stone_shape": "Round",
				"gemstone_grade": "A",
				"gemstone_size": "2",
			},
		)

	def test_zero_or_null_diamond_falls_back_to_gemstone(self):
		for value in (None, 0):
			with self.subTest(diamond=value):
				doc = self.diamond()
				doc.uoms = []
				with (
					patch.object(
						item_events, "set_diamond_attribute_weight", return_value=value
					),
					patch.object(
						item_events, "set_gemstone_attribute_weight", return_value=0.02
					) as gemstone,
				):
					item_events.update_item_uom_conversion(doc)
				self.assertEqual(doc.uoms, [{"uom": "Pcs", "conversion_factor": 0.02}])
				gemstone.assert_called_once()

	def test_nonzero_diamond_skips_gemstone_and_replaces_all_pcs_rows(self):
		doc = self.diamond()
		doc.uoms = [
			frappe._dict(uom="Pcs", conversion_factor=1),
			frappe._dict(uom="Gram", conversion_factor=1),
			frappe._dict(uom="Pcs", conversion_factor=2),
		]
		doc.remove = doc.uoms.remove
		with patch.object(item_events, "set_gemstone_attribute_weight") as gemstone:
			item_events.update_item_uom_conversion(doc)
		self.assertEqual(
			doc.uoms,
			[
				{"uom": "Gram", "conversion_factor": 1},
				{"uom": "Pcs", "conversion_factor": 0.05},
			],
		)
		gemstone.assert_not_called()
