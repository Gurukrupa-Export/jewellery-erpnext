# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	get_material_wt,
	update_new_mop_wtg,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_employee_ir_receive,
)


def _balance_row(item_code, qty, pcs=0, batch_no=None, **overrides):
	row = {
		"item_code": item_code,
		"qty": qty,
		"pcs": pcs,
		"batch_no": batch_no,
		"pcs_after_transaction": pcs,
		"pcs_after_transaction_item_based": pcs,
		"pcs_after_transaction_batch_based": pcs,
		"qty_after_transaction": qty,
		"qty_after_transaction_item_based": qty,
		"qty_after_transaction_batch_based": qty,
		"serial_and_batch_bundle": None,
		"flow_index": 2,
		"from_warehouse": "WH-A",
		"to_warehouse": "WH-B",
		"row_name": "ROW-1",
		"manufacturing_work_order": "MWO-1",
		"manufacturing_operation": "MOP-1",
	}
	row.update(overrides)
	return FrappeDict(row)


class MockRow:
	def __init__(self):
		self.manufacturing_operation = "MOP-TEST-001"
		self.name = "row-child-1"
		self.manufacturing_work_order = "MWO-TEST-001"


class TestEmployeeIRReceiveLineageGuard(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EMP-IR-ISSUE-1",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
		return_value=[
			FrappeDict({"item_code": "M-A", "batch_no": "BM1"}),
			FrappeDict({"item_code": "D-A", "batch_no": "BD1"}),
		],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[_balance_row("M-A", 5.0, batch_no="BM1")],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	def test_receive_throws_when_issue_logs_miss_current_balance_component(
		self, _log_error, _get_all, _current_balance, _resolve_issue
	):
		doc = FrappeDict({"name": "EMP-IR-RECV-1", "emp_ir_id": "EMP-IR-ISSUE-1"})
		row = MockRow()

		with self.assertRaises(frappe.ValidationError):
			create_mop_log_for_employee_ir_receive(doc, row, "WH-EMP", "WH-DEPT")

		_log_error.assert_called_once()
class TestManufacturingOperationBalance(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_current_mop_balance_rows",
		return_value=[
			FrappeDict({"item_code": "M-A", "qty": 0.23, "pcs": 0, "batch_no": "BM1"}),
			FrappeDict({"item_code": "D-A", "qty": 1.008, "pcs": 168, "batch_no": "BD1"}),
		],
	)
	def test_get_material_wt_uses_current_balance_rows(self, _current_balance):
		doc = FrappeDict(
			{
				"name": "MOP-TEST-001",
				"main_slip_no": None,
				"is_finding": 0,
				"loss_wt": 0,
				"employee_loss_wt": 0,
			}
		)

		out = get_material_wt(doc)

		self.assertEqual(out["net_wt"], 0.23)
		self.assertEqual(out["diamond_wt"], 1.008)
		self.assertEqual(out["diamond_pcs"], 168)
		self.assertAlmostEqual(out["gross_wt"], 0.4316, places=4)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_current_mop_balance_rows",
		return_value=[
			_balance_row("M-A", 0.23, batch_no="BM1", manufacturing_operation="MOP-PREV"),
			_balance_row("D-A", 1.008, pcs=168, batch_no="BD1", manufacturing_operation="MOP-PREV"),
		],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_last_mop_index",
		side_effect=[None],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	def test_update_new_mop_wtg_clones_current_balance_rows(
		self, mock_new_doc, _current_doc_index, _current_balance
	):
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log
		doc = FrappeDict(
			{
				"name": "MOP-NEXT",
				"previous_mop": "MOP-PREV",
				"gross_wt": 0,
			}
		)

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

		self.assertEqual(len(new_mop.department_source_table), 0)
		self.assertEqual(len(new_mop.department_target_table), 0)
		self.assertEqual(len(new_mop.employee_source_table), 0)
		self.assertEqual(len(new_mop.employee_target_table), 0)

		self.assertIsNone(new_mop.department_issue_id)
		self.assertIsNone(new_mop.department_receive_id)
		self.assertFalse(new_mop.department_ir_status)
		self.assertIsNone(new_mop.operation)
		self.assertIsNone(new_mop.main_slip_no)

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

	def test_get_manufacturing_operations_with_serialized_target_doc(self):
		mo = mo_creation()
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")

		target_doc = frappe.new_doc("Employee IR")
		target_doc.employee_ir_operations = []
		target_json = frappe.as_json(target_doc)

		result = get_manufacturing_operations(mo_wax.name, target_json)

		self.assertTrue(len(result.employee_ir_operations) > 0)
		self.assertEqual(
			result.employee_ir_operations[0].manufacturing_operation, mo_wax.name
		)
		self.assertEqual(result.employee_ir_operations[0].gross_wt, mo_wax.gross_wt)

	def test_validate_process_loss_proportional_loss_calculation(self):
		mo = mo_creation()
		mo.save()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.department = "Waxing - GEPL"
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "GEPL - 00157"
		eir_issue.scan_mwo = mo_wax.manufacturing_work_order
		scan_mwo_eir(eir_issue)
		eir_issue.save()
		eir_issue.submit()
		mo_wax.reload()

		from_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": eir_issue.department,
				"warehouse_type": "Manufacturing",
			},
		)
		to_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"warehouse_type": "Manufacturing",
				"disabled": 0,
				"employee": eir_issue.employee,
			},
		)

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "M-G-22KT-91.9-Y"
		mop_log.pcs_after_transaction = 3
		mop_log.qty_after_transaction = 3
		mop_log.pcs_after_transaction_item_based = 1
		mop_log.pcs_after_transaction_batch_based = 1
		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = "Employee IR"
		mop_log.voucher_no = eir_issue.name
		mop_log.row_name = eir_issue.employee_ir_operations[0].name

		mop_log.qty_after_transaction_item_based = 1
		mop_log.qty_after_transaction_batch_based = 1
		mop_log.manufacturing_operation = eir_issue.employee_ir_operations[
			0
		].manufacturing_operation
		mop_log.manufacturing_work_order = eir_issue.employee_ir_operations[
			0
		].manufacturing_work_order
		mop_log.batch_no = ""
		mop_log.save()

		eir = frappe.new_doc("Employee IR")
		eir.department = mo_wax.department
		eir.type = "Receive"
		eir.operation = "Wax Pull out"
		eir.employee = "GEPL - 00157"
		eir = get_manufacturing_operations(mo_wax.name, eir)

		if eir.employee_ir_operations:
			eir.employee_ir_operations[0].received_gross_wt = 1

			if not eir.employee_ir_operations[0].rpt_wt_issue:
				eir.employee_ir_operations[0].rpt_wt_issue = 0

		eir.save()
		eir.validate_process_loss()

		self.assertTrue(
			len(eir.employee_loss_details) > 0,
			"Employee loss details should be populated after validate_process_loss",
		)

		total_loss = sum(row.proportionally_loss for row in eir.employee_loss_details)
		self.assertGreater(
			total_loss,
			0,
			"Total proportional loss should be greater than 0",
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
		update_new_mop_wtg(doc)

		self.assertEqual(mock_new_doc.call_count, 2)
		self.assertEqual(mock_log.flow_index, 0)
		self.assertEqual(mock_log.manufacturing_operation, "MOP-NEXT")
