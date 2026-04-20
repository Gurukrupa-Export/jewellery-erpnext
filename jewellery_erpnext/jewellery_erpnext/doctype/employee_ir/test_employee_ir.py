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

		update_new_mop_wtg(doc)

		self.assertEqual(mock_new_doc.call_count, 2)
		self.assertEqual(mock_log.flow_index, 0)
		self.assertEqual(mock_log.manufacturing_operation, "MOP-NEXT")
