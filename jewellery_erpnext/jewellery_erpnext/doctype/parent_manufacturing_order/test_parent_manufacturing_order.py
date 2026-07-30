# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.test_manufacturing_plan import (
	create_sales_order,
	manufacturing_plan_creation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	get_item_code,
	validate_mfg_date,
)


class TestParentManufacturingOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

	def test_parent_manufacturing_order(self):
		create_man_plan(self)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		bom = frappe.get_doc("Tracking Bom", pmo.custom_tracking_bom)
		pmo.diamond_department = self.department
		pmo.gemstone_department = self.department
		pmo.manufacturer = "Shubh"
		pmo.save()
		pmo.submit()
		mr = 0
		if bom.metal_detail:
			mr += 1
		if bom.finding_detail:
			mr += 1
		if bom.diamond_detail:
			mr += 1
		if bom.gemstone_detail:
			mr += 1

		self.assertEqual(
			mr,
			len(
				frappe.get_all(
					"Material Request", filters={"manufacturing_order": pmo.name}
				)
			),
		)
		mwo = 1 + len(bom.metal_detail)
		for row in bom.finding_detail:
			if row.finding_category == "Chains":
				mwo += 1

		mwo_list = frappe.get_all(
			"Manufacturing Work Order", filters={"manufacturing_order": pmo.name}
		)

		for wo in mwo_list:
			mwo = frappe.get_doc("Manufacturing Work Order", wo.name)
			self.assertEqual(pmo.branch, mwo.branch)
			self.assertEqual(pmo.master_bom, mwo.master_bom)
			self.assertEqual(pmo.manufacturer, mwo.manufacturer)
			self.assertEqual(pmo.diamond_grade, mwo.diamond_grade)
			self.assertEqual(pmo.metal_touch, mwo.metal_touch)
			self.assertEqual(pmo.metal_purity, mwo.metal_purity)
			self.assertEqual(pmo.name, mwo.manufacturing_order)
			self.assertEqual(pmo.manufacturing_plan, mwo.manufacturing_plan)

	def _finding_work_order_creation(self):
		man_plan = create_man_plan(self)
		pmo = frappe.get_doc(
			"Parent Manufacturing Order", {"manufacturing_plan": man_plan.name}
		)
		bom = frappe.get_doc("Tracking Bom", pmo.custom_tracking_bom)
		bom.append(
			"finding_detail",
			{
				"metal_type": "Gold",
				"metal_touch": "22KT",
				"metal_purity": "91.9",
				"metal_colour": "Yellow",
				"finding_category": "Chains",
				"finding_type": "Kodi Chain",
				"finding_size": "2.50 MM",
				"quantity": 0.916,
			},
		)
		bom.save()
		pmo.diamond_department = self.department
		pmo.gemstone_department = self.department
		pmo.manufacturer = "Shubh"
		pmo.save()
		pmo.submit()
		mr = 0
		if bom.metal_detail:
			mr += 1
		if bom.finding_detail:
			mr += 1
		if bom.diamond_detail:
			mr += 1
		if bom.gemstone_detail:
			mr += 1

		self.assertEqual(
			mr,
			len(
				frappe.get_all(
					"Material Request", filters={"manufacturing_order": pmo.name}
				)
			),
		)
		mwo = 1 + len(bom.metal_detail)
		for row in bom.finding_detail:
			if row.finding_category == "Chains":
				mwo += 1

		mwo_list = frappe.get_all(
			"Manufacturing Work Order", filters={"manufacturing_order": pmo.name}
		)
		self.assertEqual(len(mwo_list), mwo)

		for wo in mwo_list:
			mwo = frappe.get_doc("Manufacturing Work Order", wo.name)
			self.assertEqual(pmo.branch, mwo.branch)
			self.assertEqual(pmo.master_bom, mwo.master_bom)
			self.assertEqual(pmo.manufacturer, mwo.manufacturer)
			self.assertEqual(pmo.diamond_grade, mwo.diamond_grade)
			self.assertEqual(pmo.metal_touch, mwo.metal_touch)
			self.assertEqual(pmo.metal_purity, mwo.metal_purity)
			self.assertEqual(pmo.name, mwo.manufacturing_order)
			self.assertEqual(pmo.manufacturing_plan, mwo.manufacturing_plan)

	def test_manufacturing_work_order_creation_with_multicolour(self):
		create_man_plan(self)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		bom = frappe.get_doc("Tracking Bom", pmo.custom_tracking_bom)
		bom.append(
			"metal_detail",
			{
				"metal_type": "Gold",
				"metal_touch": "22KT",
				"metal_purity": "91.6",
				"metal_colour": "Pink",
				"quantity": 0.916,
			},
		)

		bom.save()
		pmo.diamond_department = self.department
		pmo.gemstone_department = self.department
		pmo.manufacturer = "Shubh"
		pmo.save()
		pmo.submit()
		mr = 0
		if bom.metal_detail:
			mr += 1
		if bom.finding_detail:
			mr += 1
		if bom.diamond_detail:
			mr += 1
		if bom.gemstone_detail:
			mr += 1

		self.assertEqual(
			mr,
			len(
				frappe.get_all(
					"Material Request", filters={"manufacturing_order": pmo.name}
				)
			),
		)
		mwo_list = frappe.get_all(
			"Manufacturing Work Order",
			filters={"manufacturing_order": pmo.name},
			fields=["name", "metal_colour", "multicolour", "allowed_colours"],
		)
		mwo = 1 + len(bom.metal_detail)
		for row in bom.finding_detail:
			if row.finding_category == "Chains":
				mwo += 1

		self.assertEqual(len(mwo_list), mwo)

		colours = []
		for wo in mwo_list:
			if wo.multicolour:
				colours.append(wo.metal_colour[0])
		colours = "".join(sorted(colours))

		for wo in mwo_list:
			mwo = frappe.get_doc("Manufacturing Work Order", wo.name)
			if wo.multicolour:
				self.assertEqual(colours, wo.allowed_colours)
			self.assertEqual(pmo.branch, mwo.branch)
			self.assertEqual(pmo.master_bom, mwo.master_bom)
			self.assertEqual(pmo.manufacturer, mwo.manufacturer)
			self.assertEqual(pmo.diamond_grade, mwo.diamond_grade)
			self.assertEqual(pmo.metal_touch, mwo.metal_touch)
			self.assertEqual(pmo.metal_purity, mwo.metal_purity)
			self.assertEqual(pmo.name, mwo.manufacturing_order)
			self.assertEqual(pmo.manufacturing_plan, mwo.manufacturing_plan)

	def test_validate_mfg_date_throws_on_invalid_dates(self):
		pmo = frappe.new_doc("Parent Manufacturing Order")
		pmo.company = "Test_Company"
		pmo.delivery_date = "2024-01-10"
		pmo.manufacturing_end_date = "2024-01-15"
		pmo.manufacturer = "Shubh"
		pmo.qty = 1
		pmo.insert()

		with self.assertRaises(frappe.ValidationError):
			validate_mfg_date(pmo)

	def test_get_item_code_returns_item_code(self):
		with patch("frappe.db.get_value", return_value="ITEM-001"):
			self.assertEqual(get_item_code("SO-ITEM-1"), "ITEM-001")

	def test_create_material_requests_throws_when_no_bom(self):
		pmo = frappe.new_doc("Parent Manufacturing Order")
		pmo.company = "Test_Company"
		pmo.manufacturer = "Shubh"
		pmo.item_code = "ITEM-001"
		pmo.qty = 1
		pmo.delivery_date = "2024-12-31"
		pmo.insert()

		with self.assertRaises(frappe.ValidationError):
			pmo.create_material_requests()

	def test_create_material_requests_throws_when_warehouse_config_missing(self):
		if not frappe.db.exists("Item", "ITEM-001"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "ITEM-001",
					"item_name": "ITEM-001",
					"stock_uom": "Nos",
					"designer": "Administrator",
					"is_design_code": 0,
					"item_group": "Test_Item_Group",
				}
			)
			item.flags.ignore_validate = True
			item.insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "M-ITEM"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "M-ITEM",
					"item_name": "M-ITEM",
					"stock_uom": "Nos",
					"designer": "Administrator",
					"is_design_code": 0,
					"item_group": "Test_Item_Group",
				}
			)
			item.insert(ignore_permissions=True)
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": "ITEM-001",
				"company": "Test_Company",
			}
		)
		bom.append("items", {"item_code": "ITEM-001", "qty": 1, "rate": 1000})
		bom.append("items", {"item_code": "M-ITEM", "qty": 1})
		bom.insert()

		pmo = frappe.new_doc("Parent Manufacturing Order")
		pmo.company = "Test_Company"
		pmo.manufacturer = "Shubh"
		pmo.item_code = "ITEM-001"
		pmo.qty = 1
		pmo.delivery_date = "2024-12-31"
		pmo.master_bom = bom.name
		pmo.insert()

		with self.assertRaises(frappe.ValidationError):
			pmo.create_material_requests()

	def test_create_material_requests_throws_missing_default_gemstone(self):
		create_man_plan(self)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		bom = frappe.get_doc("Tracking Bom", pmo.custom_tracking_bom)

		if not frappe.db.exists("Item", "G-TEST-GEM"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "G-TEST-GEM",
					"item_name": "G-TEST-GEM",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)

		bom.append(
			"gemstone_detail",
			{
				"item_variant": "G-TEST-GEM",
				"quantity": 1,
			},
		)
		bom.flags.ignore_links = True
		bom.flags.ignore_mandatory = True
		bom.flags.ignore_validate = True
		if bom.customer:
			frappe.db.set_value(
				"Customer", bom.customer, "custom_gemstone_price_list_type", "Fixed"
			)
		bom.save()
		pmo.diamond_department = self.department
		pmo.gemstone_department = self.department
		pmo.manufacturer = "Shubh"
		pmo.save()

		if frappe.db.exists("Manufacturing Setting", "Shubh"):
			frappe.db.set_value(
				"Manufacturing Setting", "Shubh", "default_gemstone_item", ""
			)

		if not frappe.db.exists(
			"Variant based Warehouse", {"parent": "Shubh", "variant": "G"}
		):
			doc = frappe.get_doc("Manufacturer", "Shubh")
			doc.append(
				"custom_reservation_table",
				{
					"variant": "G",
					"department": self.department,
					"target_warehouse": self.warehouse,
				},
			)
			doc.save(ignore_permissions=True)

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
			get_item_type as real_get_item_type,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.get_item_type"
		) as mock_get_item_type:

			def side_effect(item_code):
				if item_code == "G-TEST-GEM":
					return "gemstone_item"
				return real_get_item_type(item_code)

			mock_get_item_type.side_effect = side_effect

			with self.assertRaises(frappe.ValidationError) as ctx:
				pmo.create_material_requests()

			self.assertTrue("Default Gemstone Item is not set" in str(ctx.exception))

	def test_create_material_requests_uses_default_gemstone(self):
		create_man_plan(self)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		bom = frappe.get_doc("Tracking Bom", pmo.custom_tracking_bom)

		if not frappe.db.exists("Item", "G-TEST-GEM"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "G-TEST-GEM",
					"item_name": "G-TEST-GEM",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)

		bom.append(
			"gemstone_detail",
			{
				"item_variant": "G-TEST-GEM",
				"quantity": 1,
			},
		)
		bom.flags.ignore_links = True
		bom.flags.ignore_mandatory = True
		bom.flags.ignore_validate = True
		if bom.customer:
			frappe.db.set_value(
				"Customer", bom.customer, "custom_gemstone_price_list_type", "Fixed"
			)
		bom.save()
		pmo.diamond_department = self.department
		pmo.gemstone_department = self.department
		pmo.manufacturer = "Shubh"
		pmo.save()

		if frappe.db.exists("Manufacturing Setting", "Shubh"):
			frappe.db.set_value(
				"Manufacturing Setting",
				"Shubh",
				"default_gemstone_item",
				"G-PER-DUM-PRE-CC",
			)
		else:
			frappe.get_doc(
				{
					"doctype": "Manufacturing Setting",
					"manufacturer": "Shubh",
					"default_gemstone_item": "G-PER-DUM-PRE-CC",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)

		if not frappe.db.exists(
			"Variant based Warehouse", {"parent": "Shubh", "variant": "G"}
		):
			doc = frappe.get_doc("Manufacturer", "Shubh")
			doc.append(
				"custom_reservation_table",
				{
					"variant": "G",
					"department": self.department,
					"target_warehouse": self.warehouse,
				},
			)
			doc.save(ignore_permissions=True)

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
			get_item_type as real_get_item_type,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.get_item_type"
		) as mock_get_item_type:

			def side_effect(item_code):
				if item_code == "G-TEST-GEM":
					return "gemstone_item"
				return real_get_item_type(item_code)

			mock_get_item_type.side_effect = side_effect

			pmo.create_material_requests()

		mr_list = frappe.get_all(
			"Material Request", filters={"manufacturing_order": pmo.name}
		)
		self.assertTrue(len(mr_list) > 0)

		found = False
		for mr_name in mr_list:
			mr = frappe.get_doc("Material Request", mr_name.name)
			for item in mr.items:
				if (
					item.item_code == "G-PER-DUM-PRE-CC"
					and item.description == "G-TEST-GEM"
				):
					found = True
					break
			if found:
				break

		self.assertTrue(
			found,
			"Material Request item for gemstone should use default item code and original item code as description",
		)

	def tearDown(self):
		return super().tearDown()


def create_man_plan(self):
	create_sales_order(self)
	doc = frappe.new_doc("Manufacturing Plan")
	doc.select_manufacture_order = "Manufacturing"
	man_plan = manufacturing_plan_creation(doc)
	man_plan.company = "Test_Company"
	man_plan.branch = self.branch
	if man_plan.setting_type:
		man_plan.setting_type = "Close"
	man_plan.is_subcontracting = "No"
	man_plan.save()
	man_plan.submit()
	return man_plan
