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


class MockEIROperation:
	def __init__(self, mop="MOP-1", mwo="MWO-1", idx=1):
		self.manufacturing_operation = mop
		self.manufacturing_work_order = mwo
		self.idx = idx
		self.name = "row-1"


class TestEmployeeIRBeforeSubmitValidation(FrappeTestCase):
	"""B1/MF-6 + B2/MF-9: before_submit guards for warehouse and MWO docstatus."""

	def _make_eir(self, emp="EMP-001", subcontracting="No", ops=None):
		doc = MagicMock()
		doc.type = "Issue"
		doc.employee = emp
		doc.subcontracting = subcontracting
		doc.employee_ir_operations = ops or [MockEIROperation()]
		return doc

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
		side_effect=[1, None],  # MWO docstatus=1, then employee warehouse=None
	)
	def test_issue_blocked_when_employee_warehouse_missing(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		doc = self._make_eir(emp="EMP-NO-WH")
		with self.assertRaises(frappe.ValidationError):
			EmployeeIR.before_submit(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
		return_value=0,  # MWO docstatus = 0 (Draft)
	)
	def test_issue_blocked_for_draft_mwo(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		doc = self._make_eir()
		with self.assertRaises(frappe.ValidationError):
			EmployeeIR.before_submit(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
		side_effect=[1, "WH-EMP-001"],  # MWO docstatus=1, employee warehouse found
	)
	def test_issue_passes_when_mwo_submitted_and_warehouse_exists(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		doc = self._make_eir()
		EmployeeIR.before_submit(doc)  # must not raise

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
		return_value=1,  # MWO docstatus=1
	)
	def test_issue_subcontracting_skips_employee_warehouse_check(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		doc = self._make_eir(subcontracting="Yes")
		EmployeeIR.before_submit(doc)  # must not raise

	def test_receive_type_skips_all_issue_guards(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		doc = MagicMock()
		doc.type = "Receive"
		EmployeeIR.before_submit(doc)  # must not raise


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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error"
	)
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
			FrappeDict(
				{"item_code": "D-A", "qty": 1.008, "pcs": 168, "batch_no": "BD1"}
			),
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
			_balance_row(
				"M-A", 0.23, batch_no="BM1", manufacturing_operation="MOP-PREV"
			),
			_balance_row(
				"D-A",
				1.008,
				pcs=168,
				batch_no="BD1",
				manufacturing_operation="MOP-PREV",
			),
		],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_last_mop_index",
		side_effect=[None],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.create_mop_log"
	)
	def test_update_new_mop_wtg_clones_current_balance_rows(
		self, mock_create_mop_log, _current_doc_index, _current_balance
	):
		doc = FrappeDict(
			{
				"name": "MOP-NEXT",
				"previous_mop": "MOP-PREV",
				"gross_wt": 0,
			}
		)

		update_new_mop_wtg(doc)

		self.assertEqual(mock_create_mop_log.call_count, 2)
		first_call_row = mock_create_mop_log.call_args_list[0].kwargs.get("row")
		self.assertEqual(first_call_row.get("manufacturing_operation"), "MOP-NEXT")
