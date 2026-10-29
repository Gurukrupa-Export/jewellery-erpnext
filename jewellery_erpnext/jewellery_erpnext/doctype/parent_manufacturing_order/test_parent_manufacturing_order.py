# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.customer_product_tolerance_master.customer_product_tolerance_master import (
	CustomerProductToleranceMaster,
)
from jewellery_erpnext.jewellery_erpnext.doctype.customer_product_tolerance_master.tolerance_utils import (
	group_tolerance_rows,
	metal_group_key,
	pick_tolerance_row,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.test_manufacturing_plan import (
	create_sales_order,
	manufacturing_plan_creation,
	open_manufacture_orders,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query import (
	custom_override_grades,
	get_diamond_grade,
	is_customer_diamond_flag,
	pick_diamond_grade,
	resolve_diamond_grade,
	sales_type_expects,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.utils import (
	resolve_parent_chains,
	update_parent_details,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
	_get_chain_finding_rows,
	_get_grade_free_key,
	_get_requested_qty_by_item,
	_requirement_key,
	_validate_chain_findings_are_manufacturing_items,
	add_material_request_comment,
	can_create_pmo_material_request,
	check_pmo_material_request_role,
	check_tracking_bom_is_draft,
	create_pmo_material_request,
	get_item_code,
	get_pending_raw_materials,
	set_diamond_tolerance_table,
	set_metal_tolerance_table,
	tracking_bom_is_draft,
	validate_material_request_items,
	validate_mfg_date,
)

PMO_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order"
PMO_UTILS = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.utils"

_UNSET = object()


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
		man_plan.setting_type = "Nova Glow"
	man_plan.is_subcontracting = "No"
	with open_manufacture_orders():
		man_plan.save()
	man_plan.submit()
	return man_plan


class FakeToleranceMaster(frappe._dict):
	pass


class FakePMO(frappe._dict):
	"""Just enough Document surface for the tolerance populators."""

	def set(self, key, value):
		self[key] = value

	def append(self, key, value):
		self.setdefault(key, []).append(frappe._dict(value))


def _metal_row(**kwargs):
	row = frappe._dict(
		weight_type="Net Weight",
		metal_type=None,
		range_type="",
		tolerance_range=0,
		from_weight=0,
		to_weight=0,
		plus_percent=0,
		minus_percent=0,
	)
	row.update(kwargs)
	return row


class TestToleranceBandSelection(UnitTestCase):
	"""Only the master row whose band covers the BOM weight may reach the PMO."""

	def _run_metal(self, master_rows, bom_gross=0.0, bom_net=15.0, customer="CUST"):
		pmo = FakePMO(
			name="PMO-TEST-0001",
			doctype="Parent Manufacturing Order",
			customer=customer,
			custom_tracking_bom="TB-0001",
			gross_weight=0.0,
			net_weight=0.0,
			metal_product_tolerance=[],
		)
		master = FakeToleranceMaster(metal_tolerance_table=master_rows)
		bom = frappe._dict(gross_weight=bom_gross, metal_and_finding_weight=bom_net)

		def fake_get_doc(doctype, name):
			return master if doctype == "Customer Product Tolerance Master" else bom

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
				"parent_manufacturing_order.frappe.db.get_value",
				return_value="PTM-TEST-0001",
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
				"parent_manufacturing_order.frappe.get_doc",
				side_effect=fake_get_doc,
			),
		):
			set_metal_tolerance_table(pmo)
		return pmo.metal_product_tolerance

	def test_only_the_covering_band_reaches_the_pmo(self):
		"""The reported bug: 15 g against 0-50 @7% and 51-100 @5% must yield ONE row."""
		rows = self._run_metal(
			[
				_metal_row(
					from_weight=0, to_weight=50, plus_percent=7, minus_percent=7
				),
				_metal_row(
					from_weight=51, to_weight=100, plus_percent=5, minus_percent=5
				),
			],
			bom_net=15.0,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].from_tolerance_wt, 13.95)
		self.assertEqual(rows[0].to_tolerance_wt, 16.05)
		self.assertEqual(rows[0].standard_tolerance_wt, 15.0)
		self.assertEqual(rows[0].from_weight, 0)
		self.assertEqual(rows[0].to_weight, 50)

	def test_higher_weight_picks_the_second_band(self):
		rows = self._run_metal(
			[
				_metal_row(
					from_weight=0, to_weight=50, plus_percent=7, minus_percent=7
				),
				_metal_row(
					from_weight=51, to_weight=100, plus_percent=5, minus_percent=5
				),
			],
			bom_net=80.0,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].from_tolerance_wt, 76.0)
		self.assertEqual(rows[0].to_tolerance_wt, 84.0)

	def test_band_upper_bound_is_inclusive_and_first_row_wins(self):
		rows = self._run_metal(
			[
				_metal_row(
					from_weight=0, to_weight=50, plus_percent=7, minus_percent=7
				),
				_metal_row(
					from_weight=50, to_weight=100, plus_percent=5, minus_percent=5
				),
			],
			bom_net=50.0,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].to_tolerance_wt, 53.5)

	def test_zero_to_weight_means_no_upper_bound(self):
		rows = self._run_metal(
			[
				_metal_row(
					from_weight=0, to_weight=50, plus_percent=7, minus_percent=7
				),
				_metal_row(
					from_weight=50, to_weight=0, plus_percent=5, minus_percent=5
				),
			],
			bom_net=5000.0,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].to_tolerance_wt, 5250.0)

	def test_gap_between_bands_throws(self):
		"""50.5 g falls between 0-50 and 51-100: master data must be corrected."""
		with self.assertRaises(frappe.ValidationError):
			self._run_metal(
				[
					_metal_row(
						from_weight=0, to_weight=50, plus_percent=7, minus_percent=7
					),
					_metal_row(
						from_weight=51, to_weight=100, plus_percent=5, minus_percent=5
					),
				],
				bom_net=50.5,
			)

	def test_gross_and_net_groups_each_yield_one_row(self):
		rows = self._run_metal(
			[
				_metal_row(
					weight_type="Gross Weight",
					from_weight=0,
					to_weight=50,
					plus_percent=10,
					minus_percent=10,
				),
				_metal_row(
					weight_type="Net Weight",
					from_weight=0,
					to_weight=50,
					plus_percent=7,
					minus_percent=7,
				),
			],
			bom_gross=20.0,
			bom_net=15.0,
		)
		self.assertEqual(len(rows), 2)
		by_type = {row.weight_type: row for row in rows}
		self.assertEqual(by_type["Gross Weight"].standard_tolerance_wt, 20.0)
		self.assertEqual(by_type["Net Weight"].standard_tolerance_wt, 15.0)

	def test_metal_types_are_independent_groups(self):
		rows = self._run_metal(
			[
				_metal_row(
					metal_type="Gold",
					from_weight=0,
					to_weight=50,
					plus_percent=7,
					minus_percent=7,
				),
				_metal_row(
					metal_type="Silver",
					from_weight=0,
					to_weight=50,
					plus_percent=5,
					minus_percent=5,
				),
			],
			bom_net=15.0,
		)
		self.assertEqual(len(rows), 2)
		self.assertEqual({row.metal_type for row in rows}, {"Gold", "Silver"})

	def test_weight_range_uses_flat_tolerance_range(self):
		rows = self._run_metal(
			[
				_metal_row(
					range_type="Weight Range",
					from_weight=0,
					to_weight=50,
					tolerance_range=2,
				)
			],
			bom_net=15.0,
		)
		self.assertEqual(rows[0].from_tolerance_wt, 13.0)
		self.assertEqual(rows[0].to_tolerance_wt, 17.0)

	def test_rebuilds_instead_of_appending(self):
		"""An amended PMO arrives with the old rows; submit must not double them."""
		master_rows = [
			_metal_row(from_weight=0, to_weight=50, plus_percent=7, minus_percent=7)
		]
		self.assertEqual(len(self._run_metal(master_rows)), 1)
		self.assertEqual(len(self._run_metal(master_rows)), 1)

	def test_no_master_leaves_the_table_untouched(self):
		pmo = FakePMO(customer="CUST", metal_product_tolerance=["existing"])
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
			"parent_manufacturing_order.frappe.db.get_value",
			return_value=None,
		):
			set_metal_tolerance_table(pmo)
		self.assertEqual(pmo.metal_product_tolerance, ["existing"])


class TestToleranceUtils(UnitTestCase):
	def test_pick_returns_none_on_gap(self):
		rows = [
			frappe._dict(from_weight=0, to_weight=50),
			frappe._dict(from_weight=51, to_weight=100),
		]
		self.assertIsNone(pick_tolerance_row(rows, 50.5))
		self.assertIsNotNone(pick_tolerance_row(rows, 50))
		self.assertIsNotNone(pick_tolerance_row(rows, 51))

	def test_bandless_row_covers_everything(self):
		rows = [frappe._dict(from_diamond=0, to_diamond=0)]
		self.assertIsNotNone(
			pick_tolerance_row(rows, 999, "from_diamond", "to_diamond")
		)

	def test_group_preserves_document_order(self):
		rows = [
			frappe._dict(weight_type="Net Weight", metal_type="Gold", idx=1),
			frappe._dict(weight_type="Net Weight", metal_type="Gold", idx=2),
			frappe._dict(weight_type="Gross Weight", metal_type="Gold", idx=3),
		]
		groups = group_tolerance_rows(rows, metal_group_key)
		self.assertEqual(len(groups), 2)
		self.assertEqual([row.idx for row in groups[("Net Weight", "Gold")]], [1, 2])


class TestToleranceMasterBandValidation(UnitTestCase):
	"""Band sanity rules on Customer Product Tolerance Master.

	Housed here rather than in test_customer_product_tolerance_master.py because CI runs
	a curated allowlist -- `--doctype "Parent Manufacturing Order"` already loads this
	module, while nothing runs the tolerance master's own test file. These rules decide
	which bands set_metal_tolerance_table can resolve, so this is their nearest home.
	"""

	def _doc(
		self, bands, table="metal_tolerance_table", frm="from_weight", to="to_weight"
	):
		doc = frappe._dict(
			metal_tolerance_table=[],
			diamond_tolerance_table=[],
			gemstone_tolerance_table=[],
		)
		doc[table] = [
			frappe._dict(
				{
					"weight_type": "Net Weight",
					"metal_type": "Gold",
					frm: a,
					to: b,
					"idx": i + 1,
				}
			)
			for i, (a, b) in enumerate(bands)
		]
		return doc

	def _validate(self, doc):
		CustomerProductToleranceMaster.validate_tolerance_bands(doc)

	def test_real_master_schedules_still_save(self):
		"""Live gk/production band shapes must not be rejected."""
		for label, bands in {
			"MHCU0008 (11 contiguous bands, some touching)": [
				(0, 1.5),
				(1.51, 3),
				(3, 5),
				(5, 10),
				(10, 15),
				(15, 25),
				(25, 50),
				(50, 75),
				(75, 100),
				(100, 150),
				(150, 99999),
			],
			"MHCU0009 (open-ended top band)": [
				(0, 4.999),
				(5, 14.999),
				(15, 49.999),
				(50, 0),
			],
			"GJCU0009 (the reported master, with a gap)": [(0, 50), (51, 100)],
		}.items():
			with self.subTest(master=label):
				self._validate(self._doc(bands))

	def test_overlapping_bands_are_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(self._doc([(0, 50), (20, 80)]))

	def test_from_greater_than_to_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(self._doc([(50, 10)]))

	def test_two_identical_bands_are_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(self._doc([(0, 50), (0, 50)]))

	def test_different_groups_may_reuse_the_same_band(self):
		"""Gold 0-50 and Silver 0-50 are independent schedules, not an overlap."""
		doc = frappe._dict(
			diamond_tolerance_table=[],
			gemstone_tolerance_table=[],
			metal_tolerance_table=[
				frappe._dict(
					weight_type="Net Weight",
					metal_type="Gold",
					from_weight=0,
					to_weight=50,
					idx=1,
				),
				frappe._dict(
					weight_type="Net Weight",
					metal_type="Silver",
					from_weight=0,
					to_weight=50,
					idx=2,
				),
				frappe._dict(
					weight_type="Gross Weight",
					metal_type="Gold",
					from_weight=0,
					to_weight=50,
					idx=3,
				),
			],
		)
		self._validate(doc)

	def test_touching_bands_are_allowed(self):
		"""...-50 and 50-... share an endpoint; pick_tolerance_row takes the first."""
		self._validate(self._doc([(0, 50), (50, 100)]))


def _bom_diamond(**kwargs):
	row = frappe._dict(
		diamond_type="Natural",
		diamond_sieve_size="+4.5-5",
		sieve_size_range="Group A",
		quantity=1.0,
		size_in_mm=1.75,
	)
	row.update(kwargs)
	return row


def _diamond_band(**kwargs):
	row = frappe._dict(
		weight_type="MM Size wise",
		diamond_type=None,
		sieve_size="+4.5-5",
		sieve_size_range=None,
		from_diamond=0,
		to_diamond=0,
		plus_percent=10,
		minus_percent=10,
	)
	row.update(kwargs)
	return row


class TestDiamondToleranceScoping(UnitTestCase):
	"""A diamond band must aggregate only the stones it is scoped to.

	No test previously exercised a populated diamond_tolerance_table at all, which is
	how the missing diamond_type filter went unnoticed.
	"""

	def _run(self, master_rows, bom_rows, diamond_weight=0.0):
		pmo = FakePMO(
			name="PMO-TEST-0001",
			doctype="Parent Manufacturing Order",
			customer="CUST",
			custom_tracking_bom="TB-0001",
			diamond_weight=diamond_weight,
			diamond_product_tolerance=[],
		)
		master = FakeToleranceMaster(diamond_tolerance_table=master_rows)
		bom = frappe._dict(diamond_detail=bom_rows)

		def fake_get_doc(doctype, name):
			return master if doctype == "Customer Product Tolerance Master" else bom

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
				"parent_manufacturing_order.frappe.db.get_value",
				return_value="PTM-TEST-0001",
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
				"parent_manufacturing_order.frappe.get_doc",
				side_effect=fake_get_doc,
			),
		):
			set_diamond_tolerance_table(pmo)
		return pmo.diamond_product_tolerance

	def test_each_type_is_measured_on_its_own_stones(self):
		"""The reported bug: both bands summed 2 + 3 = 5 cts instead of 2 and 3."""
		rows = self._run(
			[
				_diamond_band(diamond_type="Natural"),
				_diamond_band(diamond_type="LGD"),
			],
			[
				_bom_diamond(diamond_type="Natural", quantity=2.0),
				_bom_diamond(diamond_type="LGD", quantity=3.0),
			],
		)
		self.assertEqual(len(rows), 2)
		by_type = {row.diamond_type: row for row in rows}
		self.assertEqual(by_type["Natural"].standard_tolerance_wt, 2.0)
		self.assertEqual(by_type["LGD"].standard_tolerance_wt, 3.0)
		self.assertEqual(by_type["Natural"].from_tolerance_wt, 1.8)
		self.assertEqual(by_type["Natural"].to_tolerance_wt, 2.2)

	def test_band_for_a_type_absent_from_the_bom_emits_no_row(self):
		rows = self._run(
			[_diamond_band(diamond_type="LGD")],
			[_bom_diamond(diamond_type="Natural", quantity=2.0)],
		)
		self.assertEqual(rows, [])

	def test_universal_still_aggregates_every_type(self):
		rows = self._run(
			[
				_diamond_band(
					weight_type="Universal", diamond_type=None, sieve_size=None
				)
			],
			[
				_bom_diamond(diamond_type="Natural", quantity=2.0),
				_bom_diamond(diamond_type="LGD", quantity=3.0),
			],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].standard_tolerance_wt, 5.0)

	def test_weight_wise_scoped_by_type_ignores_sieve(self):
		rows = self._run(
			[
				_diamond_band(
					weight_type="Weight wise", diamond_type="Natural", sieve_size=None
				)
			],
			[
				_bom_diamond(
					diamond_type="Natural", diamond_sieve_size="+4.5-5", quantity=2.0
				),
				_bom_diamond(
					diamond_type="Natural", diamond_sieve_size="+6-7", quantity=1.0
				),
				_bom_diamond(
					diamond_type="LGD", diamond_sieve_size="+4.5-5", quantity=9.0
				),
			],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].standard_tolerance_wt, 3.0)

	def test_sieve_scope_still_applies_alongside_type(self):
		rows = self._run(
			[_diamond_band(diamond_type="Natural", sieve_size="+4.5-5")],
			[
				_bom_diamond(
					diamond_type="Natural", diamond_sieve_size="+4.5-5", quantity=2.0
				),
				_bom_diamond(
					diamond_type="Natural", diamond_sieve_size="+6-7", quantity=4.0
				),
				_bom_diamond(
					diamond_type="LGD", diamond_sieve_size="+4.5-5", quantity=8.0
				),
			],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].standard_tolerance_wt, 2.0)

	def test_size_in_mm_only_for_mm_size_wise(self):
		mm = self._run(
			[_diamond_band()],
			[_bom_diamond(quantity=2.0, size_in_mm=1.75)],
		)
		self.assertEqual(mm[0].size_in_mm, 1.75)

		for weight_type, extra in (
			("Group Size wise", {"sieve_size": None, "sieve_size_range": "Group A"}),
			("Weight wise", {"sieve_size": None}),
			("Universal", {"sieve_size": None}),
		):
			with self.subTest(weight_type=weight_type):
				rows = self._run(
					[_diamond_band(weight_type=weight_type, **extra)],
					[
						_bom_diamond(quantity=2.0, size_in_mm=1.75),
						_bom_diamond(quantity=1.0, size_in_mm=2.5),
					],
				)
				self.assertEqual(rows[0].size_in_mm, 0)


class TestParentDetailsRefCustomer(UnitTestCase):
	"""update_parent_details climbs a line's parent chain and takes Ref Customer from it.

	The climb is the point: a line's Purchase Order leads back to the PREVIOUS plan's row, and
	it is THAT row's sales order line whose quotation records the customer behind an internal
	order. This line's own quotation is only the last resort. Resolving from the current line
	alone uses the bottom rung as if it were the top, which is the bug these pin down.
	"""

	SO_ITEM = "SO-ITEM-CHILD"
	PO_ITEM = "PO-ITEM-1"
	MP_ROW = "MP-ROW-1"
	PARENT_SO_ITEM = "SO-ITEM-PARENT"
	PARENT_SALES_ORDER = "SO-PARENT"
	PARENT_MP = "MP-1"
	PURCHASE_ORDER = "PUR-ORD-1"
	OWN_QUOTATION = "QTN-OWN"

	def _patched_chain(
		self,
		quotation="QTN-1",
		quotation_ref_customer=None,
		docname=_UNSET,
		po_row=_UNSET,
		m_plan_row=_UNSET,
		po_ref_customer=None,
		own_quotation_ref_customer=None,
	):
		"""Stub the records the walk may read; docname defaults to the parent sales order item.

		Every record is pinned by name, and asking for one the fixture does not model fails the
		test outright. A stub that answered regardless of ``name`` would hand back fixture data
		for a lookup against the wrong id -- exactly the regression these exist to catch, since
		the whole point of the walk is which record each step reaches.
		"""
		m_plan_row = self.MP_ROW if m_plan_row is _UNSET else m_plan_row
		po_row = self.PO_ITEM if po_row is _UNSET else po_row
		docname = self.PARENT_SO_ITEM if docname is _UNSET else docname

		lines = {
			self.SO_ITEM: {
				"name": self.SO_ITEM,
				"custom_po_details": po_row,
				# The framework fetches the PMO's own `quotation` field from here, so the walk
				# reads it off the line rather than off the document.
				"prevdoc_docname": self.OWN_QUOTATION,
			}
		}
		if docname:
			lines[docname] = {"name": docname, "prevdoc_docname": quotation}

		tables = {
			"Sales Order Item": lines,
			"Purchase Order Item": {
				po_row: {
					"name": po_row,
					"parent": self.PURCHASE_ORDER,
					"custom_m_plan_details": m_plan_row,
				}
			}
			if po_row
			else {},
			"Manufacturing Plan Table": {
				m_plan_row: {
					"name": m_plan_row,
					"parent": self.PARENT_MP,
					"sales_order": self.PARENT_SALES_ORDER,
					"docname": docname,
				}
			}
			if m_plan_row
			else {},
			"Quotation": {
				self.OWN_QUOTATION: {
					"name": self.OWN_QUOTATION,
					"ref_customer": own_quotation_ref_customer,
				}
			},
			"Sales Order": {
				self.PARENT_SALES_ORDER: {
					"name": self.PARENT_SALES_ORDER,
					"customer": "CUST-FROM-SO",
				}
			},
			"Purchase Order": {
				self.PURCHASE_ORDER: {
					"name": self.PURCHASE_ORDER,
					"ref_customer": po_ref_customer,
				}
			},
		}
		if quotation:
			tables["Quotation"][quotation] = {
				"name": quotation,
				"ref_customer": quotation_ref_customer,
			}

		def get_all(doctype, filters=None, fields=None):
			known = tables.get(doctype, {})
			rows = []
			for name in filters["name"][1]:
				if name not in known:
					self.fail(f"unexpected lookup: {doctype} {name}")
				rows.append(frappe._dict({f: known[name].get(f) for f in fields}))
			return rows

		return patch(f"{PMO_UTILS}.frappe.get_all", side_effect=get_all)

	def test_ref_customer_comes_from_the_parent_quotation(self):
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(quotation_ref_customer="CUST-FROM-QTN"):
			update_parent_details(doc)

		self.assertEqual(doc.parent_quotation, "QTN-1")
		self.assertEqual(doc.parent_sales_order, "SO-PARENT")
		self.assertEqual(doc.parent_mp, "MP-1")
		self.assertEqual(doc.ref_customer, "CUST-FROM-QTN")

	def test_the_parent_quotation_is_not_this_lines_own_quotation(self):
		"""The regression: resolving from the current line reaches QTN-OWN, one rung too low."""
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(
			quotation_ref_customer="CUST-FROM-QTN",
			own_quotation_ref_customer="CUST-FROM-OWN-QTN",
		):
			update_parent_details(doc)

		self.assertEqual(doc.ref_customer, "CUST-FROM-QTN")

	def test_ref_customer_falls_back_to_sales_order_customer(self):
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(quotation_ref_customer=None):
			update_parent_details(doc)

		self.assertEqual(doc.parent_quotation, "QTN-1")
		self.assertEqual(doc.ref_customer, "CUST-FROM-SO")

	def test_ref_customer_falls_back_when_there_is_no_parent_quotation(self):
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(docname=None):
			update_parent_details(doc)

		self.assertIsNone(doc.parent_quotation)
		self.assertEqual(doc.ref_customer, "CUST-FROM-SO")

	def test_ref_customer_comes_from_the_purchase_order_when_the_m_plan_link_is_missing(
		self,
	):
		# Purchase Order Items raised before custom_m_plan_details existed have no link to
		# follow, so the walk stops one hop short of the manufacturing plan row
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(m_plan_row=None, po_ref_customer="CUST-FROM-PO"):
			update_parent_details(doc)

		self.assertIsNone(doc.parent_quotation)
		self.assertIsNone(doc.parent_sales_order)
		self.assertEqual(doc.ref_customer, "CUST-FROM-PO")

	def test_the_walk_follows_the_m_plan_link_it_read(self):
		# the manufacturing plan row is whatever the Purchase Order Item points at; the walk must
		# follow that link rather than any id fixed in advance
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(
			m_plan_row="MP-ROW-ALT", quotation_ref_customer="CUST-FROM-QTN"
		):
			update_parent_details(doc)

		self.assertEqual(doc.parent_mp, self.PARENT_MP)
		self.assertEqual(doc.ref_customer, "CUST-FROM-QTN")

	def test_ref_customer_comes_from_its_own_quotation_when_the_walk_never_starts(self):
		# no custom_po_details on the sales order line, so the walk exits at its first guard
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(
			po_row=None, own_quotation_ref_customer="CUST-FROM-OWN-QTN"
		):
			update_parent_details(doc)

		self.assertEqual(doc.ref_customer, "CUST-FROM-OWN-QTN")

	def test_the_parent_quotation_outranks_the_coarser_sources(self):
		# a Purchase Order carries one Ref Customer for every row on it, so the per-line
		# sources must win wherever they resolve
		doc = frappe._dict(sales_order_item=self.SO_ITEM)

		with self._patched_chain(
			quotation_ref_customer="CUST-FROM-QTN",
			po_ref_customer="CUST-FROM-PO",
			own_quotation_ref_customer="CUST-FROM-OWN-QTN",
		):
			update_parent_details(doc)

		self.assertEqual(doc.ref_customer, "CUST-FROM-QTN")

	def test_stale_parent_links_on_the_document_are_never_consulted(self):
		# parent_quotation and parent_sales_order are not read-only and survive an early exit, so
		# an earlier save can leave them behind. The walk derives every link afresh, so reading
		# either would be a lookup the stub does not recognise and the test would fail.
		doc = frappe._dict(
			sales_order_item=self.SO_ITEM,
			parent_quotation="QTN-STALE",
			parent_sales_order="SO-STALE",
		)

		with self._patched_chain(
			po_row=None, own_quotation_ref_customer="CUST-FROM-OWN-QTN"
		):
			update_parent_details(doc)

		self.assertEqual(doc.ref_customer, "CUST-FROM-OWN-QTN")

	def test_an_unresolvable_chain_leaves_ref_customer_alone(self):
		# the field is not read-only, so a save must not wipe a value set by hand
		doc = frappe._dict(sales_order_item=self.SO_ITEM, ref_customer="CUST-BY-HAND")

		with self._patched_chain(po_row=None):
			update_parent_details(doc)

		self.assertEqual(doc.ref_customer, "CUST-BY-HAND")

	def test_a_batch_resolves_every_line_the_way_one_document_would(self):
		"""Manufacturing Plan resolves a whole table through this; it must not drift."""
		with self._patched_chain(quotation_ref_customer="CUST-FROM-QTN"):
			chains = resolve_parent_chains([self.SO_ITEM, self.SO_ITEM, None])

		self.assertEqual(list(chains), [self.SO_ITEM])
		self.assertEqual(chains[self.SO_ITEM].ref_customer, "CUST-FROM-QTN")

	def test_no_lines_resolves_without_querying(self):
		with patch(f"{PMO_UTILS}.frappe.get_all") as get_all:
			self.assertEqual(resolve_parent_chains([]), {})
			self.assertEqual(resolve_parent_chains([None]), {})

		get_all.assert_not_called()

	def test_before_save_resolves_parent_details_on_insert(self):
		doc = frappe.new_doc("Parent Manufacturing Order")
		self.assertTrue(doc.is_new())

		with (
			patch(f"{PMO_MODULE}.update_parent_details") as update_parent,
			patch(f"{PMO_MODULE}.resolve_diamond_grade", return_value=None),
		):
			doc.before_save()

		update_parent.assert_called_once_with(doc)


FILTERS_QUERY = (
	"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
	"doc_events.filters_query"
)


class TestPickDiamondGrade(UnitTestCase):
	"""The rule that decides which of diamond_grade_1..4 a PMO gets.

	The two branches are deliberately asymmetric. A customer-diamond order may only take a
	grade flagged is_customer_diamond_quality, because the grade is stamped onto the tracking
	BOM and the item variant -- a wrong one there is silent. A non-customer-diamond order may
	take either kind; it only prefers the unflagged one.
	"""

	def _pick(self, grades, is_customer_diamond, flags=None):
		return pick_diamond_grade(grades, is_customer_diamond, flags=flags or {})

	def test_customer_diamond_takes_the_flagged_grade(self):
		self.assertEqual(self._pick(["A", "B"], 1, {"A": 0, "B": 1}), "B")

	def test_customer_diamond_never_substitutes_an_unflagged_grade(self):
		"""Returns None on purpose: before_save turns that into the "not mentioned" throw."""
		self.assertIsNone(self._pick(["A", "B"], 1, {"A": 0, "B": 0}))

	def test_plain_order_prefers_the_unflagged_grade(self):
		self.assertEqual(self._pick(["B", "A"], 0, {"A": 0, "B": 1}), "A")

	def test_plain_order_accepts_a_flagged_grade_when_it_is_the_only_one(self):
		"""The reported gap: this used to resolve to None and block the save."""
		self.assertEqual(self._pick(["A"], 0, {"A": 1}), "A")

	def test_plain_order_falls_back_to_the_first_grade_when_all_are_flagged(self):
		self.assertEqual(self._pick(["B", "C"], 0, {"B": 1, "C": 1}), "B")

	def test_column_order_decides_ties(self):
		self.assertEqual(self._pick(["B", "A"], 0, {"A": 0, "B": 0}), "B")

	def test_blank_columns_are_skipped_not_returned(self):
		self.assertEqual(self._pick([None, "", "A"], 0, {"A": 0}), "A")

	def test_no_grades_configured_resolves_to_nothing(self):
		self.assertIsNone(self._pick([], 0))
		self.assertIsNone(self._pick([None, None, None, None], 1))

	def test_accepts_the_tuple_db_get_value_returns(self):
		"""resolve_diamond_grade passes the raw get_value row straight through."""
		self.assertEqual(self._pick(("A", "B", None, None), 1, {"A": 0, "B": 1}), "B")

	def test_flags_are_looked_up_when_the_caller_does_not_supply_them(self):
		with patch(
			f"{FILTERS_QUERY}.frappe.get_all",
			return_value=[
				frappe._dict(name="A", is_customer_diamond_quality=0),
				frappe._dict(name="B", is_customer_diamond_quality=1),
			],
		) as get_all:
			self.assertEqual(pick_diamond_grade(["A", "B"], 1), "B")

		get_all.assert_called_once()

	def test_flag_lookup_is_skipped_when_there_is_nothing_to_pick(self):
		with patch(f"{FILTERS_QUERY}.frappe.get_all") as get_all:
			self.assertIsNone(pick_diamond_grade([None, ""], 1))

		get_all.assert_not_called()


class TestIsCustomerDiamondFlag(UnitTestCase):
	"""Sales Order Item stores Yes/No text; the PMO stores a checkbox.

	Manufacturing Plan used to lowercase the string while the PMO compared it to "Yes", so a
	row saved as "yes" graded one way at plan time and the other on the PMO.
	"""

	def test_yes_in_any_casing_is_a_customer_diamond(self):
		for value in ("Yes", "yes", "YES", " Yes "):
			with self.subTest(value=value):
				self.assertEqual(is_customer_diamond_flag(value), 1)

	def test_everything_else_is_not(self):
		for value in ("No", "no", "", None, "Y", "1"):
			with self.subTest(value=value):
				self.assertEqual(is_customer_diamond_flag(value), 0)


class TestResolveDiamondGrade(UnitTestCase):
	"""resolve_diamond_grade wraps the picker with the Customer Diamond Grade lookup."""

	def _resolve(self, row, customer="CUST", quality="VVS", is_customer_diamond=0):
		with (
			patch(
				f"{FILTERS_QUERY}.frappe.db.get_value", return_value=row
			) as get_value,
			patch(
				f"{FILTERS_QUERY}.frappe.get_all",
				return_value=[
					frappe._dict(name="A", is_customer_diamond_quality=0),
					frappe._dict(name="B", is_customer_diamond_quality=1),
				],
			),
		):
			grade = resolve_diamond_grade(customer, quality, is_customer_diamond)
		return grade, get_value

	def test_reads_the_row_for_the_customer_and_quality(self):
		grade, get_value = self._resolve(("A", "B", None, None), is_customer_diamond=1)

		self.assertEqual(grade, "B")
		get_value.assert_called_once_with(
			"Customer Diamond Grade",
			{"parent": "CUST", "diamond_quality": "VVS"},
			[
				"diamond_grade_1",
				"diamond_grade_2",
				"diamond_grade_3",
				"diamond_grade_4",
			],
		)

	def test_no_row_for_the_quality_resolves_to_nothing(self):
		grade, _ = self._resolve(None)

		self.assertIsNone(grade)

	def test_a_missing_customer_or_quality_never_queries(self):
		with patch(f"{FILTERS_QUERY}.frappe.db.get_value") as get_value:
			self.assertIsNone(resolve_diamond_grade(None, "VVS", 1))
			self.assertIsNone(resolve_diamond_grade("CUST", None, 1))

		get_value.assert_not_called()


class TestDiamondGradeLinkQuery(UnitTestCase):
	"""The dropdown must offer what the controller would store, from the same customer.

	It used to filter on the ordering customer while the controller resolved against
	ref_customer, and to fall back to the first grade regardless of the flag -- so the list
	could offer a value the next save replaced.
	"""

	# One Customer Diamond Grade row, the shape frappe.db.get_value returns for GRADE_FIELDS.
	# A is a plain grade, B a customer-diamond one.
	ROW = ("A", "B", None, None)

	def _query(self, filters, rows=_UNSET):
		with (
			patch(
				f"{FILTERS_QUERY}.frappe.db.get_value",
				return_value=self.ROW if rows is _UNSET else rows,
			) as get_value,
			patch(
				f"{FILTERS_QUERY}.frappe.get_all",
				return_value=[
					frappe._dict(name="A", is_customer_diamond_quality=0),
					frappe._dict(name="B", is_customer_diamond_quality=1),
				],
			),
		):
			result = get_diamond_grade(
				"Attribute Value", "", "diamond_grade", 0, 20, filters
			)
		return result, get_value

	def test_ref_customer_outranks_the_ordering_customer(self):
		_, get_value = self._query(
			{"customer": "CUST", "ref_customer": "REF-CUST", "diamond_quality": "VVS"}
		)

		self.assertEqual(get_value.call_args[0][1]["parent"], "REF-CUST")

	def test_falls_back_to_the_ordering_customer(self):
		_, get_value = self._query({"customer": "CUST", "diamond_quality": "VVS"})

		self.assertEqual(get_value.call_args[0][1]["parent"], "CUST")

	def test_accepts_the_json_string_frappe_passes_for_filters(self):
		result, _ = self._query('{"customer": "CUST", "diamond_quality": "VVS"}')

		self.assertEqual(result, [("A",)])

	def test_offers_the_grade_the_controller_would_store(self):
		result, _ = self._query(
			{"customer": "CUST", "diamond_quality": "VVS", "is_customer_diamond": 1}
		)

		self.assertEqual(result, [("B",)])

	def test_offers_nothing_rather_than_a_grade_the_save_would_reject(self):
		result, _ = self._query(
			{"customer": "CUST", "diamond_quality": "VVS", "is_customer_diamond": 1},
			rows=("A", None, None, None),
		)

		self.assertEqual(result, [])

	def test_manual_override_lists_every_grade(self):
		result, _ = self._query(
			{
				"customer": "CUST",
				"diamond_quality": "VVS",
				"use_custom_diamond_grade": 1,
				"is_customer_diamond": 1,
			}
		)

		self.assertEqual(result, [("A",), ("B",)])

	def test_a_customer_with_no_row_for_the_quality_lists_nothing(self):
		result, _ = self._query(
			{"customer": "CUST", "diamond_quality": "VVS"}, rows=None
		)

		self.assertEqual(result, [])


class TestSalesTypeGradeOverride(UnitTestCase):
	"""Outright/Outwork constrain the manual override list, and only that list.

	Outright is a company-owned sale and Outwork a customer-supplied one, so each admits one
	kind of grade. Sales Type is also what sets custom_customer_diamond on the quotation, which
	becomes is_customer_diamond here -- so the two disagreeing means the PMO was edited into a
	state neither answer fits, and the list is empty rather than arbitrary.
	"""

	ROW = ("A", "B", None, None)

	def _query(self, **filters):
		filters.setdefault("customer", "CUST")
		filters.setdefault("diamond_quality", "VVS")
		filters.setdefault("use_custom_diamond_grade", 1)

		with (
			patch(f"{FILTERS_QUERY}.frappe.db.get_value", return_value=self.ROW),
			patch(
				f"{FILTERS_QUERY}.frappe.get_all",
				return_value=[
					frappe._dict(name="A", is_customer_diamond_quality=0),
					frappe._dict(name="B", is_customer_diamond_quality=1),
				],
			),
		):
			return get_diamond_grade(
				"Attribute Value", "", "diamond_grade", 0, 20, filters
			)

	def test_outright_offers_only_unflagged_grades(self):
		self.assertEqual(
			self._query(sales_type="Outright", is_customer_diamond=0), [("A",)]
		)

	def test_outwork_offers_only_flagged_grades(self):
		self.assertEqual(
			self._query(sales_type="Outwork", is_customer_diamond=1), [("B",)]
		)

	def test_outright_with_the_box_ticked_offers_nothing(self):
		self.assertEqual(self._query(sales_type="Outright", is_customer_diamond=1), [])

	def test_outwork_with_the_box_unticked_offers_nothing(self):
		self.assertEqual(self._query(sales_type="Outwork", is_customer_diamond=0), [])

	def test_a_sales_type_without_a_rule_keeps_the_full_list(self):
		"""Hybrid, Branch Sales, Certification and Repairing admit either kind."""
		for sales_type in ("Hybrid", "Branch Sales", "Certification", "Repairing"):
			for is_customer_diamond in (0, 1):
				with self.subTest(sales_type=sales_type, icd=is_customer_diamond):
					self.assertEqual(
						self._query(
							sales_type=sales_type,
							is_customer_diamond=is_customer_diamond,
						),
						[("A",), ("B",)],
					)

	def test_a_blank_sales_type_keeps_the_full_list(self):
		"""sales_type is fetch_from + read_only, so PMOs predating the field have none."""
		for filters in ({}, {"sales_type": None}, {"sales_type": ""}):
			with self.subTest(filters=filters):
				self.assertEqual(self._query(**filters), [("A",), ("B",)])

	def test_sales_type_does_not_reach_the_automatic_grade(self):
		"""The override is the only thing Sales Type constrains.

		With use_custom_diamond_grade off the field is read-only and the query returns the one
		grade the controller would store, which is is_customer_diamond's business alone.
		"""
		self.assertEqual(
			self._query(
				sales_type="Outright",
				is_customer_diamond=1,
				use_custom_diamond_grade=0,
			),
			[("B",)],
		)


class TestSalesTypeExpects(UnitTestCase):
	"""Only Outright and Outwork dictate an ownership; everything else stays neutral."""

	def test_the_two_types_that_dictate_ownership(self):
		self.assertEqual(sales_type_expects("Outright"), 0)
		self.assertEqual(sales_type_expects("Outwork"), 1)

	def test_every_other_type_dictates_nothing(self):
		for sales_type in ("Hybrid", "Branch Sales", "Certification", "Repairing"):
			with self.subTest(sales_type=sales_type):
				self.assertIsNone(sales_type_expects(sales_type))

	def test_a_missing_sales_type_dictates_nothing(self):
		"""sales_type is fetch_from + read_only, so PMOs predating the field carry none."""
		for sales_type in (None, ""):
			with self.subTest(sales_type=sales_type):
				self.assertIsNone(sales_type_expects(sales_type))


class FakePMOPolicy(FakePMO):
	"""FakePMO carrying the real policy methods, so the tests run the shipped code.

	Calling ParentManufacturingOrder._validate_diamond_grade_policy(fake) unbound does not work:
	frappe._dict resolves a missing attribute to None instead of raising, so the first call the
	method makes on self -- _grade_policy_inputs_changed -- comes back None and is then invoked.
	Binding the methods onto the class puts them where normal attribute lookup finds them, which
	is ahead of __getattr__.
	"""

	GRADE_POLICY_FIELDS = ParentManufacturingOrder.GRADE_POLICY_FIELDS
	_grade_policy_inputs_changed = ParentManufacturingOrder._grade_policy_inputs_changed
	_validate_diamond_grade_policy = (
		ParentManufacturingOrder._validate_diamond_grade_policy
	)


class TestDiamondGradePolicyValidation(UnitTestCase):
	"""The grade rules have to hold on the save path, not only in the dropdown.

	A REST write, an import, a server script, or a value already sitting in the field when the
	user ticks Use Custom Diamond Grade all reach save without passing through the link query.
	Since before_submit copies this grade onto the tracking BOM and it also selects the diamond
	item variant, a wrong value here is silent and durable.
	"""

	FILTERS_IN_PMO = f"{PMO_MODULE}.customer_grades"

	def _validate(self, before=_UNSET, configured=("PLAIN", "CUSTOMER"), **fields):
		doc = FakePMOPolicy(
			customer="CUST",
			ref_customer=None,
			diamond_quality="VVS",
			sales_type=None,
			is_customer_diamond=0,
			use_custom_diamond_grade=0,
			diamond_grade=None,
			flags=frappe._dict(ignore_validations=False),
		)
		doc.update(fields)
		# None models an insert, which must always validate
		doc.get_doc_before_save = lambda: None if before is _UNSET else before

		flags = {"PLAIN": 0, "CUSTOMER": 1}
		with (
			patch(self.FILTERS_IN_PMO, return_value=list(configured)),
			patch(
				f"{PMO_MODULE}.custom_override_grades",
				side_effect=lambda grades, st, icd: custom_override_grades(
					grades, st, icd, flags=flags
				),
			),
		):
			doc._validate_diamond_grade_policy()

	# --- the Sales Type invariant, which applies in both modes ---

	def test_outright_with_customer_diamond_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(sales_type="Outright", is_customer_diamond=1)

	def test_outwork_without_customer_diamond_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(sales_type="Outwork", is_customer_diamond=0)

	def test_matching_combinations_pass(self):
		self._validate(sales_type="Outright", is_customer_diamond=0)
		self._validate(sales_type="Outwork", is_customer_diamond=1)

	def test_the_invariant_holds_in_automatic_mode_too(self):
		"""The gap this closes: the dropdown blocked the mismatch, the save did not."""
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				sales_type="Outright", is_customer_diamond=1, use_custom_diamond_grade=0
			)

	def test_a_neutral_sales_type_imposes_nothing(self):
		for sales_type in ("Hybrid", None):
			for is_customer_diamond in (0, 1):
				with self.subTest(sales_type=sales_type, icd=is_customer_diamond):
					self._validate(
						sales_type=sales_type, is_customer_diamond=is_customer_diamond
					)

	# --- the manual override allowed-list ---

	def test_a_custom_grade_outside_the_allowed_list_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				sales_type="Outright",
				is_customer_diamond=0,
				use_custom_diamond_grade=1,
				diamond_grade="CUSTOMER",
			)

	def test_a_custom_grade_inside_the_allowed_list_passes(self):
		self._validate(
			sales_type="Outright",
			is_customer_diamond=0,
			use_custom_diamond_grade=1,
			diamond_grade="PLAIN",
		)

	def test_the_stale_grade_left_by_switching_to_custom_mode_is_rejected(self):
		"""The reported walkthrough, end to end.

		Outright, not customer diamond, and the only configured grade is a customer-diamond
		one: automatic mode legitimately resolves it, then the user ticks Use Custom Diamond
		Grade and the value stays in the field although the override may no longer hold it.
		"""
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				sales_type="Outright",
				is_customer_diamond=0,
				use_custom_diamond_grade=1,
				diamond_grade="CUSTOMER",
				configured=("CUSTOMER",),
			)

	def test_a_customer_with_no_configured_grade_rejects_any_custom_grade(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				use_custom_diamond_grade=1, diamond_grade="ANYTHING", configured=()
			)

	def test_automatic_mode_does_not_check_the_allowed_list(self):
		"""Sales Type narrows the override only; the automatic grade is the picker's business."""
		self._validate(
			sales_type="Outright",
			is_customer_diamond=0,
			use_custom_diamond_grade=0,
			diamond_grade="CUSTOMER",
			configured=("CUSTOMER",),
		)

	def test_custom_mode_without_a_grade_is_left_to_the_existing_check(self):
		# before_save already throws "Diamond Grade is not mentioned in customer" for an item
		# without a batch no; this validator must not pre-empt it with a different message
		self._validate(use_custom_diamond_grade=1, diamond_grade=None)

	# --- blast radius on records that predate the policy ---

	def test_a_save_touching_none_of_the_inputs_is_left_alone(self):
		before = frappe._dict(
			customer="CUST",
			ref_customer=None,
			diamond_quality="VVS",
			sales_type="Outright",
			is_customer_diamond=1,
			use_custom_diamond_grade=1,
			diamond_grade="CUSTOMER",
		)

		# the same invalid state on both sides: someone is editing an unrelated field
		self._validate(
			before=before,
			sales_type="Outright",
			is_customer_diamond=1,
			use_custom_diamond_grade=1,
			diamond_grade="CUSTOMER",
		)

	def test_touching_any_input_revalidates(self):
		for field, value in (
			("diamond_grade", "CUSTOMER"),
			("use_custom_diamond_grade", 1),
			("sales_type", "Outright"),
			("is_customer_diamond", 1),
			("diamond_quality", "VS"),
			("customer", "OTHER"),
			("ref_customer", "OTHER"),
		):
			before = frappe._dict(
				customer="CUST",
				ref_customer=None,
				diamond_quality="VVS",
				sales_type="Outright",
				is_customer_diamond=0,
				use_custom_diamond_grade=0,
				diamond_grade=None,
			)
			fields = dict(before)
			fields[field] = value
			fields["sales_type"] = "Outright"
			fields["is_customer_diamond"] = 1

			with self.subTest(field=field):
				with self.assertRaises(frappe.ValidationError):
					self._validate(before=before, **fields)

	def test_ignore_validations_is_still_an_escape_hatch(self):
		doc = FakePMOPolicy(
			customer="CUST",
			diamond_quality="VVS",
			sales_type="Outright",
			is_customer_diamond=1,
			use_custom_diamond_grade=0,
			diamond_grade=None,
			flags=frappe._dict(ignore_validations=True),
		)
		doc.get_doc_before_save = lambda: None

		doc._validate_diamond_grade_policy()


class TestMaterialRequestOwnershipStampPersists(IntegrationTestCase):
	"""``make_manufacturing_order`` tags customer-supplied Material Request rows.

	It wrote the value to ``custom_inventory_type``. Material Request Item has no such
	field -- confirmed in ``tabCustom Field`` on the live kg-gk site as well as here --
	and Frappe's ``get_valid_dict`` silently drops child keys it does not recognise. So
	the stamp had never reached the database on any site, and nothing failed to say so.

	These tests assert the MECHANISM rather than the assignment. A test that checked the
	dict passed to ``append()`` would have passed happily throughout the defect's life:
	the code always did set the key, it just set one that goes nowhere.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_material_request_item_has_inventory_type(self):
		"""The field the fix writes to must exist, or the fix is the same bug again."""
		self.assertTrue(
			frappe.get_meta("Material Request Item").has_field("inventory_type"),
			msg="Material Request Item has no inventory_type field",
		)

	def test_material_request_item_has_no_custom_inventory_type(self):
		"""The field the defect wrote to. If this ever starts existing, revisit the fix."""
		self.assertFalse(
			frappe.get_meta("Material Request Item").has_field("custom_inventory_type"),
			msg="custom_inventory_type now exists -- the original write may have been "
			"intentional after all; re-check before trusting this fix",
		)

	def test_the_ownership_value_survives_get_valid_dict(self):
		"""The actual failure mode, reproduced both ways round.

		``get_valid_dict`` is what discards the key, so it is what the test exercises --
		not a mock of it.
		"""
		row = frappe.new_doc("Material Request Item")
		row.update(
			{
				"inventory_type": "Customer Stock",
				"custom_inventory_type": "Customer Stock",
			}
		)
		valid = row.get_valid_dict()

		self.assertEqual(
			valid.get("inventory_type"),
			"Customer Stock",
			msg="the stamp the fix writes did not survive",
		)
		self.assertNotIn(
			"custom_inventory_type",
			valid,
			msg="the key the defect wrote is silently dropped -- this is the whole bug",
		)

	def test_every_inventory_type_this_writes_is_a_real_record(self):
		"""``inventory_type`` is a LINK, so a value with no master hard-throws on insert.

		This could not bite while the key was ``custom_inventory_type``: ``get_valid_dict``
		dropped it before any link check ran, so the value was never validated. Landing the stamp
		makes it real, and a wrong one does not fail quietly -- it raises LinkValidationError and
		takes down Material Request creation for every customer-supplied BOM.

		The row stamp was "Customer Stock", which exists only on a disposable test site whose
		fixtures create it. kg-gk, alfarsi and gk each hold exactly two Inventory Type records.
		Asserting against the site's own masters rather than a hardcoded list, so this fails
		wherever it would actually break.
		"""
		import inspect

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
			parent_manufacturing_order as pmo,
		)

		source = inspect.getsource(pmo)
		written = set(re.findall(r'"inventory_type":\s*"([^"]+)"', source))
		written |= set(re.findall(r'\.inventory_type\s*=\s*"([^"]+)"', source))
		self.assertTrue(written, msg="found no inventory_type literals to check")

		for value in sorted(written):
			self.assertTrue(
				frappe.db.exists("Inventory Type", value),
				msg=(
					f"parent_manufacturing_order stamps inventory_type={value!r}, which is not "
					f"an Inventory Type record on this site -- the insert would raise "
					f"LinkValidationError"
				),
			)

	def test_the_row_stamp_agrees_with_the_header_stamp(self):
		"""Header and rows described the same metal two different ways.

		The parent was stamped "Customer Goods" and the rows "Customer Stock". Only the header
		value existed as a master, and nothing reconciled the two.
		"""
		import inspect

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
			parent_manufacturing_order as pmo,
		)

		source = inspect.getsource(pmo)
		row_values = set(re.findall(r'"inventory_type":\s*"([^"]+)"', source))
		header_values = set(re.findall(r'\.inventory_type\s*=\s*"([^"]+)"', source))

		self.assertEqual(
			row_values & {"Customer Goods", "Customer Stock"},
			header_values & {"Customer Goods", "Customer Stock"},
			msg="the Material Request header and its item rows stamp different ownership types",
		)

	def test_custom_is_customer_item_does_persist(self):
		"""Why the row looked half-tagged: the sibling key on the same dict is real."""
		row = frappe.new_doc("Material Request Item")
		row.update({"custom_is_customer_item": 1})

		self.assertEqual(row.get_valid_dict().get("custom_is_customer_item"), 1)


class TestCancelAllLinked(UnitTestCase):
	"""PMO1 with its own MWO / Department IR / Stock Entries; PMO2 and a Sales Order hang off SE3."""

	def setUp(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		# Keep every test off the real site state the job touches: in-progress flags go to a
		# throwaway Redis key deleted afterwards (the real key other sessions read is never
		# written), and rollbacks / commit-rollback callbacks are stubbed. Tests that need their
		# own behaviour patch these again inside their `with` blocks.
		flag_key = f"test_pmo_cancel_all_in_progress::{frappe.generate_hash(length=10)}"
		self.addCleanup(frappe.cache.delete_value, flag_key)
		for patcher in (
			patch.object(cancel_all, "IN_FLIGHT_GRACE_SECONDS", 0),
			patch.object(cancel_all, "IN_PROGRESS_KEY", flag_key),
			patch.object(cancel_all.frappe.db, "rollback"),
			patch.object(cancel_all.frappe.db, "after_commit", MagicMock()),
			patch.object(cancel_all.frappe.db, "after_rollback", MagicMock()),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	GRAPH = {
		("Parent Manufacturing Order", "PMO1"): {
			"Manufacturing Work Order": ["MWO1"],
			"Stock Entry": ["SE1"],
		},
		("Manufacturing Work Order", "MWO1"): {
			"Department IR": ["DIR1"],
			"Stock Entry": ["SE2"],
		},
		("Department IR", "DIR1"): {"Stock Entry": ["SE3", "SE1"]},
		("Stock Entry", "SE3"): {
			"Serial and Batch Bundle": ["SBB3"],
			# another PMO and a shared upstream record: never walked into
			"Parent Manufacturing Order": ["PMO2"],
			"Sales Order": ["SO-SHARED"],
		},
		("Serial and Batch Bundle", "SBB3"): {"Sales Order": ["SO-UNRELATED"]},
		("Parent Manufacturing Order", "PMO2"): {"Stock Entry": ["SE-OF-PMO2"]},
	}

	def _children(self, _tree, parent_dt, parent_names):
		out = {}
		for name in parent_names:
			for dt, names in self.GRAPH.get((parent_dt, name), {}).items():
				out.setdefault(dt, []).extend(names)
		return out

	def _plan(self, created=None):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		created = created or {}
		with (
			patch.object(
				cancel_all,
				"_creation_times",
				side_effect=lambda keys: {k: created[k] for k in keys if k in created},
			),
			patch.object(cancel_all, "_shared_upstream", return_value=([], {})),
			patch.object(
				cancel_all.LeveledSubmittableTree,
				"get_next_level_children",
				autospec=True,
				side_effect=self._children,
			),
			patch.object(cancel_all, "get_exempted_doctypes", return_value=[]),
		):
			return cancel_all.get_cancel_plan("PMO1")

	def test_plan_is_this_pmo_and_what_was_made_from_it(self):
		plan = self._plan()

		self.assertEqual(plan[-1], ("Parent Manufacturing Order", "PMO1"))
		self.assertEqual(len(plan), len(set(plan)))
		self.assertEqual(
			set(plan),
			{
				("Manufacturing Work Order", "MWO1"),
				("Department IR", "DIR1"),
				("Stock Entry", "SE1"),
				("Stock Entry", "SE2"),
				("Stock Entry", "SE3"),
				("Parent Manufacturing Order", "PMO1"),
			},
		)

	def test_other_pmos_and_shared_upstream_are_never_walked_into(self):
		plan = self._plan()

		self.assertNotIn(("Parent Manufacturing Order", "PMO2"), plan)
		self.assertNotIn(("Stock Entry", "SE-OF-PMO2"), plan)
		self.assertNotIn(("Sales Order", "SO-SHARED"), plan)

	def test_newest_created_goes_first(self):
		plan = self._plan(
			created={
				("Manufacturing Work Order", "MWO1"): "2026-09-10 10:00:00",
				("Department IR", "DIR1"): "2026-09-11 10:00:00",
				("Stock Entry", "SE1"): "2026-09-12 10:00:00",
				("Stock Entry", "SE2"): "2026-09-13 10:00:00",
				("Stock Entry", "SE3"): "2026-09-14 10:00:00",
			}
		)

		self.assertEqual(
			plan,
			[
				("Stock Entry", "SE3"),
				("Stock Entry", "SE2"),
				("Stock Entry", "SE1"),
				("Department IR", "DIR1"),
				("Manufacturing Work Order", "MWO1"),
				("Parent Manufacturing Order", "PMO1"),
			],
		)

	def test_unknown_creation_is_kept_and_ordered_by_depth(self):
		plan = self._plan(created={})

		self.assertEqual(len(plan), 6)
		self.assertLess(
			plan.index(("Stock Entry", "SE3")),
			plan.index(("Manufacturing Work Order", "MWO1")),
		)

	def _shared(
		self, pmo_links, docstatus, referrers, so_quotations=(), item_tracking_boms=None
	):
		"""Runs _shared_upstream against fake records: referrers[X] = submitted records using X."""
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		def get_value(dt, dn, fields, as_dict=False):
			if dt == "Parent Manufacturing Order":
				return frappe._dict(pmo_links)
			return docstatus.get((dt, dn), 1)

		downstream = {
			("Parent Manufacturing Order", "PMO1"),
			("Manufacturing Work Order", "MWO1"),
		}
		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(
				frappe,
				"get_all",
				side_effect=lambda dt, filters, pluck, distinct: list(so_quotations)
				if pluck == "prevdoc_docname"
				else list((item_tracking_boms or {}).get((dt, filters["parent"]), [])),
			),
			patch.object(
				cancel_all,
				"_submitted_referrers",
				side_effect=lambda key: referrers.get(key, []),
			),
		):
			return cancel_all._shared_upstream(
				("Parent Manufacturing Order", "PMO1"), downstream
			)

	PMO_LINKS = {
		"manufacturing_plan": "MP1",
		"sales_order": "SO1",
		"quotation": "QTN1",
		"custom_tracking_bom": "TB1",
	}

	def test_case_1_plan_shared_with_a_submitted_pmo_keeps_everything_above(self):
		pmo1, pmo2 = (
			("Parent Manufacturing Order", "PMO1"),
			("Parent Manufacturing Order", "PMO2"),
		)
		mp, so, qtn, tb = (
			("Manufacturing Plan", "MP1"),
			("Sales Order", "SO1"),
			("Quotation", "QTN1"),
			("Tracking Bom", "TB1"),
		)
		included, kept = self._shared(
			self.PMO_LINKS,
			{},
			{
				mp: [pmo1, pmo2],
				so: [mp, pmo1, pmo2],
				qtn: [so, pmo1, pmo2],
				tb: [pmo1, pmo2],
			},
		)

		self.assertEqual(included, [])
		self.assertEqual(set(kept), {mp, so, qtn, tb})
		self.assertEqual(kept[mp], [pmo2])

	def test_case_2_other_pmos_already_cancelled_lets_everything_above_go(self):
		# cancelled PMOs no longer show up as submitted users
		pmo1 = ("Parent Manufacturing Order", "PMO1")
		mp, so, qtn, tb = (
			("Manufacturing Plan", "MP1"),
			("Sales Order", "SO1"),
			("Quotation", "QTN1"),
			("Tracking Bom", "TB1"),
		)
		included, kept = self._shared(
			self.PMO_LINKS,
			{},
			{mp: [pmo1], so: [mp, pmo1], qtn: [so, pmo1], tb: [pmo1, mp, so, qtn]},
		)

		self.assertEqual(included, [mp, so, qtn, tb])
		self.assertEqual(kept, {})

	def test_case_3_only_pmo_of_its_plan_but_tracking_bom_shared_with_another_order(
		self,
	):
		pmo1 = ("Parent Manufacturing Order", "PMO1")
		mp, so, qtn, tb = (
			("Manufacturing Plan", "MP1"),
			("Sales Order", "SO1"),
			("Quotation", "QTN1"),
			("Tracking Bom", "TB1"),
		)
		other_so = ("Sales Order", "SO-OTHER")
		included, kept = self._shared(
			self.PMO_LINKS,
			{},
			{
				mp: [pmo1],
				so: [mp, pmo1],
				qtn: [so, pmo1],
				tb: [pmo1, so, qtn, other_so],
			},
		)

		self.assertEqual(included, [mp, so, qtn])
		self.assertEqual(kept, {tb: [other_so]})

	def test_sales_order_stays_while_another_plan_of_it_is_submitted(self):
		pmo1 = ("Parent Manufacturing Order", "PMO1")
		mp, so, qtn = (
			("Manufacturing Plan", "MP1"),
			("Sales Order", "SO1"),
			("Quotation", "QTN1"),
		)
		other_mp = ("Manufacturing Plan", "MP-OTHER")
		included, kept = self._shared(
			self.PMO_LINKS, {}, {mp: [pmo1], so: [mp, pmo1, other_mp], qtn: [so, pmo1]}
		)

		self.assertEqual(included, [mp, ("Tracking Bom", "TB1")])
		self.assertEqual(kept[so], [other_mp])
		self.assertEqual(kept[qtn], [so])

	def test_tracking_bom_of_another_item_on_the_order_goes_when_unused(self):
		"""SO/Quotation carry a second item whose PMO is already cancelled: its Tracking Bom goes too."""
		pmo1 = ("Parent Manufacturing Order", "PMO1")
		mp, so, qtn, tb = (
			("Manufacturing Plan", "MP1"),
			("Sales Order", "SO1"),
			("Quotation", "QTN1"),
			("Tracking Bom", "TB1"),
		)
		other_item_tb = ("Tracking Bom", "TB-OTHER-ITEM")
		included, kept = self._shared(
			self.PMO_LINKS,
			{},
			{
				mp: [pmo1],
				so: [mp, pmo1],
				qtn: [so, pmo1],
				tb: [so, qtn, pmo1],
				other_item_tb: [so, qtn],
			},
			item_tracking_boms={
				("Sales Order Item", "SO1"): ["TB1", "TB-OTHER-ITEM"],
				("Quotation Item", "QTN1"): ["TB1", "TB-OTHER-ITEM"],
			},
		)

		self.assertEqual(included, [mp, so, qtn, tb, other_item_tb])
		self.assertEqual(kept, {})

	def test_cancelled_workflow_records_move_to_the_cancelled_state(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with (
			patch(
				"frappe.model.workflow.get_workflow_name",
				side_effect=lambda dt: "QWF" if dt == "Quotation" else None,
			),
			patch(
				"frappe.model.workflow.get_workflow_state_field",
				return_value="workflow_state",
			),
			patch.object(frappe.db, "get_value", return_value="Cancelled") as get_value,
			patch.object(frappe, "get_all", return_value=["QTN1"]) as get_all,
			patch.object(frappe.db, "set_value") as set_value,
		):
			cancel_all.mark_workflow_cancelled(
				[("Quotation", "QTN1"), ("Stock Entry", "SE1")]
			)

		get_value.assert_called_once_with(
			"Workflow Document State", {"parent": "QWF", "doc_status": "2"}, "state"
		)
		self.assertEqual(
			get_all.call_args.args[1], {"name": ["in", ["QTN1"]], "docstatus": 2}
		)
		set_value.assert_called_once_with(
			"Quotation", "QTN1", "workflow_state", "Cancelled", update_modified=False
		)

	def _covered(self, rows, mwo_owner, pmo_docstatus=None):
		"""rows: [{fieldname: value}] of one record (parent first); mwo_owner[MWO] = (PMO, docstatus)."""
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		link_fields = [
			frappe._dict(
				fieldname="manufacturing_work_order", options="Manufacturing Work Order"
			),
			frappe._dict(
				fieldname="parent_manufacturing_order",
				options="Parent Manufacturing Order",
			),
			frappe._dict(
				fieldname="amended_from", options="Parent Manufacturing Order"
			),
			frappe._dict(fieldname="item", options="Item"),
		]

		def make(values):
			row = frappe._dict(values)
			row.meta = frappe._dict(get_link_fields=lambda: link_fields)
			return row

		parent, *children = [make(r) for r in rows]
		parent.get_all_children = lambda: children

		def get_value(dt, dn, fields):
			if dt == "Parent Manufacturing Order":
				return (pmo_docstatus or {}).get(dn, 1)
			return mwo_owner.get(dn)

		with (
			patch.object(frappe, "get_doc", return_value=parent),
			patch.object(frappe.db, "get_value", side_effect=get_value),
		):
			return cancel_all._other_orders_covered(parent, {"PMO1"})

	def test_record_covering_another_orders_work_order_is_reported(self):
		covered = self._covered(
			[
				{},
				{"manufacturing_work_order": "MWO-MINE"},
				{"manufacturing_work_order": "MWO-OTHER"},
			],
			{"MWO-MINE": ("PMO1", 1), "MWO-OTHER": ("PMO2", 1)},
		)

		self.assertEqual(covered, [("Manufacturing Work Order", "MWO-OTHER")])

	def test_other_orders_already_cancelled_or_amended_from_are_ignored(self):
		covered = self._covered(
			[
				{
					"amended_from": "PMO-OLD",
					"parent_manufacturing_order": "PMO1",
					"item": "X",
				},
				{"manufacturing_work_order": "MWO-OTHER-CANCELLED"},
				{"parent_manufacturing_order": "PMO-CANCELLED"},
			],
			{"MWO-OTHER-CANCELLED": ("PMO2", 2)},
			pmo_docstatus={"PMO-CANCELLED": 2},
		)

		self.assertEqual(covered, [])

	def test_record_covering_another_pmo_directly_is_reported(self):
		covered = self._covered([{"parent_manufacturing_order": "PMO2"}], {})

		self.assertEqual(covered, [("Parent Manufacturing Order", "PMO2")])

	def test_done_event_waits_for_the_commit(self):
		# Through _job(): the gate, the wait, the rollback and the commit callbacks are all stubbed,
		# so no real Redis flag, rollback or callback is left behind on the test site.
		m = self._job()

		self.assertIsNone(m.error)
		self.assertEqual(m.publish.call_args.args[1]["status"], "done")
		self.assertTrue(m.publish.call_args.kwargs.get("after_commit"))

	def test_blocker_name_must_match_whole(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		message = 'Cannot link cancelled document: Reference Docname: <a href="/app/bom/BOM-00012">BOM-00012</a>'
		self.assertFalse(cancel_all._names_in("BOM-0001", message))
		self.assertTrue(cancel_all._names_in("BOM-00012", message))

	def _operation_users(self, rows, linked=()):
		"""rows[(child_doctype, parenttype)] = submitted IRs using the operation through their rows;
		linked = submitted documents Frappe's own cancel check reports (Stock Entry, SNC, ...)."""

		from jewellery_erpnext import utils

		def get_all(child_doctype, filters, pluck, distinct):
			return rows.get((child_doctype, filters["parenttype"]), [])

		return (
			patch.object(utils.frappe, "get_all", side_effect=get_all),
			patch.object(
				utils.frappe,
				"get_doc",
				return_value=frappe._dict(
					doctype="Manufacturing Operation", name="MOP-B"
				),
			),
			patch.object(utils, "get_submitted_linked_docs", return_value=list(linked)),
		)

	def _check_in_use(self, rows, linked=()):
		from jewellery_erpnext import utils

		a, b, c = self._operation_users(rows, linked)
		with a, b, c:
			utils.ensure_operation_not_in_use("MOP-B", "Employee IR", "EIR-1")

	def test_ir_cancel_stops_while_another_submitted_ir_uses_the_operation(self):
		with self.assertRaises(frappe.LinkExistsError) as ctx:
			self._check_in_use(
				{("Department IR Operation", "Department IR"): ["DIR-LATER"]}
			)
		self.assertIn("DIR-LATER", str(ctx.exception))

	def test_ir_cancel_stops_while_a_submitted_stock_entry_uses_the_operation(self):
		with self.assertRaises(frappe.LinkExistsError) as ctx:
			self._check_in_use({}, linked=[("Stock Entry", "SE-LATER")])
		self.assertIn("SE-LATER", str(ctx.exception))

	def test_ir_cancel_ignores_its_own_rows(self):
		self._check_in_use(
			{("Employee IR Operation", "Employee IR"): ["EIR-1"]},
			linked=[("Employee IR", "EIR-1")],
		)

	def test_latest_operation_skips_revert_leftovers_but_keeps_empty_status(self):
		from jewellery_erpnext import utils

		rows = [
			frappe._dict(name="MOP-LEFTOVER", department_ir_status="Revert"),
			frappe._dict(name="MOP-CURRENT", department_ir_status=None),
			frappe._dict(name="MOP-OLDER", department_ir_status="Received"),
		]
		with patch.object(utils.frappe, "get_all", return_value=rows) as get_all:
			self.assertEqual(
				utils.latest_operation("MWO1", status="Not Started"), "MOP-CURRENT"
			)
		self.assertEqual(
			get_all.call_args.args[1],
			{"manufacturing_work_order": "MWO1", "status": "Not Started"},
		)
		self.assertEqual(get_all.call_args.kwargs["order_by"], "creation desc")

	def _tracking_bom_cancel(self, docstatus, users):
		from jewellery_erpnext import utils

		tracking_bom = frappe._dict(
			doctype="Tracking Bom", name="TB1", docstatus=docstatus
		)
		tracking_bom.cancel = MagicMock()
		with (
			patch.object(utils.frappe, "get_doc", return_value=tracking_bom),
			patch.object(utils, "get_submitted_linked_docs", return_value=list(users)),
		):
			utils.cancel_tracking_bom_if_unused("TB1")
		return tracking_bom.cancel

	def test_tracking_bom_is_cancelled_by_its_last_user(self):
		# e.g. the Quotation, cancelled after its Sales Order: nothing submitted uses it any more
		self._tracking_bom_cancel(1, []).assert_called_once()

	def test_tracking_bom_stays_while_another_order_uses_it(self):
		self._tracking_bom_cancel(1, [("Sales Order", "SO-OTHER")]).assert_not_called()

	def test_tracking_bom_draft_or_cancelled_is_left_alone(self):
		self._tracking_bom_cancel(0, []).assert_not_called()
		self._tracking_bom_cancel(2, []).assert_not_called()

	def test_amended_quotation_drops_only_cancelled_tracking_boms(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import quotation

		rows = [
			frappe._dict(custom_tracking_bom="TB-CANCELLED"),
			frappe._dict(custom_tracking_bom="TB-SHARED-ACTIVE"),
			frappe._dict(custom_tracking_bom=None),
		]
		docstatus = {"TB-CANCELLED": 2, "TB-SHARED-ACTIVE": 1}
		# The docstatuses are read in one bulk get_all, not a get_value per row.
		with patch.object(
			quotation.frappe,
			"get_all",
			return_value=[
				frappe._dict(name=name, docstatus=status)
				for name, status in docstatus.items()
			],
		) as get_all:
			quotation.clear_cancelled_tracking_boms(SimpleNamespace(items=rows))

		self.assertEqual(
			[r.custom_tracking_bom for r in rows], [None, "TB-SHARED-ACTIVE", None]
		)
		self.assertEqual(get_all.call_count, 1)
		self.assertEqual(
			sorted(get_all.call_args.kwargs["filters"]["name"][1]),
			["TB-CANCELLED", "TB-SHARED-ACTIVE"],
		)

	def test_ir_rows_on_reverted_operations_are_refused(self):
		from jewellery_erpnext import utils

		rows = [
			frappe._dict(idx=1, manufacturing_operation="MOP-LIVE"),
			frappe._dict(idx=2, manufacturing_operation="MOP-REVERTED"),
			frappe._dict(idx=3, manufacturing_operation=None),
		]
		doc = frappe._dict(employee_ir_operations=rows)
		with patch.object(
			utils.frappe, "get_all", return_value=["MOP-REVERTED"]
		) as get_all:
			with self.assertRaises(frappe.ValidationError) as ctx:
				utils.validate_no_reverted_operations(doc, "employee_ir_operations")

		self.assertIn("Row 2", str(ctx.exception))
		self.assertNotIn("Row 1", str(ctx.exception))
		self.assertEqual(
			get_all.call_args.args[1]["name"], ["in", ["MOP-LIVE", "MOP-REVERTED"]]
		)

	def test_ir_rows_on_live_operations_pass(self):
		from jewellery_erpnext import utils

		doc = frappe._dict(
			department_ir_operation=[
				frappe._dict(idx=1, manufacturing_operation="MOP-LIVE")
			]
		)
		with patch.object(utils.frappe, "get_all", return_value=[]):
			utils.validate_no_reverted_operations(doc, "department_ir_operation")

	def test_statuses_are_read_once_per_doctype(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		keys = [
			("Stock Entry", "SE1"),
			("Stock Entry", "SE2"),
			("Department IR", "DIR1"),
		]
		with patch.object(
			frappe,
			"get_all",
			side_effect=lambda dt, filters, pluck, order_by: ["SE2"]
			if dt == "Stock Entry"
			else ["DIR1"],
		) as get_all:
			still = cancel_all._still_submitted(keys)

		self.assertEqual(still, {("Stock Entry", "SE2"), ("Department IR", "DIR1")})
		self.assertEqual(get_all.call_count, 2)

	def test_same_work_order_is_looked_up_once(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		link_fields = [
			frappe._dict(
				fieldname="manufacturing_work_order", options="Manufacturing Work Order"
			)
		]

		def row(mwo):
			r = frappe._dict(manufacturing_work_order=mwo)
			r.meta = frappe._dict(get_link_fields=lambda: link_fields)
			return r

		first, second = row(None), row(None)
		first.get_all_children = lambda: [row("MWO-OTHER"), row("MWO-OTHER")]
		second.get_all_children = lambda: [row("MWO-OTHER")]
		order_of = {}
		with patch.object(
			frappe.db, "get_value", return_value=("PMO2", 1)
		) as get_value:
			self.assertEqual(
				cancel_all._other_orders_covered(first, {"PMO1"}, order_of),
				[("Manufacturing Work Order", "MWO-OTHER")],
			)
			self.assertEqual(
				cancel_all._other_orders_covered(second, {"PMO1"}, order_of),
				[("Manufacturing Work Order", "MWO-OTHER")],
			)
		get_value.assert_called_once()

	def test_batched_links_map_each_record_and_report_child_rows_as_their_parent(self):
		from jewellery_erpnext import utils

		metas = {
			"Stock Entry": frappe._dict(
				name="Stock Entry", istable=0, issingle=0, is_submittable=1
			),
			"Department IR Operation": frappe._dict(
				name="Department IR Operation", istable=1, issingle=0, is_submittable=0
			),
			"MOP Log": frappe._dict(
				name="MOP Log", istable=0, issingle=0, is_submittable=0
			),
		}
		link_fields = [
			{
				"parent": "Stock Entry",
				"fieldname": "manufacturing_operation",
				"issingle": 0,
			},
			{
				"parent": "Department IR Operation",
				"fieldname": "manufacturing_operation",
				"issingle": 0,
			},
			{
				"parent": "MOP Log",
				"fieldname": "manufacturing_operation",
				"issingle": 0,
			},
		]
		rows = {
			"Stock Entry": [frappe._dict(name="SE1", manufacturing_operation="MOP-A")],
			"Department IR Operation": [
				frappe._dict(
					name="row1",
					manufacturing_operation="MOP-B",
					parent="DIR1",
					parenttype="Department IR",
				),
				frappe._dict(
					name="row2",
					manufacturing_operation="MOP-B",
					parent="DIR1",
					parenttype="Department IR",
				),
			],
		}
		queried = []

		def get_values(dt, filters, fields, as_dict, order_by):
			queried.append((dt, filters["manufacturing_operation"]))
			return rows.get(dt, [])

		with (
			patch("frappe.model.rename_doc.get_link_fields", return_value=link_fields),
			patch("frappe.model.dynamic_links.get_dynamic_link_map", return_value={}),
			patch.object(utils.frappe, "get_meta", side_effect=lambda dt: metas[dt]),
			patch.object(utils.frappe.db, "get_values", side_effect=get_values),
		):
			found = utils.get_submitted_links(
				"Manufacturing Operation", ["MOP-A", "MOP-B", "MOP-C"]
			)

		self.assertEqual(
			found,
			{
				"MOP-A": [("Stock Entry", "SE1")],
				"MOP-B": [("Department IR", "DIR1")],
				"MOP-C": [],
			},
		)
		# one query per link field for all three operations; MOP Log never holds submitted rows
		self.assertEqual(
			queried,
			[
				("Stock Entry", ["in", ["MOP-A", "MOP-B", "MOP-C"]]),
				("Department IR Operation", ["in", ["MOP-A", "MOP-B", "MOP-C"]]),
			],
		)

	def _doc_with_rows(self, doctype, name, rows):
		"""A fake document whose rows carry the given link values."""
		link_fields = [
			frappe._dict(
				fieldname="manufacturing_work_order", options="Manufacturing Work Order"
			),
			frappe._dict(
				fieldname="manufacturing_operation", options="Manufacturing Operation"
			),
			frappe._dict(
				fieldname="parent_manufacturing_order",
				options="Parent Manufacturing Order",
			),
			frappe._dict(
				fieldname="amended_from", options="Parent Manufacturing Order"
			),
		]

		def make(values):
			row = frappe._dict(values)
			row.meta = frappe._dict(
				get_link_fields=lambda: link_fields, get_dynamic_link_fields=lambda: []
			)
			return row

		doc = make({"doctype": doctype, "name": name})
		children = [make(r) for r in rows]
		doc.get_all_children = lambda: children
		return doc

	def _gate(self, doc, busy, owners=None):
		"""busy: PMOs being cancelled, or {pmo: records on its list}.
		owners: {"Manufacturing Operation": {op: mwo}, "Manufacturing Work Order": {mwo: pmo}}."""
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		owners = owners or {}

		def get_all(dt, filters, pluck, order_by):
			return [owners.get(dt, {}).get(n) for n in filters["name"][1]]

		with (
			patch.object(
				cancel_all,
				"pmos_being_cancelled",
				return_value=busy
				if isinstance(busy, dict)
				else {pmo: set() for pmo in busy},
			),
			patch.object(frappe, "get_all", side_effect=get_all) as get_all_mock,
		):
			cancel_all.block_submit_while_pmo_cancels(doc)
		return get_all_mock

	def test_submit_touching_a_pmo_being_cancelled_is_refused(self):
		doc = self._doc_with_rows(
			"Employee IR", "EIR-NEW", [{"manufacturing_work_order": "MWO-A"}]
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._gate(doc, {"PMO1"}, {"Manufacturing Work Order": {"MWO-A": "PMO1"}})
		self.assertIn("PMO1", str(ctx.exception))

	def test_submit_reaching_the_pmo_through_an_operation_is_refused(self):
		doc = self._doc_with_rows(
			"Stock Entry", "SE-NEW", [{"manufacturing_operation": "MOP-A"}]
		)
		with self.assertRaises(frappe.ValidationError):
			self._gate(
				doc,
				{"PMO1"},
				{
					"Manufacturing Operation": {"MOP-A": "MWO-A"},
					"Manufacturing Work Order": {"MWO-A": "PMO1"},
				},
			)

	def test_submit_for_other_pmos_goes_through(self):
		doc = self._doc_with_rows(
			"Employee IR",
			"EIR-NEW",
			[{"manufacturing_work_order": "MWO-B"}, {"amended_from": "PMO1"}],
		)
		self._gate(doc, {"PMO1"}, {"Manufacturing Work Order": {"MWO-B": "PMO2"}})

	def test_gate_costs_nothing_when_no_cancel_runs(self):
		doc = self._doc_with_rows(
			"Employee IR", "EIR-NEW", [{"manufacturing_work_order": "MWO-A"}]
		)
		self._gate(doc, set()).assert_not_called()

	def test_the_cancel_job_itself_is_not_blocked(self):
		doc = self._doc_with_rows(
			"Repost Item Valuation", "RIV-1", [{"parent_manufacturing_order": "PMO1"}]
		)
		frappe.flags.in_pmo_cancel_all = True
		try:
			self._gate(doc, {"PMO1"})
		finally:
			frappe.flags.in_pmo_cancel_all = False

	def test_expired_in_progress_flags_are_ignored(self):
		import time

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		flags = {
			b"PMO-RUNNING": {
				"until": time.time() + 600,
				"records": [["Sales Order", "SO-1"]],
			},
			b"PMO-CRASHED": {"until": time.time() - 1, "records": []},
		}
		with patch.object(cancel_all.frappe.cache, "hgetall", return_value=flags):
			self.assertEqual(
				cancel_all.pmos_being_cancelled(),
				{"PMO-RUNNING": {("Sales Order", "SO-1")}},
			)

	def _job(self, calls=None, **patches):
		"""Runs cancel_all with the gate helpers and transaction callbacks mocked; returns the mocks.
		`calls` records the gate, wait and rollback steps in order (pass one to add your own)."""
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		m = frappe._dict(
			after_commit=MagicMock(),
			after_rollback=MagicMock(),
			calls=calls or MagicMock(),
		)
		defaults = {
			"get_cancel_plan": [("Parent Manufacturing Order", "PMO1")],
			"get_linked_records": ({}, []),
			"_cancel_in_order": [],
		}
		with (
			patch.object(
				cancel_all,
				"mark_cancel_in_progress",
				side_effect=lambda *a: m.calls("mark", *a),
			),
			patch.object(
				cancel_all,
				"clear_cancel_in_progress",
				side_effect=lambda *a: m.calls("clear", *a),
			),
			patch.object(
				cancel_all.time, "sleep", side_effect=lambda sec: m.calls("sleep")
			),
			patch.object(
				cancel_all.frappe.db,
				"rollback",
				side_effect=lambda **kw: m.calls("rollback", kw),
			),
			patch.object(cancel_all.frappe.db, "savepoint"),
			patch.object(cancel_all.frappe.db, "after_commit", m.after_commit),
			patch.object(cancel_all.frappe.db, "after_rollback", m.after_rollback),
			patch.object(cancel_all, "mark_workflow_cancelled"),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "log_error"),
			patch.object(frappe, "publish_realtime") as publish,
		):
			for name, value in {**defaults, **patches}.items():
				kind = (
					"side_effect"
					if isinstance(value, Exception) or callable(value)
					else "return_value"
				)
				stack = patch.object(cancel_all, name, **{kind: value})
				stack.start()
				self.addCleanup(stack.stop)
			try:
				cancel_all.cancel_all("PMO1", "Administrator")
				m.error = None
			except Exception as e:
				m.error = e
			m.publish = publish
		return m

	def test_job_gates_the_whole_list_waits_then_checks_in_a_fresh_transaction(self):
		plan = [("Sales Order", "SO-1"), ("Parent Manufacturing Order", "PMO1")]
		calls = MagicMock()

		def linked(p):
			calls("pre-check")
			return ({}, [])

		result = self._job(calls=calls, get_cancel_plan=plan, get_linked_records=linked)
		names = [c.args[0] for c in calls.call_args_list]
		# gate on the PMO, then on the whole list, wait, fresh transaction, and only then the check
		self.assertEqual(names[:5], ["mark", "mark", "sleep", "rollback", "pre-check"])
		self.assertEqual(result.calls.call_args_list[1].args, ("mark", "PMO1", plan))
		self.assertIsNone(result.error)
		self.assertEqual(result.publish.call_args.args[1]["status"], "done")

	def test_job_keeps_the_gate_until_its_commit_or_rollback(self):
		m = self._job(get_linked_records=frappe.ValidationError("outside link"))
		self.assertIsInstance(m.error, frappe.ValidationError)
		# failed after the hand-over: not cleared directly, but by the job runner's rollback
		self.assertNotIn("clear", [c.args[0] for c in m.calls.call_args_list])
		m.after_commit.add.call_args.args[0]()
		m.after_rollback.add.call_args.args[0]()
		self.assertEqual([c.args[0] for c in m.calls.call_args_list].count("clear"), 2)
		self.assertFalse(frappe.flags.in_pmo_cancel_all)

	def test_job_that_cannot_raise_the_gate_reports_and_still_clears_it(self):
		m = self._job(mark_cancel_in_progress=frappe.ValidationError("cache down"))
		self.assertIsInstance(m.error, frappe.ValidationError)
		# failed before anything was cancelled: the button's flag is lowered straight away
		self.assertIn(("clear", "PMO1"), [c.args for c in m.calls.call_args_list])
		m.after_rollback.add.assert_not_called()
		self.assertEqual(m.publish.call_args.args[1]["status"], "failed")
		self.assertIn("cache down", m.publish.call_args.args[1]["error"])
		self.assertFalse(frappe.flags.in_pmo_cancel_all)

	def test_submit_against_a_sales_order_on_the_list_is_refused(self):
		doc = self._doc_with_rows("Delivery Note", "DN-NEW", [])
		doc.meta = frappe._dict(
			get_link_fields=lambda: [
				frappe._dict(fieldname="against_sales_order", options="Sales Order")
			],
			get_dynamic_link_fields=lambda: [],
		)
		doc.against_sales_order = "SO-1"
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._gate(doc, {"PMO1": {("Sales Order", "SO-1")}})
		self.assertIn("PMO1", str(ctx.exception))

	def test_submit_through_a_dynamic_link_to_the_list_is_refused(self):
		doc = self._doc_with_rows("Payment Entry", "PE-NEW", [])
		doc.meta = frappe._dict(
			get_link_fields=lambda: [],
			get_dynamic_link_fields=lambda: [
				frappe._dict(fieldname="reference_name", options="reference_doctype")
			],
		)
		doc.reference_doctype, doc.reference_name = "Sales Order", "SO-1"
		with self.assertRaises(frappe.ValidationError):
			self._gate(doc, {"PMO1": {("Sales Order", "SO-1")}})

	def test_second_click_while_a_job_is_queued_does_not_touch_the_gate(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with (
			patch.object(cancel_all.frappe, "only_for"),
			patch.object(cancel_all.frappe, "has_permission", return_value=True),
			patch("frappe.utils.background_jobs.is_job_enqueued", return_value=True),
			patch.object(cancel_all, "mark_cancel_in_progress") as mark,
			patch.object(cancel_all.frappe, "enqueue") as enqueue,
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				cancel_all.enqueue_cancel_all("PMO1")
		self.assertIn("already running", str(ctx.exception))
		mark.assert_not_called()
		enqueue.assert_not_called()

	def test_button_raises_the_gate_and_lowers_it_if_the_job_cannot_be_queued(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with (
			patch.object(cancel_all.frappe, "only_for"),
			patch.object(cancel_all.frappe, "has_permission", return_value=True),
			patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False),
			patch.object(cancel_all, "mark_cancel_in_progress") as mark,
			patch.object(cancel_all, "clear_cancel_in_progress") as clear,
			patch.object(
				cancel_all.frappe, "enqueue", side_effect=RuntimeError("redis down")
			),
		):
			self.assertRaises(RuntimeError, cancel_all.enqueue_cancel_all, "PMO1")
		mark.assert_called_once_with("PMO1")
		clear.assert_called_once_with("PMO1")

	def test_redis_down_lets_submits_through(self):
		import redis

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with patch.object(
			cancel_all.frappe.cache,
			"hgetall",
			side_effect=redis.exceptions.ConnectionError("down"),
		):
			self.assertEqual(cancel_all.pmos_being_cancelled(), {})

	def test_cancel_all_does_not_start_if_the_flag_did_not_stick(self):
		import redis

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		# hset swallows a Redis outage, so the read-back is what notices it
		for read_back in ({}, redis.exceptions.ConnectionError("down")):
			kwargs = (
				{"side_effect": read_back}
				if isinstance(read_back, Exception)
				else {"return_value": read_back}
			)
			with (
				patch.object(cancel_all.frappe.cache, "hset"),
				patch.object(cancel_all.frappe.cache, "hgetall", **kwargs),
			):
				with self.assertRaises(frappe.ValidationError) as ctx:
					cancel_all.mark_cancel_in_progress("PMO1")
			self.assertIn("cache server is not reachable", str(ctx.exception))

	def test_cancel_all_starts_when_the_flag_is_up(self):
		import time

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with (
			patch.object(cancel_all.frappe.cache, "hset") as hset,
			patch.object(
				cancel_all.frappe.cache,
				"hgetall",
				return_value={b"PMO1": {"until": time.time() + 600}},
			),
		):
			cancel_all.mark_cancel_in_progress("PMO1")
		self.assertEqual(hset.call_args.args[:2], (cancel_all.IN_PROGRESS_KEY, "PMO1"))

	def test_already_cancelled_upstream_is_skipped(self):
		mp = ("Manufacturing Plan", "MP1")
		included, kept = self._shared(self.PMO_LINKS, {mp: 2}, {})

		self.assertNotIn(mp, included)
		self.assertNotIn(mp, kept)

	def test_snc_release_makes_bom_wait_for_it_instead_of_blocking(self):
		snc, bom, pmo = (
			("Serial Number Creator", "SNC1"),
			("BOM", "BOM1"),
			("Parent Manufacturing Order", "PMO1"),
		)
		tb = ("Tracking Bom", "TB1")
		referrers, outside = self._linked_records(
			[snc, bom, pmo], {bom: [snc, tb], pmo: [snc]}, released={(bom, tb): snc}
		)

		self.assertEqual(outside, [])
		self.assertEqual(referrers[bom], {snc})

	def test_loop_is_broken_ignoring_only_links_from_the_plan(self):
		"""SNC and BOM each refuse to go first; one is let through, then the other cancels normally."""
		from frappe.model import delete_doc

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		snc, bom, pmo = (
			("Serial Number Creator", "SNC1"),
			("BOM", "BOM1"),
			("Parent Manufacturing Order", "PMO1"),
		)
		plan = [snc, bom, pmo]
		docstatus = dict.fromkeys(plan, 1)
		links_to = {snc: [bom], bom: [snc], pmo: [snc]}
		cancelled = []

		def fake_linked_docs(doc, method="Delete"):
			key = (doc.doctype, doc.name)
			return [
				{"reference_doctype": dt, "reference_docname": dn}
				for dt, dn in links_to.get(key, [])
				if docstatus[(dt, dn)] == 1
			]

		def get_doc(dt, dn):
			doc = frappe._dict(doctype=dt, name=dn)

			def cancel():
				if delete_doc.get_linked_docs(doc, "Cancel"):
					raise frappe.LinkExistsError(f"{dt} {dn} is linked")
				cancelled.append((dt, dn))
				docstatus[(dt, dn)] = 2

			doc.cancel = cancel
			return doc

		from jewellery_erpnext import utils

		with (
			patch.object(delete_doc, "get_linked_docs", side_effect=fake_linked_docs),
			patch.object(delete_doc, "get_dynamic_linked_docs", return_value=[]),
			patch.object(
				utils,
				"get_submitted_links",
				side_effect=lambda dt, names: {
					n: [
						(link["reference_doctype"], link["reference_docname"])
						for link in fake_linked_docs(
							frappe._dict(doctype=dt, name=n), "Cancel"
						)
					]
					for n in names
				},
			),
			patch.object(cancel_all, "get_cancel_plan", return_value=plan),
			patch.object(cancel_all, "mark_workflow_cancelled"),
			patch.object(cancel_all, "_released_by_plan", return_value={}),
			patch.object(cancel_all, "_other_orders_covered", return_value=[]),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=lambda dt, dn, f: docstatus[(dt, dn)],
			),
			patch.object(
				cancel_all,
				"_still_submitted",
				side_effect=lambda keys: {k for k in keys if docstatus[k] == 1},
			),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(frappe, "publish_progress"),
			patch.object(frappe, "publish_realtime") as publish,
		):
			cancel_all.cancel_all("PMO1", "Administrator")

		self.assertEqual(cancelled, [snc, bom, pmo])
		message = publish.call_args.args[1]
		self.assertEqual(message["status"], "done")
		self.assertEqual(message["loop_breaks"], [snc])

	def _run_restarts(self, plan, cancel):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		docstatus = dict.fromkeys(plan, 1)
		snapshots = []

		def savepoint(name):
			if name.startswith("pmo_cancel_run_"):
				snapshots.append(dict(docstatus))

		def rollback(save_point=None):
			if save_point and save_point.startswith("pmo_cancel_run_"):
				docstatus.clear()
				docstatus.update(snapshots[-1])

		def get_doc(dt, dn):
			doc = frappe._dict(doctype=dt, name=dn)
			doc.cancel = lambda: cancel((dt, dn), docstatus)
			return doc

		with (
			patch.object(cancel_all, "get_cancel_plan", return_value=plan),
			patch.object(cancel_all, "mark_workflow_cancelled"),
			patch.object(cancel_all, "get_linked_records", return_value=({}, [])),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=lambda dt, dn, f: docstatus[(dt, dn)],
			),
			patch.object(
				cancel_all,
				"_still_submitted",
				side_effect=lambda keys: {k for k in keys if docstatus[k] == 1},
			),
			patch.object(frappe.db, "savepoint", side_effect=savepoint),
			patch.object(frappe.db, "rollback", side_effect=rollback),
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(frappe, "publish_progress"),
			patch.object(frappe, "publish_realtime") as publish,
		):
			cancel_all.cancel_all("PMO1", "Administrator")

		return docstatus, publish.call_args.args[1]

	def test_cancelled_link_teaches_order_and_restarts(self):
		"""Quotation's hook saves a Tracking Bom that links to a BOM: the BOM must wait for it."""
		bom, qtn, pmo = (
			("BOM", "BOM-1"),
			("Quotation", "QTN-1"),
			("Parent Manufacturing Order", "PMO1"),
		)

		def cancel(key, docstatus):
			if key == qtn and docstatus[bom] == 2:
				raise frappe.CancelledLinkError(
					"Cannot link cancelled document: Reference Docname: BOM-1"
				)
			docstatus[key] = 2

		docstatus, message = self._run_restarts([bom, qtn, pmo], cancel)

		self.assertEqual(set(docstatus.values()), {2})
		self.assertEqual(message["learned_order"], [(qtn, [bom])])

	def test_editing_a_cancelled_doc_teaches_order_and_restarts(self):
		tracking, qtn, pmo = (
			("Tracking Bom", "TB-1"),
			("Quotation", "QTN-1"),
			("Parent Manufacturing Order", "PMO1"),
		)

		class FakeTrackingBom:
			doctype, name = tracking

			def check_docstatus_transition(self, to_docstatus):
				raise frappe.ValidationError("Cannot edit cancelled document")

		def cancel(key, docstatus):
			if key == qtn and docstatus[tracking] == 2:
				FakeTrackingBom().check_docstatus_transition(2)
			docstatus[key] = 2

		docstatus, message = self._run_restarts([tracking, qtn, pmo], cancel)

		self.assertEqual(set(docstatus.values()), {2})
		self.assertEqual(message["learned_order"], [(qtn, [tracking])])

	def test_rule_follows_the_document_that_cancelled_the_blocker(self):
		"""The Sales Order's cancel takes the Tracking Bom down with it, so the Sales Order must wait."""
		so, tracking, qtn, pmo = (
			("Sales Order", "SO-1"),
			("Tracking Bom", "TB-1"),
			("Quotation", "QTN-1"),
			("Parent Manufacturing Order", "PMO1"),
		)

		class FakeTrackingBom:
			doctype, name = tracking

			def check_docstatus_transition(self, to_docstatus):
				raise frappe.ValidationError("Cannot edit cancelled document")

		def cancel(key, docstatus):
			if key == qtn and docstatus[tracking] == 2:
				FakeTrackingBom().check_docstatus_transition(2)
			docstatus[key] = 2
			if key == so:
				docstatus[tracking] = 2

		docstatus, message = self._run_restarts([so, tracking, qtn, pmo], cancel)

		self.assertEqual(set(docstatus.values()), {2})
		self.assertEqual(len(message["learned_order"]), 1)
		self.assertEqual(message["learned_order"][0][0], qtn)
		self.assertEqual(set(message["learned_order"][0][1]), {tracking, so})

	def test_same_rule_twice_stops_instead_of_looping(self):
		tracking, qtn, pmo = (
			("Tracking Bom", "TB-1"),
			("Quotation", "QTN-1"),
			("Parent Manufacturing Order", "PMO1"),
		)

		class FakeTrackingBom:
			doctype, name = tracking

			def check_docstatus_transition(self, to_docstatus):
				raise frappe.ValidationError("Cannot edit cancelled document")

		def cancel(key, docstatus):
			if key == qtn:
				# a hook that fails whatever the order: waiting cannot fix it, so don't restart forever
				FakeTrackingBom().check_docstatus_transition(2)
			docstatus[key] = 2

		with patch.object(frappe, "log_error"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._run_restarts([tracking, qtn, pmo], cancel)
		self.assertIn("still cannot be cancelled", str(ctx.exception))

	def _linked_records(self, plan, links_to, released=None):
		"""links_to[X] = records linking to X (what Frappe's cancel check would report)."""

		from jewellery_erpnext import utils
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with (
			patch.object(cancel_all, "_released_by_plan", return_value=released or {}),
			patch.object(cancel_all, "_other_orders_covered", return_value=[]),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda dt, dn: frappe._dict(doctype=dt, name=dn),
			),
			patch.object(
				utils,
				"get_submitted_links",
				side_effect=lambda dt, names: {
					n: list(links_to.get((dt, n), [])) for n in names
				},
			),
		):
			return cancel_all.get_linked_records(plan)

	def test_linked_records_split_inside_and_outside_the_plan(self):
		se, mr, pmo = (
			("Stock Entry", "SE1"),
			("Material Request", "MR1"),
			("Parent Manufacturing Order", "PMO1"),
		)
		referrers, outside = self._linked_records(
			[se, mr, pmo],
			{
				mr: [se],
				# reversed by the Stock Entry's own cancel, not blockers
				se: [
					("Serial and Batch Bundle", "SBB1"),
					("GL Entry", "GLE1"),
					("Stock Ledger Entry", "SLE1"),
				],
				pmo: [mr, ("Sales Invoice", "SINV-1")],
			},
		)

		self.assertEqual(referrers, {mr: {se}, pmo: {mr}})
		self.assertEqual(outside, [(pmo, ("Sales Invoice", "SINV-1"))])

	def test_outside_link_stops_before_anything_is_cancelled(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		pmo = ("Parent Manufacturing Order", "PMO1")
		with (
			patch.object(cancel_all, "get_cancel_plan", return_value=[pmo]),
			patch.object(
				cancel_all,
				"get_linked_records",
				return_value=({}, [(pmo, ("Sales Invoice", "SINV-1"))]),
			),
			patch.object(cancel_all, "_cancel_in_order") as run,
			patch.object(frappe, "log_error"),
			patch.object(frappe, "publish_realtime") as publish,
		):
			self.assertRaises(
				frappe.ValidationError, cancel_all.cancel_all, "PMO1", "Administrator"
			)

		run.assert_not_called()
		self.assertIn("SINV-1", publish.call_args.args[1]["error"])

	def test_records_linking_to_x_are_ordered_before_x(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		qtn, so, tb, pmo = (
			("Quotation", "Q"),
			("Sales Order", "SO"),
			("Tracking Bom", "TB"),
			("Parent Manufacturing Order", "P"),
		)
		# newest-first would try the Quotation first, but the Sales Order links to it
		plan = [qtn, tb, so, pmo]
		order = cancel_all._order_by_links(plan, {qtn: {so}, tb: {qtn}, pmo: {tb}})

		self.assertEqual(order, [so, qtn, tb, pmo])

	def test_link_loop_is_entered_at_its_newest_record(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		snc, bom, pmo = (
			("Serial Number Creator", "S"),
			("BOM", "B"),
			("Parent Manufacturing Order", "P"),
		)
		order = cancel_all._order_by_links(
			[bom, snc, pmo], {snc: {bom}, bom: {snc}, pmo: {snc}}
		)

		self.assertEqual(order, [bom, snc, pmo])

	def test_record_is_not_attempted_while_a_planned_record_still_links_to_it(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		so, qtn = ("Sales Order", "SO"), ("Quotation", "Q")
		docstatus = {so: 1, qtn: 1}
		attempted = []

		def try_cancel(dt, dn, *args):
			attempted.append((dt, dn))
			docstatus[(dt, dn)] = 2
			return True

		with (
			patch.object(
				frappe.db,
				"get_value",
				side_effect=lambda dt, dn, f: docstatus[(dt, dn)],
			),
			patch.object(
				cancel_all,
				"_still_submitted",
				side_effect=lambda keys: {k for k in keys if docstatus[k] == 1},
			),
			patch.object(cancel_all, "_try_cancel", side_effect=try_cancel),
		):
			cancel_all._cancel_in_order(
				[qtn, so], {}, (), lambda key: None, {}, {qtn: {so}}
			)

		self.assertEqual(attempted, [so, qtn])

	def test_unrelated_validation_error_is_not_learned(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		def cancel(key, docstatus):
			raise frappe.ValidationError("some business rule")

		with patch.object(frappe, "log_error"):
			self.assertRaises(
				frappe.ValidationError,
				self._run_restarts,
				[("Quotation", "QTN-1"), ("Parent Manufacturing Order", "PMO1")],
				cancel,
			)
		self.assertEqual(cancel_all._PlanDocs.value, frozenset())

	def test_loop_rule_never_ignores_links_from_outside_the_plan(self):
		from frappe.model import delete_doc

		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		outside = {"reference_doctype": "Sales Invoice", "reference_docname": "SINV-1"}
		inside = {"reference_doctype": "BOM", "reference_docname": "BOM1"}
		with patch.object(
			delete_doc, "get_linked_docs", return_value=[outside, inside]
		):
			with cancel_all._ignore_links_from({("BOM", "BOM1")}):
				self.assertEqual(
					delete_doc.get_linked_docs(frappe._dict(), "Cancel"), [outside]
				)
				# deletes keep every link
				self.assertEqual(
					delete_doc.get_linked_docs(frappe._dict(), "Delete"),
					[outside, inside],
				)

	def test_negative_stock_is_retried_on_next_pass(self):
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
			BatchNegativeStockError,
		)

		plan = [
			("Stock Entry", "SE-EARLY"),
			("Stock Entry", "SE-LATE"),
			("Parent Manufacturing Order", "PMO1"),
		]
		docstatus = dict.fromkeys(plan, 1)
		effects = {
			("Stock Entry", "SE-EARLY"): [
				BatchNegativeStockError("batch negative"),
				None,
			]
		}

		cancelled, _rollback, message = self._run(plan, docstatus, effects)

		self.assertEqual(
			cancelled,
			[
				("Stock Entry", "SE-LATE"),
				("Parent Manufacturing Order", "PMO1"),
				("Stock Entry", "SE-EARLY"),
			],
		)
		self.assertEqual(message["status"], "done")

	def test_failed_attempt_drops_cached_bundles(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		frappe.local.cache["document_cache::Serial and Batch Bundle::SBB1"] = "stale"
		frappe.flags.currently_saving.append(("Stock Entry", "SE1"))
		with patch.object(frappe, "clear_document_cache") as clear:
			cancel_all._reset_after_failed_attempt("Stock Entry", "SE1")

		self.assertNotIn(
			"document_cache::Serial and Batch Bundle::SBB1", frappe.local.cache
		)
		self.assertEqual(frappe.flags.currently_saving, [])
		cleared = {c.args[0] for c in clear.call_args_list}
		self.assertEqual(cleared, {"Serial and Batch Bundle", "Stock Entry"})

	def test_bundles_are_left_to_their_stock_entry(self):
		plan = self._plan()

		self.assertNotIn(("Serial and Batch Bundle", "SBB3"), plan)
		# and the walk does not continue through a bundle either
		self.assertNotIn(("Sales Order", "SO-UNRELATED"), plan)

	def _run(self, plan, docstatus, cancel_side_effects, expect_error=None):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		cancelled = []

		def get_doc(dt, dn):
			doc = frappe._dict(doctype=dt, name=dn)

			def cancel():
				effects = cancel_side_effects.get((dt, dn))
				if effects:
					effect = effects.pop(0)
					if effect:
						raise effect
				cancelled.append((dt, dn))
				docstatus[(dt, dn)] = 2

			doc.cancel = cancel
			return doc

		with (
			patch.object(cancel_all, "get_cancel_plan", return_value=plan),
			patch.object(cancel_all, "mark_workflow_cancelled"),
			patch.object(cancel_all, "get_linked_records", return_value=({}, [])),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=lambda dt, dn, f: docstatus[(dt, dn)],
			),
			patch.object(
				cancel_all,
				"_still_submitted",
				side_effect=lambda keys: {k for k in keys if docstatus[k] == 1},
			),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(frappe, "publish_progress"),
			patch.object(frappe, "log_error"),
			patch.object(frappe, "publish_realtime") as publish,
		):
			if expect_error:
				self.assertRaises(
					expect_error, cancel_all.cancel_all, "PMO1", "Administrator"
				)
			else:
				cancel_all.cancel_all("PMO1", "Administrator")

		return cancelled, rollback, publish.call_args.args[1]

	def test_link_error_is_retried_on_next_pass(self):
		plan = [("Stock Entry", "SE1"), ("Parent Manufacturing Order", "PMO1")]
		docstatus = dict.fromkeys(plan, 1)
		# PMO1 is tried first here to force a LinkExistsError, then succeeds after SE1.
		plan = list(reversed(plan))
		effects = {
			("Parent Manufacturing Order", "PMO1"): [
				frappe.LinkExistsError("linked"),
				None,
			]
		}

		cancelled, rollback, message = self._run(plan, docstatus, effects)

		self.assertEqual(
			cancelled, [("Stock Entry", "SE1"), ("Parent Manufacturing Order", "PMO1")]
		)
		self.assertEqual(message["status"], "done")
		# one savepoint rollback for the retried record (the other is the job's fresh-transaction start)
		self.assertEqual(
			len([c for c in rollback.call_args_list if c.kwargs.get("save_point")]), 1
		)
		self.assertIn("save_point", rollback.call_args.kwargs)

	def test_already_cancelled_doc_is_skipped(self):
		plan = [("Stock Entry", "SE1"), ("Parent Manufacturing Order", "PMO1")]
		docstatus = {
			("Stock Entry", "SE1"): 2,
			("Parent Manufacturing Order", "PMO1"): 1,
		}

		cancelled, _rollback, message = self._run(plan, docstatus, {})

		self.assertEqual(cancelled, [("Parent Manufacturing Order", "PMO1")])
		self.assertEqual(message["status"], "done")

	def test_no_progress_raises_so_job_runner_rolls_back(self):
		plan = [("Stock Entry", "SE1"), ("Parent Manufacturing Order", "PMO1")]
		docstatus = dict.fromkeys(plan, 1)
		stuck = [frappe.LinkExistsError("still linked")] * 5
		effects = {("Parent Manufacturing Order", "PMO1"): list(stuck)}

		_cancelled, _rollback, message = self._run(
			plan, docstatus, effects, expect_error=frappe.ValidationError
		)

		self.assertEqual(message["status"], "failed")
		self.assertIn("PMO1", message["error"])

	def test_other_error_raises_immediately(self):
		plan = [("Stock Entry", "SE1"), ("Parent Manufacturing Order", "PMO1")]
		docstatus = dict.fromkeys(plan, 1)
		effects = {("Stock Entry", "SE1"): [frappe.ValidationError("negative stock")]}

		cancelled, _rollback, message = self._run(
			plan, docstatus, effects, expect_error=frappe.ValidationError
		)

		self.assertEqual(cancelled, [])
		self.assertEqual(message["status"], "failed")
		self.assertIn("negative stock", message["error"])

	def test_only_system_manager_can_start(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
			cancel_all,
		)

		with (
			patch.dict(frappe.local.session, {"user": "someone@example.com"}),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(frappe, "enqueue") as enqueue,
		):
			self.assertRaises(
				frappe.PermissionError, cancel_all.enqueue_cancel_all, "PMO1"
			)
			self.assertRaises(
				frappe.PermissionError, cancel_all.get_cancel_preview, "PMO1"
			)
		enqueue.assert_not_called()


def _bom_raw_material(item_code, qty, **kwargs):
	row = {
		"item_code": item_code,
		"qty": qty,
		"from_warehouse": "RM-WH",
		"warehouse": "DEPT-WH",
		"is_customer_item": 0,
		"sub_setting_type": None,
		"pcs": None,
	}
	row.update(kwargs)
	return row


class TestPendingRawMaterials(UnitTestCase):
	"""Pending qty for the Create Material Request button: BOM requirement less what the
	PMO's non-cancelled Material Requests already cover, so nothing is requested twice."""

	# Template plus every attribute except Diamond Grade, as _get_grade_free_key builds it.
	GRADE_FREE_KEYS = {
		"D-NT-RO-7-+8-8.5": ("D", "Natural", "Round", "+8-8.5"),
		"D-NT-RO-6B-+8-8.5": ("D", "Natural", "Round", "+8-8.5"),
		"D-NT-RO-6B-+9-9.5": ("D", "Natural", "Round", "+9-9.5"),
	}

	def _run_rows(self, bom, requested, pmo_qty=1, grade_free_key=None):
		"""``requested`` is keyed like _get_requested_qty_by_item; a bare item code stands
		for a company-owned line with no sub setting type."""
		doc = SimpleNamespace(
			name="PMO-TEST-0001",
			qty=pmo_qty,
			manufacturer="MNF",
			get_bom_raw_materials=lambda: bom,
		)
		requested = {
			key if isinstance(key, tuple) else (key, 0, ""): qty
			for key, qty in requested.items()
		}
		with (
			patch(
				f"{PMO_MODULE}._get_requested_qty_by_item",
				return_value=requested,
			),
			patch(
				f"{PMO_MODULE}._get_grade_free_key",
				side_effect=grade_free_key or self.GRADE_FREE_KEYS.get,
			),
			patch(f"{PMO_MODULE}.frappe.get_precision", return_value=3),
		):
			return get_pending_raw_materials(doc)

	def _run(self, bom, requested, pmo_qty=1, grade_free_key=None):
		rows = self._run_rows(bom, requested, pmo_qty, grade_free_key)
		return {row["item_code"]: row for row in rows}

	# One item on several BOM rows (review finding: matching by item code alone).

	def test_request_covers_the_row_it_was_raised_for_not_the_first_row(self):
		"""Customer and company rows of one stone; only the company request remains."""
		customer_row, company_row = self._run_rows(
			{
				"gemstone_item": [
					_bom_raw_material("G-RUBY", 5, is_customer_item=1),
					_bom_raw_material("G-RUBY", 5, is_customer_item=0),
				]
			},
			{("G-RUBY", 0, ""): 5},
		)
		self.assertQty(customer_row, 5, 0, 5)
		self.assertQty(company_row, 5, 5, 0)

	def test_rows_of_one_item_in_two_settings_are_kept_apart(self):
		open_row, close_row = self._run_rows(
			{
				"gemstone_item": [
					_bom_raw_material("G-RUBY", 0.4, sub_setting_type="Open"),
					_bom_raw_material("G-RUBY", 0.6, sub_setting_type="Close"),
				]
			},
			{("G-RUBY", 0, "Close"): 0.6},
		)
		self.assertQty(open_row, 0.4, 0, 0.4)
		self.assertQty(close_row, 0.6, 0.6, 0)

	def test_line_edited_to_other_ownership_still_counts_by_item(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 2.0)]},
			{("M-G-22KT", 1, ""): 2.0},
		)
		self.assertQty(rows["M-G-22KT"], 2.0, 2.0, 0)

	def test_exact_row_match_comes_before_item_code_match(self):
		"""Leftover by item code fills only what exact matches did not."""
		customer_row, company_row = self._run_rows(
			{
				"gemstone_item": [
					_bom_raw_material("G-RUBY", 5, is_customer_item=1),
					_bom_raw_material("G-RUBY", 5, is_customer_item=0),
				]
			},
			{("G-RUBY", 0, ""): 5, ("G-RUBY", 1, "Bezel"): 2},
		)
		self.assertQty(customer_row, 5, 2, 3)
		self.assertQty(company_row, 5, 5, 0)

	def assertQty(self, row, required, requested, pending):
		self.assertEqual(
			(row["required_qty"], row["requested_qty"], row["pending_qty"]),
			(required, requested, pending),
		)

	def test_fully_requested_item_has_nothing_pending(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 9.351)]},
			{"M-G-22KT": 9.351},
		)
		self.assertQty(rows["M-G-22KT"], 9.351, 9.351, 0)

	def test_partly_requested_item_keeps_the_remainder(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 2.0)]},
			{"M-G-22KT": 1.2},
		)
		self.assertQty(rows["M-G-22KT"], 2.0, 1.2, 0.8)

	def test_item_added_to_the_bom_later_is_fully_pending(self):
		rows = self._run(
			{
				"metal_item": [_bom_raw_material("M-G-22KT", 2.0)],
				"finding_item": [_bom_raw_material("F-SCREW", 1.1)],
			},
			{"M-G-22KT": 2.0},
		)
		self.assertQty(rows["M-G-22KT"], 2.0, 2.0, 0)
		self.assertQty(rows["F-SCREW"], 1.1, 0, 1.1)

	def test_required_qty_is_bom_qty_times_pmo_qty(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 1.25)]},
			{"M-G-22KT": 1.0},
			pmo_qty=3,
		)
		self.assertQty(rows["M-G-22KT"], 3.75, 1.0, 2.75)

	def test_zero_qty_bom_row_is_left_out(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 0)]},
			{},
		)
		self.assertEqual(rows, {})

	def test_request_above_the_requirement_never_goes_negative(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 2.0)]},
			{"M-G-22KT": 5.0},
		)
		self.assertQty(rows["M-G-22KT"], 2.0, 2.0, 0)

	def test_regraded_diamond_counts_as_requested(self):
		"""The reported case: BOM resolves grade 7, the user regraded the request to 6B."""
		rows = self._run(
			{"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.78)]},
			{"D-NT-RO-6B-+8-8.5": 0.78},
		)
		self.assertQty(rows["D-NT-RO-7-+8-8.5"], 0.78, 0.78, 0)

	def test_exact_grade_match_is_taken_before_the_regrade_fallback(self):
		rows = self._run(
			{
				"diamond_item": [
					_bom_raw_material("D-NT-RO-7-+8-8.5", 0.78),
					_bom_raw_material("D-NT-RO-6B-+8-8.5", 0.5),
				]
			},
			{"D-NT-RO-6B-+8-8.5": 0.5},
		)
		self.assertQty(rows["D-NT-RO-6B-+8-8.5"], 0.5, 0.5, 0)
		self.assertQty(rows["D-NT-RO-7-+8-8.5"], 0.78, 0, 0.78)

	def test_only_the_leftover_after_exact_match_is_regraded(self):
		rows = self._run(
			{
				"diamond_item": [
					_bom_raw_material("D-NT-RO-7-+8-8.5", 0.78),
					_bom_raw_material("D-NT-RO-6B-+8-8.5", 0.5),
				]
			},
			{"D-NT-RO-6B-+8-8.5": 0.9},
		)
		self.assertQty(rows["D-NT-RO-6B-+8-8.5"], 0.5, 0.5, 0)
		self.assertQty(rows["D-NT-RO-7-+8-8.5"], 0.78, 0.4, 0.38)

	def test_different_sieve_size_is_not_matched(self):
		rows = self._run(
			{"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.78)]},
			{"D-NT-RO-6B-+9-9.5": 0.78},
		)
		self.assertQty(rows["D-NT-RO-7-+8-8.5"], 0.78, 0, 0.78)

	def test_regrade_fallback_is_only_for_diamonds(self):
		rows = self._run(
			{"metal_item": [_bom_raw_material("M-NEW", 2.0)]},
			{"M-OLD": 2.0},
			grade_free_key=lambda item_code: ("M", "same"),
		)
		self.assertQty(rows["M-NEW"], 2.0, 0, 2.0)


class TestRequestedQtyByItem(UnitTestCase):
	"""What the PMO's Material Requests already cover, per BOM row requirement
	(item, ownership, sub setting type), in stock UOM."""

	def _run(self, lines, default_gemstone_item="G-PER-DUM-PRE-CC", for_update=False):
		doc = SimpleNamespace(name="PMO-TEST-0001", manufacturer="MNF")
		lines = [
			frappe._dict(
				{"custom_is_customer_item": 0, "custom_sub_setting_type": None, **line}
			)
			for line in lines
		]
		with (
			patch.object(frappe.db, "sql", return_value=lines) as sql,
			patch.object(frappe.db, "get_value", return_value=default_gemstone_item),
		):
			requested = _get_requested_qty_by_item(doc, for_update=for_update)
		# Only lines of this PMO's draft and submitted Material Requests are read.
		query, params = str(sql.call_args.args[0]), sql.call_args.args[1]
		self.assertIn("`manufacturing_order`=%(param1)s", query)
		self.assertEqual(params["param1"], "PMO-TEST-0001")
		self.assertIn("`docstatus`<2", query)
		self.last_query = query
		return requested

	def test_lines_of_the_same_item_are_summed(self):
		requested = self._run(
			[
				{"item_code": "M-G-22KT", "description": "M", "stock_qty": 1.5},
				{"item_code": "M-G-22KT", "description": "M", "stock_qty": 0.5},
				{"item_code": "F-SCREW", "description": "F", "stock_qty": 1.1},
			]
		)
		self.assertEqual(requested, {("M-G-22KT", 0, ""): 2.0, ("F-SCREW", 0, ""): 1.1})

	def test_gemstone_line_counts_against_the_bom_item_in_its_description(self):
		requested = self._run(
			[
				{
					"item_code": "G-PER-DUM-PRE-CC",
					"description": "<p>G-RUBY</p>",
					"stock_qty": 0.38,
				}
			]
		)
		self.assertEqual(requested, {("G-RUBY", 0, ""): 0.38})

	def test_without_default_gemstone_item_lines_keep_their_item_code(self):
		requested = self._run(
			[
				{
					"item_code": "G-PER-DUM-PRE-CC",
					"description": "G-RUBY",
					"stock_qty": 0.38,
				}
			],
			default_gemstone_item=None,
		)
		self.assertEqual(requested, {("G-PER-DUM-PRE-CC", 0, ""): 0.38})

	def test_ownership_and_sub_setting_type_keep_lines_apart(self):
		requested = self._run(
			[
				{"item_code": "D-A", "description": "D", "stock_qty": 1.0},
				{
					"item_code": "D-A",
					"description": "D",
					"stock_qty": 0.5,
					"custom_is_customer_item": 1,
				},
				{
					"item_code": "D-A",
					"description": "D",
					"stock_qty": 0.2,
					"custom_sub_setting_type": "Open",
				},
			]
		)
		self.assertEqual(
			requested,
			{("D-A", 0, ""): 1.0, ("D-A", 1, ""): 0.5, ("D-A", 0, "Open"): 0.2},
		)

	def test_stock_uom_quantity_is_counted(self):
		"""A line in another UOM counts by its stock qty, the unit the BOM uses."""
		requested = self._run(
			[{"item_code": "M-G-22KT", "description": "M", "qty": 2, "stock_qty": 2000}]
		)
		self.assertEqual(requested, {("M-G-22KT", 0, ""): 2000})
		self.assertIn("`stock_qty`", self.last_query)

	def test_creation_reads_the_requests_with_a_locking_read(self):
		self._run([], for_update=True)
		self.assertIn("FOR UPDATE", self.last_query)

	def test_plain_reads_take_no_lock(self):
		self._run([])
		self.assertNotIn("FOR UPDATE", self.last_query)


class TestGradeFreeKey(UnitTestCase):
	"""Two diamonds share a key only when they differ in Diamond Grade alone."""

	ATTRIBUTES = {
		"D-NT-RO-7-+8-8.5": [
			("Diamond Type", "Natural"),
			("Stone Shape", "Round"),
			("Diamond Grade", "7"),
			("Diamond Sieve Size", "+8-8.5"),
		],
		"D-NT-RO-6B-+8-8.5": [
			("Diamond Type", "Natural"),
			("Stone Shape", "Round"),
			("Diamond Grade", "6B"),
			("Diamond Sieve Size", "+8-8.5"),
		],
		"D-NT-RO-6B-+9-9.5": [
			("Diamond Type", "Natural"),
			("Stone Shape", "Round"),
			("Diamond Grade", "6B"),
			("Diamond Sieve Size", "+9-9.5"),
		],
		"M-G-22KT": [("Metal Type", "Gold"), ("Metal Touch", "22KT")],
	}

	def _key(self, item_code, variant_of="D"):
		attributes = [
			frappe._dict(attribute=a, attribute_value=v)
			for a, v in self.ATTRIBUTES.get(item_code, [])
		]
		with (
			patch(f"{PMO_MODULE}.frappe.db.get_value", return_value=variant_of),
			patch(f"{PMO_MODULE}.frappe.get_all", return_value=attributes),
		):
			return _get_grade_free_key(item_code)

	def test_same_diamond_in_another_grade_shares_the_key(self):
		self.assertEqual(self._key("D-NT-RO-7-+8-8.5"), self._key("D-NT-RO-6B-+8-8.5"))

	def test_another_sieve_size_gets_another_key(self):
		self.assertNotEqual(
			self._key("D-NT-RO-6B-+8-8.5"), self._key("D-NT-RO-6B-+9-9.5")
		)

	def test_variant_without_diamond_grade_has_no_key(self):
		self.assertIsNone(self._key("M-G-22KT", variant_of="M"))

	def test_item_that_is_not_a_variant_has_no_key(self):
		self.assertIsNone(self._key("D-NT-RO-7-+8-8.5", variant_of=None))


class TestTrackingBomIsDraft(UnitTestCase):
	"""Only a draft Tracking BOM can still change; a submitted one means the item is made."""

	def _is_draft(self, tracking_bom, docstatus=None):
		doc = SimpleNamespace(custom_tracking_bom=tracking_bom)
		with patch(
			f"{PMO_MODULE}.frappe.db.get_value", return_value=docstatus
		) as get_value:
			return tracking_bom_is_draft(doc), get_value

	def test_draft_tracking_bom(self):
		is_draft, get_value = self._is_draft("TB-0001", docstatus=0)
		self.assertTrue(is_draft)
		get_value.assert_called_once_with("Tracking Bom", "TB-0001", "docstatus")

	def test_submitted_or_cancelled_tracking_bom(self):
		self.assertFalse(self._is_draft("TB-0001", docstatus=1)[0])
		self.assertFalse(self._is_draft("TB-0001", docstatus=2)[0])

	def test_pmo_without_tracking_bom(self):
		is_draft, get_value = self._is_draft(None)
		self.assertFalse(is_draft)
		get_value.assert_not_called()

	def test_check_refuses_a_submitted_tracking_bom(self):
		with patch(f"{PMO_MODULE}.tracking_bom_is_draft", return_value=False):
			with self.assertRaises(frappe.ValidationError) as caught:
				check_tracking_bom_is_draft(
					SimpleNamespace(custom_tracking_bom="TB-0001")
				)
		self.assertIn("already manufactured", str(caught.exception))


class TestGetBomRawMaterials(UnitTestCase):
	def test_missing_tracking_bom_is_refused(self):
		doc = SimpleNamespace(custom_tracking_bom=None)
		self.assertRaises(
			frappe.ValidationError, ParentManufacturingOrder.get_bom_raw_materials, doc
		)

	# The Tracking BOM read that PMO submit and the button share.

	ITEM_TYPES = {
		"M-1": "metal_item",
		"D-1": "diamond_item",
		"F-1": "finding_item",
		"G-1": "gemstone_item",
		"O-1": "other_item",
	}

	def _read(
		self, rows_by_table, warehouse_variants=("M", "D", "F", "G", "O"), resolve=None
	):
		pmo = SimpleNamespace(
			custom_tracking_bom="TB-0001", manufacturer="MNF", diamond_grade="7"
		)
		warehouses = [
			frappe._dict(
				variant=variant,
				department=f"DEPT-{variant}",
				target_warehouse=f"TGT-{variant}",
			)
			for variant in warehouse_variants
		]

		def get_all(doctype, filters=None, fields=None, **kwargs):
			return [frappe._dict(row) for row in rows_by_table.get(doctype, [])]

		with (
			patch.object(frappe.db, "get_all", return_value=warehouses),
			patch.object(frappe.db, "has_column", return_value=True),
			patch(f"{PMO_MODULE}.frappe.get_all", side_effect=get_all),
			patch(
				f"{PMO_MODULE}._get_raw_material_warehouse",
				side_effect=lambda department: f"{department}-RM",
			),
			patch(
				f"{PMO_MODULE}._resolve_existing_variant_item_code",
				side_effect=resolve
				or (
					lambda row, table, grade: row.get("item_variant")
					or row.get("item_code")
				),
			),
			patch(f"{PMO_MODULE}._validate_non_template_item"),
			patch(f"{PMO_MODULE}._validate_item_exists"),
			patch(f"{PMO_MODULE}._validate_metal_item_attributes"),
			patch(
				f"{PMO_MODULE}.get_item_type",
				side_effect=lambda item_code: self.ITEM_TYPES[item_code],
			),
		):
			return ParentManufacturingOrder.get_bom_raw_materials(pmo)

	def test_rows_are_bucketed_by_item_type_with_their_warehouses(self):
		items = self._read(
			{
				"BOM Metal Detail": [
					{"item_variant": "M-1", "quantity": 2.0, "is_customer_item": 0}
				],
				"BOM Diamond Detail": [
					{
						"item_variant": "D-1",
						"quantity": 0.5,
						"pcs": 10,
						"is_customer_item": 1,
						"sub_setting_type": "Open",
					}
				],
				"BOM Finding Detail": [
					{
						"item_variant": "F-1",
						"quantity": 1.1,
						"qty": 2,
						"is_customer_item": 0,
					}
				],
				"BOM Gemstone Detail": [
					{
						"item_variant": "G-1",
						"quantity": 0.4,
						"pcs": 3,
						"is_customer_item": 0,
					}
				],
				"BOM Other Detail": [{"item_code": "O-1", "quantity": 5, "qty": 1}],
			}
		)
		self.assertEqual(
			{
				bucket: [row["item_code"] for row in rows]
				for bucket, rows in items.items()
			},
			{
				"metal_item": ["M-1"],
				"diamond_item": ["D-1"],
				"finding_item": ["F-1"],
				"gemstone_item": ["G-1"],
				"other_item": ["O-1"],
			},
		)
		diamond = items["diamond_item"][0]
		self.assertEqual(
			(
				diamond["qty"],
				diamond["pcs"],
				diamond["is_customer_item"],
				diamond["sub_setting_type"],
				diamond["from_warehouse"],
				diamond["warehouse"],
			),
			(0.5, 10, 1, "Open", "DEPT-D-RM", "TGT-D"),
		)

	def test_pcs_come_from_qty_for_findings_and_other_items(self):
		items = self._read(
			{
				"BOM Finding Detail": [
					{
						"item_variant": "F-1",
						"quantity": 1.1,
						"qty": 2,
						"is_customer_item": 0,
					}
				],
				"BOM Other Detail": [{"item_code": "O-1", "quantity": 5, "qty": 7}],
			}
		)
		self.assertEqual(items["finding_item"][0]["pcs"], 2)
		self.assertEqual(items["other_item"][0]["pcs"], 7)

	def test_other_items_are_never_customer_items(self):
		items = self._read(
			{
				"BOM Other Detail": [
					{"item_code": "O-1", "quantity": 5, "is_customer_item": 1}
				]
			}
		)
		self.assertEqual(items["other_item"][0]["is_customer_item"], "0")

	def test_empty_tables_give_empty_buckets(self):
		items = self._read({})
		self.assertEqual(
			items,
			{
				"metal_item": [],
				"diamond_item": [],
				"finding_item": [],
				"gemstone_item": [],
				"other_item": [],
			},
		)

	def test_item_type_without_manufacturer_warehouse_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._read(
				{"BOM Metal Detail": [{"item_variant": "M-1", "quantity": 2.0}]},
				warehouse_variants=("D", "F", "G", "O"),
			)
		self.assertIn(
			"Please mention warehouse details in Manufacturer", str(caught.exception)
		)

	def test_row_without_an_item_code_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._read(
				{"BOM Metal Detail": [{"quantity": 2.0}]},
				resolve=lambda row, table, grade: None,
			)
		self.assertIn(
			"item_variant is missing in BOM Metal Detail", str(caught.exception)
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			self._read(
				{"BOM Other Detail": [{"quantity": 2.0}]},
				resolve=lambda row, table, grade: None,
			)
		self.assertIn("item_code is missing in BOM Other Detail", str(caught.exception))


def _pending_row(
	item_code, required_qty, pending_qty, item_type="diamond_item", **kwargs
):
	row = {
		"item_type": item_type,
		"item_code": item_code,
		"required_qty": required_qty,
		"requested_qty": required_qty - pending_qty,
		"pending_qty": pending_qty,
		"pcs": 70,
		"from_warehouse": "RM-WH",
		"warehouse": "DEPT-WH",
		"is_customer_item": 0,
		"sub_setting_type": None,
	}
	row.update(kwargs)
	return row


class TestValidateMaterialRequestItems(UnitTestCase):
	"""The dialog's selection is checked against the pending qty worked out afresh on the
	server, never against what the dialog showed."""

	PENDING = [
		_pending_row("D-NT-RO-7-+8-8.5", 1.736, 1.736),
		_pending_row("M-G-22KT", 2.0, 0.8, item_type="metal_item", pcs=None),
		_pending_row("F-SCREW", 1.1, 0, item_type="finding_item"),
	]

	def _run(
		self, items, pending=None, docstatus=1, blocked=False, tracking_bom_draft=True
	):
		doc = SimpleNamespace(name="PMO-TEST-0001", docstatus=docstatus)

		def refuse_while_blocked(pmo_name, what):
			if blocked:
				frappe.throw(f"{pmo_name} is on Hold. {what}")

		with (
			patch(
				f"{PMO_MODULE}.get_pending_raw_materials",
				return_value=[
					dict(row) for row in (self.PENDING if pending is None else pending)
				],
			),
			patch(
				f"{PMO_MODULE}.refuse_while_blocked", side_effect=refuse_while_blocked
			),
			patch(
				f"{PMO_MODULE}.tracking_bom_is_draft", return_value=tracking_bom_draft
			),
			patch(f"{PMO_MODULE}.frappe.get_precision", return_value=3),
		):
			return validate_material_request_items(doc, items)

	def test_submitted_tracking_bom_is_refused(self):
		self.assertRefused(
			[{"item_code": "M-G-22KT", "qty": 0.8}],
			"only while the PMO's Tracking BOM is in draft",
			tracking_bom_draft=False,
		)

	def assertRefused(self, items, message, **kwargs):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run(items, **kwargs)
		self.assertIn(message, str(caught.exception))
		return str(caught.exception)

	def test_draft_pmo_is_refused(self):
		self.assertRefused(
			[{"item_code": "M-G-22KT", "qty": 0.8}],
			"only for a submitted PMO",
			docstatus=0,
		)

	def test_pmo_on_hold_is_refused(self):
		self.assertRefused(
			[{"item_code": "M-G-22KT", "qty": 0.8}],
			"No Material Request can be created for it.",
			blocked=True,
		)

	def test_nothing_pending_shows_the_no_pending_message(self):
		self.assertRefused(
			[{"item_code": "F-SCREW", "qty": 1}],
			"No pending raw materials are available for Material Request creation.",
			pending=[_pending_row("F-SCREW", 1.1, 0)],
		)

	def test_empty_selection_is_refused(self):
		self.assertRefused([], "Please select at least one raw material.")

	def test_item_not_pending_in_the_bom_is_refused(self):
		self.assertRefused(
			[{"item_code": "F-SCREW", "qty": 0.5}], "is not pending in the BOM"
		)
		self.assertRefused(
			[{"item_code": "NOT-IN-BOM", "qty": 0.5}], "is not pending in the BOM"
		)

	def test_zero_or_negative_qty_is_refused(self):
		self.assertRefused(
			[{"item_code": "M-G-22KT", "qty": 0}], "must be greater than 0"
		)
		self.assertRefused(
			[{"item_code": "M-G-22KT", "qty": -1}], "must be greater than 0"
		)

	def test_qty_above_pending_is_refused(self):
		self.assertRefused(
			[{"item_code": "M-G-22KT", "qty": 0.801}],
			"exceeds the pending quantity (0.8)",
		)

	def test_same_item_selected_twice_is_summed_against_pending(self):
		self.assertRefused(
			[
				{"item_code": "M-G-22KT", "qty": 0.5},
				{"item_code": "M-G-22KT", "qty": 0.5},
			],
			"(1.0) exceeds the pending quantity (0.8)",
		)

	def test_every_problem_is_reported_at_once(self):
		message = self.assertRefused(
			[
				{"item_code": "NOT-IN-BOM", "qty": 1},
				{"item_code": "M-G-22KT", "qty": 5},
				{"item_code": "D-NT-RO-7-+8-8.5", "qty": 0},
			],
			"is not pending in the BOM",
		)
		self.assertIn("exceeds the pending quantity", message)
		self.assertIn("must be greater than 0", message)

	def test_exactly_the_pending_qty_is_accepted(self):
		selected = self._run([{"item_code": "M-G-22KT", "qty": "0.8"}])
		self.assertEqual(selected["metal_item"][0]["qty"], 0.8)

	def test_selection_is_bucketed_with_the_bom_row_details(self):
		selected = self._run(
			[
				{"item_code": "D-NT-RO-7-+8-8.5", "qty": 1.736},
				{"item_code": "M-G-22KT", "qty": 0.5},
			]
		)
		self.assertEqual(
			selected,
			{
				"diamond_item": [
					{
						"item_code": "D-NT-RO-7-+8-8.5",
						"qty": 1.736,
						"from_warehouse": "RM-WH",
						"warehouse": "DEPT-WH",
						"is_customer_item": 0,
						"sub_setting_type": None,
						"pcs": 70,
					}
				],
				"metal_item": [
					{
						"item_code": "M-G-22KT",
						"qty": 0.5,
						"from_warehouse": "RM-WH",
						"warehouse": "DEPT-WH",
						"is_customer_item": 0,
						"sub_setting_type": None,
						"pcs": None,
					}
				],
			},
		)

	def test_bom_pcs_are_dropped_for_part_of_a_row(self):
		selected = self._run([{"item_code": "D-NT-RO-7-+8-8.5", "qty": 1.0}])
		self.assertIsNone(selected["diamond_item"][0]["pcs"])

	def test_qty_the_first_bom_row_covers_leaves_the_next_row_alone(self):
		selected = self._run(
			[{"item_code": "D-NT-RO-7-+8-8.5", "qty": 0.3}],
			pending=[
				_pending_row("D-NT-RO-7-+8-8.5", 0.5, 0.5, sub_setting_type="Open"),
				_pending_row("D-NT-RO-7-+8-8.5", 0.4, 0.4, sub_setting_type="Close"),
			],
		)
		self.assertEqual(
			[(row["qty"], row["sub_setting_type"]) for row in selected["diamond_item"]],
			[(0.3, "Open")],
		)

	def test_qty_is_spread_over_bom_rows_of_the_same_item(self):
		selected = self._run(
			[{"item_code": "D-NT-RO-7-+8-8.5", "qty": 0.7}],
			pending=[
				_pending_row("D-NT-RO-7-+8-8.5", 0.5, 0.5, sub_setting_type="Open"),
				_pending_row("D-NT-RO-7-+8-8.5", 0.4, 0.4, sub_setting_type="Close"),
			],
		)
		self.assertEqual(
			[(row["qty"], row["sub_setting_type"]) for row in selected["diamond_item"]],
			[(0.5, "Open"), (0.2, "Close")],
		)


class TestCreatePMOMaterialRequest(UnitTestCase):
	"""The dialog's Create action: validate, create through the existing logic, note it."""

	SELECTED = {"metal_item": [{"item_code": "M-G-22KT", "qty": 0.8}]}

	def test_pmo_is_locked_before_anything_is_read(self):
		"""Review finding: two calls for one PMO must not both see the same pending qty.
		The PMO row lock comes first, and pending is then read with a locking read."""
		calls = MagicMock()
		doc = MagicMock()
		doc.make_material_requests.return_value = ["MR-0001"]
		calls.get_doc.return_value = doc
		calls.pending.return_value = [_pending_row("M-G-22KT", 0.8, 0.8)]
		with (
			patch.object(frappe.db, "get_value", calls.lock),
			patch(f"{PMO_MODULE}.frappe.get_doc", calls.get_doc),
			patch(f"{PMO_MODULE}.get_pending_raw_materials", calls.pending),
			patch(
				f"{PMO_MODULE}.validate_material_request_items",
				return_value=self.SELECTED,
			),
			patch(f"{PMO_MODULE}.add_material_request_comment"),
			patch(f"{PMO_MODULE}.check_pmo_material_request_role"),
		):
			create_pmo_material_request("PMO-TEST-0001")

		self.assertEqual(
			[name for name, _args, _kwargs in calls.mock_calls if "." not in name][:3],
			["lock", "get_doc", "pending"],
		)
		self.assertEqual(
			calls.lock.call_args,
			(
				("Parent Manufacturing Order", "PMO-TEST-0001", "name"),
				{"for_update": True},
			),
		)
		self.assertEqual(calls.pending.call_args.kwargs, {"for_update": True})

	def test_validated_selection_is_created_without_pmo_qty_multiplier(self):
		doc = MagicMock()
		doc.make_material_requests.return_value = ["MR-0001"]
		with (
			patch(f"{PMO_MODULE}.frappe.get_doc", return_value=doc),
			patch(
				f"{PMO_MODULE}.validate_material_request_items",
				return_value=self.SELECTED,
			) as validate,
			patch(f"{PMO_MODULE}.add_material_request_comment") as comment,
		):
			created = create_pmo_material_request(
				"PMO-TEST-0001", '[{"item_code": "M-G-22KT", "qty": 0.8}]'
			)

		self.assertEqual(created, ["MR-0001"])
		doc.check_permission.assert_called_once_with("read")
		validate.assert_called_once_with(doc, [{"item_code": "M-G-22KT", "qty": 0.8}])
		doc.make_material_requests.assert_called_once_with(self.SELECTED, 1)
		comment.assert_called_once_with(doc, ["MR-0001"])

	def test_nothing_is_created_when_validation_fails(self):
		doc = MagicMock()
		with (
			patch(f"{PMO_MODULE}.frappe.get_doc", return_value=doc),
			patch(
				f"{PMO_MODULE}.validate_material_request_items",
				side_effect=frappe.ValidationError,
			),
		):
			self.assertRaises(
				frappe.ValidationError,
				create_pmo_material_request,
				"PMO-TEST-0001",
				"[]",
			)
		doc.make_material_requests.assert_not_called()

	def _run_without_items(self, pending):
		doc = MagicMock()
		doc.make_material_requests.return_value = ["MR-0001"]
		with (
			patch(f"{PMO_MODULE}.frappe.get_doc", return_value=doc),
			patch(f"{PMO_MODULE}.get_pending_raw_materials", return_value=pending),
			patch(
				f"{PMO_MODULE}.validate_material_request_items",
				return_value=self.SELECTED,
			) as validate,
			patch(f"{PMO_MODULE}.add_material_request_comment"),
		):
			create_pmo_material_request("PMO-TEST-0001")
		return doc, validate.call_args.args[1]

	def test_button_requests_every_pending_item_at_its_full_pending_qty(self):
		_doc, items = self._run_without_items(
			[
				_pending_row("M-G-22KT", 2.0, 0.8, item_type="metal_item"),
				_pending_row("F-SCREW", 1.1, 0, item_type="finding_item"),
				_pending_row("D-NT-RO-7-+8-8.5", 1.736, 1.736),
			]
		)
		self.assertEqual(
			items,
			[
				{"item_code": "M-G-22KT", "qty": 0.8},
				{"item_code": "D-NT-RO-7-+8-8.5", "qty": 1.736},
			],
		)

	def test_button_with_nothing_pending_sends_nothing_to_validation(self):
		"""Validation then refuses with "No pending raw materials are available..."."""
		_doc, items = self._run_without_items(
			[_pending_row("F-SCREW", 1.1, 0, item_type="finding_item")]
		)
		self.assertEqual(items, [])


class TestMakeMaterialRequests(UnitTestCase):
	"""The creation loop shared by PMO submit and the Create Material Request button."""

	def _run(
		self,
		items,
		qty_multiplier,
		customer=None,
		customer_owned=False,
		default_gemstone_item="G-PER-DUM-PRE-CC",
	):
		pmo = SimpleNamespace(
			name="PMO-TEST-0001",
			manufacturer="MNF",
			company="CO",
			item_code="RI00650",
			customer=customer,
		)
		created = []
		real_get_value = frappe.db.get_value
		fake_values = {
			"Manufacturing Setting": default_gemstone_item,
			"Warehouse": "Test_Department",
		}

		def get_value(doctype, *args, **kwargs):
			if doctype in fake_values:
				return fake_values[doctype]
			return real_get_value(doctype, *args, **kwargs)

		def new_doc(doctype):
			mr = MagicMock(name="mr")
			mr.name = f"MR-{len(created) + 1:04d}"
			created.append(mr)
			return mr

		with (
			patch(f"{PMO_MODULE}.frappe.new_doc", side_effect=new_doc),
			patch(f"{PMO_MODULE}.frappe.get_value", return_value="LH"),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch(
				f"{PMO_MODULE}.pmo_expects_customer_goods", return_value=customer_owned
			),
		):
			names = ParentManufacturingOrder.make_material_requests(
				pmo, items, qty_multiplier
			)
		return names, created

	def _rows(self, mr):
		return [c.args[1] for c in mr.append.call_args_list]

	def test_qty_is_multiplied_and_one_request_per_item_type(self):
		names, created = self._run(
			{
				"metal_item": [_bom_raw_material("M-G-22KT", 0.5)],
				"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.25, pcs=10)],
			},
			qty_multiplier=3,
		)
		self.assertEqual(names, ["MR-0001", "MR-0002"])
		self.assertEqual(created[0].manufacturing_order, "PMO-TEST-0001")
		self.assertEqual(self._rows(created[0])[0]["qty"], 1.5)
		self.assertEqual(self._rows(created[1])[0]["qty"], 0.75)
		self.assertEqual(self._rows(created[1])[0]["pcs"], 10)
		for mr in created:
			mr.save.assert_called_once()

	def test_gemstone_is_requested_as_the_default_item_with_bom_item_in_description(
		self,
	):
		_names, created = self._run(
			{"gemstone_item": [_bom_raw_material("G-RUBY", 0.38)]}, qty_multiplier=1
		)
		row = self._rows(created[0])[0]
		self.assertEqual(
			(row["item_code"], row["description"], row["qty"]),
			("G-PER-DUM-PRE-CC", "G-RUBY", 0.38),
		)

	# Existing PMO submit behaviour, unchanged by moving the loop into this method.

	def test_request_header_is_filled_as_on_pmo_submit(self):
		_names, created = self._run(
			{"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.25)]},
			qty_multiplier=1,
		)
		mr = created[0]
		self.assertEqual(
			(
				mr.title,
				mr.company,
				mr.material_request_type,
				mr.manufacturing_order,
				mr.custom_manufacturer,
				mr.custom_department,
			),
			(
				"MRD-LH-(RI00650)-1",
				"CO",
				"Manufacture",
				"PMO-TEST-0001",
				"MNF",
				"Test_Department",
			),
		)
		self.assertEqual(mr.schedule_date, frappe.utils.nowdate())

	def test_request_titles_are_numbered_per_item_type(self):
		_names, created = self._run(
			{
				"metal_item": [_bom_raw_material("M-G-22KT", 0.5)],
				"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.25)],
				"finding_item": [],
				"gemstone_item": [_bom_raw_material("G-RUBY", 0.38)],
			},
			qty_multiplier=1,
		)
		self.assertEqual(
			[mr.title for mr in created],
			["MRM-LH-(RI00650)-1", "MRD-LH-(RI00650)-2", "MRG-LH-(RI00650)-3"],
		)

	def test_rows_of_one_item_type_go_on_one_request(self):
		_names, created = self._run(
			{
				"diamond_item": [
					_bom_raw_material(
						"D-NT-RO-7-+8-8.5", 0.25, sub_setting_type="Open"
					),
					_bom_raw_material(
						"D-NT-RO-7-+9-9.5", 0.1, sub_setting_type="Close"
					),
				]
			},
			qty_multiplier=2,
		)
		self.assertEqual(len(created), 1)
		self.assertEqual(
			[
				(row["item_code"], row["qty"], row["custom_sub_setting_type"])
				for row in self._rows(created[0])
			],
			[("D-NT-RO-7-+8-8.5", 0.5, "Open"), ("D-NT-RO-7-+9-9.5", 0.2, "Close")],
		)

	def test_company_owned_rows_carry_no_ownership_stamp(self):
		_names, created = self._run(
			{"metal_item": [_bom_raw_material("M-G-22KT", 0.5)]}, qty_multiplier=1
		)
		row = self._rows(created[0])[0]
		self.assertEqual((row["inventory_type"], row["customer"]), (None, None))

	def test_customer_owned_request_is_stamped_on_header_and_rows(self):
		_names, created = self._run(
			{"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.25)]},
			qty_multiplier=1,
			customer="CUST-1",
			customer_owned=True,
		)
		mr = created[0]
		row = self._rows(mr)[0]
		self.assertEqual((mr.customer, mr.inventory_type), ("CUST-1", "Customer Goods"))
		self.assertEqual(
			(row["inventory_type"], row["customer"]), ("Customer Goods", "CUST-1")
		)

	def test_customer_item_row_is_stamped_even_without_the_pmo_flag(self):
		_names, created = self._run(
			{
				"diamond_item": [
					_bom_raw_material("D-NT-RO-7-+8-8.5", 0.25, is_customer_item=1)
				]
			},
			qty_multiplier=1,
			customer="CUST-1",
		)
		row = self._rows(created[0])[0]
		self.assertEqual(
			(row["inventory_type"], row["customer"]), ("Customer Goods", "CUST-1")
		)

	def test_customer_owned_rows_without_a_customer_are_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run(
				{"diamond_item": [_bom_raw_material("D-NT-RO-7-+8-8.5", 0.25)]},
				qty_multiplier=1,
				customer_owned=True,
			)
		self.assertIn("the order has no Customer", str(caught.exception))

	def test_zero_qty_bom_row_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run(
				{"metal_item": [_bom_raw_material("M-G-22KT", 0)]}, qty_multiplier=1
			)
		self.assertIn("Please Check BOM Table", str(caught.exception))

	def test_gemstone_without_default_gemstone_item_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run(
				{"gemstone_item": [_bom_raw_material("G-RUBY", 0.38)]},
				qty_multiplier=1,
				default_gemstone_item=None,
			)
		self.assertIn("Default Gemstone Item is not set", str(caught.exception))


class TestCreateMaterialRequestsOnSubmit(UnitTestCase):
	"""PMO submit still requests the whole Tracking BOM x PMO qty, as before the refactor."""

	def _pmo(self, bom="TB-0001", qty=3):
		pmo = MagicMock()
		pmo.custom_tracking_bom = bom
		pmo.qty = qty
		pmo.get_bom_raw_materials.return_value = {"metal_item": ["row"]}
		return pmo

	def test_whole_bom_is_requested_times_pmo_qty(self):
		pmo = self._pmo()
		bom_doc = MagicMock()
		bom_doc.get.return_value = "Fixed"
		with (
			patch(f"{PMO_MODULE}.frappe.get_doc", return_value=bom_doc),
			patch(f"{PMO_MODULE}.frappe.msgprint") as msgprint,
		):
			ParentManufacturingOrder.create_material_requests(pmo)

		pmo.make_material_requests.assert_called_once_with({"metal_item": ["row"]}, 3)
		msgprint.assert_called_once_with("Material Request Created !!")
		bom_doc.save.assert_not_called()

	def test_template_bom_gets_its_variants_set_before_it_is_read(self):
		pmo = self._pmo()
		bom_doc = MagicMock()
		bom_doc.get.return_value = "Template"
		order = MagicMock()
		order.attach_mock(bom_doc.save, "bom_save")
		order.attach_mock(pmo.get_bom_raw_materials, "read_bom")
		with (
			patch(f"{PMO_MODULE}.frappe.get_doc", return_value=bom_doc),
			patch(f"{PMO_MODULE}.set_item_variant") as set_item_variant,
			patch(f"{PMO_MODULE}.frappe.msgprint"),
		):
			ParentManufacturingOrder.create_material_requests(pmo)

		set_item_variant.assert_called_once_with(bom_doc)
		self.assertEqual([c[0] for c in order.mock_calls], ["bom_save", "read_bom"])

	def test_missing_tracking_bom_is_refused_before_anything_is_created(self):
		pmo = self._pmo(bom=None)
		self.assertRaises(
			frappe.ValidationError,
			ParentManufacturingOrder.create_material_requests,
			pmo,
		)
		pmo.make_material_requests.assert_not_called()


class TestAddMaterialRequestComment(UnitTestCase):
	def test_new_requests_are_noted_on_the_pmo_timeline(self):
		doc = MagicMock()
		with patch(
			f"{PMO_MODULE}.get_link_to_form",
			side_effect=lambda doctype, name: f"<a>{name}</a>",
		):
			add_material_request_comment(doc, ["MR-0001", "MR-0002"])
		comment_type, content = doc.add_comment.call_args.args
		self.assertEqual(comment_type, "Info")
		self.assertIn("<a>MR-0001</a>, <a>MR-0002</a>", content)

	def test_no_comment_without_requests(self):
		doc = MagicMock()
		add_material_request_comment(doc, [])
		doc.add_comment.assert_not_called()


class TestPMOMaterialRequestRole(UnitTestCase):
	"""Create Material Request is for Administrator, System Manager and the roles listed
	in Jewellery Settings; the server refuses everyone else, whatever the form shows."""

	def _can(self, user_roles, allowed_roles=(), user="someone@example.com"):
		with (
			patch(f"{PMO_MODULE}.frappe.get_roles", return_value=list(user_roles)),
			patch(
				f"{PMO_MODULE}.frappe.get_all", return_value=list(allowed_roles)
			) as get_all,
		):
			result = can_create_pmo_material_request(user)
		return result, get_all

	def test_administrator_is_always_allowed(self):
		result, _get_all = self._can([], user="Administrator")
		self.assertTrue(result)

	def test_system_manager_is_always_allowed(self):
		result, get_all = self._can(["System Manager"])
		self.assertTrue(result)
		get_all.assert_not_called()

	def test_holder_of_a_listed_role_is_allowed(self):
		result, get_all = self._can(
			["Stock User", "Manufacturing Manager"],
			allowed_roles=["Manufacturing Manager", "PPC"],
		)
		self.assertTrue(result)
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"parenttype": "Jewellery Settings",
				"parentfield": "pmo_material_request_roles",
			},
		)

	def test_user_without_a_listed_role_is_refused(self):
		result, _get_all = self._can(["Stock User"], allowed_roles=["PPC"])
		self.assertFalse(result)

	def test_no_role_listed_means_nobody_else_is_allowed(self):
		result, _get_all = self._can(["Stock User"])
		self.assertFalse(result)

	def test_blank_role_row_grants_nothing(self):
		result, _get_all = self._can(["Stock User"], allowed_roles=[None, ""])
		self.assertFalse(result)

	def test_check_raises_permission_error(self):
		with patch(f"{PMO_MODULE}.can_create_pmo_material_request", return_value=False):
			self.assertRaises(frappe.PermissionError, check_pmo_material_request_role)

	def test_endpoint_refuses_before_the_pmo_is_loaded(self):
		with (
			patch(f"{PMO_MODULE}.can_create_pmo_material_request", return_value=False),
			patch(f"{PMO_MODULE}.frappe.get_doc") as get_doc,
		):
			self.assertRaises(
				frappe.PermissionError,
				create_pmo_material_request,
				"PMO-TEST-0001",
				'[{"item_code": "M-G-22KT", "qty": 0.8}]',
			)
		get_doc.assert_not_called()

	def _onload(self, docstatus, allowed, blocking_state=None, tracking_bom_draft=True):
		pmo = MagicMock(docstatus=docstatus)
		with (
			patch(
				f"{PMO_MODULE}.can_create_pmo_material_request", return_value=allowed
			),
			patch(f"{PMO_MODULE}.pmo_blocking_state", return_value=blocking_state),
			patch(
				f"{PMO_MODULE}.tracking_bom_is_draft", return_value=tracking_bom_draft
			),
		):
			ParentManufacturingOrder.onload(pmo)
		key, value = pmo.set_onload.call_args.args
		self.assertEqual(key, "can_create_material_request")
		return value

	def test_form_shows_the_button_on_a_submitted_pmo_to_an_allowed_user(self):
		self.assertTrue(self._onload(docstatus=1, allowed=True))

	def test_form_hides_the_button_from_a_user_without_the_role(self):
		self.assertFalse(self._onload(docstatus=1, allowed=False))

	def test_form_hides_the_button_on_a_draft_or_cancelled_pmo(self):
		self.assertFalse(self._onload(docstatus=0, allowed=True))
		self.assertFalse(self._onload(docstatus=2, allowed=True))

	def test_form_hides_the_button_on_a_pmo_on_hold_or_closed(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open import (
			CLOSED_STATE,
			HOLD_STATE,
		)

		for state in (HOLD_STATE, CLOSED_STATE):
			self.assertFalse(
				self._onload(docstatus=1, allowed=True, blocking_state=state), state
			)

	def test_form_hides_the_button_once_the_tracking_bom_is_submitted(self):
		self.assertFalse(
			self._onload(docstatus=1, allowed=True, tracking_bom_draft=False)
		)


class TestPMOCreateMaterialRequestFlow(IntegrationTestCase):
	"""End to end on a real submitted PMO: whatever its Tracking BOM needs beyond the
	Material Requests it already has is requested by Create Material Request, and
	nothing is requested twice.

	One PMO is built and submitted for the class; each test runs inside a savepoint that
	tearDown rolls back, so every test starts from the PMO as submit left it -- its
	Tracking BOM fully covered by the Material Requests submit created.
	"""

	SAVEPOINT = "pmo_create_material_request"

	@classmethod
	def setUpClass(cls):
		# No super(), like the other integration classes here: IntegrationTestCase would
		# generate test records for every doctype the PMO links to, which runs ERPNext's
		# _Test Company bootstrap and fails on this app's site. The CI test data
		# (Test_Company, Test_Department, ...) is what create_man_plan builds on.
		cls.pmo_name = cls.make_submitted_pmo()

	@classmethod
	def make_submitted_pmo(cls):
		cls.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		man_plan = create_man_plan(cls)
		pmo = frappe.get_doc(
			"Parent Manufacturing Order", {"manufacturing_plan": man_plan.name}
		)
		pmo.diamond_department = cls.department
		pmo.gemstone_department = cls.department
		pmo.manufacturer = "Shubh"
		pmo.save()
		pmo.submit()
		return pmo.name

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(self.SAVEPOINT)
		if not tracking_bom_is_draft(
			self.pmo()
		) and not self._testMethodName.startswith("test_submitted_tracking_bom"):
			self.skipTest(
				"the PMO's Tracking BOM is submitted; the feature leaves it alone"
			)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=self.SAVEPOINT)
		frappe.local.message_log = []
		super().tearDown()

	# helpers

	def pmo(self):
		return frappe.get_doc("Parent Manufacturing Order", self.pmo_name)

	def tracking_bom(self):
		return frappe.get_doc("Tracking Bom", self.pmo().custom_tracking_bom)

	def raise_first_row_qty(self, bom, table, by):
		bom.get(table)[0].quantity = flt(bom.get(table)[0].quantity) + by
		self.save_tracking_bom(bom)

	def add_row(self, bom, table, values):
		bom.append(table, values)
		self.save_tracking_bom(bom)

	def save_tracking_bom(self, bom):
		# As submit_bom does: the edit under test is the raw material, not the pricing.
		bom.flags.ignore_validations = True
		bom.save()

	def bom_rows(self, item_type=None):
		return [
			row
			for row in get_pending_raw_materials(self.pmo())
			if item_type is None or row["item_type"] == item_type
		]

	def pending(self):
		return {
			row["item_code"]: row["pending_qty"]
			for row in get_pending_raw_materials(self.pmo())
			if row["pending_qty"] > 0
		}

	def requested_lines(self, material_requests):
		return [
			(row.item_code, row.qty)
			for name in material_requests
			for row in frappe.get_doc("Material Request", name).items
		]

	def material_requests_of_pmo(self, docstatus=None):
		filters = {"manufacturing_order": self.pmo_name}
		if docstatus is not None:
			filters["docstatus"] = docstatus
		return frappe.get_all("Material Request", filters, pluck="name")

	def first_bom_table_with_rows(self):
		bom = self.tracking_bom()
		for table, item_type in (
			("metal_detail", "metal_item"),
			("diamond_detail", "diamond_item"),
			("finding_detail", "finding_item"),
		):
			if bom.get(table):
				return bom, table, item_type
		self.skipTest("Tracking BOM has no metal, diamond or finding rows")

	def assertNothingPending(self):
		self.assertEqual(self.pending(), {})
		with self.assertRaises(frappe.ValidationError) as caught:
			create_pmo_material_request(self.pmo_name)
		self.assertIn(
			"No pending raw materials are available for Material Request creation.",
			str(caught.exception),
		)

	# flows

	def test_nothing_is_pending_right_after_submit(self):
		self.assertTrue(self.material_requests_of_pmo(), "submit raised no requests")
		self.assertNothingPending()

	def test_higher_bom_qty_requests_only_the_extra(self):
		bom, table, item_type = self.first_bom_table_with_rows()
		self.raise_first_row_qty(bom, table, by=1)
		extra = flt(1 * self.pmo().qty, 3)
		item_code = self.bom_rows(item_type)[0]["item_code"]

		self.assertEqual(self.pending(), {item_code: extra})
		created = create_pmo_material_request(self.pmo_name)

		self.assertEqual(self.requested_lines(created), [(item_code, extra)])
		self.assertNothingPending()

	def test_row_added_to_the_tracking_bom_is_requested(self):
		bom, table, item_type = self.first_bom_table_with_rows()
		row = {
			key: value
			for key, value in bom.get(table)[0].as_dict().items()
			if key
			not in ("name", "idx", "parent", "parentfield", "parenttype", "doctype")
		}
		row["quantity"] = 0.2
		self.add_row(bom, table, row)
		item_code = self.bom_rows(item_type)[-1]["item_code"]

		created = create_pmo_material_request(self.pmo_name)

		self.assertEqual(
			self.requested_lines(created), [(item_code, flt(0.2 * self.pmo().qty, 3))]
		)
		material_request = frappe.get_doc("Material Request", created[0])
		self.assertEqual(material_request.docstatus, 0)
		self.assertEqual(material_request.material_request_type, "Manufacture")
		self.assertEqual(material_request.manufacturing_order, self.pmo_name)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": "Parent Manufacturing Order",
					"reference_name": self.pmo_name,
					"comment_type": "Info",
					"content": ["like", f"%{created[0]}%"],
				},
			)
		)
		self.assertNothingPending()

	def test_material_request_missing_since_submit_is_requested_again(self):
		drafts = self.material_requests_of_pmo(docstatus=0)
		if not drafts:
			self.skipTest("submit left no draft Material Request to remove")
		removed = frappe.get_doc("Material Request", drafts[0])
		removed_items = sorted(row.item_code for row in removed.items)
		frappe.delete_doc("Material Request", removed.name, force=True)

		created = create_pmo_material_request(self.pmo_name)

		self.assertEqual(
			sorted(item_code for item_code, _qty in self.requested_lines(created)),
			removed_items,
		)
		self.assertNothingPending()

	def test_cancelled_material_request_no_longer_counts(self):
		name = self.material_requests_of_pmo()[0]
		frappe.db.set_value("Material Request", name, "docstatus", 2)

		self.assertTrue(self.pending())

	def test_part_then_the_remainder_and_never_more(self):
		bom, table, item_type = self.first_bom_table_with_rows()
		self.raise_first_row_qty(bom, table, by=1)
		extra = flt(1 * self.pmo().qty, 3)
		part = flt(extra / 4, 3)
		item_code = self.bom_rows(item_type)[0]["item_code"]

		create_pmo_material_request(
			self.pmo_name, frappe.as_json([{"item_code": item_code, "qty": part}])
		)
		self.assertEqual(self.pending(), {item_code: flt(extra - part, 3)})

		with self.assertRaises(frappe.ValidationError) as caught:
			create_pmo_material_request(
				self.pmo_name, frappe.as_json([{"item_code": item_code, "qty": extra}])
			)
		self.assertIn("exceeds the pending quantity", str(caught.exception))

		created = create_pmo_material_request(self.pmo_name)
		self.assertEqual(
			self.requested_lines(created), [(item_code, flt(extra - part, 3))]
		)
		self.assertNothingPending()

	def test_gemstones_are_matched_through_the_description(self):
		rows = self.bom_rows("gemstone_item")
		if not rows:
			self.skipTest("Tracking BOM has no gemstone rows")
		default_gemstone_item = frappe.db.get_value(
			"Manufacturing Setting",
			{"manufacturer": self.pmo().manufacturer},
			"default_gemstone_item",
		)
		gemstone_lines = [
			row
			for name in self.material_requests_of_pmo()
			for row in frappe.get_doc("Material Request", name).items
			if row.item_code == default_gemstone_item
		]
		self.assertTrue(gemstone_lines)
		for row in rows:
			self.assertEqual(row["pending_qty"], 0, row["item_code"])

	def test_regraded_diamond_counts_as_requested(self):
		rows = self.bom_rows("diamond_item")
		if not rows:
			self.skipTest("Tracking BOM has no diamond rows")
		bom_item = rows[0]["item_code"]
		key = _get_grade_free_key(bom_item)
		# The request line for this diamond, in whatever grade it was requested.
		line = next(
			(
				line
				for line in frappe.get_all(
					"Material Request Item",
					{"parent": ["in", self.material_requests_of_pmo()]},
					["name", "item_code"],
				)
				if _get_grade_free_key(line.item_code) == key
			),
			None,
		)
		self.assertTrue(line, f"no request line for {bom_item}")
		other_grade = next(
			(
				item_code
				for item_code in frappe.get_all(
					"Item",
					{"variant_of": frappe.db.get_value("Item", bom_item, "variant_of")},
					pluck="name",
				)
				if item_code not in (bom_item, line.item_code)
				and _get_grade_free_key(item_code) == key
			),
			None,
		)
		if not other_grade:
			self.skipTest(f"no other grade of {bom_item} to regrade to")
		frappe.db.set_value(
			"Material Request Item", line.name, "item_code", other_grade
		)

		self.assertNothingPending()

	def test_editing_the_template_bom_leaves_the_requirement_alone(self):
		master_bom = self.pmo().master_bom
		row = frappe.get_all(
			"BOM Metal Detail", {"parent": master_bom}, ["name", "quantity"], limit=1
		)
		if not row:
			self.skipTest("Master BOM has no metal row")
		frappe.db.set_value(
			"BOM Metal Detail", row[0].name, "quantity", flt(row[0].quantity) + 5
		)

		self.assertNothingPending()

	def test_only_allowed_roles_get_the_button_and_the_endpoint(self):
		settings = frappe.get_single("Jewellery Settings")
		settings.set("pmo_material_request_roles", [{"role": "Manufacturing Manager"}])
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
		allowed = self._make_user("pmo-mr-allowed@example.com", "Manufacturing Manager")
		refused = self._make_user("pmo-mr-refused@example.com", "Stock User")

		self.assertTrue(can_create_pmo_material_request(allowed))
		self.assertFalse(can_create_pmo_material_request(refused))

		frappe.set_user(refused)
		pmo = frappe.get_doc("Parent Manufacturing Order", self.pmo_name)
		pmo.run_method("onload")
		self.assertFalse(pmo.get_onload().get("can_create_material_request"))
		self.assertRaises(
			frappe.PermissionError, create_pmo_material_request, self.pmo_name
		)

		frappe.set_user(allowed)
		pmo = frappe.get_doc("Parent Manufacturing Order", self.pmo_name)
		pmo.run_method("onload")
		self.assertTrue(pmo.get_onload().get("can_create_material_request"))

	def _make_user(self, email, role):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
					"roles": [{"role": role}],
				}
			).insert(ignore_permissions=True)
		return email

	def test_pmo_on_hold_or_closed_is_refused(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open import (
			CLOSED_STATE,
			HOLD_STATE,
		)

		if not frappe.get_meta("Parent Manufacturing Order").has_field(
			"workflow_state"
		):
			self.skipTest("no PMO workflow on this site")
		for state in (HOLD_STATE, CLOSED_STATE):
			frappe.db.set_value(
				"Parent Manufacturing Order", self.pmo_name, "workflow_state", state
			)
			pmo = frappe.get_doc("Parent Manufacturing Order", self.pmo_name)
			pmo.run_method("onload")
			self.assertFalse(pmo.get_onload().get("can_create_material_request"), state)
			with self.assertRaises(frappe.ValidationError) as caught:
				create_pmo_material_request(self.pmo_name)
			self.assertIn(
				"No Material Request can be created for it.", str(caught.exception)
			)

	def test_draft_pmo_is_refused(self):
		frappe.db.set_value("Parent Manufacturing Order", self.pmo_name, "docstatus", 0)

		with self.assertRaises(frappe.ValidationError) as caught:
			create_pmo_material_request(self.pmo_name)
		self.assertIn("only for a submitted PMO", str(caught.exception))

	def test_submitted_tracking_bom_is_left_alone(self):
		"""Submitted means the finished item is made: no button, nothing requested."""
		bom = self.tracking_bom()
		if bom.docstatus == 0:
			# Leave something pending, so only the submitted state can stop the request.
			self.raise_first_row_qty(*self.first_bom_table_with_rows()[:2], by=1)
			frappe.db.set_value("Tracking Bom", bom.name, "docstatus", 1)

		pmo = self.pmo()
		pmo.run_method("onload")
		self.assertFalse(pmo.get_onload().get("can_create_material_request"))
		with self.assertRaises(frappe.ValidationError) as caught:
			create_pmo_material_request(self.pmo_name)
		self.assertIn("already manufactured", str(caught.exception))


class _FindingQB:
	"""frappe.qb stand-in whose BOM Finding Detail query returns the given rows."""

	def __init__(self, rows):
		self._rows = rows

	def DocType(self, _name):
		return SimpleNamespace(parent="parent")

	def from_(self, _table):
		return self

	def select(self, *_cols):
		return self

	def where(self, *_args):
		return self

	def run(self, **_kwargs):
		return [frappe._dict(row) for row in self._rows]


def _finding_row(
	category="Chains", item="F", item_variant=None, parentfield="finding_detail"
):
	return {
		"item": item,
		"item_variant": item_variant,
		"finding_category": category,
		"finding_type": "Box Chain",
		"finding_size": "1.00 INCH",
		"parentfield": parentfield,
	}


class TestChainFindingRows(UnitTestCase):
	"""_get_chain_finding_rows(): the rows create_manufacturing_work_order() raises a
	Finding Manufacturing Work Order for, each with its variant resolved."""

	def _rows(self, rows, ignore_wo=()):
		pmo = frappe._dict(custom_tracking_bom="TB-1", diamond_grade="EF-VVS")

		def get_value(doctype, name, fieldname):
			if fieldname == "custom_ignore_work_order":
				return 1 if name in ignore_wo else 0
			return None

		with (
			patch.object(frappe, "qb", _FindingQB(rows)),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch(
				f"{PMO_MODULE}._resolve_existing_variant_item_code",
				side_effect=lambda row, table, grade: f"{row.item}-{row.finding_size}",
			),
		):
			return _get_chain_finding_rows(pmo)

	def test_only_chains_rows_are_returned_with_their_variant(self):
		rows = self._rows([_finding_row(), _finding_row(category="Screws")])
		self.assertEqual([row.item_variant for row in rows], ["F-1.00 INCH"])

	def test_a_stored_variant_is_kept(self):
		rows = self._rows([_finding_row(item_variant="F-G-22KT-CHA-BC-1.00 INCH")])
		self.assertEqual(
			[row.item_variant for row in rows], ["F-G-22KT-CHA-BC-1.00 INCH"]
		)

	def test_chains_outside_finding_detail_are_skipped(self):
		self.assertEqual(self._rows([_finding_row(parentfield="other_detail")]), [])

	def test_ignore_work_order_template_is_skipped(self):
		self.assertEqual(self._rows([_finding_row()], ignore_wo={"F"}), [])


class TestChainFindingsMustBeManufacturingItems(UnitTestCase):
	"""PMO submit is refused when a chain's variant is not ticked Is Manufacturing Item:
	create_finding_mwo() would skip it and the PMO would go on without its Finding MWO."""

	def _validate(
		self,
		rows,
		ticked=(),
		pmo_type="Manufacturing",
		variant_of="EA",
		tracking_bom="TB-1",
	):
		pmo = frappe._dict(
			custom_tracking_bom=tracking_bom, type=pmo_type, item_code="ITEM"
		)

		def get_value(doctype, name, fieldname):
			if fieldname == "custom_is_manufacturing_item":
				return 1 if name in ticked else 0
			return None

		with (
			patch(
				f"{PMO_MODULE}._get_chain_finding_rows",
				return_value=[frappe._dict(item_variant=v) for v in rows],
			) as get_rows,
			patch.object(frappe, "get_cached_value", return_value=variant_of),
			patch.object(frappe.db, "get_value", side_effect=get_value),
		):
			_validate_chain_findings_are_manufacturing_items(pmo)
		return get_rows

	def test_unticked_chain_variant_blocks_submit_and_is_named(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._validate(
				["F-BC-1.00 INCH", "F-BC-1.50 INCH"], ticked={"F-BC-1.50 INCH"}
			)
		self.assertIn("F-BC-1.00 INCH", str(ctx.exception))
		self.assertNotIn("F-BC-1.50 INCH", str(ctx.exception))

	def test_ticked_chain_variants_pass(self):
		self._validate(["F-BC-1.00 INCH"], ticked={"F-BC-1.00 INCH"})

	def test_no_chains_pass(self):
		self._validate([])

	def test_no_tracking_bom_is_not_checked(self):
		get_rows = self._validate(["F-BC-1.00 INCH"], tracking_bom=None)
		get_rows.assert_not_called()

	def test_finding_manufacturing_pmo_of_a_non_f_item_is_not_checked(self):
		get_rows = self._validate(
			["F-BC-1.00 INCH"], pmo_type="Finding Manufacturing", variant_of="EA"
		)
		get_rows.assert_not_called()

	def test_finding_manufacturing_pmo_of_an_f_item_is_checked(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				["F-BC-1.00 INCH"], pmo_type="Finding Manufacturing", variant_of="F"
			)


class TestPMOMaterialRequestEdgeCases(UnitTestCase):
	"""Behaviour around the edges that the main classes do not pin down."""

	def test_requirement_key_reads_ownership_whatever_its_type(self):
		for owned in (0, "0", None, "", False):
			self.assertEqual(
				_requirement_key("M", owned, None), ("M", 0, ""), repr(owned)
			)
		for owned in (1, "1", True):
			self.assertEqual(
				_requirement_key("M", owned, "Open"), ("M", 1, "Open"), repr(owned)
			)

	def test_pending_asks_for_a_locking_read_only_when_told_to(self):
		doc = SimpleNamespace(
			name="PMO-TEST-0001",
			qty=1,
			manufacturer="MNF",
			get_bom_raw_materials=lambda: {},
		)
		for for_update in (True, False):
			with (
				patch(
					f"{PMO_MODULE}._get_requested_qty_by_item", return_value={}
				) as requested,
				patch(f"{PMO_MODULE}.frappe.get_precision", return_value=3),
			):
				get_pending_raw_materials(doc, for_update=for_update)
			requested.assert_called_once_with(doc, for_update=for_update)

	def _create_failing_with(self, error):
		doc = MagicMock()
		doc.make_material_requests.side_effect = error
		with (
			patch(f"{PMO_MODULE}.check_pmo_material_request_role"),
			patch.object(frappe.db, "get_value"),
			patch(f"{PMO_MODULE}.frappe.get_doc", return_value=doc),
			patch(
				f"{PMO_MODULE}.validate_material_request_items",
				return_value={"metal_item": [{"item_code": "M-1", "qty": 1}]},
			),
			patch(f"{PMO_MODULE}.add_material_request_comment") as comment,
		):
			with self.assertRaises(Exception) as caught:
				create_pmo_material_request("PMO-TEST-0001", "[]")
		# A failed creation never announces requests on the PMO's timeline.
		comment.assert_not_called()
		return caught.exception

	def test_user_who_cannot_create_material_requests_gets_the_permission_error(self):
		"""The role setting gates the button, not ERPNext's own permission: a user allowed
		by the setting whose roles cannot create a Material Request is stopped by the
		save, and nothing is left behind -- no request, no timeline comment."""
		raised = self._create_failing_with(frappe.PermissionError())
		self.assertIsInstance(raised, frappe.PermissionError)

	def test_a_failure_part_way_through_leaves_no_timeline_comment(self):
		"""Requests are saved one by one in one transaction; Frappe rolls the lot back when
		the call fails, and the comment must not announce requests that no longer exist."""
		raised = self._create_failing_with(
			frappe.ValidationError("second request failed")
		)
		self.assertIn("second request failed", str(raised))

	def test_lock_wait_timeout_and_deadlock_read_as_try_again(self):
		"""Another session holding the PMO or one of its requests past the lock wait used to
		surface as the raw "(1205, 'Lock wait timeout exceeded; ...')" database error."""
		for error in (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
			raised = self._create_failing_with(
				error("(1205, 'Lock wait timeout exceeded')")
			)
			self.assertIsInstance(raised, frappe.ValidationError, error.__name__)
			self.assertIn("Another change to this PMO's Material Requests", str(raised))
			self.assertNotIn("1205", str(raised))

	def test_other_errors_are_not_mistaken_for_a_lock_wait(self):
		raised = self._create_failing_with(
			frappe.ValidationError("Item X is not pending")
		)
		self.assertIn("Item X is not pending", str(raised))
		self.assertNotIn("Another change", str(raised))

	def test_jewellery_settings_keeps_the_role_field(self):
		"""The roles are read from this field; losing it from the DocType JSON (a desk
		re-export, a bad merge) would leave only System Manager able to use the button."""
		import json

		path = frappe.get_app_path(
			"jewellery_erpnext",
			"jewellery_erpnext",
			"doctype",
			"jewellery_settings",
			"jewellery_settings.json",
		)
		with open(path) as f:
			doctype = json.load(f)
		fields = {field["fieldname"]: field for field in doctype["fields"]}
		role_field = fields["pmo_material_request_roles"]
		self.assertEqual(role_field["fieldtype"], "Table MultiSelect")
		self.assertEqual(role_field["options"], "Has Role")
		self.assertIn("pmo_material_request_roles", doctype["field_order"])
		self.assertEqual(
			fields["pmo_material_request_section"]["fieldtype"], "Section Break"
		)
