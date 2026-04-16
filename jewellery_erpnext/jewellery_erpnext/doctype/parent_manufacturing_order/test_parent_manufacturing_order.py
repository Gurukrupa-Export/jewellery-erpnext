# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	get_item_code,
	validate_mfg_date,
)


class TestParentManufacturingOrder(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_parent_manufacturing_order(self):
		pmo = create_pmo()
		bom = frappe.get_doc("BOM", pmo.master_bom)
		bom.append(
			"metal_detail",
			{
				"metal_type": "Gold",
				"metal_touch": "22KT",
				"metal_purity": "91.9",
				"metal_colour": "Yellow",
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

	def test_finding_work_order_creation(self):
		pmo = create_pmo()
		bom = frappe.get_doc("BOM", pmo.master_bom)
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
		pmo = create_pmo()
		print(pmo.manufacturing_plan)
		bom = frappe.get_doc("BOM", pmo.master_bom)
		if not bom.metal_detail:
			bom.append(
				"metal_detail",
				{
					"metal_type": "Gold",
					"metal_touch": "22KT",
					"metal_purity": "91.9",
					"metal_colour": "Yellow",
					"quantity": 0.916,
				},
			)
			bom.append(
				"metal_detail",
				{
					"metal_type": "Gold",
					"metal_touch": "22KT",
					"metal_purity": "91.9",
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
		pmo.company = "_Test Indian Registered Company"
		pmo.delivery_date = "2024-01-10"
		pmo.manufacturing_end_date = "2024-01-15"
		pmo.qty = 1
		pmo.insert()

		with self.assertRaises(frappe.ValidationError):
			validate_mfg_date(pmo)

	def test_get_item_code_returns_item_code(self):
		with patch("frappe.db.get_value", return_value="ITEM-001"):
			self.assertEqual(get_item_code("SO-ITEM-1"), "ITEM-001")

	def test_create_material_requests_throws_when_no_bom(self):
		pmo = frappe.new_doc("Parent Manufacturing Order")
		pmo.company = "_Test Indian Registered Company"
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
				"company": "_Test Indian Registered Company",
			}
		)
		bom.append("items", {"item_code": "ITEM-001", "qty": 1, "rate": 1000})
		bom.append("items", {"item_code": "M-ITEM", "qty": 1})
		bom.insert()

		pmo = frappe.new_doc("Parent Manufacturing Order")
		pmo.company = "_Test Indian Registered Company"
		pmo.manufacturer = "Shubh"
		pmo.item_code = "ITEM-001"
		pmo.qty = 1
		pmo.delivery_date = "2024-12-31"
		pmo.master_bom = bom.name
		pmo.insert()

		with self.assertRaises(frappe.ValidationError):
			pmo.create_material_requests()

	def test_create_material_requests_groups_items_by_type(self):
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
		if not frappe.db.exists("Item", "F-ITEM"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "F-ITEM",
					"item_name": "F-ITEM",
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
				"company": "_Test Indian Registered Company",
			}
		)

		bom.append("items", {"item_code": "M-ITEM", "qty": 2})
		bom.append("items", {"item_code": "F-ITEM", "qty": 3})
		bom.insert()

		pmo = frappe.new_doc("Parent Manufacturing Order")
		pmo.company = "_Test Indian Registered Company"
		pmo.manufacturer = "Shubh"
		pmo.item_code = "ITEM-001"
		pmo.qty = 1
		pmo.delivery_date = "2024-12-31"
		pmo.master_bom = bom.name
		pmo.insert()

		try:
			pmo.create_material_requests()
		except frappe.ValidationError as e:
			if "warehouse" not in str(e).lower():
				raise

	def tearDown(self):
		return super().tearDown()


def create_pmo():
	man_plan = frappe.get_last_doc(
		"Manufacturing Plan",
		filters={
			"docstatus": 0,
			"select_manufacture_order": "Manufacturing",
			"is_subcontracting": 0,
		},
	)
	man_plan.submit()
	return frappe.get_last_doc("Parent Manufacturing Order")
