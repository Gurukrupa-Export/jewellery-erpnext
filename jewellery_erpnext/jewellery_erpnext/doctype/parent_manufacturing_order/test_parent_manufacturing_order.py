# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

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
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	get_item_code,
	set_metal_tolerance_table,
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

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
			"parent_manufacturing_order.frappe.db.get_value",
			return_value="PTM-TEST-0001",
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
			"parent_manufacturing_order.frappe.get_doc",
			side_effect=fake_get_doc,
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
