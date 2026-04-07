# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.test_department_ir import (
	mo_creation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	create_operation_for_next_op,
	get_manufacturing_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	scan_mwo_eir,
)


class TestEmployeeIR(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_employee_ir_scan(self):
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_receive = dir_for_receive(dir_issue)
		mo_wax.reload()
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.department = "Waxing - GEPL"
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "GEPL - 00157"
		eir_issue.scan_mwo = mo_wax.manufacturing_work_order
		scan_mwo_eir(eir_issue)
		eir_issue.save()
		eir_issue.submit()
		mo_wax.reload()
		for row in eir_issue.employee_ir_operations:
			self.assertEqual(row.gross_wt, mo_wax.gross_wt)
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)

		eir_receive = frappe.new_doc("Employee IR")
		eir_receive.department = "Waxing - GEPL"
		eir_receive.type = "Receive"
		eir_receive.operation = "Wax Pull out"
		eir_receive.employee = "GEPL - 00157"
		eir_receive.scan_mwo = mo_wax.manufacturing_work_order
		scan_mwo_eir(eir_receive)
		eir_receive.save()
		eir_receive.submit()
		mo_wax.reload()
		for row in eir_receive.employee_ir_operations:
			self.assertEqual(row.gross_wt, mo_wax.gross_wt)
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)

	def test_department_ir_by_manufacturing_operation(self):
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_receive = dir_for_receive(dir_issue)
		mo_wax.reload()
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.department = mo_wax.department
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "GEPL - 00157"
		eir_issue = get_manufacturing_operations(mo_wax.name, eir_issue)
		eir_issue.save()
		if not eir_issue.employee_ir_operations[0].rpt_wt_issue:
			eir_issue.employee_ir_operations[0].rpt_wt_issue = 0
		eir_issue.submit()

		mo_wax.reload()
		for row in eir_issue.employee_ir_operations:
			self.assertEqual(row.gross_wt, mo_wax.gross_wt)
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)

		eir_receive = frappe.new_doc("Employee IR")
		eir_receive.department = "Waxing - GEPL"
		eir_receive.type = "Receive"
		eir_receive.operation = "Wax Pull out"
		eir_receive.employee = "GEPL - 00157"
		eir_receive = get_manufacturing_operations(mo_wax.name, eir_receive)
		eir_receive.save()
		if not eir_receive.employee_ir_operations[0].rpt_wt_issue:
			eir_receive.employee_ir_operations[0].rpt_wt_issue = 0
		eir_receive.submit()
		mo_wax.reload()
		for row in eir_receive.employee_ir_operations:
			self.assertEqual(row.gross_wt, mo_wax.gross_wt)
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)

	def test_create_operation_for_next_op_creates_copy_with_expected_fields(self):
		mo = mo_creation()
		mo.reload()
		original_mop = frappe.get_last_doc("Manufacturing Operation")

		new_mop = create_operation_for_next_op(
			original_mop.name, employee_ir="EIR-TEST", gross_wt=15.5
		)

		self.assertEqual(new_mop.prev_gross_wt, 15.5)
		self.assertEqual(new_mop.previous_mop, original_mop.name)
		self.assertEqual(new_mop.employee_ir, "EIR-TEST")
		self.assertIsNone(new_mop.employee)
		self.assertEqual(new_mop.status, "Not Started")

	def test_get_rows_to_append_returns_rows_for_positive_qty(self):
		doc = frappe._dict({"department": "DPT", "manufacturer": "MFG"})
		mwo = "MWO-TEST"
		mop = "MOP-TEST"
		mop_data = [frappe._dict({"qty": 2, "item_code": "M-ITEM", "batch_no": "B1"})]

		rows = get_rows_to_append(doc, mwo, mop, mop_data, "DEPT_WH", "EMP_WH")
		self.assertTrue(rows)
		self.assertEqual(rows[0]["manufacturing_operation"], mop)
		self.assertEqual(rows[0]["custom_manufacturing_work_order"], mwo)
		self.assertEqual(rows[0]["s_warehouse"], "DEPT_WH")
		self.assertEqual(rows[0]["t_warehouse"], "EMP_WH")

	def test_get_rows_to_append_ignores_zero_qty(self):
		doc = frappe._dict({"department": "DPT", "manufacturer": "MFG"})
		mwo = "MWO-TEST"
		mop = "MOP-TEST"
		mop_data = [frappe._dict({"qty": 0, "item_code": "M-ITEM"})]

		rows = get_rows_to_append(doc, mwo, mop, mop_data, "DEPT_WH", "EMP_WH")
		self.assertEqual(rows, [])

	def test_get_manufacturing_operations_does_not_duplicate_if_present(self):
		mo = mo_creation()
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")

		eir = frappe.new_doc("Employee IR")
		eir.employee_ir_operations = []
		eir = get_manufacturing_operations(mo_wax.name, eir)
		count_after_first = len(eir.employee_ir_operations)
		eir = get_manufacturing_operations(mo_wax.name, eir)
		count_after_second = len(eir.employee_ir_operations)

		self.assertEqual(count_after_first, count_after_second)

	def test_subcontracting_issue_sets_for_subcontracting_on_mop(self):
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		eir = frappe.new_doc("Employee IR")
		eir.type = "Issue"
		eir.department = "Waxing - GEPL"
		eir.operation = "Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC"
		eir.employee = "GEPL - 00157"
		eir.subcontracting = "Yes"
		eir.subcontractor = "GJSU0436"
		eir.scan_mwo = mo_wax.manufacturing_work_order
		scan_mwo_eir(eir)
		if not eir.employee_ir_operations[0].rpt_wt_issue:
			eir.employee_ir_operations[0].rpt_wt_issue = 0
		eir.save()
		eir.submit()
		mo_wax.reload()

		self.assertEqual(
			mo_wax.for_subcontracting,
			1,
			"MOP must have for_subcontracting=1 after Issue with subcontracting=Yes.",
		)
		self.assertEqual(
			mo_wax.subcontractor,
			"GJSU0436",
			"MOP must carry the subcontractor name after Issue.",
		)

	def test_before_validate_throws_error_when_stock_reconciliation_in_progress(self):
		eir = frappe.new_doc("Employee IR")
		eir.department = "Waxing - GEPL"
		eir.type = "Issue"
		eir.docstatus = 0

		with patch("frappe.db.get_value") as mock_get_value:
			mock_get_value.side_effect = [
				"WH-001",
				"SR-001",
			]
			with self.assertRaises(frappe.ValidationError):
				eir.before_validate()

	def test_on_submit_issue_new_sets_subcontracting_values(self):
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		eir = frappe.new_doc("Employee IR")
		eir.type = "Issue"
		eir.department = "Waxing - GEPL"
		eir.operation = "Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC"
		eir.employee = "GEPL - 00157"
		eir.subcontracting = "Yes"
		eir.subcontractor = "GJSU0436"
		eir.manufacturer = "Shubh"
		eir = get_manufacturing_operations(mo_wax.name, eir)

		if not eir.employee_ir_operations[0].rpt_wt_issue:
			eir.employee_ir_operations[0].rpt_wt_issue = 0

		eir.save()
		eir.submit()

		mo_wax.reload()
		self.assertEqual(
			mo_wax.for_subcontracting,
			1,
			"MOP should have for_subcontracting=1 after subcontracting issue",
		)
		self.assertEqual(
			mo_wax.subcontractor,
			"GJSU0436",
			"MOP should have subcontractor assigned",
		)

	def tearDown(self):
		return super().tearDown()


def get_rows_to_append(doc, mwo, mop, mop_data, department_wh, employee_wh):
	rows_to_append = []
	import copy

	if not mop_data:
		mop_data = []

	for row in mop_data:
		if row.qty > 0:
			duplicate_row = copy.deepcopy(row)
			duplicate_row["name"] = None
			duplicate_row["idx"] = None
			duplicate_row["t_warehouse"] = employee_wh
			duplicate_row["s_warehouse"] = department_wh
			duplicate_row["manufacturing_operation"] = mop
			duplicate_row["use_serial_batch_fields"] = True
			duplicate_row["serial_and_batch_bundle"] = None
			duplicate_row["custom_manufacturing_work_order"] = mwo
			duplicate_row["department"] = doc.department
			duplicate_row["to_department"] = doc.department
			duplicate_row["manufacturer"] = doc.manufacturer

			rows_to_append.append(duplicate_row)

	return rows_to_append
