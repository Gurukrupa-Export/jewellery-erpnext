"""Real insert/save/open/submit checks for BOM and Item.

These exercise the controllers and installed app hooks without mocking them; every
test rolls back. They are not browser, concurrency or remote-sync tests.
"""

from uuid import uuid4

import frappe
from frappe.desk.form.load import getdoc
from frappe.desk.form.save import savedocs
from frappe.tests import IntegrationTestCase
from erpnext.controllers.item_variant import create_variant


class TestBOMItemLifecycle(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Administrator")
		self.prefix = f"_Test BI Lifecycle {uuid4().hex[:8]}"
		if not frappe.db.exists("Attribute Value", "New Design"):
			frappe.get_doc(
				{"doctype": "Attribute Value", "attribute_value": "New Design"}
			).insert()
		self.uom = frappe.get_doc(
			{
				"doctype": "UOM",
				"uom_name": self.prefix + " Unit",
				"must_be_whole_number": 0,
			}
		).insert()
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "All Item Groups",
					"is_group": 1,
				}
			).insert()
		self.group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": self.prefix + " Group",
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()

	def item(self, suffix, **values):
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": self.prefix + " " + suffix,
				"item_name": self.prefix + " " + suffix,
				"item_group": self.group.name,
				"stock_uom": self.uom.name,
				"is_stock_item": 1,
				"include_item_in_manufacturing": 1,
				"valuation_rate": 7,
				# Not a sales Item: India Compliance would otherwise demand an HSN code.
				"is_sales_item": 0,
				**values,
			}
		).insert()

	def company(self):
		if not frappe.db.exists("Warehouse Type", "Transit"):
			frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert()
		return frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": self.prefix + " Company",
				"abbr": "BL" + uuid4().hex[:3],
				"country": "India",
				"default_currency": "INR",
				"chart_of_accounts": "Standard",
				"enable_perpetual_inventory": 0,
			}
		).insert()

	def bom(self, bom_type="Sales Order"):
		company = self.company()
		finished = self.item("Finished")
		material = self.item("Material")
		doc = frappe.get_doc(
			{
				"doctype": "BOM",
				"company": company.name,
				"item": finished.name,
				"bom_type": bom_type,
				"quantity": 1,
				"currency": "INR",
				"rm_cost_as_per": "Valuation Rate",
				"is_active": 1,
				"is_default": 1,
				"items": [
					{
						"item_code": material.name,
						"qty": 2,
						"uom": self.uom.name,
						"conversion_factor": 1,
						"rate": 7,
					}
				],
			}
		).insert()
		return doc, material

	def open_doc(self, doc):
		frappe.local.response = frappe._dict(docs=[])
		getdoc(doc.doctype, doc.name)
		self.assertEqual(len(frappe.response.docs), 1)
		result = frappe.response.docs[0]
		self.assertEqual(result.name, doc.name)
		self.assertIn("docinfo", frappe.response)
		return result

	def save_response(self, doc):
		frappe.local.response = frappe._dict(docs=[])
		savedocs(frappe.as_json(doc), "Save")
		self.assertEqual(len(frappe.response.docs), 1)
		self.assertIn("docinfo", frappe.response)
		return frappe.response.docs[0]

	def test_item_insert_save_open_and_response_onload(self):
		doc = self.item("Ordinary")
		loaded = self.open_doc(doc)
		self.assertIn("stock_exists", loaded.get("__onload"))
		self.assertIn("asset_exists", loaded.get("__onload"))
		doc.item_name = self.prefix + " Renamed display"
		response = self.save_response(doc)
		self.assertEqual(response["item_name"], doc.item_name)
		self.assertIn("stock_exists", response["__onload"])
		self.assertEqual(frappe.get_doc("Item", doc.name).item_name, doc.item_name)
		self.assertEqual(frappe.db.count("Item", {"name": doc.name}), 1)

	def test_item_stale_save_rejected_without_overwriting_first_edit(self):
		doc = self.item("Stale")
		first = frappe.get_doc("Item", doc.name)
		second = frappe.get_doc("Item", doc.name)
		first.item_name = "First intentional edit"
		first.save()
		second.item_name = "Stale edit"
		with self.assertRaises(frappe.TimestampMismatchError):
			second.save()
		self.assertEqual(
			frappe.get_doc("Item", doc.name).item_name, "First intentional edit"
		)

	def test_item_schema_has_no_submit_action(self):
		self.assertFalse(frappe.get_meta("Item").is_submittable)
		self.assertTrue(frappe.get_meta("BOM").is_submittable)

	def test_bom_insert_save_open_and_response_onload(self):
		doc, material = self.bom()
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items],
			[(material.name, 2, self.uom.name)],
		)
		self.assertEqual(doc.total_cost, 14)
		loaded = self.open_doc(doc)
		self.assertIn("use_multi_level_bom", loaded.get("__onload"))
		doc.items[0].qty = 3
		response = self.save_response(doc)
		self.assertEqual(response["total_cost"], 21)
		self.assertIn("use_multi_level_bom", response["__onload"])
		doc.reload()
		self.assertEqual((doc.items[0].qty, doc.total_cost), (3, 21))
		self.assertEqual(len(doc.exploded_items), 1)
		self.assertEqual(doc.exploded_items[0].stock_qty, 3)

	def test_bom_stale_save_rejected_without_overwriting_children(self):
		doc, _ = self.bom()
		first = frappe.get_doc("BOM", doc.name)
		second = frappe.get_doc("BOM", doc.name)
		first.items[0].qty = 3
		first.save()
		second.items[0].qty = 8
		with self.assertRaises(frappe.TimestampMismatchError):
			second.save()
		self.assertEqual(frappe.get_doc("BOM", doc.name).items[0].qty, 3)

	def test_bom_submit_and_permitted_update(self):
		doc, _ = self.bom()
		doc.submit()
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(frappe.get_doc("Item", doc.item).default_bom, doc.name)
		doc.is_active = 0
		doc.save()
		doc.reload()
		self.assertEqual((doc.docstatus, doc.is_active), (1, 0))
		self.assertEqual(
			(
				int(doc.doc_pricision),
				int(doc.diamond_pricision),
				int(doc.gemstone_pricision),
			),
			(3, 3, 3),
		)

	def attribute_value(self, suffix):
		name = self.prefix + " " + suffix
		frappe.get_doc({"doctype": "Attribute Value", "attribute_value": name}).insert()
		return name

	def test_submit_rebuild_totals_repeated_metal_and_replaces_other_rows(self):
		doc, material = self.bom()
		gold = self.item("Gold")
		cord = self.item("Cord")
		metal = {
			# The row's Item link defaults to the "M" system item, absent on a test site.
			"item": self.item("Metal").name,
			"metal_type": self.attribute_value("Metal Type"),
			"metal_colour": self.attribute_value("Metal Colour"),
			"metal_purity": self.attribute_value("Metal Purity"),
			"stock_uom": self.uom.name,
			"item_variant": gold.name,
		}
		doc.append("metal_detail", {**metal, "quantity": 2})
		doc.append("metal_detail", {**metal, "quantity": 3})
		doc.append(
			"other_detail",
			{"item_code": cord.name, "qty": 1.5, "quantity": 1.5, "uom": self.uom.name},
		)
		# A Cord row left from an earlier rebuild must be replaced, not kept beside the new one.
		doc.append(
			"items",
			{
				"item_code": cord.name,
				"qty": 1.5,
				"uom": self.uom.name,
				"conversion_factor": 1,
			},
		)
		doc.save()
		doc.submit()
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items],
			[
				(material.name, 2, self.uom.name),
				(gold.name, 5, self.uom.name),
				(cord.name, 1.5, self.uom.name),
			],
		)
		# Valuation rate 7 for every Item: (2 + 5 + 1.5) x 7.
		self.assertEqual(doc.raw_material_cost, 59.5)

	def test_template_submit_rejected_and_savepoint_rollback_preserves_draft(self):
		doc, material = self.bom("Template")
		frappe.db.set_single_value("Jewellery Settings", "defualt_item", material.name)
		before = [(r.name, r.item_code, r.qty) for r in doc.items]
		frappe.db.savepoint("before_template_submit")
		with self.assertRaisesRegex(
			frappe.ValidationError, "Template BOM Can't Be Submitted"
		):
			doc.submit()
		frappe.db.rollback(save_point="before_template_submit")
		doc.reload()
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual([(r.name, r.item_code, r.qty) for r in doc.items], before)
		self.assertFalse(frappe.db.get_value("Item", doc.item, "default_bom"))

	def template_with_numeric_attribute(self):
		attribute = frappe.get_doc(
			{
				"doctype": "Item Attribute",
				"attribute_name": self.prefix + " Length",
				"numeric_values": 1,
				"from_range": 1,
				"to_range": 100,
				"increment": 1,
			}
		).insert()
		subcategory = frappe.get_doc(
			{
				"doctype": "Attribute Value",
				"attribute_value": self.prefix + " Subcategory",
				"is_subcategory": 1,
				"item_attributes": [
					{"item_attribute": attribute.name, "in_item_variant": 1}
				],
			}
		).insert()
		settings = frappe.get_single("Item Variant Settings")
		settings.do_not_update_variants = 0
		# Add to, never replace, the copied fields: other apps register theirs here.
		copied = {row.field_name for row in settings.fields}
		for field in [
			"min_order_qty",
			"is_stock_item",
			"include_item_in_manufacturing",
			"is_sales_item",
		]:
			if field not in copied:
				settings.append("fields", {"field_name": field})
		settings.save()
		template = self.item(
			"Template",
			has_variants=1,
			is_stock_item=0,
			variant_based_on="Item Attribute",
			subcategory=subcategory.name,
			min_order_qty=1,
		)
		self.assertEqual(
			[
				(r.attribute, r.numeric_values, r.from_range, r.to_range, r.increment)
				for r in template.attributes
			],
			[(attribute.name, 1, 1, 100, 1)],
		)
		return template, attribute

	def assert_variant_propagation(self, count):
		template, attribute = self.template_with_numeric_attribute()
		names = []
		for value in range(1, count + 1):
			variant = create_variant(template.name, {attribute.name: value})
			variant.insert()
			names.append(variant.name)
		self.assertEqual(frappe.db.count("Item", {"variant_of": template.name}), count)
		template.min_order_qty = 2
		template.save()
		values = frappe.get_all(
			"Item",
			filters={"variant_of": template.name},
			fields=["name", "min_order_qty"],
		)
		self.assertEqual({row.name for row in values}, set(names))
		self.assertTrue(all(row.min_order_qty == 2 for row in values))
		self.assertEqual(len(template.attributes), 1)

	def test_template_zero_variants_preserves_initialized_attributes_on_save(self):
		self.assert_variant_propagation(0)

	def test_template_30_variants_complete_synchronously(self):
		self.assert_variant_propagation(30)

	def test_template_31_variants_complete_inline_under_test_flag(self):
		# ERPNext deliberately passes now=frappe.in_test above 30; this validates
		# final data only, not after-commit dispatch, queue age or worker recovery.
		self.assertTrue(frappe.in_test)
		self.assert_variant_propagation(31)
