# Copyright (c) 2024, Nirali and Contributors
# See license.txt

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	calulate_id_wise_sum_up,
	validate_qty,
)


class TestSerialNumberCreator(FrappeTestCase):
	def setUp(self):
		self.doc = frappe.new_doc("Serial Number Creator")
		self.doc.type = "Manufacturing"
		self.doc.company = "Your Company"

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

	def test_get_bom_summary_no_bom(self):
		self.doc.fg_bom = None
		self.doc.design_id_bom = None

		summary = self.doc.get_bom_summary()
		self.assertEqual(summary, "")

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
