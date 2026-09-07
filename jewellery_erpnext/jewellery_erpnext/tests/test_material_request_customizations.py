# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the Material Request customizations: Gemstone validation, Department SE, MOP SE."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.material_request import (
	material_request as mr_custom,
)
from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
	before_validate as mr_before_validate,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod

_MR_EVENTS = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"
_MR_CUSTOM = "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request"


class MockMR:
	def __init__(
		self,
		workflow_state="Material Reserved",
		mr_type="Manufacture",
		manufacturer="Manu-A",
	):
		self.workflow_state = workflow_state
		self.material_request_type = mr_type
		self.custom_manufacturer = manufacturer
		self.items = []


class TestValidateGemstoneAlternativeItems(IntegrationTestCase):
	def test_ignores_non_reserved_state(self):
		mr = MockMR(workflow_state="Draft")
		mr_mod.validate_gemstone_alternative_items(mr)  # Should return without error

	def test_ignores_non_manufacture_type(self):
		mr = MockMR(mr_type="Material Transfer")
		mr_mod.validate_gemstone_alternative_items(mr)  # Should return without error

	@patch(f"{_MR_EVENTS}._get_default_gemstone_item", return_value="GEM-DUMMY")
	def test_throws_when_alternative_item_missing(self, mock_get_default):
		mr = MockMR()
		mr.items = [
			SimpleNamespace(item_code="GEM-DUMMY", custom_alternative_item=None)
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.validate_gemstone_alternative_items(mr)
		self.assertIn(
			"Please select Alternative Item for dummy gemstone item", str(ctx.exception)
		)

	@patch(f"{_MR_EVENTS}._get_default_gemstone_item", return_value="GEM-DUMMY")
	def test_throws_when_alternative_item_same_as_default(self, mock_get_default):
		mr = MockMR()
		mr.items = [
			SimpleNamespace(item_code="GEM-DUMMY", custom_alternative_item="GEM-DUMMY")
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.validate_gemstone_alternative_items(mr)
		self.assertIn(
			"Alternative Item cannot be dummy gemstone item", str(ctx.exception)
		)

	@patch(f"{_MR_EVENTS}._get_default_gemstone_item", return_value="GEM-DUMMY")
	def test_passes_when_alternative_item_valid(self, mock_get_default):
		mr = MockMR()
		mr.items = [
			SimpleNamespace(item_code="GEM-DUMMY", custom_alternative_item="REAL-GEM-1")
		]
		mr_mod.validate_gemstone_alternative_items(mr)  # Should not throw


class TestUpdateDepartmentAndCreateStockEntry(IntegrationTestCase):
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.sql", return_value=[])
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value", return_value="WH-RESERVE")
	@patch(f"{_MR_CUSTOM}.make_department_stock_entry", return_value="SE-NEW-1")
	def test_updates_department_and_creates_se(
		self, mock_make_se, mock_get_value, mock_sql, mock_get_doc
	):
		doc = MagicMock()
		doc.custom_department = "Dept A"
		mock_get_doc.return_value = doc

		mr_custom.update_department_and_create_stock_entry("MR-1", "Dept B")
		doc.db_set.assert_called_once_with(
			{
				"custom_department": "Dept B",
				"custom_custom_counter": 1,
				"workflow_state": "Material Transferred to Department",
				"custom_operation_type": "Transfer to Department",
			}
		)
		mock_make_se.assert_called_once()

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	def test_throws_when_same_department(self, mock_get_doc):
		doc = MagicMock()
		doc.custom_department = "Dept A"
		mock_get_doc.return_value = doc

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_custom.update_department_and_create_stock_entry("MR-1", "Dept A")
		self.assertIn("Raw material is already in this department", str(ctx.exception))

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.sql")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value", return_value="WH-RESERVE-A")
	def test_throws_when_last_se_t_warehouse_is_same(
		self, mock_get_value, mock_sql, mock_get_doc
	):
		doc = MagicMock()
		doc.custom_department = "Dept Old"
		mock_get_doc.return_value = doc

		# mock_sql returning last stock entry
		mock_sql.return_value = [
			{"name": "SE-1", "s_warehouse": "WH-S", "t_warehouse": "WH-RESERVE-A"}
		]

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_custom.update_department_and_create_stock_entry("MR-1", "Dept A")
		self.assertIn("Raw material is already in this department", str(ctx.exception))


class TestMakeMopStockEntry(IntegrationTestCase):
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	@patch(f"{_MR_CUSTOM}.frappe.get_cached_value", return_value=("MWO-1", "MO-1"))
	@patch(f"{_MR_CUSTOM}.mri_warehouse_map", return_value={"MRI-1": "WH-FROM"})
	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	def test_creates_mop_stock_entry(
		self, mock_copy, mock_mri_map, mock_cached, mock_get_value, mock_get_doc
	):
		se = MagicMock()
		se.items = [MagicMock(material_request_item="MRI-1")]
		mock_copy.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {
					"department": "Dept A",
					"status": "Pending",
					"employee": None,
					"department_ir_status": None,
				}
			if doctype == "Warehouse":
				return "WH-TARGET"
			return None

		mock_get_value.side_effect = _gv

		mr_dict = {"custom_reserve_se": "SE-OLD", "name": "MR-1"}

		# Mocking the dictionary .get / .db_set on `self` for make_mop_stock_entry
		mr_obj = MagicMock()
		mr_obj.get.side_effect = lambda k: mr_dict.get(k)

		mr_custom.make_mop_stock_entry(mr_obj, mop="MOP-1")

		se.save.assert_called_once()
		se.submit.assert_called_once()
		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")
		self.assertEqual(se.manufacturing_operation, "MOP-1")
		self.assertEqual(se.to_department, "Dept A")
		mr_obj.db_set.assert_called_once_with("custom_mop_se", se.name)

	@patch(f"{_MR_CUSTOM}.frappe.log_error")
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	def test_throws_when_in_transit(self, mock_get_value, mock_get_doc, mock_log_error):
		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {"department": "Dept A", "department_ir_status": "In-Transit"}
			return None

		mock_get_value.side_effect = _gv

		mr_obj = MagicMock()
		mr_obj.get.return_value = "SE-OLD"

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_custom.make_mop_stock_entry(mr_obj, mop="MOP-1")
		self.assertIn("in-transit status", str(ctx.exception))


class TestMakeDepartmentStockEntry(IntegrationTestCase):
	@patch(f"{_MR_CUSTOM}.frappe.db.set_value")
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	@patch(f"{_MR_CUSTOM}.frappe.db.sql")
	def test_creates_department_stock_entry(
		self, mock_sql, mock_get_value, mock_copy, mock_get_doc, mock_set_value
	):
		se = MagicMock()
		se.items = [MagicMock(material_request_item="MRI-1")]
		mock_copy.return_value = se

		mock_get_value.return_value = "WH-TARGET-RES"
		mock_sql.return_value = [{"t_warehouse": "WH-SRC-PREV"}]

		mr_obj = MagicMock()
		mr_obj.name = "MR-1"
		mr_obj.get.side_effect = (
			lambda k: "SE-RESERVE"
			if k == "custom_reserve_se"
			else "DEPT-A"
			if k == "custom_department"
			else None
		)

		# For `self.custom_material_request_department_transfer[-1]`
		child_row = MagicMock()
		mr_obj.custom_material_request_department_transfer = [child_row]

		mr_custom.make_department_stock_entry(mr_obj)

		se.save.assert_called_once()
		se.submit.assert_called_once()
		self.assertEqual(se.stock_entry_type, "Material Transfered to Department")
		self.assertEqual(se.to_department, "DEPT-A")
		self.assertEqual(se.to_warehouse, "WH-TARGET-RES")
		self.assertEqual(se.items[0].s_warehouse, "WH-SRC-PREV")
		self.assertEqual(se.items[0].t_warehouse, "WH-TARGET-RES")

		child_row.db_set.assert_called_once_with("stock_entry_created", 1)

	def test_returns_none_if_no_reserve_se(self):
		mr_obj = MagicMock()
		mr_obj.get.return_value = None
		self.assertIsNone(mr_custom.make_department_stock_entry(mr_obj))

	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value", return_value=None)
	def test_throws_if_reserve_warehouse_missing(
		self, mock_get_value, mock_get_doc, mock_copy
	):
		mr_obj = MagicMock()
		mr_obj.get.side_effect = (
			lambda k: "SE-RESERVE" if k == "custom_reserve_se" else "DEPT-A"
		)
		child_row = MagicMock()
		mr_obj.custom_material_request_department_transfer = [child_row]

		with self.assertRaises(Exception) as ctx:
			mr_custom.make_department_stock_entry(mr_obj)
		self.assertIn("No warehouse for Selected Department", str(ctx.exception))
		self.assertEqual(len(mr_obj.custom_material_request_department_transfer), 0)


class TestMakeDepartmentMopStockEntry(IntegrationTestCase):
	def test_returns_none_if_no_reserve_se(self):
		mr_obj = MagicMock()
		mr_obj.get.return_value = None
		self.assertIsNone(mr_custom.make_department_mop_stock_entry(mr_obj))

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	def test_throws_if_in_transit(self, mock_get_value, mock_get_doc):
		mr_obj = MagicMock()
		mr_obj.get.return_value = "SE-RESERVE"
		mock_get_value.return_value = {"department_ir_status": "In-Transit"}

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_custom.make_department_mop_stock_entry(mr_obj, mop="MOP-1")
		self.assertIn("in-transit status", str(ctx.exception))

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.sql", return_value=[])
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	@patch(f"{_MR_CUSTOM}.frappe.get_cached_value", return_value=("MWO-1", "MO-1"))
	def test_uses_fallback_swarehouse_and_employee_warehouse(
		self, mock_cached, mock_get_value, mock_sql, mock_copy, mock_get_doc
	):
		se = MagicMock()
		se.items = [MagicMock(material_request_item="MRI-1")]
		mock_copy.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {
					"department": "Dept A",
					"status": "WIP",
					"employee": "EMP-1",
					"department_ir_status": None,
				}
			if doctype == "Warehouse" and "employee" in name:
				return "WH-EMP"
			if doctype == "Warehouse":
				return "WH-DEPT-MFG"
			return None

		mock_get_value.side_effect = _gv

		mr_obj = MagicMock()
		mr_obj.name = "MR-1"
		mr_obj.items = [MagicMock(warehouse="WH-FALLBACK")]
		mr_obj.get.side_effect = (
			lambda k: "SE-RESERVE"
			if k == "custom_reserve_se"
			else "DEPT-A"
			if k == "custom_department"
			else None
		)

		mr_custom.make_department_mop_stock_entry(mr_obj, mop="MOP-1")
		self.assertEqual(se.items[0].s_warehouse, "WH-FALLBACK")
		self.assertEqual(se.items[0].t_warehouse, "WH-EMP")
		self.assertEqual(se.to_department, "DEPT-A")


class TestMakeInTransitStockEntry(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_throws_if_missing_transit_warehouse(self, mock_get_value):
		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Regular", None)
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			return None

		mock_get_value.side_effect = _gv

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertIn("Transit warehouse is not mentioned", str(ctx.exception))

	@patch(f"{_MR_EVENTS}.make_stock_entry")
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_throws_if_transfer_type_has_no_se_type(self, mock_get_value, mock_mse):
		se = MagicMock()
		mock_mse.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Regular", "WH-TRANSIT")
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			if doctype == "Warehouse" and name == "Dept From":
				return "Regular"
			if doctype == "Transfer Type":
				return None
			return None

		mock_get_value.side_effect = _gv

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertIn("Please specify a Stock Entry Type", str(ctx.exception))

	@patch(f"{_MR_EVENTS}.make_stock_entry")
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_handles_customer_goods(self, mock_get_value, mock_mse):
		se = MagicMock()
		se.items = [MagicMock(customer="CUST-1")]
		mock_mse.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Regular", "WH-TRANSIT")
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			if doctype == "Warehouse" and name == "Dept From":
				return "Regular"
			if doctype == "Transfer Type":
				return "Regular Transfer"
			return None

		mock_get_value.side_effect = _gv

		res = mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertEqual(res.stock_entry_type, "Customer Goods Transfer")

	@patch(f"{_MR_EVENTS}.make_stock_entry")
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_handles_consumables(self, mock_get_value, mock_mse):
		se = MagicMock()
		se.items = [MagicMock(customer=None)]
		mock_mse.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Consumables", "WH-TRANSIT")
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			if doctype == "Warehouse" and name == "Dept From":
				return "Consumables"
			if doctype == "Transfer Type":
				return "Regular Transfer"
			return None

		mock_get_value.side_effect = _gv

		res = mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertEqual(res.stock_entry_type, "Consumables Issue to  Department")
		self.assertEqual(res.to_warehouse, "WH-SET")


class TestGetPmoData(IntegrationTestCase):
	@patch(f"{_MR_CUSTOM}.frappe.qb.from_")
	@patch(f"{_MR_CUSTOM}.get_mapped_doc")
	def test_maps_variants(self, mock_mapped_doc, mock_from):
		mock_chain = MagicMock()
		mock_from.return_value = mock_chain
		mock_chain.join.return_value = mock_chain
		mock_chain.on.return_value = mock_chain
		mock_chain.select.return_value = mock_chain
		mock_chain.where.return_value = mock_chain

		mock_chain.run.return_value = [
			frappe._dict(
				{
					"item_code": "ITM-1",
					"qty": 1,
					"uom": "Nos",
					"rate": 100,
					"inventory_type": "Regular",
					"customer": None,
					"conversion_factor": 1,
					"t_warehouse": "T",
					"s_warehouse": "S",
					"batch_no": "B1",
				}
			)
		]

		def side_effect(*args, **kwargs):
			set_missing_values = args[4]
			target = MagicMock()
			target.custom_item_type = "Gemstone"
			set_missing_values(MagicMock(), target)
			return target

		mock_mapped_doc.side_effect = side_effect

		res = mr_custom.get_pmo_data("PMO-1", None)
		res.append.assert_called_once_with(
			"items",
			{
				"warehouse": "T",
				"from_warehouse": "S",
				"item_code": "ITM-1",
				"qty": 1,
				"uom": "Nos",
				"conversion_factor": 1,
				"rate": 100,
				"inventory_type": "Regular",
				"customer": None,
				"batch_no": "B1",
			},
		)


class TestGetItemDetails(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.nowdate", return_value="2026-01-01")
	@patch(f"{_MR_EVENTS}.frappe.qb.from_")
	def test_throws_if_inactive_or_missing(self, mock_from, mock_nowdate):
		mock_chain = MagicMock()
		mock_from.return_value = mock_chain
		mock_chain.left_join.return_value = mock_chain
		mock_chain.on.return_value = mock_chain
		mock_chain.select.return_value = mock_chain
		mock_chain.where.return_value = mock_chain
		mock_chain.run.return_value = []

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.get_item_details({"item_code": "INV-1"})
		self.assertIn("inactive or its end-of-life", str(ctx.exception))

	@patch(f"{_MR_EVENTS}.nowdate", return_value="2026-01-01")
	@patch(f"{_MR_EVENTS}.frappe.qb.from_")
	def test_returns_correct_details(self, mock_from, mock_nowdate):
		mock_chain = MagicMock()
		mock_from.return_value = mock_chain
		mock_chain.left_join.return_value = mock_chain
		mock_chain.on.return_value = mock_chain
		mock_chain.select.return_value = mock_chain
		mock_chain.where.return_value = mock_chain
		mock_chain.run.return_value = [
			frappe._dict(
				{
					"stock_uom": "Nos",
					"description": "Desc",
					"image": "img.png",
					"item_name": "Item Name",
					"has_serial_no": 1,
					"has_batch_no": 0,
					"sample_quantity": 2,
					"expense_account": "Exp",
				}
			)
		]

		res = mr_mod.get_item_details({"item_code": "ITM-1", "qty": 10})
		self.assertEqual(res.uom, "Nos")
		self.assertEqual(res.qty, 10)
		self.assertEqual(res.has_serial_no, 1)


_MR_BV = "jewellery_erpnext.jewellery_erpnext.customization.material_request.utils.before_validate"


class TestUpdatePureQty(IntegrationTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils.prefetch_purity_percentages"
	)
	@patch("frappe.db.get_value")
	def test_throws_if_pure_gold_item_missing(self, mock_get_value, mock_prefetch):
		mock_get_value.return_value = None
		mr = MagicMock()
		mr.custom_transfer_type = "Transfer to Reserve"
		mr.custom_manufacturer = "Manu-1"
		mr.items = [
			MagicMock(
				custom_variant_of="M", custom_alternative_item="ITEM-ALLOY", qty=10.0
			)
		]

		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_before_validate.update_pure_qty(mr)
		self.assertIn("Select Manufacturer in session defaults", str(ctx.exception))


class TestValidateWarehouse(IntegrationTestCase):
	def test_throws_if_set_warehouse_same(self):
		mr = MagicMock(
			material_request_type="Material Transfer",
			set_from_warehouse="WH-1",
			set_warehouse="WH-1",
		)
		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_before_validate.validate_warehouse(mr)
		self.assertIn("cannot be the same", str(ctx.exception))

	def test_throws_if_row_warehouse_same(self):
		mr = MagicMock(
			material_request_type="Material Transfer",
			set_from_warehouse="WH-1",
			set_warehouse="WH-2",
		)
		mr.items = [MagicMock(from_warehouse="WH-3", warehouse="WH-3")]
		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_before_validate.validate_warehouse(mr)
		self.assertIn("cannot be the same", str(ctx.exception))

	def test_ignores_non_material_transfer(self):
		mr = MagicMock(
			material_request_type="Manufacture",
			set_from_warehouse="WH-1",
			set_warehouse="WH-1",
		)
		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		mr_before_validate.validate_warehouse(mr)  # Should not throw


class TestMakeStockInEntry(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.get_mapped_doc")
	def test_mapping_configuration(self, mock_get_mapped_doc):
		def mock_get_mapped(*args, **kwargs):
			set_missing_values = args[4]
			target = MagicMock()
			source = MagicMock(_customer="CUST-1")
			set_missing_values(source, target)

			self.assertEqual(target.material_request_type, "Material Transfer")
			self.assertEqual(target.customer, "CUST-1")
			self.assertIsNone(target.custom_reserve_se)

			update_item = args[2]["Stock Entry Detail"]["postprocess"]
			target_row = frappe._dict()
			source_row = frappe._dict(
				parent="MR-1", name="MRI-1", t_warehouse="WH-T", qty=10
			)
			update_item(source_row, target_row, source)

			self.assertEqual(target_row.material_request, "MR-1")
			self.assertEqual(target_row.from_warehouse, "WH-T")
			self.assertEqual(target_row.warehouse, "")

			return target

		mock_get_mapped_doc.side_effect = mock_get_mapped
		mr_mod.make_stock_in_entry("MR-1")


class TestMakeStockEntry(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.get_mapped_doc")
	@patch(f"{_MR_EVENTS}.frappe.get_value")
	def test_mapping_configuration(self, mock_get_value, mock_get_mapped_doc):
		mock_get_value.return_value = frappe._dict(bom_no="BOM-1", for_quantity=100)

		def mock_get_mapped(*args, **kwargs):
			set_missing_values = args[4]
			update_item = args[2]["Material Request Item"]["postprocess"]

			source = MagicMock(
				material_request_type="Material Transfer",
				inventory_type="Customer Goods",
				job_card="JC-1",
				name="MR-1",
			)
			source.items = [
				MagicMock(
					item_code="ITM-1", idx=1, batch_no="BATCH-1", serial_no="SR-1"
				)
			]

			target = MagicMock()
			target_row = frappe._dict(item_code="ITM-1", idx=1, conversion_factor=1)
			target.items = [target_row]

			set_missing_values(source, target)

			self.assertEqual(target.purpose, "Material Transfer for Manufacture")
			self.assertEqual(target.stock_entry_type, "Customer Goods Transfer")
			self.assertEqual(target_row.batch_no, "BATCH-1")
			self.assertEqual(target.bom_no, "BOM-1")

			source_row = frappe._dict(
				stock_qty=10,
				ordered_qty=5,
				conversion_factor=1,
				warehouse="WH-S",
				from_warehouse="WH-FROM",
			)
			update_item(source_row, target_row, source)

			self.assertEqual(target_row.qty, 5)
			self.assertEqual(target_row.t_warehouse, "WH-S")
			self.assertNotIn("allow_zero_valuation_rate", target_row)

			return target

		mock_get_mapped_doc.side_effect = mock_get_mapped
		mr_mod.make_stock_entry("MR-1")


_GEPL = "Gurukrupa Export Private Limited"


def _reservation_mr(**kwargs):
	"""Minimal Manufacture MR. SimpleNamespace, not MagicMock: the rule branches on
	falsiness of set_warehouse/custom_manufacturer, which a MagicMock would make truthy."""
	doc = {
		"material_request_type": "Manufacture",
		"set_warehouse": None,
		"custom_manufacturer": "Shubh",
		"company": _GEPL,
		"items": [],
	}
	doc.update(kwargs)
	return SimpleNamespace(**doc)


def _reservation_row(variant, warehouse=None):
	return SimpleNamespace(custom_variant_of=variant, warehouse=warehouse)


@patch("frappe.get_cached_value", return_value=_GEPL)
@patch(
	f"{_MR_BV}.get_variant_warehouse_map",
	return_value={
		"M": "Waxing RSV - GEPL",
		"D": "Diamond Setting RSV - GEPL",
		"G": "Diamond Setting RSV - GEPL",
		"F": "Central RSV - GEPL",
	},
)
class TestSetReservationWarehouse(IntegrationTestCase):
	def test_fills_header_and_empty_rows(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Waxing RSV - GEPL")
		self.assertEqual(mr.items[0].warehouse, "Waxing RSV - GEPL")

	def test_distinct_variants_sharing_one_warehouse_still_fill(
		self, mock_map, mock_cached
	):
		# D and G both map to Diamond Setting RSV, so the header can still express it.
		mr = _reservation_mr(items=[_reservation_row("D"), _reservation_row("G")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Diamond Setting RSV - GEPL")

	def test_leaves_rows_that_already_match(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M", "Waxing RSV - GEPL")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Waxing RSV - GEPL")

	def test_ignores_non_manufacture_type(self, mock_map, mock_cached):
		mr = _reservation_mr(
			material_request_type="Material Transfer", items=[_reservation_row("M")]
		)
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_never_overwrites_an_existing_warehouse(self, mock_map, mock_cached):
		mr = _reservation_mr(
			set_warehouse="RM Procurement - GEPL", items=[_reservation_row("M")]
		)
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "RM Procurement - GEPL")

	def test_ignores_rows_routed_elsewhere(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M", "RM Procurement - GEPL")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)
		self.assertEqual(mr.items[0].warehouse, "RM Procurement - GEPL")

	def test_ignores_unmapped_variant(self, mock_map, mock_cached):
		# The dummy gemstone item carries variant_of = NULL.
		mr = _reservation_mr(items=[_reservation_row(None)])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_ignores_partially_mappable_request(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M"), _reservation_row(None)])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)
		self.assertIsNone(mr.items[0].warehouse)

	def test_ignores_variants_resolving_to_different_warehouses(
		self, mock_map, mock_cached
	):
		mr = _reservation_mr(items=[_reservation_row("M"), _reservation_row("D")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_ignores_empty_items(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_ignores_warehouse_of_another_company(self, mock_map, mock_cached):
		mock_cached.return_value = "KG GK Jewellers Private Limited"
		mr = _reservation_mr(items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	@patch("frappe.defaults.get_user_default", return_value=None)
	def test_ignores_missing_manufacturer(self, mock_default, mock_map, mock_cached):
		mock_map.return_value = {}
		mr = _reservation_mr(custom_manufacturer=None, items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	@patch("frappe.defaults.get_user_default", return_value="Shubh")
	def test_falls_back_to_session_default_manufacturer(
		self, mock_default, mock_map, mock_cached
	):
		mr = _reservation_mr(custom_manufacturer=None, items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Waxing RSV - GEPL")
		mock_map.assert_called_once_with("Shubh")
