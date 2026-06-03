# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.test_manufacturing_plan import (
	create_sales_order,
	manufacturing_plan_creation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	get_item_code,
	validate_mfg_date,
)


class TestParentManufacturingOrder(FrappeTestCase):
	def setUp(self):
		create_test_data()
		self.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		self.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		self.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)
		return super().setUp()

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

	def _validate_mfg_date_throws_on_invalid_dates(self):
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
	man_plan.save()
	man_plan.submit()
	return man_plan
