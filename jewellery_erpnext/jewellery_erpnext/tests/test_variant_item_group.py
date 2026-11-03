"""A variant created in code moves from its template's "- T" group to the "- V" group.

Item-Form-118 used to do this only when someone opened the variant in the form.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe
from erpnext.controllers.item_variant import create_variant
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import item as item_events


def item(**values):
	return SimpleNamespace(
		**{"variant_of": None, "item_group": None, "item_subcategory": None, **values}
	)


class TestVariantItemGroupRule(TestCase):
	def group_for(self, doc, existing=()):
		db = SimpleNamespace(exists=lambda doctype, name: name in existing)
		with patch.object(item_events.frappe, "db", db):
			return item_events.variant_item_group(doc)

	def test_material_variant_swaps_template_suffix(self):
		doc = item(variant_of="FL", item_group="Finding Loose - T")
		self.assertEqual(
			self.group_for(doc, {"Finding Loose - V"}), "Finding Loose - V"
		)

	def test_design_variant_takes_its_subcategory_group(self):
		doc = item(
			variant_of="RI00086", item_group="Casual Ring - T", item_subcategory="Ring"
		)
		self.assertEqual(self.group_for(doc, {"Ring - V"}), "Ring - V")

	def test_consumable_and_asset_groups_are_kept(self):
		for group in ("Chemicals", "Laptop"):
			with self.subTest(group=group):
				doc = item(variant_of="X", item_group=group, item_subcategory="Ring")
				self.assertIsNone(self.group_for(doc, {"Ring - V"}))

	def test_missing_target_group_keeps_the_group(self):
		doc = item(variant_of="D", item_group="Diamond - T")
		self.assertIsNone(self.group_for(doc, set()))

	def test_non_variant_and_no_subcategory_are_kept(self):
		self.assertIsNone(
			self.group_for(item(item_group="Diamond - T"), {"Diamond - V"})
		)
		doc = item(variant_of="RI00086", item_group="Casual Ring - T")
		self.assertIsNone(self.group_for(doc, {" - V"}))

	def test_group_already_right_is_kept(self):
		doc = item(variant_of="RI00086", item_group="Ring - V", item_subcategory="Ring")
		self.assertIsNone(self.group_for(doc, {"Ring - V"}))


class TestVariantItemGroupOnInsert(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# DB-stored Item Server Scripts are not the code under test.
		patcher = patch("frappe.model.document.run_server_script_for_doc_event")
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(frappe.db.rollback)
		self.prefix = f"_Test VIG {uuid4().hex[:8]}"
		# Item's reason-for-design-code field defaults to this Attribute Value.
		if not frappe.db.exists("Attribute Value", "New Design"):
			frappe.get_doc(
				{"doctype": "Attribute Value", "attribute_value": "New Design"}
			).insert()
		self.uom = frappe.get_doc(
			{"doctype": "UOM", "uom_name": self.prefix + " Unit"}
		).insert()
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "All Item Groups",
					"is_group": 1,
				}
			).insert()

	def group(self, name):
		return (
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": name,
					"parent_item_group": "All Item Groups",
				}
			)
			.insert()
			.name
		)

	def template(self, subcategory):
		attribute = frappe.get_doc(
			{
				"doctype": "Item Attribute",
				"attribute_name": self.prefix + " Size",
				"numeric_values": 1,
				"from_range": 1,
				"to_range": 10,
				"increment": 1,
			}
		).insert()
		frappe.get_doc(
			{"doctype": "Attribute Value", "attribute_value": subcategory}
		).insert()
		template = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": self.prefix + " Template",
				"item_group": self.group(self.prefix + " Ring - T"),
				"item_subcategory": subcategory,
				"stock_uom": self.uom.name,
				"is_stock_item": 0,
				"is_sales_item": 0,
				"has_variants": 1,
				"variant_based_on": "Item Attribute",
				"attributes": [
					{
						"attribute": attribute.name,
						"numeric_values": 1,
						"from_range": 1,
						"to_range": 10,
						"increment": 1,
					}
				],
			}
		).insert()
		return template, attribute

	def test_variant_created_in_code_lands_in_subcategory_group(self):
		subcategory = self.prefix + " Ring"
		v_group = self.group(subcategory + " - V")
		template, attribute = self.template(subcategory)
		variant = create_variant(template.name, {attribute.name: 3})
		# The order flows (Order, Repair Order, Sketch Order...) set the subcategory on the
		# new variant; create_variant does not copy it (not a Variant Field).
		variant.item_subcategory = subcategory
		variant.is_sales_item = 0
		variant.insert()
		self.assertEqual(variant.item_group, v_group)
		self.assertEqual(
			frappe.db.get_value("Item", variant.name, "item_group"), v_group
		)

	def test_without_a_v_group_the_template_group_is_kept(self):
		subcategory = self.prefix + " Ring"
		template, attribute = self.template(subcategory)
		variant = create_variant(template.name, {attribute.name: 4})
		variant.item_subcategory = subcategory
		variant.is_sales_item = 0
		variant.insert()
		self.assertEqual(variant.item_group, template.item_group)
