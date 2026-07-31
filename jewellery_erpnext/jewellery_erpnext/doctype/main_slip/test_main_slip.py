# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase


from unittest.mock import MagicMock, patch

from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	MainSlip,
	create_loss_item,
	create_loss_stock_entries,
	create_material_request,
	create_process_loss,
	create_tree_number,
	get_item_loss_item,
)

class TestMainSlip(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self.doc = frappe.new_doc("Main Slip")
		self.doc.flags.ignore_permissions = True

	def test_autoname_success(self):
		self.doc.department = "Dept"
		self.doc.metal_type = "Gold"
		self.doc.metal_colour = "Yellow"
		with patch("frappe.get_value", return_value="DPT"):
			self.doc.autoname()
			self.assertEqual(self.doc.dep_abbr, "DPT")
			self.assertEqual(self.doc.type_abbr, "G")
			self.assertEqual(self.doc.color_abbr, "Y")

	def test_autoname_allowed_colours(self):
		self.doc.department = "Dept"
		self.doc.metal_type = "Silver"
		self.doc.metal_colour = None
		self.doc.allowed_colours = "Multicolor"
		with patch("frappe.get_value", return_value="DPT"):
			self.doc.autoname()
			self.assertEqual(self.doc.color_abbr, "MULTICOLOR")

	def test_autoname_missing_dept_abbr_raises(self):
		self.doc.department = "Dept"
		with patch("frappe.get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				self.doc.autoname()

	def test_validate_sets_warehouses_for_employee(self):
		self.doc.for_subcontracting = 0
		self.doc.employee = "EMP-001"
		self.doc.company = "Comp"
		
		def mock_get_value(doctype, filters):
			if filters.get("warehouse_type") == "Manufacturing":
				return "EMP-MFG-WH"
			return "EMP-RAW-WH"

		self.doc.validate_metal_properties = MagicMock()
		self.doc.update_batch_details = MagicMock()

		with patch("frappe.db.get_value", side_effect=mock_get_value):
			self.doc.validate()
			self.assertEqual(self.doc.warehouse, "EMP-MFG-WH")
			self.assertEqual(self.doc.raw_material_warehouse, "EMP-RAW-WH")
			self.doc.validate_metal_properties.assert_called_once()

	def test_validate_missing_warehouse_raises(self):
		self.doc.for_subcontracting = 0
		self.doc.employee = "EMP-001"
		self.doc.validate_metal_properties = MagicMock()
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "Please set warehouse"):
				self.doc.validate()

	def test_validate_subcontracting_and_computed_gold_wt(self):
		self.doc.for_subcontracting = 1
		self.doc.subcontractor = "SUB-001"
		self.doc.metal_touch = "18KT"
		self.doc.tree_wax_wt = 10.0
		self.doc.is_tree_reqd = 1
		
		def mock_get_value(doctype, filters, *args, **kwargs):
			if doctype == "Warehouse":
				return "SUB-WH"
			if doctype == "Manufacturing Setting":
				return 0.750
			return None
			
		self.doc.validate_metal_properties = MagicMock()
		self.doc.update_batch_details = MagicMock()
		self.doc.get_item_from_attribute = MagicMock()
		
		with patch("frappe.db.get_value", side_effect=mock_get_value), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_material_request"):
			self.doc.validate()
			self.assertEqual(self.doc.warehouse, "SUB-WH")
			self.assertEqual(self.doc.raw_material_warehouse, "SUB-WH")
			self.assertEqual(self.doc.computed_gold_wt, 7.5)

	def test_validate_metal_properties_mismatch_raises(self):
		self.doc.main_slip_operation = [frappe._dict(manufacturing_work_order="MWO-001")]
		self.doc.metal_type = "Gold"
		self.doc.metal_touch = "22KT"
		self.doc.metal_purity = "91.6"
		self.doc.metal_colour = "Yellow"
		self.doc.check_color = 1
		
		mwo_data = frappe._dict(
			metal_type="Silver", metal_touch="22KT", metal_purity="91.6", 
			metal_colour="Yellow", multicolour=0, allowed_colours=""
		)
		with patch("frappe.db.get_value", return_value=mwo_data):
			with self.assertRaisesRegex(frappe.ValidationError, "Metal properties in MWO: MWO-001 do not match"):
				self.doc.validate_metal_properties()

	def test_validate_metal_properties_matches_passes(self):
		self.doc.main_slip_operation = [frappe._dict(manufacturing_work_order="MWO-001")]
		self.doc.metal_type = "Gold"
		self.doc.metal_touch = "22KT"
		self.doc.metal_purity = "91.6"
		self.doc.metal_colour = "Yellow"
		self.doc.check_color = 1
		
		mwo_data = frappe._dict(
			metal_type="Gold", metal_touch="22KT", metal_purity="91.6", 
			metal_colour="Yellow", multicolour=0, allowed_colours=""
		)
		with patch("frappe.db.get_value", return_value=mwo_data):
			self.doc.validate_metal_properties()

	def test_update_batch_details_aggregates_stock_and_loss(self):
		self.doc.stock_details = [
			frappe._dict(variant_of="M", item_code="ITEM1", batch_no="B1", inventory_type="Reg", 
			             qty=10, consume_qty=2, mop_qty=5, mop_consume_qty=1, employee_qty=0, customer=None)
		]
		self.doc.batch_details = []
		self.doc.loss_details = []
		
		self.doc.update_batch_details()
		
		self.assertEqual(len(self.doc.batch_details), 1)
		self.assertEqual(self.doc.batch_details[0].qty, 10)
		self.assertEqual(self.doc.batch_details[0].consume_qty, 2)
		self.assertEqual(self.doc.batch_details[0].mop_qty, 5)
		
		self.assertEqual(self.doc.pending_metal, 12)
		
		self.assertEqual(len(self.doc.loss_details), 1)
		self.assertEqual(self.doc.loss_details[0].msl_qty, 12)

	def test_on_submit_raises_if_mop_not_finished(self):
		self.doc.main_slip_operation = [
			frappe._dict(manufacturing_operation="MOP-001", manufacturing_work_order="MWO-001")
		]
		with patch("frappe.db.get_value", return_value="Draft"):
			with self.assertRaisesRegex(frappe.ValidationError, "Manufacturing Operations are not finished yet"):
				self.doc.on_submit()

	def test_before_insert_creates_tree_number(self):
		self.doc.is_tree_reqd = 1
		with patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_tree_number", return_value="TREE-001"):
			self.doc.before_insert()
			self.assertEqual(self.doc.tree_number, "TREE-001")

	def test_create_tree_number(self):
		self.doc.company = "Comp"
		mock_tree = MagicMock()
		mock_tree.name = "TREE-002"
		mock_tree.insert.return_value = mock_tree
		with patch("frappe.get_doc", return_value=mock_tree):
			tree_no = create_tree_number(self.doc)
			self.assertEqual(tree_no, "TREE-002")
			mock_tree.insert.assert_called_once()

	def test_create_material_request(self):
		self.doc.metal_type = "Gold"
		self.doc.metal_touch = "22KT"
		self.doc.metal_purity = "91.6"
		self.doc.metal_colour = "Yellow"
		self.doc.name = "MS-001"
		self.doc.department = "DPT"
		self.doc.manufacturer = "MFG"
		self.doc.computed_gold_wt = 15.5
		
		mock_mr = MagicMock()
		with patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_from_attribute", return_value="ITEM-GOLD"), \
		     patch("frappe.new_doc", return_value=mock_mr), \
		     patch("frappe.db.get_value", return_value="DPT-WH"), \
		     patch("frappe.utils.nowdate", return_value="2026-07-31"):
			
			create_material_request(self.doc)
			self.assertEqual(mock_mr.material_request_type, "Material Transfer")
			self.assertEqual(mock_mr.to_main_slip, "MS-001")
			self.assertTrue(mock_mr.append.called)
			append_args = mock_mr.append.call_args[0]
			self.assertEqual(append_args[0], "items")
			self.assertEqual(append_args[1]["item_code"], "ITEM-GOLD")
			self.assertEqual(append_args[1]["qty"], 15.5)
			mock_mr.save.assert_called_once()

	def test_create_loss_stock_entries(self):
		self.doc.name = "MS-001"
		self.doc.subcontractor = "SUB"
		self.doc.company = "Comp"
		self.doc.department = "DPT"
		self.doc.manufacturer = "MFG"
		self.doc.raw_material_warehouse = "RAW-WH"
		self.doc.warehouse = "MFG-WH"
		self.doc.append("batch_details", {
			"item_code": "ITEM-001", "batch_no": "B1", "inventory_type": "Reg", "qty": 10, "employee_qty": 2
		})
		
		mock_se = MagicMock()
		
		with patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_metal_loss") as mock_loss, \
		     patch("frappe.new_doc", return_value=mock_se), \
		     patch("frappe.db.get_value", return_value="RAW-DPT-WH"):
			
			create_loss_stock_entries(self.doc, "ITEM-001", "M", 5.0, 1.0)
			
			mock_loss.assert_called_once()
			
			self.assertTrue(mock_se.append.called)
			append_args = mock_se.append.call_args[0]
			self.assertEqual(append_args[0], "items")
			self.assertEqual(append_args[1]["item_code"], "ITEM-001")
			self.assertEqual(append_args[1]["qty"], 5.0)
			self.assertEqual(append_args[1]["t_warehouse"], "RAW-DPT-WH")
			mock_se.save.assert_called_once()

	def test_create_process_loss(self):
		mock_doc = MagicMock()
		mock_doc.name = "MS-001"
		mock_doc.department = "DPT"
		mock_doc.employee = "EMP"
		mock_doc.subcontractor = "SUB"
		mock_doc.manufacturer = "MFG"
		mock_doc.raw_material_warehouse = "RAW"

		mock_se = MagicMock()
		mock_se.items = [frappe._dict(basic_rate=100.0)]

		with patch("frappe.get_doc", side_effect=lambda dt, dn: mock_doc if dt=="Main Slip" else mock_se), \
		     patch("frappe.new_doc", return_value=mock_se), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item", return_value="DUST-001"), \
		     patch("frappe.db.get_value", side_effect=lambda dt, dn, fn, **kw: {"loss_warehouse": "LOSS-WH"} if dt=="Variant Loss Warehouse" else 0), \
		     patch("frappe.db.set_value"):
			
			create_process_loss("MS-001", "MOP1", "ITEM1", 10, 8, -2, "B1", "Reg")
			
			self.assertEqual(mock_se.stock_entry_type, "Process Loss")
			self.assertTrue(mock_se.append.called)
			
			calls = mock_se.append.call_args_list
			# First call should be the item
			self.assertEqual(calls[0][0][1]["item_code"], "ITEM1")
			self.assertEqual(calls[0][0][1]["qty"], 2.0)
			
			# Second call should be the dust
			self.assertEqual(calls[1][0][1]["item_code"], "DUST-001")
			self.assertEqual(calls[1][0][1]["t_warehouse"], "LOSS-WH")
			
			mock_se.save.assert_called_once()

	def test_get_item_loss_item_existing(self):
		with patch("frappe.db.get_value", side_effect=lambda dt, dn, fn, **kw: "EXISTING_LOSS_ITEM" if dt=="Variant Loss Table" else None), \
		     patch("frappe.db.get_all", return_value=[frappe._dict(attribute="Color", attribute_value="Red")]), \
		     patch("jewellery_erpnext.utils.set_items_from_attribute") as mock_set:
			
			mock_item = MagicMock()
			mock_item.name = "FOUND-ITEM"
			mock_set.return_value = mock_item
			
			name = get_item_loss_item("Comp", "SRC-ITEM", "M")
			self.assertEqual(name, "FOUND-ITEM")
			mock_item.save.assert_called_once()

	def test_get_item_loss_item_creates_new(self):
		with patch("frappe.db.get_value", side_effect=lambda dt, dn, fn, **kw: "VARIANT_TMPL" if dt=="Variant Loss Table" else None), \
		     patch("frappe.db.get_all", return_value=[frappe._dict(attribute="Color", attribute_value="Red")]), \
		     patch("jewellery_erpnext.utils.set_items_from_attribute", return_value=None), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_loss_item", return_value="NEW-ITEM"):
			
			name = get_item_loss_item("Comp", "SRC-ITEM", "M")
			self.assertEqual(name, "NEW-ITEM")

	def test_create_stock_entries(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import create_stock_entries
		self.doc.name = "MS-001"
		self.doc.department = "DPT"
		self.doc.manufacturer = "MFG"
		self.doc.raw_material_warehouse = "RAW-WH"
		self.doc.warehouse = "MFG-WH"
		self.doc.append("batch_details", {
			"item_code": "WRONG-ITEM", "batch_no": "B1", "inventory_type": "Reg", "qty": 10, "mop_qty": 5, "consume_qty": 0
		})
		
		mock_se = MagicMock()
		
		with patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_metal_loss") as mock_loss, \
		     patch("frappe.new_doc", return_value=mock_se), \
		     patch("frappe.get_doc", return_value=self.doc), \
		     patch("frappe.db.get_value", return_value="RAW-DPT-WH"), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_from_attribute", return_value="ITEM-001"):
			
			create_stock_entries(self.doc, actual_qty=5.0, metal_loss=1.0, metal_type="Gold", metal_touch="18KT", metal_purity=75.0, metal_colour="Yellow")
			
			mock_loss.assert_called_once()
			self.assertFalse(mock_se.append.called)

	def test_create_metal_loss(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import create_metal_loss
		self.doc.name = "MS-001"
		self.doc.department = "DPT"
		self.doc.manufacturer = "MFG"
		self.doc.raw_material_warehouse = "RAW-WH"
		self.doc.warehouse = "MFG-WH"
		self.doc.employee = "EMP"
		self.doc.subcontractor = "SUB"
		
		batch_data = [
			{"batch_no": "B1", "qty": 10.0, "inventory_type": "Reg"}
		]
		
		mock_se = MagicMock()
		
		with patch("frappe.new_doc", return_value=mock_se), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item", return_value="LOSS-ITEM"), \
		     patch("frappe.db.get_value", side_effect=lambda dt, dn, fn, **kw: {"loss_warehouse": "LOSS-WH"} if dt=="Variant Loss Warehouse" else None), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.stamp_produce_rows_from_consumes", create=True) as mock_stamp:
			
			create_metal_loss(self.doc, "ITEM-001", "M", 2.0, batch_data)
			
			self.assertEqual(mock_se.stock_entry_type, "Repack")
			self.assertTrue(mock_se.append.called)
			
			calls = mock_se.append.call_args_list
			# Consumption row
			self.assertEqual(calls[0][0][1]["item_code"], "ITEM-001")
			self.assertEqual(calls[0][0][1]["s_warehouse"], "RAW-WH")
			self.assertEqual(calls[0][0][1]["qty"], 2.0)
			
			# Produce row
			self.assertEqual(calls[1][0][1]["item_code"], "LOSS-ITEM")
			self.assertEqual(calls[1][0][1]["t_warehouse"], "LOSS-WH")
			self.assertEqual(calls[1][0][1]["qty"], 2.0)
			
			mock_stamp.assert_called_once_with(mock_se)
			mock_se.save.assert_called_once()

	def test_get_main_slip_item(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import get_main_slip_item
		mock_ms = frappe._dict({"metal_type": "Gold", "metal_touch": "18KT", "metal_purity": "75.0", "metal_colour": "Yellow"})
		with patch("frappe.db.get_value", return_value=mock_ms), \
		     patch("jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_from_attribute", return_value="ITEM-18K-Y"):
			
			item = get_main_slip_item("MS-001")
			self.assertEqual(item, "ITEM-18K-Y")
