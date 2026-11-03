"""Repair of variants left in their template's "- T" item group (dry run by default).

Legacy variants are written with db_insert, as before_insert did not set their group.
"""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import variant_group_repair


class TestVariantGroupRepair(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Administrator")
		self.prefix = f"_Test VGR {uuid4().hex[:8]}"
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "All Item Groups",
					"is_group": 1,
				}
			).insert()
		self.t_group = self.group(" Ring - T")
		self.subcategory = self.prefix + " Ring"
		self.v_group = self.group(" Ring - V")
		self.template = self.legacy_item("Template", has_variants=1)

	def group(self, suffix):
		return (
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": self.prefix + suffix,
					"parent_item_group": "All Item Groups",
				}
			)
			.insert()
			.name
		)

	def legacy_item(self, suffix, attributes=(), **values):
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"name": self.prefix + " " + suffix,
				"item_code": self.prefix + " " + suffix,
				"item_name": self.prefix + " " + suffix,
				"item_group": self.t_group,
				"item_subcategory": self.subcategory,
				"stock_uom": "Nos",
				**values,
			}
		)
		doc.db_insert()
		for idx, (attribute, value) in enumerate(attributes, start=1):
			frappe.get_doc(
				{
					"doctype": "Item Variant Attribute",
					"parent": doc.name,
					"parenttype": "Item",
					"parentfield": "attributes",
					"idx": idx,
					"attribute": attribute,
					"attribute_value": value,
				}
			).db_insert()
		return doc

	def variant(self, suffix, attributes=()):
		return self.legacy_item(
			suffix, attributes=attributes, variant_of=self.template.name
		)

	def mine(self, entries):
		return sorted(e["item"] for e in entries if e["item"].startswith(self.prefix))

	def test_dry_run_reports_and_changes_nothing(self):
		good = self.variant("Good", [("Size", "5")])
		before = frappe.db.get_value("Item", good.name, ["item_group", "modified"])
		with patch("builtins.print"):
			result = variant_group_repair.repair()
		self.assertTrue(result["summary"]["dry_run"])
		self.assertEqual(self.mine(result["move"]), [good.name])
		self.assertEqual(
			frappe.db.get_value("Item", good.name, ["item_group", "modified"]), before
		)

	def test_real_run_moves_design_variants_without_touching_modified(self):
		good = self.variant("Good", [("Size", "5")])
		modified = frappe.db.get_value("Item", good.name, "modified")
		with patch("builtins.print"):
			variant_group_repair.repair(dry_run=0)
		self.assertEqual(
			frappe.db.get_value("Item", good.name, ["item_group", "modified"]),
			(self.v_group, modified),
		)
		self.assertEqual(
			frappe.db.get_value("Item", self.template.name, "item_group"), self.t_group
		)

	def test_variant_with_an_empty_attribute_is_listed_not_moved(self):
		empty = self.variant("Empty", [("Size", "5"), ("Colour", "")])
		with patch("builtins.print"):
			result = variant_group_repair.repair(dry_run=0)
		listed = [e for e in result["empty_attribute"] if e["item"] == empty.name]
		self.assertEqual(listed[0]["attributes"], ["Colour"])
		self.assertEqual(
			frappe.db.get_value("Item", empty.name, "item_group"), self.t_group
		)

	def test_batch_group_variants_are_listed_not_moved(self):
		material = self.variant("Material", [("Size", "5")])
		with patch.object(
			variant_group_repair, "variant_item_group", return_value="Diamond - V"
		):
			with patch("builtins.print"):
				result = variant_group_repair.repair(dry_run=0)
		self.assertIn(material.name, self.mine(result["batch_group"]))
		self.assertEqual(
			frappe.db.get_value("Item", material.name, "item_group"), self.t_group
		)

	def test_only_system_manager_may_run_it(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			variant_group_repair.repair()
