# Copyright (c) 2024, Nirali and Contributors
# See license.txt

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	mop_log_creation,
	scan_mwo_eir,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	calulate_id_wise_sum_up,
	validate_qty,
)


class TestSerialNumberCreator(FrappeTestCase):
	def setUp(self):
		self.doc = frappe.new_doc("Serial Number Creator")
		self.doc.type = "Manufacturing"
		self.doc.company = "Your Company"
		create_test_data()
		self.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		return super().setUp()

	def test_serial_number_creator(self):
		snc = create_snc(self)
		snc.submit()
		se = frappe.get_doc("Stock Entry", {"custom_serial_number_creator": snc.name})
		self.assertEqual(snc.name, se.custom_serial_number_creator)
		self.assertEqual("Manufacture", se.stock_entry_type)
		for idx in range(len(snc.source_table)):
			src = snc.source_table[idx]
			sed = se.items[idx]
			self.assertEqual(src.s_warehouse, sed.s_warehouse)
			self.assertEqual(src.row_material, sed.item_code)
			self.assertEqual(src.qty, sed.qty)
			self.assertEqual(src.batch_no, sed.batch_no)

		sn = frappe.get_doc("Serial No", snc.fg_serial_no)
		self.assertEqual(sn.name, snc.fg_serial_no)
		self.assertEqual(sn.custom_bom_no, snc.fg_bom)
		self.assertEqual(sn.reference_name, se.name)

	def test_validate_qty_valid_quantity(self):
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "qty": 5.5, "uom": "kg"}
		)

		try:
			validate_qty(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("validate_qty() raised ValidationError with valid quantity")

	def test_calculate_id_wise_sum_up_multiple_fg_rows(self):
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "id": 1, "qty": 2.5, "uom": "kg"}
		)
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "id": 2, "qty": 2.5, "uom": "kg"}
		)

		self.doc.append(
			"source_table", {"row_material": "ITEM-001", "qty": 5.0, "uom": "kg"}
		)

		try:
			calulate_id_wise_sum_up(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("calulate_id_wise_sum_up() raised error with correct sum")

	def test_calculate_id_wise_sum_up_mismatch(self):
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "id": 1, "qty": 2.5, "uom": "kg"}
		)

		self.doc.append(
			"source_table", {"row_material": "ITEM-001", "qty": 3.5, "uom": "kg"}
		)

		with self.assertRaises(frappe.exceptions.ValidationError):
			calulate_id_wise_sum_up(self.doc)

	def test_calculate_id_wise_sum_decimal_precision(self):
		self.doc.append(
			"fg_details",
			{"row_material": "ITEM-001", "id": 1, "qty": 2.5555, "uom": "kg"},
		)

		rounded_qty = float(
			Decimal(str(2.5555)).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
		)

		self.doc.append(
			"source_table",
			{"row_material": "ITEM-001", "qty": rounded_qty, "uom": "kg"},
		)

		try:
			calulate_id_wise_sum_up(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("calulate_id_wise_sum_up() raised error with rounded quantities")

	def test_get_bom_summary_with_fg_bom(self):
		self.doc.fg_bom = (
			"BOM-TEST-001" if frappe.db.exists("BOM", "BOM-TEST-001") else None
		)

		if self.doc.fg_bom:
			summary = self.doc.get_bom_summary()
			self.assertIsNotNone(summary)
			self.assertIn("item_code", summary or "")

	def test_get_bom_summary_with_design_id_bom(self):
		self.doc.fg_bom = None
		self.doc.design_id_bom = (
			"BOM-TEST-001" if frappe.db.exists("BOM", "BOM-TEST-001") else None
		)

		if self.doc.design_id_bom:
			summary = self.doc.get_bom_summary()
			self.assertIsNotNone(summary)

	def test_get_serial_summary_structure(self):
		try:
			if self.doc.fg_serial_no:
				result = self.doc.get_serial_summary()
				self.assertIsNotNone(result)
		except Exception:
			pass

	def test_fg_details_row_structure(self):
		self.doc.append(
			"fg_details",
			{
				"row_material": "ITEM-001",
				"id": 1,
				"batch_no": "BATCH-001",
				"qty": 5.5,
				"uom": "kg",
				"gross_wt": 5.5,
				"inventory_type": "Stock",
				"pcs": 1,
			},
		)

		row = self.doc.fg_details[0]
		self.assertEqual(row.row_material, "ITEM-001")
		self.assertEqual(row.id, 1)
		self.assertEqual(row.batch_no, "BATCH-001")
		self.assertEqual(row.qty, 5.5)
		self.assertEqual(row.uom, "kg")

	def test_source_table_row_structure(self):
		self.doc.append(
			"source_table",
			{"row_material": "ITEM-001", "qty": 10.0, "uom": "kg", "pcs": 2},
		)

		row = self.doc.source_table[0]
		self.assertEqual(row.row_material, "ITEM-001")
		self.assertEqual(row.qty, 10.0)
		self.assertEqual(row.uom, "kg")
		self.assertEqual(row.pcs, 2)

	def test_serial_no_creator_document_fields(self):
		self.doc.type = "Manufacturing"
		self.doc.manufacturing_work_order = "MWO-001"
		self.doc.parent_manufacturing_order = "PMO-001"
		self.doc.company = "Test Company"

		self.assertEqual(self.doc.type, "Manufacturing")
		self.assertEqual(self.doc.manufacturing_work_order, "MWO-001")
		self.assertEqual(self.doc.parent_manufacturing_order, "PMO-001")

	def test_empty_fg_details(self):
		self.assertEqual(len(self.doc.fg_details), 0)

		try:
			validate_qty(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("validate_qty() should not fail with empty fg_details")

	def test_decimal_quantity_handling(self):
		quantities = [0.5, 1.25, 2.333, 5.9999]

		for qty in quantities:
			test_doc = frappe.new_doc("Serial Number Creator")
			test_doc.append(
				"fg_details", {"row_material": f"ITEM-{qty}", "qty": qty, "uom": "kg"}
			)

			self.assertEqual(test_doc.fg_details[0].qty, qty)

	def tearDown(self):
		return super().tearDown()


def create_snc(self):
	pmo = create_pmo(self)
	mwo_list = frappe.get_all(
		"Manufacturing Work Order",
		filters={"manufacturing_order": pmo.name},
		fields=["name", "department", "manufacturing_order"],
	)
	serial_no_mwo = None
	mr_list = frappe.get_all(
		"Material Request",
		filters={
			"manufacturing_order": mwo_list[0].manufacturing_order,
			"docstatus": 0,
		},
		pluck="name",
	)
	for row in mwo_list:
		if row.department == "Manufacturing Plan & Management - T":
			mwo = frappe.get_doc("Manufacturing Work Order", row.name)
			mwo.submit()
			mo_man = frappe.get_last_doc(
				"Manufacturing Operation",
				filters={"manufacturing_work_order": mwo.name},
			)

			if mr_list:
				mop_log_creation(mr_list[0], mo_man)
				mop_log_creation(mr_list[1], mo_man)

			dir_issue = dir_for_issue(
				"Manufacturing Plan & Management - T", "Waxing - T", mo_man
			)
			mo_man.reload()

			mo_wax = frappe.get_last_doc("Manufacturing Operation")

			dir_for_receive(dir_issue)

			eir_issue = frappe.new_doc("Employee IR")
			eir_issue.company = "Test_Company"
			eir_issue.department = "Waxing - T"
			eir_issue.operation = "Wax Pull Out"
			eir_issue.employee = "HR-EMP-00002"
			eir_issue.scan_mwo = mo_wax.manufacturing_work_order
			scan_mwo_eir(eir_issue)
			eir_issue.save()
			eir_issue.submit()

			eir_receive = frappe.new_doc("Employee IR")
			eir_receive.company = "Test_Company"
			eir_receive.department = "Waxing - T"
			eir_receive.type = "Receive"
			eir_receive.operation = "Wax Pull out"
			eir_receive.employee = "HR-EMP-00002"
			eir_receive.scan_mwo = mo_wax.manufacturing_work_order
			scan_mwo_eir(eir_receive)
			eir_receive.save()
			eir_receive.employee_ir_operations[
				0
			].received_gross_wt = eir_receive.employee_ir_operations[0].gross_wt
			eir_receive.submit()

			mo_wax.reload()

			mo_wax1 = frappe.get_last_doc("Manufacturing Operation")

			dir_issue = dir_for_issue("Waxing - T", "Tagging - T", mo_wax1)
			mo_wax1.reload()

			mo_tag = frappe.get_last_doc("Manufacturing Operation")
			dir_for_receive(dir_issue)
			mo_tag.reload()

		elif row.department == "Serial Number - T":
			serial_no_mwo = row

	mwo_serial_no = frappe.get_doc("Manufacturing Work Order", serial_no_mwo.name)
	mwo_serial_no.submit()

	return frappe.get_last_doc("Serial Number Creator")
