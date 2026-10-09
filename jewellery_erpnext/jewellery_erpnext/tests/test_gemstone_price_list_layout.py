# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Layout checks for the Gemstone Price List DocType JSON.

A Diamond Range price list is keyed on Gemstone Grade, so the form must show
Gemstone Grade and hide Stone Quality for that type. The other price list
types keep their layout. These tests read the DocType JSON shipped next to the
doctype package and need no database.
"""

import json
import os

from frappe.tests import UnitTestCase

from jewellery_erpnext.gurukrupa_exports.doctype import (
	gemstone_price_list as gemstone_price_list_pkg,
)

DIAMOND_RANGE_EXCLUSION = 'eval: doc.price_list_type != "Diamond Range";'
PRICE_LIST_TYPE_OPTIONS = "\nFixed\nDiamond Range\nSize Range"


def _load_doctype_json():
	path = os.path.join(
		os.path.dirname(gemstone_price_list_pkg.__file__), "gemstone_price_list.json"
	)
	with open(path) as f:
		return json.load(f)


class TestGemstonePriceListLayout(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.doctype_json = _load_doctype_json()
		cls.fields = {df["fieldname"]: df for df in cls.doctype_json["fields"]}

	def test_stone_quality_is_hidden_on_diamond_range(self):
		self.assertEqual(
			self.fields["gemstone_quality"].get("depends_on"), DIAMOND_RANGE_EXCLUSION
		)

	def test_gemstone_grade_is_always_shown(self):
		self.assertNotIn("depends_on", self.fields["gemstone_grade"])

	def test_price_list_type_options_are_unchanged(self):
		self.assertEqual(
			self.fields["price_list_type"].get("options"), PRICE_LIST_TYPE_OPTIONS
		)

	def test_size_and_weight_range_fields_are_kept(self):
		for fieldname in ("from_size", "to_size", "from_weight", "to_weight"):
			with self.subTest(fieldname=fieldname):
				self.assertIn(fieldname, self.fields)

	def test_field_order_matches_field_definitions(self):
		field_order = self.doctype_json["field_order"]
		self.assertEqual(
			len(field_order), len(set(field_order)), "field_order has duplicate entries"
		)
		self.assertEqual(
			len(self.fields),
			len(self.doctype_json["fields"]),
			"duplicate field definitions",
		)
		self.assertEqual(
			sorted(set(field_order) - set(self.fields)),
			[],
			"field_order entries without a definition",
		)
		self.assertEqual(
			sorted(set(self.fields) - set(field_order)),
			[],
			"fields missing from field_order",
		)
