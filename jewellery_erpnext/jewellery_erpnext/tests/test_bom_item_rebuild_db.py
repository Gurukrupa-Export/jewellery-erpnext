"""Real-database regressions for the narrow BOM metadata/rebuild/precision helpers.

Requires an isolated site with ERPNext, Jewellery ERPNext and the BOM Other
Detail/Customer custom fields installed. Missing schema is an error, not a skip.
Fixture db_insert intentionally isolates helper behavior from document lifecycle
validation; these are not complete BOM save, costing, stock or sync tests.
"""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import bom as bom_events


class TestBOMItemRebuildDatabase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		required_fields = {
			"BOM": [
				"bom_type",
				"metal_detail",
				"diamond_detail",
				"gemstone_detail",
				"finding_detail",
				"other_detail",
				"customer",
				"doc_pricision",
				"diamond_pricision",
				"gemstone_pricision",
			],
			"Customer": [
				"custom_consider_2_digit_for_bom",
				"custom_consider_2_digit_for_diamond",
				"custom_consider_2_digit_for_gemstone",
			],
			"Jewellery System Item": ["item_code"],
		}
		for doctype, fields in required_fields.items():
			meta = frappe.get_meta(doctype)
			missing = [field for field in fields if not meta.has_field(field)]
			if missing:
				raise RuntimeError(f"BLOCKED: missing schema {doctype}: {missing}")
		frappe.get_meta("BOM Other Detail")

	def setUp(self):
		super().setUp()
		self.savepoint = f"bom_item_{uuid4().hex}"
		frappe.db.savepoint(self.savepoint)
		self.addCleanup(frappe.db.rollback, save_point=self.savepoint)
		self.prefix = f"_Test BOM Item {uuid4().hex[:10]}"
		self.template = self.insert_item("Metal", "Gram", has_variants=1)
		self.gold = self.insert_item("Gold", "Gram", variant_of=self.template.name)
		self.cord = self.insert_item("Cord", "Meter")
		self.box = self.insert_item("Box", "Nos")
		frappe.get_doc(
			{
				"doctype": "Jewellery System Item",
				"name": self.prefix + " System Item",
				"parent": "Jewellery Settings",
				"parenttype": "Jewellery Settings",
				"parentfield": "system_item",
				"item_code": self.template.name,
			}
		).db_insert()

	def insert_item(self, suffix, uom, **values):
		name = f"{self.prefix} {suffix}"
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"name": name,
				"item_code": name,
				"item_name": name,
				"stock_uom": uom,
				"item_group": "All Item Groups",
				**values,
			}
		)
		doc.db_insert()
		return doc

	def make_bom(self, **values):
		return frappe.get_doc(
			{
				"doctype": "BOM",
				"name": self.prefix + " BOM",
				"bom_type": "Sales Order",
				"item": self.box.name,
				"quantity": 1,
				"items": [],
				"metal_detail": [],
				"diamond_detail": [],
				"gemstone_detail": [],
				"finding_detail": [],
				"other_detail": [],
				**values,
			}
		)

	def rebuild(self, doc):
		bom_events._set_bom_items_by_child_tables(doc, None)

	def test_other_only_real_item_lookup_preserves_uom_after_reload(self):
		doc = self.make_bom(
			other_detail=[
				{"item_code": self.cord.name, "quantity": 2.125},
				{"item_code": self.box.name, "quantity": 3},
			]
		)
		self.rebuild(doc)
		doc.db_insert()
		for child in doc.get_all_children():
			child.db_insert()
		doc.reload()
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items],
			[
				(self.cord.name, 2.125, "Meter"),
				(self.box.name, 3, "Nos"),
			],
		)

	def test_material_rebuild_keeps_manual_metadata_and_correct_other_uom(self):
		doc = self.make_bom(
			items=[
				{"item_code": self.gold.name, "qty": 999, "uom": "Gram", "rate": 0},
				{
					"item_code": self.box.name,
					"qty": 4,
					"uom": "Nos",
					"rate": 9,
					"operation": "Packing",
				},
			],
			metal_detail=[{"item_variant": self.gold.name, "quantity": 0.001}],
			other_detail=[{"item_code": self.cord.name, "quantity": 2}],
		)
		manual = doc.items[1]
		self.rebuild(doc)
		self.assertIs(doc.items[0], manual)
		self.assertEqual((manual.qty, manual.rate, manual.operation), (4, 9, "Packing"))
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items[1:]],
			[
				(self.gold.name, 0.001, "Gram"),
				(self.cord.name, 2, "Meter"),
			],
		)

	def test_case_equivalent_links_match_scalar_database_lookup(self):
		alias = self.gold.name.lower()
		expected = frappe.db.get_value("Item", alias, "stock_uom")
		self.assertEqual(
			expected, "Gram", "Fixture requires case-insensitive Item name collation"
		)
		doc = self.make_bom(metal_detail=[{"item_variant": alias, "quantity": 2}])
		self.rebuild(doc)
		self.assertEqual((doc.items[0].item_code, doc.items[0].uom), (alias, expected))

	def test_accent_equivalent_links_match_scalar_database_lookup(self):
		item = self.insert_item("Café", "Meter")
		alias = item.name.replace("Café", "Cafe")
		expected = frappe.db.get_value("Item", alias, "stock_uom")
		self.assertEqual(
			expected, "Meter", "Fixture requires accent-insensitive Item name collation"
		)
		doc = self.make_bom(other_detail=[{"item_code": alias, "quantity": 2}])
		self.rebuild(doc)
		self.assertEqual(doc.items[0].uom, expected)

	def test_missing_item_retains_missing_metadata(self):
		doc = self.make_bom(
			metal_detail=[{"item_variant": self.prefix + " Missing", "quantity": 2}]
		)
		self.rebuild(doc)
		self.assertIsNone(doc.items[0].uom)

	def test_weight_master_change_is_visible_next_call(self):
		for uom in ["Meter", "Foot"]:
			frappe.db.set_value("Item", self.cord.name, "stock_uom", uom)
			doc = self.make_bom(
				other_detail=[{"item_code": self.cord.name, "quantity": 2}]
			)
			self.rebuild(doc)
			self.assertEqual(doc.items[0].uom, uom)

	def test_precision_changes_persist_independently_after_reload(self):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"name": self.prefix + " Customer",
				"customer_name": self.prefix + " Customer",
				"customer_type": "Company",
				"custom_consider_2_digit_for_bom": 1,
				"custom_consider_2_digit_for_diamond": 0,
				"custom_consider_2_digit_for_gemstone": 1,
			}
		)
		customer.db_insert()
		doc = self.make_bom(
			customer=customer.name,
			docstatus=1,
			doc_pricision=2,
			diamond_pricision=2,
			gemstone_pricision=2,
		)
		doc.db_insert()
		with (
			patch.object(bom_events, "set_bom_rate"),
			patch.object(bom_events, "calculate_total"),
		):
			bom_events.on_update_after_submit(doc, "on_update_after_submit")
			doc.reload()
			self.assertEqual(
				tuple(
					int(doc.get(field))
					for field in [
						"doc_pricision",
						"diamond_pricision",
						"gemstone_pricision",
					]
				),
				(2, 3, 2),
			)
			frappe.db.set_value(
				"Customer",
				customer.name,
				{
					"custom_consider_2_digit_for_bom": 0,
					"custom_consider_2_digit_for_diamond": 1,
					"custom_consider_2_digit_for_gemstone": 0,
				},
			)
			bom_events.on_update_after_submit(doc, "on_update_after_submit")
			doc.reload()
			self.assertEqual(
				tuple(
					int(doc.get(field))
					for field in [
						"doc_pricision",
						"diamond_pricision",
						"gemstone_pricision",
					]
				),
				(3, 2, 3),
			)
