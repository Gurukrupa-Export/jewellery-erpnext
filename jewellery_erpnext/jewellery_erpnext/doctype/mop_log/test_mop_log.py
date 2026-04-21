# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	_get_mop_logs_for_employee_ir_issue,
	create_mop_log_for_employee_ir_receive,
	creste_mop_log_for_employee_ir,
	get_current_mop_balance_rows,
	resolve_employee_ir_issue_voucher_for_receive,
)


def _sample_log(**overrides):
	base = {
		"item_code": "M-TEST",
		"pcs_after_transaction": 1,
		"pcs_after_transaction_item_based": 1,
		"pcs_after_transaction_batch_based": 1,
		"qty_after_transaction": 10.0,
		"qty_after_transaction_item_based": 10.0,
		"qty_after_transaction_batch_based": 10.0,
		"serial_and_batch_bundle": None,
		"batch_no": "B1",
		"flow_index": 2,
		"voucher_type": None,
		"voucher_no": None,
	}
	base.update(overrides)
	return FrappeDict(base)


class MockEmployeeIR:
	doctype = "Employee IR"
	name = "EIR-TEST-001"


class MockRow:
	def __init__(self):
		self.manufacturing_operation = "MOP-TEST-001"
		self.name = "row-child-1"
		self.manufacturing_work_order = "MWO-TEST-001"


class TestCurrentMOPBalanceRows(FrappeTestCase):
	def test_get_current_balance_rows_keeps_latest_per_item_batch(self):
		rows = [
			_sample_log(name="LOG-NEW-D", creation="2026-04-17 10:00:00", item_code="D-A", batch_no="BD1", qty_after_transaction_batch_based=1.0),
			_sample_log(name="LOG-NEW-M", creation="2026-04-17 09:00:00", item_code="M-A", batch_no=None, qty_after_transaction_batch_based=5.0),
			_sample_log(name="LOG-OLD-D", creation="2026-04-17 08:00:00", item_code="D-A", batch_no="BD1", qty_after_transaction_batch_based=0.4),
		]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=rows,
		):
			out = get_current_mop_balance_rows("MOP-TEST-001", include_fields=["item_code", "batch_no", "qty_after_transaction_batch_based"])

		self.assertEqual(len(out), 2)
		out_by_key = {(row.item_code, row.batch_no): row for row in out}
		self.assertEqual(out_by_key[("D-A", "BD1")].qty_after_transaction_batch_based, 1.0)
		self.assertEqual(out_by_key[("M-A", None)].qty_after_transaction_batch_based, 5.0)


class TestEmployeeIRIssueMOPLogSource(FrappeTestCase):
	def test_get_source_uses_current_balance_rows(self):
		row = MockRow()
		current_balance_rows = [_sample_log(flow_index=3)]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
			return_value=current_balance_rows,
		) as mock_current_balance:
			out = _get_mop_logs_for_employee_ir_issue(row, "DIR-RECV-1")
		self.assertEqual(out, current_balance_rows)
		mock_current_balance.assert_called_once_with(
			row.manufacturing_operation,
			include_fields=[
				"item_code",
				"pcs_after_transaction",
				"pcs_after_transaction_item_based",
				"pcs_after_transaction_batch_based",
				"qty_after_transaction",
				"qty_after_transaction_item_based",
				"qty_after_transaction_batch_based",
				"serial_and_batch_bundle",
				"batch_no",
				"flow_index",
				"voucher_type",
				"voucher_no",
			],
		)

	def test_get_source_empty_when_no_current_balance_rows(self):
		row = MockRow()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
			return_value=[],
		):
			out = _get_mop_logs_for_employee_ir_issue(row, None)
		self.assertEqual(out, [])

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value="DIR-1",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log._get_mop_logs_for_employee_ir_issue",
		return_value=[],
	)
	def test_creste_throws_when_no_source(self, _mock_src, _gv, _ex, _log_err):
		doc = MockEmployeeIR()
		row = MockRow()
		with self.assertRaises(frappe.ValidationError):
			creste_mop_log_for_employee_ir(doc, row, "WH-A", "WH-B")
		_log_err.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=True,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_creste_idempotent_skips_when_logs_exist(self, mock_new_doc, _exists):
		doc = MockEmployeeIR()
		row = MockRow()
		creste_mop_log_for_employee_ir(doc, row, "WH-A", "WH-B")
		mock_new_doc.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value="DIR-RECV-1",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log._get_mop_logs_for_employee_ir_issue",
		return_value=[
			_sample_log(item_code="M-A", batch_no="B1", flow_index=1),
			_sample_log(item_code="M-F", batch_no=None, flow_index=1),
		],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_creste_clones_multi_row(self, mock_new_doc, _gs, _gv, _ex):
		doc = MockEmployeeIR()
		row = MockRow()
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log

		creste_mop_log_for_employee_ir(doc, row, "WH-DEPT", "WH-EMP")

		self.assertEqual(mock_new_doc.call_count, 2)
		self.assertEqual(mock_log.from_warehouse, "WH-DEPT")
		self.assertEqual(mock_log.to_warehouse, "WH-EMP")
		self.assertEqual(mock_log.voucher_type, "Employee IR")
		self.assertEqual(mock_log.voucher_no, doc.name)
		self.assertEqual(mock_log.flow_index, 2)
		self.assertEqual(mock_log.save.call_count, 2)


class TestResolveEmployeeIRIssueVoucher(FrappeTestCase):
	def test_resolve_uses_emp_ir_id_when_valid(self):
		doc = MagicMock()
		doc.emp_ir_id = "EMP-IR-ISSUE-01"
		row = MockRow()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
			return_value=FrappeDict({"docstatus": 1, "type": "Issue"}),
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
			return_value=True,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.sql",
		) as mock_sql:
			out = resolve_employee_ir_issue_voucher_for_receive(doc, row)
		self.assertEqual(out, "EMP-IR-ISSUE-01")
		mock_sql.assert_not_called()

	def test_resolve_falls_back_to_latest_sql_issue(self):
		doc = MagicMock()
		doc.emp_ir_id = None
		row = MockRow()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.sql",
			return_value=[("EMP-IR-ISSUE-99",)],
		):
			out = resolve_employee_ir_issue_voucher_for_receive(doc, row)
		self.assertEqual(out, "EMP-IR-ISSUE-99")

	def test_resolve_returns_none_when_no_issue(self):
		doc = MagicMock()
		doc.emp_ir_id = ""
		row = MockRow()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.sql",
			return_value=[],
		):
			out = resolve_employee_ir_issue_voucher_for_receive(doc, row)
		self.assertIsNone(out)

class MockDepartmentIR:
	doctype = "Department IR"
	name = "DIR-TEST-001"
	type = "Receive"
	receive_against = "DIR-ISSUE-001"


class TestDepartmentIRIdempotency(FrappeTestCase):
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index")
	def test_department_ir_idempotency_safe(self, mock_last_index, mock_get_all, mock_new_doc, mock_exists):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import create_mop_log_for_department_ir
		
		doc = MockDepartmentIR()
		row = MockRow()
		
		# Scenario 1: Log already exists
		mock_exists.return_value = True
		create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")
		mock_new_doc.assert_not_called()
		
		# Scenario 2: Log does not exist
		mock_exists.return_value = False
		mock_get_all.return_value = [_sample_log(flow_index=2)]
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log
		
		create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")
		self.assertEqual(mock_new_doc.call_count, 1)

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index")
	def test_department_ir_receive_lineage(self, mock_last_index, mock_get_all, mock_new_doc, mock_exists):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import create_mop_log_for_department_ir
		
		doc = MockDepartmentIR()
		row = MockRow()
		mock_exists.return_value = False
		
		# Set up get_all to verify filters
		def mock_get_all_side_effect(doctype, filters, *args, **kwargs):
			if filters.get("voucher_no") == "DIR-ISSUE-001":
				return [_sample_log(flow_index=2)]
			return []
		
		mock_get_all.side_effect = mock_get_all_side_effect
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log
		
		create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")
		
		# Assert the right voucher_no was passed to get_all
		call_args = mock_get_all.call_args_list[0]
		self.assertEqual(call_args[1]["filters"]["voucher_no"], "DIR-ISSUE-001")
		
		# It shouldn't fallback
		mock_last_index.assert_not_called()

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index")
	def test_department_ir_receive_clones_latest_issue_flow_only(
		self, mock_last_index, mock_get_all, mock_new_doc, mock_exists
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_department_ir,
		)

		doc = MockDepartmentIR()
		row = MockRow()
		mock_exists.return_value = False

		def mock_get_all_side_effect(doctype, filters, *args, **kwargs):
			if filters.get("voucher_no") == "DIR-ISSUE-001":
				return [
					_sample_log(flow_index=1, item_code="M-OLD"),
					_sample_log(flow_index=2, item_code="M-NEW"),
				]
			return []

		mock_get_all.side_effect = mock_get_all_side_effect
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log

		create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")

		self.assertEqual(mock_new_doc.call_count, 1)
		self.assertEqual(mock_log.item_code, "M-NEW")
		mock_last_index.assert_not_called()

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_site_config")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	def test_department_ir_receive_strict_blocks_tail_without_issue_logs(
		self, mock_log_error, mock_get_all, mock_exists, mock_site_cfg
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_department_ir,
		)

		mock_site_cfg.return_value = {"department_ir_receive_strict_lineage": True}
		mock_exists.return_value = False
		mock_get_all.return_value = []

		doc = MockDepartmentIR()
		row = MockRow()
		with self.assertRaises(frappe.ValidationError):
			create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")

		mock_get_all.assert_called()
		mock_log_error.assert_called()

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_site_config")
	def test_department_ir_receive_tail_logs_mixed_dir_voucher(
		self,
		mock_site_cfg,
		mock_last_index,
		mock_get_all,
		mock_new_doc,
		mock_exists,
		mock_log_error,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_department_ir,
		)

		mock_site_cfg.return_value = {}
		mock_exists.return_value = False
		mock_last_index.return_value = 3

		bad_tail = [
			_sample_log(
				flow_index=3,
				item_code="M-X",
				voucher_type="Department IR",
				voucher_no="OTHER-DIR-ISSUE",
			)
		]

		def mock_get_all_side_effect(doctype, filters, *args, **kwargs):
			if filters.get("voucher_no") == "DIR-ISSUE-001":
				return []
			if filters.get("flow_index") == 3:
				return bad_tail
			return []

		mock_get_all.side_effect = mock_get_all_side_effect
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log

		doc = MockDepartmentIR()
		row = MockRow()
		create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")

		titles = [c.kwargs.get("title") or (c.args[0] if c.args else "") for c in mock_log_error.call_args_list]
		self.assertTrue(
			any("unrelated Department IR voucher" in (t or "") for t in titles),
			msg=f"Expected mixed-voucher diagnostic log, got titles={titles}",
		)


class MockMainSlipIssueEIR:
	doctype = "Employee IR"
	name = "EIR-MS-ISSUE-001"
	is_main_slip_required = 1


class MockMainSlipReceiveEIR:
	doctype = "Employee IR"
	name = "EIR-MS-RECV-001"
	is_main_slip_required = 1
	emp_ir_id = None


class TestMainSlipEmployeeIRRelaxations(FrappeTestCase):
	"""Regressions for the is_main_slip_required gate in mop_log writers."""

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists", return_value=False)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log._get_mop_logs_for_employee_ir_issue",
		return_value=[],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	def test_issue_returns_silently_when_main_slip_required_and_empty(
		self, mock_log_error, mock_get_issue_logs, mock_get_value, mock_exists
	):
		doc = MockMainSlipIssueEIR()
		row = MockRow()
		# Should NOT raise - the Main Slip path permits zero baseline.
		creste_mop_log_for_employee_ir(doc, row, "FROM", "TO")
		titles = [c.kwargs.get("title") or (c.args[0] if c.args else "") for c in mock_log_error.call_args_list]
		self.assertTrue(
			any("Main Slip: empty starting balance" in (t or "") for t in titles),
			msg=f"Expected Main Slip-allowed log, got titles={titles}",
		)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value=None,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	def test_receive_returns_silently_when_main_slip_required_and_no_issue_voucher(
		self, mock_log_error, mock_resolve
	):
		doc = MockMainSlipReceiveEIR()
		row = MockRow()
		# Should NOT raise - zero-baseline Receive is allowed under Main Slip.
		create_mop_log_for_employee_ir_receive(doc, row, "FROM", "TO")
		titles = [c.kwargs.get("title") or (c.args[0] if c.args else "") for c in mock_log_error.call_args_list]
		self.assertTrue(
			any("Main Slip: no Issue voucher" in (t or "") for t in titles),
			msg=f"Expected Main Slip-allowed log, got titles={titles}",
		)

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	mo_creation,
	mop_log_creation,
)


class TestMOPLog(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_mop_log_creation(self):
		mwo_list = mo_creation()
		mr_list = frappe.get_all(
			"Material Request",
			filters={
				"manufacturing_order": mwo_list[0].manufacturing_order,
				"docstatus": 0,
			},
			pluck="name",
		)
		for row in mwo_list:
			if row.department == "Manufacturing Plan & Management - GEPL":
				mwo = frappe.get_doc("Manufacturing Work Order", row.name)
				mwo.submit()
				mo_man = frappe.get_last_doc(
					"Manufacturing Operation",
					filters={"manufacturing_work_order": mwo.name},
				)

				if mr_list:
					mop_log_se = mop_log_creation(mr_list[0], mo_man)
					sed = frappe.get_doc("Stock Entry Detail", mop_log_se.row_name)
					self.assertEqual(mop_log_se.voucher_no, sed.parent)
					self.assertEqual(mop_log_se.row_name, sed.name)
					self.assertEqual(mop_log_se.item_code, sed.item_code)
					self.assertEqual(mop_log_se.from_warehouse, sed.s_warehouse)
					self.assertEqual(mop_log_se.to_warehouse, sed.t_warehouse)
					self.assertEqual(mop_log_se.qty_change, sed.qty)
					self.assertEqual(
						mop_log_se.serial_and_batch_bundle, sed.serial_and_batch_bundle
					)
					self.assertEqual(mop_log_se.batch_no, sed.batch_no)
					self.assertEqual(
						mop_log_se.manufacturing_operation, sed.manufacturing_operation
					)

				dir_issue = dir_for_issue(
					"Manufacturing Plan & Management - GEPL", "Tagging - GEPL", mo_man
				)
				mo_man.reload()
				self.assertEqual("Finished", mo_man.status)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value("MOP Log", filters={"voucher_no": dir_issue.name}),
				)
				from_warehouse = frappe.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_issue.current_department,
						"warehouse_type": "Manufacturing",
					},
				)
				to_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_issue.next_department,
						"warehouse_type": "Manufacturing",
					},
					"default_in_transit_warehouse",
				)
				self.assertEqual(mop_log.voucher_no, dir_issue.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, dir_issue.department_ir_operation[0].name
				)

				mo_wax = frappe.get_last_doc("Manufacturing Operation")
				self.assertIsNotNone(mo_wax.department_issue_id)
				self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

				dir_receive = dir_for_receive(dir_issue)
				mo_wax.reload()
				self.assertIsNotNone(mo_wax.department_receive_id)
				self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value(
						"MOP Log", filters={"voucher_no": dir_receive.name}
					),
				)
				to_warehouse = frappe.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_receive.current_department,
						"warehouse_type": "Manufacturing",
					},
				)
				from_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_receive.current_department,
						"warehouse_type": "Manufacturing",
					},
					"default_in_transit_warehouse",
				)

				self.assertEqual(mop_log.voucher_no, dir_receive.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, dir_receive.department_ir_operation[0].name
				)

	def test_mop_log_validate_with_empty_item_code(self):
		mo = create_test_manufacturing_operation()

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = ""
		mop_log.qty_after_transaction = 10.0
		mop_log.pcs_after_transaction = 5
		mop_log.manufacturing_operation = mo.name

		try:
			mop_log.validate()
		except IndexError:
			self.fail("validate() raised IndexError with empty item_code")

	def test_mop_log_validate_with_diamond_prefix(self):
		mo = create_test_manufacturing_operation()

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "D-001"
		mop_log.qty_after_transaction = 10.0
		mop_log.pcs_after_transaction = 20
		mop_log.manufacturing_operation = mo.name

		mop_log.validate()

		updated_mo = frappe.get_doc("Manufacturing Operation", mo.name)
		self.assertEqual(updated_mo.diamond_wt, 10.0)
		self.assertEqual(updated_mo.diamond_wt_in_gram, 10.0 * 0.2)  # 2.0
		self.assertEqual(updated_mo.diamond_pcs, 20)

	def test_mop_log_validate_with_invalid_prefix(self):
		mo = create_test_manufacturing_operation()
		initial_net_wt = mo.net_wt

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "X-001"
		mop_log.qty_after_transaction = 20.0
		mop_log.pcs_after_transaction = 10
		mop_log.manufacturing_operation = mo.name

		mop_log.validate()

		updated_mo = frappe.get_doc("Manufacturing Operation", mo.name)
		self.assertEqual(updated_mo.net_wt, initial_net_wt)


def create_test_manufacturing_operation():
	mo = frappe.new_doc("Manufacturing Operation")
	mo.net_wt = 10.0
	mo.finding_wt = 5.0
	mo.diamond_wt_in_gram = 2.0
	mo.gemstone_wt_in_gram = 1.5
	mo.other_wt = 0.5
	mo.previous_mop = None
	mo.save()

	return mo
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EIR-ISSUE-001",
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error")
	def test_receive_tolerates_missing_keys_when_main_slip_required(
		self,
		mock_log_error,
		mock_resolve,
		mock_get_all,
		mock_current_balance,
		mock_new_doc,
	):
		# Issue snapshot: only metal.
		mock_get_all.return_value = [
			_sample_log(item_code="M-X", batch_no="MBATCH"),
		]
		# Current balance adds a diamond row that is NOT on the Issue snapshot.
		mock_current_balance.return_value = [
			_sample_log(item_code="M-X", batch_no="MBATCH"),
			_sample_log(item_code="D-1CT", batch_no="DBATCH"),
		]
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log

		doc = MockMainSlipReceiveEIR()
		row = MockRow()
		# Should NOT raise even though missing_keys is non-empty.
		create_mop_log_for_employee_ir_receive(doc, row, "FROM", "TO")
		titles = [c.kwargs.get("title") or (c.args[0] if c.args else "") for c in mock_log_error.call_args_list]
		self.assertTrue(
			any("current balance extends Issue snapshot" in (t or "") for t in titles),
			msg=f"Expected Main Slip extends-snapshot log, got titles={titles}",
		)
