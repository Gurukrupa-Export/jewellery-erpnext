# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import re
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.tests import IntegrationTestCase
from frappe.types.frappedict import _dict as FrappeDict
from frappe.utils import add_to_date, now_datetime

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	CustomSubmissionQueue,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	current_operation_guard as guard,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.test_department_ir import (
	mo_creation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	validate_employee_ir_receive_delay,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
	create_operation_for_next_op,
	get_manufacturing_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	ManufacturingOperation,
	get_material_wt,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	scan_mwo_eir,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log import (
	mop_log as mop_log_module,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_employee_ir_receive,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import mop_eod_sync


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


class TestEmployeeIRReceiveLineageGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EMP-IR-ISSUE-1",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_employee_ir_loss_map",
		return_value={},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_receive_creates_mop_log_clones_from_issue_logs(
		self, _new_doc, _get_all, _get_loss_map, _resolve_issue
	):
		doc = FrappeDict({"name": "EMP-IR-RECV-1", "emp_ir_id": "EMP-IR-ISSUE-1"})
		row = MockRow()

		mock_mop_log = MagicMock()
		_new_doc.return_value = mock_mop_log

		def get_all_side_effect(doctype, filters=None, fields=None, **kwargs):
			if doctype == "MOP Log" and filters.get("voucher_type") == "Employee IR":
				return [_balance_row("M-A", 5.0, batch_no="BM1")]
			return []

		_get_all.side_effect = get_all_side_effect

		create_mop_log_for_employee_ir_receive(doc, row, "WH-EMP", "WH-DEPT")

		_new_doc.assert_called_with("MOP Log")
		mock_mop_log.save.assert_called_once()
		self.assertEqual(mock_mop_log.item_code, "M-A")
		self.assertEqual(mock_mop_log.batch_no, "BM1")
		self.assertEqual(mock_mop_log.voucher_type, "Employee IR")
		self.assertEqual(mock_mop_log.voucher_no, "EMP-IR-RECV-1")


class TestEmployeeIRReceiveDelayGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _mock_row(mop="MOP-TEST-001", name="row-1", idx=1):
		row = MockRow()
		row.manufacturing_operation = mop
		row.name = name
		row.idx = idx
		return row

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value=None,
	)
	def test_no_resolvable_issue_skips_check(self, _resolve):
		doc = FrappeDict({"employee_ir_operations": [self._mock_row()]})
		validate_employee_ir_receive_delay(doc)  # must not raise

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EMP-IR-ISSUE-1",
	)
	def test_zero_delay_allows_immediate_receive(self, _resolve, _get_value):
		def side_effect(doctype, name=None, fields=None, as_dict=False):
			if doctype == "Employee IR":
				return FrappeDict(
					{
						"operation": "Casting",
						"issue_submitted_on": now_datetime(),
						"date_time": now_datetime(),
					}
				)
			if doctype == "Department Operation":
				return 0
			return None

		_get_value.side_effect = side_effect
		doc = FrappeDict({"employee_ir_operations": [self._mock_row()]})
		validate_employee_ir_receive_delay(doc)  # must not raise

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EMP-IR-ISSUE-1",
	)
	def test_delay_not_elapsed_blocks(self, _resolve, _get_value):
		recent = add_to_date(now_datetime(), minutes=-1)

		def side_effect(doctype, name=None, fields=None, as_dict=False):
			if doctype == "Employee IR":
				return FrappeDict(
					{
						"operation": "Casting",
						"issue_submitted_on": recent,
						"date_time": recent,
					}
				)
			if doctype == "Department Operation":
				return 5
			return None

		_get_value.side_effect = side_effect
		doc = FrappeDict({"employee_ir_operations": [self._mock_row()]})

		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_employee_ir_receive_delay(doc)
		self.assertIn("cannot be submitted at this stage", str(ctx.exception))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EMP-IR-ISSUE-1",
	)
	def test_delay_elapsed_allows_receive(self, _resolve, _get_value):
		old = add_to_date(now_datetime(), minutes=-10)

		def side_effect(doctype, name=None, fields=None, as_dict=False):
			if doctype == "Employee IR":
				return FrappeDict(
					{
						"operation": "Casting",
						"issue_submitted_on": old,
						"date_time": old,
					}
				)
			if doctype == "Department Operation":
				return 5
			return None

		_get_value.side_effect = side_effect
		doc = FrappeDict({"employee_ir_operations": [self._mock_row()]})
		validate_employee_ir_receive_delay(doc)  # must not raise

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EMP-IR-ISSUE-1",
	)
	def test_legacy_issue_falls_back_to_date_time(self, _resolve, _get_value):
		recent = add_to_date(now_datetime(), minutes=-1)

		def side_effect(doctype, name=None, fields=None, as_dict=False):
			if doctype == "Employee IR":
				return FrappeDict(
					{
						"operation": "Casting",
						"issue_submitted_on": None,
						"date_time": recent,
					}
				)
			if doctype == "Department Operation":
				return 5
			return None

		_get_value.side_effect = side_effect
		doc = FrappeDict({"employee_ir_operations": [self._mock_row()]})

		with self.assertRaises(frappe.ValidationError):
			validate_employee_ir_receive_delay(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive"
	)
	def test_multiple_issues_still_block_until_delays_elapse(
		self, _resolve, _get_value
	):
		row_a = self._mock_row(mop="MOP-A", name="row-a", idx=1)
		row_b = self._mock_row(mop="MOP-B", name="row-b", idx=2)

		def resolve_side_effect(doc, row):
			return (
				"EMP-IR-ISSUE-A"
				if row.manufacturing_operation == "MOP-A"
				else "EMP-IR-ISSUE-B"
			)

		_resolve.side_effect = resolve_side_effect

		near_deadline = add_to_date(
			now_datetime(), minutes=-4
		)  # ~1 min remaining of a 5 min delay
		far_deadline = add_to_date(
			now_datetime(), minutes=-1
		)  # ~9 min remaining of a 10 min delay

		def get_value_side_effect(doctype, name=None, fields=None, as_dict=False):
			if doctype == "Employee IR":
				if name == "EMP-IR-ISSUE-A":
					return FrappeDict(
						{
							"operation": "Casting",
							"issue_submitted_on": near_deadline,
							"date_time": near_deadline,
						}
					)
				return FrappeDict(
					{
						"operation": "Polishing",
						"issue_submitted_on": far_deadline,
						"date_time": far_deadline,
					}
				)
			if doctype == "Department Operation":
				return 5 if name == "Casting" else 10
			return None

		_get_value.side_effect = get_value_side_effect
		doc = FrappeDict({"employee_ir_operations": [row_a, row_b]})

		# The throw message is intentionally generic (no row / operation / issue / minutes),
		# so which row is worst-case is not observable here — only that the doc is blocked.
		with self.assertRaises(frappe.ValidationError):
			validate_employee_ir_receive_delay(doc)


class TestManufacturingOperationBalance(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
				"is_finding": 0,
				"loss_wt": 0,
				"employee_loss_wt": 0,
			}
		)

		out = get_material_wt(doc)

		self.assertEqual(out["net_wt"], 0.23)
		self.assertEqual(out["diamond_wt"], 1.008)
		self.assertEqual(out["diamond_pcs"], 168)
		# 1.008 ct -> flt(0.2016, 3) = 0.202. get_material_wt used to leave the gram
		# twin UNROUNDED (0.2016) and so disagreed with the MOP Log recompute about
		# the same ledger; both now derive it through carat_to_gram.
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.202, places=3)
		self.assertAlmostEqual(out["gross_wt"], 0.432, places=3)


# create_test_data's subcontracted operation. Employee IR.department is fetch_from
# operation.department, so an Issue on this operation runs in the operation's own department
# whatever the test sets -- in the seed that is "Sub Contracting - T", not Waxing. Production keeps
# its subcontracted operations in the department the work order is in, and the current-operation
# guard issues only a work order whose current operation was received in the Issue's department,
# so these tests move the work order into that department first.
SUBCONTRACTED_OPERATION = (
	"Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC"
)


class TestEmployeeIR(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

	def _received_for_subcontracting(self):
		"""A fresh work order's current operation, received by a Department IR into the
		subcontracted operation's department, and that department (see SUBCONTRACTED_OPERATION)."""
		department = frappe.db.get_value(
			"Department Operation", SUBCONTRACTED_OPERATION, "department"
		)
		create_pmo(self)
		mo = mo_creation()
		dir_issue = dir_for_issue("Manufacturing Plan & Management - T", department, mo)
		mo.reload()
		mo_sc = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_sc.reload()
		return mo_sc, department

	def test_employee_ir_scan(self):
		frappe.db.set_value(
			"Department Operation", "Wax Pull Out", "employee_ir_receive_delay", 0
		)
		create_pmo(self)
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_receive = dir_for_receive(dir_issue)
		mo_wax.reload()
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.department = "Waxing - T"
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "HR-EMP-00001"
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
		eir_receive.department = "Waxing - T"
		eir_receive.type = "Receive"
		eir_receive.operation = "Wax Pull out"
		eir_receive.employee = "HR-EMP-00001"
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

	def test_employee_ir_receive_blocked_until_delay_elapses(self):
		create_pmo(self)
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		frappe.db.set_value(
			"Department Operation", "Wax Pull Out", "employee_ir_receive_delay", 5
		)

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.type = "Issue"
		eir_issue.department = "Waxing - T"
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "HR-EMP-00001"
		eir_issue.scan_mwo = mo_wax.manufacturing_work_order
		scan_mwo_eir(eir_issue)
		eir_issue.save()
		eir_issue.submit()
		self.assertIsNotNone(eir_issue.issue_submitted_on)

		eir_receive = frappe.new_doc("Employee IR")
		eir_receive.department = "Waxing - T"
		eir_receive.type = "Receive"
		eir_receive.operation = "Wax Pull Out"
		eir_receive.employee = "HR-EMP-00001"
		eir_receive.scan_mwo = mo_wax.manufacturing_work_order
		scan_mwo_eir(eir_receive)
		eir_receive.save()

		with self.assertRaises(frappe.ValidationError):
			eir_receive.submit()

		# Backdate the Issue's submission timestamp to simulate the delay elapsing,
		# rather than sleeping the test for 5 real minutes.
		frappe.db.set_value(
			"Employee IR",
			eir_issue.name,
			"issue_submitted_on",
			add_to_date(now_datetime(), minutes=-10),
			update_modified=False,
		)
		eir_receive.reload()
		eir_receive.submit()
		self.assertEqual(eir_receive.docstatus, 1)

	def test_department_ir_by_manufacturing_operation(self):
		frappe.db.set_value(
			"Department Operation", "Wax Pull Out", "employee_ir_receive_delay", 0
		)
		create_pmo(self)
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_receive = dir_for_receive(dir_issue)
		mo_wax.reload()
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.department = mo_wax.department
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "HR-EMP-00001"
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
		eir_receive.department = "Waxing - T"
		eir_receive.type = "Receive"
		eir_receive.operation = "Wax Pull out"
		eir_receive.employee = "HR-EMP-00001"
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
		create_pmo(self)
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

		self.assertIsNone(new_mop.department_issue_id)
		self.assertIsNone(new_mop.department_receive_id)
		self.assertFalse(new_mop.department_ir_status)
		self.assertIsNone(new_mop.operation)

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
		# Its own work order: mo_creation submits the newest PMO's draft producing work order.
		create_pmo(self)
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
		# Its own work order: reusing the previous test's one would issue an operation that has
		# already moved on, which the current-operation guard refuses.
		mo_sc, department = self._received_for_subcontracting()

		eir = frappe.new_doc("Employee IR")
		eir.company = "Test_Company"
		eir.type = "Issue"
		eir.department = department
		eir.operation = SUBCONTRACTED_OPERATION
		eir.employee = "HR-EMP-00002"
		eir.subcontracting = "Yes"
		eir.subcontractor = "Test_Supplier"
		eir.scan_mwo = mo_sc.manufacturing_work_order
		scan_mwo_eir(eir)
		if not eir.employee_ir_operations[0].rpt_wt_issue:
			eir.employee_ir_operations[0].rpt_wt_issue = 0
		eir.save()
		eir.submit()
		mo_sc.reload()

		self.assertEqual(
			mo_sc.for_subcontracting,
			1,
			"MOP must have for_subcontracting=1 after Issue with subcontracting=Yes.",
		)
		self.assertEqual(
			mo_sc.subcontractor,
			"Test_Supplier",
			"MOP must carry the subcontractor name after Issue.",
		)

	def test_on_submit_issue_new_sets_subcontracting_values(self):
		mo_sc, department = self._received_for_subcontracting()

		eir = frappe.new_doc("Employee IR")
		eir.type = "Issue"
		eir.department = department
		eir.company = "Test_Company"
		eir.operation = SUBCONTRACTED_OPERATION
		eir.employee = "HR-EMP-00002"
		eir.subcontracting = "Yes"
		eir.subcontractor = "Test_Supplier"
		eir.manufacturer = "Shubh"
		eir = get_manufacturing_operations(mo_sc.name, eir)

		if not eir.employee_ir_operations[0].rpt_wt_issue:
			eir.employee_ir_operations[0].rpt_wt_issue = 0

		eir.save()
		eir.submit()

		mo_sc.reload()
		self.assertEqual(
			mo_sc.for_subcontracting,
			1,
			"MOP should have for_subcontracting=1 after subcontracting issue",
		)
		self.assertEqual(
			mo_sc.subcontractor,
			"Test_Supplier",
			"MOP should have subcontractor assigned",
		)

	def test_get_manufacturing_operations_with_serialized_target_doc(self):
		create_pmo(self)
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
		create_pmo(self)
		mo = mo_creation()
		mo.save()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		eir_issue = frappe.new_doc("Employee IR")
		eir_issue.company = "Test_Company"
		eir_issue.department = "Waxing - T"
		eir_issue.operation = "Wax Pull Out"
		eir_issue.employee = "HR-EMP-00002"
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

		# All three balance tiers must agree for a single row. The family tier
		# (qty_after_transaction) is by construction the SUM of that family's batch
		# tiers -- get_mop_opening_balances derives all three from
		# qty_after_transaction_batch_based, so on an operation whose only row is
		# this one they are necessarily equal. Carrying family=3 against batch=1
		# claimed 2g of metal with no ledger row behind it, and only survived
		# because MOPLog.validate used to stamp the header from the family tier.
		# It now derives the header from the batch tier, so gross_wt would read 1,
		# equal to received_gross_wt below, and book_metal_loss' `gwt != r_gwt`
		# gate would skip -- leaving employee_loss_details empty.
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "M-G-22KT-91.6-Y"
		mop_log.pcs_after_transaction = 3
		mop_log.qty_after_transaction = 3
		mop_log.pcs_after_transaction_item_based = 3
		mop_log.pcs_after_transaction_batch_based = 3
		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = "Employee IR"
		mop_log.voucher_no = eir_issue.name
		mop_log.row_name = eir_issue.employee_ir_operations[0].name

		mop_log.qty_after_transaction_item_based = 3
		mop_log.qty_after_transaction_batch_based = 3
		mop_log.manufacturing_operation = eir_issue.employee_ir_operations[
			0
		].manufacturing_operation
		mop_log.manufacturing_work_order = eir_issue.employee_ir_operations[
			0
		].manufacturing_work_order
		mop_log.batch_no = ""
		mop_log.save()

		eir = frappe.new_doc("Employee IR")
		eir.company = "Test_Company"
		eir.department = mo_wax.department
		eir.type = "Receive"
		eir.operation = "Wax Pull out"
		eir.employee = "HR-EMP-00002"
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


class TestDuplicateWorkOrderGuard(IntegrationTestCase):
	"""``validate_duplication_and_gr_wt`` must reject the same work order twice on one IR.

	The existing guards key on ``manufacturing_operation``. ``create_operation_for_next_op`` mints
	a NEW operation per cycle, so a re-scanned work order arrives carrying a different MOP and
	sails past them. Only the scan field guarded this, client-side — the Get Operations dialog,
	Load Full Casting Tree, grid bulk-edit and the REST API had nothing.
	"""

	MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils"

	@classmethod
	def setUpClass(cls):
		# Same as every other class here: skip the ERPNext master bootstrap, which this pure-logic
		# suite does not need and which fails on this bench (_Test Holiday List: custom_company).
		pass

	WEIGHTS = {
		"gross_wt": 1.0,
		"net_wt": 1.0,
		"finding_wt": 0.0,
		"diamond_wt": 0.0,
		"gemstone_wt": 0.0,
		"diamond_pcs": 0,
		"gemstone_pcs": 0,
	}

	def _doc(self, rows):
		return FrappeDict(
			{
				"name": "EIR-TEST",
				"type": "Receive",
				"operation": "Casting - TEST",
				"is_raw_material": 1,
				"main_slip": None,
				"employee_ir_operations": [FrappeDict(r) for r in rows],
			}
		)

	def _run(self, rows):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
			validation_utils as vu,
		)

		def _gv(doctype, *a, **kw):
			if doctype == "Department Operation":
				return 0
			return FrappeDict(dict(self.WEIGHTS))

		qb = MagicMock()
		qb.from_.return_value.left_join.return_value.on.return_value.select.return_value.where.return_value.run.return_value = []
		with (
			patch.object(vu.frappe.db, "get_single_value", return_value=3),
			patch.object(vu.frappe.db, "get_value", side_effect=_gv),
			patch.object(vu.frappe, "qb", qb),
			patch.object(vu, "get_loss_details", return_value={}),
		):
			return vu.validate_duplication_and_gr_wt(self._doc(rows))

	def test_same_work_order_twice_throws_even_with_different_operations(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._run(
				[
					{
						"manufacturing_work_order": "MWO-1",
						"manufacturing_operation": "MOP-1",
					},
					{
						"manufacturing_work_order": "MWO-1",
						"manufacturing_operation": "MOP-2",
					},
				]
			)
		self.assertIn("MWO-1", str(cm.exception))
		self.assertIn("already scanned", str(cm.exception))

	def test_distinct_work_orders_pass(self):
		self._run(
			[
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				},
				{
					"manufacturing_work_order": "MWO-2",
					"manufacturing_operation": "MOP-2",
				},
			]
		)

	def test_blank_work_orders_are_not_treated_as_duplicates(self):
		"""``manufacturing_work_order`` is fetch_if_empty, so rows can legitimately arrive blank."""
		self._run(
			[
				{"manufacturing_work_order": None, "manufacturing_operation": "MOP-1"},
				{"manufacturing_work_order": "", "manufacturing_operation": "MOP-2"},
			]
		)

	def test_the_operation_guard_still_fires(self):
		"""The pre-existing MOP-keyed check must keep working alongside the new one."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._run(
				[
					{
						"manufacturing_work_order": "MWO-1",
						"manufacturing_operation": "MOP-1",
					},
					{
						"manufacturing_work_order": "MWO-2",
						"manufacturing_operation": "MOP-1",
					},
				]
			)
		self.assertIn("appeared multiple times", str(cm.exception))


# =============================================================================================
# Work-order current-operation guard (doc_events/current_operation_guard.py)
#
# DB-free: every read the guard makes is either patched or fed pre-built "locked rows", so these
# classes need no CI seed and run through `run-tests --doctype "Employee IR"`. Department IR
# rules and wiring live in test_department_ir.py, the lock helpers in tests/test_lock_order.py.
# =============================================================================================

_EIR_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"
_EOD_LOCK_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock"
_RECON_WINDOW_MODULE = "jewellery_erpnext.jewellery_erpnext.stock_recon_window"
_MAIN_SLIP_INJECT_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject"


class _Stop(Exception):
	"""Raised by a patched step to end a controller method at a known point."""


def _warm_frappe_caches():
	"""Load what frappe._ / frappe.format / flt / now() read lazily on first use.

	The tests below patch frappe.db; a first-use read landing on the fake would change what the
	code under test sees (a mocked rounding lookup makes flt return 0, for one).
	"""
	frappe._("Work Order Current Operation")
	frappe.get_system_settings("float_precision")
	frappe.format(now_datetime(), {"fieldtype": "Datetime"})
	frappe.utils.flt(1.23456, 3)
	frappe.utils.now()


def _rec(calls, label, result=None, raises=None):
	"""Side effect that records ``label`` in ``calls``, then returns ``result`` or raises."""

	def side_effect(*args, **kwargs):
		calls.append(label)
		if raises is not None:
			raise raises
		return result

	return side_effect


class _GuardDoc(FrappeDict):
	"""The slice of a Document the guard and the controllers touch.

	Keys double as attributes, so a controller method called unbound on it
	(``EmployeeIR.on_submit(doc)``) finds plain values and stand-in "methods" alike.
	"""

	def is_new(self):
		return bool(self.get("_is_new"))

	def get_doc_before_save(self):
		return self.get("_before")


def _eir_doc(
	kind="Issue",
	rows=(("MOP-1", "MWO-1"),),
	*,
	name="EIR-T-0001",
	docstatus=0,
	new=False,
	before=None,
	**header,
):
	doc = _GuardDoc(
		doctype="Employee IR",
		name=name,
		type=kind,
		docstatus=docstatus,
		company="Co-T",
		department="Dept-T",
		operation="Op-T",
		employee="EMP-T",
		subcontracting="No",
		subcontractor=None,
		employee_ir_operations=[
			FrappeDict(
				idx=idx,
				manufacturing_operation=mop,
				manufacturing_work_order=mwo,
				gross_wt=1.0,
				received_gross_wt=1.0,
			)
			for idx, (mop, mwo) in enumerate(rows, start=1)
		],
		manually_book_loss_details=[],
		employee_loss_details=[],
		flags=FrappeDict(),
		_is_new=new,
		_before=before,
	)
	doc.update(header)
	return doc


def _op(name, mwo="MWO-1", **fields):
	"""A Manufacturing Operation row as lock_manufacturing_operations returns it."""
	row = FrappeDict(
		name=name,
		manufacturing_work_order=mwo,
		company="Co-T",
		department="Dept-T",
		status="Not Started",
		department_ir_status="Received",
		operation=None,
		employee=None,
		subcontractor=None,
		for_subcontracting=0,
		department_issue_id=None,
		department_receive_id=None,
		employee_ir=None,
		previous_mop=None,
	)
	row.update(fields)
	return row


def _wo(name="MWO-1", pointer="MOP-1", docstatus=1):
	"""A Manufacturing Work Order row as lock_work_orders returns it."""
	return FrappeDict(
		name=name,
		docstatus=docstatus,
		company="Co-T",
		manufacturing_operation=pointer,
		department="Dept-T",
	)


def _family_of(*ops):
	family = {}
	for op in ops:
		family.setdefault(op.manufacturing_work_order, []).append(op)
	return family


def _evaluate(doc, ops, wos, family=None, phase="save"):
	"""Run the pure rule evaluation over pre-built locked rows."""
	return guard._row_problems(
		doc,
		guard.profile(doc),
		phase,
		{op.name: op for op in ops},
		{wo.name: wo for wo in wos},
		_family_of(*ops) if family is None else family,
	)


def _one_problem(test, problems, exc):
	test.assertEqual(len(problems), 1, problems)
	test.assertIs(problems[0][0], exc, problems)
	return problems[0][1]


def _check_args(check):
	"""``(phase, locking)`` of the single ``_check`` call, however it was spelled."""
	check.assert_called_once()
	args, kwargs = check.call_args
	locking = (
		kwargs["locking"]
		if "locking" in kwargs
		else (args[2] if len(args) > 2 else False)
	)
	return args[1], locking


def _draft(name="EIR-D-0001", mwo="MWO-1", owner="maker@example.com"):
	return dict(draft=name, mwo=mwo, idx=1, owner=owner, creation=now_datetime())


class TestCurrentOperationGuardDocHelpers(IntegrationTestCase):
	"""profile / refs / refs_changed: what decides whether a save re-runs the guard."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def test_reverted_operations_are_history(self):
		"""One rule for every reader: open status, and not marked Revert either way."""
		for status, transit, reverted, open_ in (
			("Not Started", "Revert", True, False),
			("Revert", None, True, False),
			("Not Started", "Received", False, True),
			("WIP", None, False, True),
			("Finished", None, False, False),
		):
			with self.subTest(status=status, transit=transit):
				self.assertIs(guard.is_reverted(status, transit), reverted)
				self.assertIs(guard.is_open_operation(status, transit), open_)

	def test_profile_per_doctype_and_type(self):
		self.assertEqual(guard.profile(_eir_doc("Issue")), guard.EIR_ISSUE)
		self.assertEqual(guard.profile(_eir_doc("Receive")), guard.EIR_RECEIVE)
		self.assertEqual(
			guard.profile(_GuardDoc(doctype="Department IR", type="Issue")),
			guard.DIR_ISSUE,
		)
		self.assertEqual(
			guard.profile(_GuardDoc(doctype="Department IR", type="Receive")),
			guard.DIR_RECEIVE,
		)
		self.assertIsNone(guard.profile(_GuardDoc(doctype="Stock Entry", type="Issue")))

	def test_refs_strip_names_and_keep_row_order(self):
		doc = _eir_doc(rows=((" MOP-2 ", "MWO-2"), ("MOP-1", None)))
		self.assertEqual(guard.refs(doc), [(1, "MOP-2", "MWO-2"), (2, "MOP-1", "")])

	def test_new_document_always_counts_as_changed(self):
		self.assertTrue(guard.refs_changed(_eir_doc(new=True, before=_eir_doc())))

	def test_document_without_a_saved_version_counts_as_changed(self):
		self.assertTrue(guard.refs_changed(_eir_doc(before=None)))

	def test_unchanged_resave_is_not_rechecked(self):
		"""Row order and fields outside the predicate do not matter."""
		before = _eir_doc(rows=(("MOP-1", "MWO-1"), ("MOP-2", "MWO-2")))
		doc = _eir_doc(
			rows=(("MOP-2", "MWO-2"), ("MOP-1", "MWO-1")),
			before=before,
			remarks="edited",
		)
		self.assertFalse(guard.refs_changed(doc))

	def test_blank_and_missing_header_values_are_equal(self):
		doc = _eir_doc(subcontractor="", before=_eir_doc(subcontractor=None))
		self.assertFalse(guard.refs_changed(doc))

	def test_each_employee_ir_predicate_field_change_is_rechecked(self):
		changes = {
			"type": "Receive",
			"company": "Co-X",
			"department": "Dept-X",
			"operation": "Op-X",
			"employee": "EMP-X",
			"subcontracting": "Yes",
			"subcontractor": "SUP-X",
		}
		self.assertEqual(
			set(changes), set(guard.PREDICATE_HEADER_FIELDS["Employee IR"])
		)
		for field, value in changes.items():
			with self.subTest(field=field):
				doc = _eir_doc(before=_eir_doc(), **{field: value})
				self.assertTrue(guard.refs_changed(doc))

	def test_each_department_ir_predicate_field_change_is_rechecked(self):
		base = dict(
			doctype="Department IR",
			type="Issue",
			company="Co-T",
			current_department="Dept-A",
			next_department="Dept-B",
			receive_against=None,
			department_ir_operation=[
				FrappeDict(
					idx=1,
					manufacturing_operation="MOP-1",
					manufacturing_work_order="MWO-1",
				)
			],
		)
		changes = {
			"type": "Receive",
			"company": "Co-X",
			"current_department": "Dept-X",
			"next_department": "Dept-Y",
			"receive_against": "DIR-X",
		}
		self.assertEqual(
			set(changes), set(guard.PREDICATE_HEADER_FIELDS["Department IR"])
		)
		for field, value in changes.items():
			with self.subTest(field=field):
				doc = _GuardDoc(base, _before=_GuardDoc(base))
				doc[field] = value
				self.assertTrue(guard.refs_changed(doc))

	def test_row_moved_to_another_work_order_is_rechecked(self):
		before = _eir_doc(rows=(("MOP-1", "MWO-1"),))
		self.assertTrue(
			guard.refs_changed(_eir_doc(rows=(("MOP-1", "MWO-2"),), before=before))
		)

	def test_row_operation_swapped_is_rechecked(self):
		before = _eir_doc(rows=(("MOP-1", "MWO-1"),))
		self.assertTrue(
			guard.refs_changed(_eir_doc(rows=(("MOP-9", "MWO-1"),), before=before))
		)

	def test_row_added_or_removed_is_rechecked(self):
		before = _eir_doc(rows=(("MOP-1", "MWO-1"),))
		added = _eir_doc(rows=(("MOP-1", "MWO-1"), ("MOP-2", "MWO-2")), before=before)
		self.assertTrue(guard.refs_changed(added))
		self.assertTrue(guard.refs_changed(_eir_doc(rows=(), before=before)))

	def test_old_work_orders_come_from_the_saved_version(self):
		before = _eir_doc(rows=(("MOP-1", "MWO-OLD"), ("MOP-2", "")))
		self.assertEqual(guard._old_ref_mwos(_eir_doc(before=before)), {"MWO-OLD"})
		self.assertEqual(guard._old_ref_mwos(_eir_doc(new=True, before=before)), set())
		self.assertEqual(guard._old_ref_mwos(_eir_doc(before=None)), set())


class TestCurrentOperationGuardRowRules(IntegrationTestCase):
	"""Rules every row must pass whatever the transaction (shown on an Employee Issue)."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def test_current_operation_passes(self):
		self.assertEqual(_evaluate(_eir_doc(), [_op("MOP-1")], [_wo()]), [])

	def test_blank_operation_row_is_skipped_on_save_but_required_on_submit(self):
		doc = _eir_doc(rows=(("", "MWO-1"),))
		self.assertEqual(_evaluate(doc, [], [_wo()], phase="save"), [])
		message = _one_problem(
			self,
			_evaluate(doc, [], [_wo()], phase="submit"),
			guard.CurrentOperationError,
		)
		self.assertIn("Manufacturing Operation is required", message)

	def test_same_operation_twice_is_refused(self):
		doc = _eir_doc(rows=(("MOP-1", "MWO-1"), ("MOP-1", "MWO-1")))
		message = _one_problem(
			self, _evaluate(doc, [_op("MOP-1")], [_wo()]), guard.CurrentOperationError
		)
		self.assertIn("is already on row 1", message)

	def test_unknown_operation_is_refused(self):
		message = _one_problem(
			self, _evaluate(_eir_doc(), [], [_wo()]), guard.CurrentOperationError
		)
		self.assertIn("does not exist", message)

	def test_row_work_order_must_own_the_operation(self):
		doc = _eir_doc(rows=(("MOP-1", "MWO-2"),))
		problems = _evaluate(
			doc, [_op("MOP-1", mwo="MWO-1")], [_wo(), _wo("MWO-2", "MOP-1")]
		)
		message = _one_problem(self, problems, guard.CurrentOperationError)
		self.assertIn(
			"belongs to work order <strong>MWO-1</strong>, not <strong>MWO-2", message
		)

	def test_blank_row_work_order_uses_the_operations_own(self):
		"""manufacturing_work_order is fetch_if_empty, so a row can arrive without it."""
		doc = _eir_doc(rows=(("MOP-1", ""),))
		self.assertEqual(_evaluate(doc, [_op("MOP-1")], [_wo()]), [])
		stale = _evaluate(doc, [_op("MOP-1")], [_wo(pointer="MOP-2")], family={})
		self.assertIn("MWO-1", _one_problem(self, stale, guard.StaleOperationError))

	def test_same_work_order_twice_is_refused_even_with_different_operations(self):
		"""Family B of the audit: two rows of one transfer minted twin operations."""
		doc = _eir_doc(rows=(("MOP-1", "MWO-1"), ("MOP-2", "MWO-1")))
		problems = _evaluate(doc, [_op("MOP-1"), _op("MOP-2")], [_wo()])
		message = _one_problem(self, problems, guard.CurrentOperationError)
		self.assertIn(
			"Row 2: work order <strong>MWO-1</strong> is already on row 1", message
		)

	def test_work_order_that_is_not_submitted_is_refused(self):
		for label, wos in (
			("draft", [_wo(docstatus=0)]),
			("cancelled", [_wo(docstatus=2)]),
			("missing", []),
		):
			with self.subTest(work_order=label):
				problems = _evaluate(_eir_doc(), [_op("MOP-1")], wos)
				message = _one_problem(self, problems, guard.CurrentOperationError)
				self.assertIn("is not submitted", message)

	def test_work_order_without_a_current_operation_is_ambiguous(self):
		problems = _evaluate(_eir_doc(), [_op("MOP-1")], [_wo(pointer=None)])
		message = _one_problem(self, problems, guard.AmbiguousOperationError)
		self.assertIn("has no current operation", message)

	def test_stale_operation_is_refused_and_names_the_current_one(self):
		"""EMP-IR-Labh-2026-43405: the draft named the Model Making operation four days after
		the work order had moved on to Final Polish."""
		old = _op("MOP-OLD", status="Finished", department="Model Making - T")
		new = _op(
			"MOP-NEW",
			status="WIP",
			department="Final Polish - T",
			previous_mop="MOP-OLD",
		)
		doc = _eir_doc(rows=(("MOP-OLD", "MWO-1"),), department="Model Making - T")
		problems = _evaluate(
			doc, [old], [_wo(pointer="MOP-NEW")], family=_family_of(old, new)
		)
		message = _one_problem(self, problems, guard.StaleOperationError)
		for token in (
			"MOP-OLD",
			"MWO-1",
			"MOP-NEW",
			"Final Polish - T",
			"(WIP)",
			"Discard",
		):
			self.assertIn(token, message)

	def test_stale_message_falls_back_to_the_pointer_name(self):
		problems = _evaluate(
			_eir_doc(), [_op("MOP-1")], [_wo(pointer="MOP-9")], family={}
		)
		self.assertIn("MOP-9", _one_problem(self, problems, guard.StaleOperationError))

	def test_current_operation_that_already_has_a_successor_is_ambiguous(self):
		family = _family_of(_op("MOP-1"), _op("MOP-2", previous_mop="MOP-1"))
		problems = _evaluate(_eir_doc(), [_op("MOP-1")], [_wo()], family=family)
		message = _one_problem(self, problems, guard.AmbiguousOperationError)
		self.assertIn("already has a later operation", message)

	def test_a_reverted_successor_is_history_not_a_later_operation(self):
		"""kggk_uat keeps the operation a cancelled Department Issue / Employee Receive minted,
		marked Revert, with previous_mop naming the restored current operation. Issue, Receive and
		the next transfer of that current operation must all go through."""
		for marks in (
			{"department_ir_status": "Revert", "status": "Not Started"},
			{"status": "Revert"},
		):
			reverted = _op("MOP-R", previous_mop="MOP-1", **marks)
			with self.subTest(marks=marks, kind="Issue"):
				ops = [_op("MOP-1")]
				self.assertEqual(
					_evaluate(
						_eir_doc(), ops, [_wo()], family=_family_of(*ops, reverted)
					),
					[],
				)
			with self.subTest(marks=marks, kind="Receive"):
				held = _op("MOP-1", status="WIP", operation="Op-T", employee="EMP-T")
				self.assertEqual(
					_evaluate(
						_eir_doc("Receive"),
						[held],
						[_wo()],
						family=_family_of(held, reverted),
					),
					[],
				)

	def test_a_live_successor_beside_a_reverted_one_is_still_ambiguous(self):
		family = _family_of(
			_op("MOP-1"),
			_op("MOP-R", previous_mop="MOP-1", department_ir_status="Revert"),
			_op("MOP-2", previous_mop="MOP-1"),
		)
		problems = _evaluate(_eir_doc(), [_op("MOP-1")], [_wo()], family=family)
		_one_problem(self, problems, guard.AmbiguousOperationError)

	def test_company_mismatch_is_refused(self):
		problems = _evaluate(_eir_doc(), [_op("MOP-1", company="Co-X")], [_wo()])
		message = _one_problem(self, problems, guard.CurrentOperationError)
		self.assertIn("belongs to company <strong>Co-X</strong>", message)

	def test_blank_company_on_either_side_skips_the_company_match(self):
		self.assertEqual(
			_evaluate(_eir_doc(company=None), [_op("MOP-1", company="Co-X")], [_wo()]),
			[],
		)
		self.assertEqual(
			_evaluate(_eir_doc(), [_op("MOP-1", company=None)], [_wo()]), []
		)

	def test_one_stale_row_is_reported_alone_among_valid_rows(self):
		doc = _eir_doc(
			rows=(("MOP-1", "MWO-1"), ("MOP-OLD", "MWO-2"), ("MOP-3", "MWO-3"))
		)
		ops = [
			_op("MOP-1"),
			_op("MOP-OLD", mwo="MWO-2", status="Finished"),
			_op("MOP-3", mwo="MWO-3"),
		]
		wos = [_wo(), _wo("MWO-2", "MOP-NEW"), _wo("MWO-3", "MOP-3")]
		message = _one_problem(
			self, _evaluate(doc, ops, wos), guard.StaleOperationError
		)
		self.assertTrue(message.startswith("Row 2:"), message)
		self.assertNotIn("MWO-1", message)
		self.assertNotIn("MWO-3", message)


class TestCurrentOperationGuardEmployeeIssueRule(IntegrationTestCase):
	"""Employee Issue: the current operation must be Not Started, unassigned and received in the
	Issue's department -- the employee_ir.js scan / Get Operations filters, server-side."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _issue(self, doc=None, **op_fields):
		return _evaluate(doc or _eir_doc("Issue"), [_op("MOP-1", **op_fields)], [_wo()])

	def _refused(self, doc=None, **op_fields):
		message = _one_problem(
			self, self._issue(doc, **op_fields), guard.CurrentOperationError
		)
		self.assertIn("an Employee Issue needs", message)
		return message

	def test_not_started_unassigned_operation_received_in_the_department_passes(self):
		self.assertEqual(self._issue(), [])

	def test_operation_that_never_changed_department_passes(self):
		self.assertEqual(self._issue(department_ir_status=None), [])

	def test_every_status_but_not_started_is_refused(self):
		for status in (
			"WIP",
			"On Hold",
			"QC Pending",
			"QC Completed",
			"Finished",
			"Revert",
			None,
		):
			with self.subTest(status=status):
				self._refused(status=status)

	def test_operation_in_another_department_is_refused(self):
		message = self._refused(department="Dept-X")
		self.assertIn("Dept-X", message)
		self.assertIn("received in <strong>Dept-T</strong>", message)

	def test_department_match_ignores_case_and_padding(self):
		self.assertEqual(self._issue(department="  dept-t "), [])

	def test_operation_in_transit_or_reverted_is_refused(self):
		for transit in ("In-Transit", "Revert"):
			with self.subTest(transit=transit):
				self._refused(department_ir_status=transit)

	def test_operation_already_given_an_operation_is_refused(self):
		self._refused(operation="Op-Previous")

	def test_in_house_issue_refuses_an_operation_held_by_a_subcontractor(self):
		self._refused(subcontractor="SUP-1")

	def test_in_house_issue_accepts_an_employee_left_on_the_operation(self):
		"""In-house work only requires the subcontractor to be unset (employee may be set)."""
		self.assertEqual(self._issue(employee="EMP-OLD"), [])

	def test_subcontracting_issue_refuses_an_operation_held_by_an_employee(self):
		doc = _eir_doc(
			"Issue", subcontracting="Yes", subcontractor="SUP-1", employee=None
		)
		self._refused(doc, employee="EMP-OLD")

	def test_subcontracting_issue_accepts_a_subcontractor_left_on_the_operation(self):
		doc = _eir_doc(
			"Issue", subcontracting="Yes", subcontractor="SUP-1", employee=None
		)
		self.assertEqual(self._issue(doc, subcontractor="SUP-OLD"), [])

	def test_message_names_the_row_operation_status_and_work_order(self):
		message = self._refused(status="WIP")
		for token in ("Row 1", "MOP-1", "is WIP", "MWO-1"):
			self.assertIn(token, message)

	def test_rule_mirrors_the_issue_eligibility_filters(self):
		"""The client and Load Full Casting Tree go through these filters; the guard must agree."""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
			_issue_eligibility_filters,
		)

		in_house = _issue_eligibility_filters("Dept-T", "No")
		subcontracted = _issue_eligibility_filters("Dept-T", "Yes")
		for filters in (in_house, subcontracted):
			self.assertEqual(filters["status"], ["in", ["Not Started"]])
			self.assertEqual(filters["operation"], ["is", "not set"])
			self.assertEqual(
				filters["department_ir_status"],
				["not in", list(guard.ISSUE_BLOCKED_TRANSIT)],
			)
		self.assertEqual(in_house["subcontractor"], ["is", "not set"])
		self.assertNotIn("employee", in_house)
		self.assertEqual(subcontracted["employee"], ["is", "not set"])
		self.assertNotIn("subcontractor", subcontracted)


class TestCurrentOperationGuardEmployeeReceiveRule(IntegrationTestCase):
	"""Employee Receive: the current operation must be held by this Receive's employee /
	subcontractor for this operation in this department."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _receive(self, doc=None, **op_fields):
		fields = {"status": "WIP", "operation": "Op-T", "employee": "EMP-T"}
		fields.update(op_fields)
		return _evaluate(doc or _eir_doc("Receive"), [_op("MOP-1", **fields)], [_wo()])

	def _mismatch(self, doc=None, **op_fields):
		message = _one_problem(
			self, self._receive(doc, **op_fields), guard.CurrentOperationError
		)
		self.assertIn("does not match this Receive", message)
		return message

	def test_each_held_status_is_receivable(self):
		"""Repeat and QC receives included."""
		for status in ("WIP", "On Hold", "QC Completed"):
			with self.subTest(status=status):
				self.assertEqual(self._receive(status=status), [])

	def test_operation_not_held_is_refused(self):
		for status in ("Not Started", "QC Pending", "Finished", "Revert", None):
			with self.subTest(status=status):
				self.assertIn("status", self._mismatch(status=status))

	def test_other_operation_is_refused(self):
		self.assertIn(
			"operation <strong>Op-X</strong>", self._mismatch(operation="Op-X")
		)

	def test_other_employee_is_refused(self):
		self.assertIn(
			"employee <strong>EMP-X</strong>", self._mismatch(employee="EMP-X")
		)

	def test_other_department_is_refused(self):
		self.assertIn(
			"department <strong>Dept-X</strong>", self._mismatch(department="Dept-X")
		)

	def test_blank_header_employee_skips_the_employee_match(self):
		doc = _eir_doc("Receive", employee=None)
		self.assertEqual(self._receive(doc, employee="EMP-ANY"), [])

	def test_subcontracting_receive_matches_the_subcontractor(self):
		doc = _eir_doc(
			"Receive", subcontracting="Yes", subcontractor="SUP-1", employee=None
		)
		self.assertEqual(self._receive(doc, employee=None, subcontractor="SUP-1"), [])
		message = self._mismatch(doc, employee=None, subcontractor="SUP-2")
		self.assertIn("subcontractor <strong>SUP-2</strong>", message)

	def test_in_house_receive_ignores_the_subcontractor_field(self):
		self.assertEqual(self._receive(subcontractor="SUP-ANY"), [])

	def test_every_mismatch_is_listed_in_one_message(self):
		message = self._mismatch(
			status="Not Started", operation="Op-X", employee="EMP-X"
		)
		for token in (
			"status Not Started",
			"operation <strong>Op-X",
			"employee <strong>EMP-X",
		):
			self.assertIn(token, message)


class TestCurrentOperationGuardRaise(IntegrationTestCase):
	"""_raise: one aggregated single-line error, the most severe class, and the kill switch."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def tearDown(self):
		frappe.clear_messages()
		return super().tearDown()

	@staticmethod
	def _mode(value):
		return patch.dict(frappe.local.conf, {"current_operation_guard": value})

	def test_no_problems_raises_nothing(self):
		guard._raise([], _eir_doc())

	def test_most_severe_problem_class_is_raised(self):
		g = guard
		cases = (
			(
				(
					g.CurrentOperationError,
					g.StaleOperationError,
					g.AmbiguousOperationError,
				),
				g.AmbiguousOperationError,
			),
			((g.StaleOperationError, g.HistoryRewriteError), g.HistoryRewriteError),
			((g.OutstandingDraftError, g.StaleOperationError), g.StaleOperationError),
			(
				(g.CurrentOperationError, g.OutstandingDraftError),
				g.OutstandingDraftError,
			),
			((g.CurrentOperationError,), g.CurrentOperationError),
		)
		for classes, expected in cases:
			with self.subTest(expected=expected.__name__), self._mode(None):
				problems = [(cls, f"problem {i}") for i, cls in enumerate(classes)]
				with self.assertRaises(guard.CurrentOperationError) as cm:
					guard._raise(problems, _eir_doc())
				self.assertIs(type(cm.exception), expected)

	def test_message_is_one_line_of_unique_rows_joined_with_br(self):
		"""The Submission Queue banner shows only the last traceback line."""
		problems = [
			(guard.StaleOperationError, "first"),
			(guard.OutstandingDraftError, "second"),
			(guard.StaleOperationError, "first"),
		]
		with self._mode(None), self.assertRaises(guard.StaleOperationError) as cm:
			guard._raise(problems, _eir_doc())
		self.assertEqual(str(cm.exception), "first<br>second")

	def test_throw_carries_the_guard_title(self):
		with self._mode(None), patch.object(
			guard.frappe, "throw", side_effect=_Stop
		) as throw:
			with self.assertRaises(_Stop):
				guard._raise([(guard.StaleOperationError, "stale")], _eir_doc())
		args, kwargs = throw.call_args
		self.assertEqual(args[0], "stale")
		self.assertIs(kwargs["exc"], guard.StaleOperationError)
		self.assertEqual(kwargs["title"], "Work Order Current Operation")

	def test_warn_mode_logs_and_shows_an_orange_message_instead_of_raising(self):
		doc = _eir_doc(name="EIR-T-0042")
		problems = [
			(guard.StaleOperationError, "stale"),
			(guard.OutstandingDraftError, "draft"),
		]
		with (
			self._mode("warn"),
			patch.object(guard.frappe, "log_error") as log_error,
			patch.object(guard.frappe, "msgprint") as msgprint,
		):
			guard._raise(problems, doc)
		kwargs = log_error.call_args.kwargs
		self.assertEqual(kwargs["message"], "stale<br>draft")
		self.assertEqual(kwargs["reference_doctype"], "Employee IR")
		self.assertEqual(kwargs["reference_name"], "EIR-T-0042")
		self.assertIn("EIR-T-0042", kwargs["title"])
		self.assertEqual(msgprint.call_args.args[0], "stale<br>draft")
		self.assertEqual(msgprint.call_args.kwargs["indicator"], "orange")

	def test_warn_mode_on_a_new_document_logs_without_a_reference(self):
		with (
			self._mode("warn"),
			patch.object(guard.frappe, "log_error") as log_error,
			patch.object(guard.frappe, "msgprint"),
		):
			guard._raise([(guard.StaleOperationError, "stale")], _eir_doc(new=True))
		self.assertIsNone(log_error.call_args.kwargs["reference_name"])

	def test_warn_mode_value_is_trimmed_and_case_insensitive(self):
		with (
			self._mode("  WARN "),
			patch.object(guard.frappe, "log_error") as log_error,
			patch.object(guard.frappe, "msgprint"),
		):
			guard._raise([(guard.StaleOperationError, "stale")], _eir_doc())
		log_error.assert_called_once()

	def test_cancel_guards_raise_even_in_warn_mode(self):
		with self._mode("warn"), patch.object(guard.frappe, "log_error") as log_error:
			with self.assertRaises(guard.HistoryRewriteError):
				guard._raise(
					[(guard.HistoryRewriteError, "rewrite")], _eir_doc(), always=True
				)
		log_error.assert_not_called()

	def test_any_other_mode_enforces(self):
		for value in ("enforce", "off", "", None):
			with self.subTest(mode=value), self._mode(value):
				with self.assertRaises(guard.StaleOperationError):
					guard._raise([(guard.StaleOperationError, "stale")], _eir_doc())

	def test_draft_message_names_the_draft_its_owner_and_the_work_order(self):
		problems = guard._draft_problems(
			[FrappeDict(_draft("EIR-D-7", "MWO-7", "a@example.com"))]
		)
		message = _one_problem(self, problems, guard.OutstandingDraftError)
		for token in ("MWO-7", "EIR-D-7", "a@example.com", "Discard"):
			self.assertIn(token, message)

	def test_busy_is_a_retryable_error_naming_the_record(self):
		with self.assertRaises(guard.WorkOrderBusyError) as cm:
			guard._busy("Work order", "MWO-1")
		self.assertIn("<strong>MWO-1</strong>", str(cm.exception))
		self.assertIn("try again", str(cm.exception))

	def test_busy_with_an_unknown_row_names_the_candidates_capped(self):
		names = [f"MWO-{i:02d}" for i in range(14)]
		with self.assertRaises(guard.WorkOrderBusyError) as cm:
			guard._busy("Work order", names, plural="these work orders")
		message = str(cm.exception)
		self.assertTrue(message.startswith("One of these work orders"), message)
		self.assertIn("<strong>MWO-09</strong>", message)
		self.assertNotIn("MWO-10", message)
		self.assertIn("(+4 more)", message)

	def test_draft_problems_are_one_line_per_draft_with_the_work_orders_capped(self):
		"""A 167-row document held by one draft used to produce 167 near-identical lines."""
		drafts = [
			FrappeDict(_draft("EIR-D-BIG", f"MWO-{i:03d}", "big@example.com"))
			for i in range(20)
		] + [FrappeDict(_draft("EIR-D-SMALL", "MWO-900", "small@example.com"))]
		problems = guard._draft_problems(drafts)
		self.assertEqual(len(problems), 2)
		big, small = (message for _cls, message in problems)
		self.assertTrue(big.startswith("Work orders"), big)
		for token in (
			"EIR-D-BIG",
			"big@example.com",
			"MWO-009",
			"(+10 more)",
			"Discard",
		):
			self.assertIn(token, big)
		self.assertNotIn("MWO-010", big)
		self.assertLess(len(big), 1500)
		self.assertTrue(
			small.startswith("Work order <strong>MWO-900</strong> is"), small
		)
		self.assertIn("for this work order", small)

	def test_message_lines_are_capped(self):
		problems = [(guard.StaleOperationError, f"row {i}") for i in range(30)]
		with self._mode(None), self.assertRaises(guard.StaleOperationError) as cm:
			guard._raise(problems, _eir_doc())
		lines = str(cm.exception).split("<br>")
		self.assertEqual(len(lines), guard.MAX_MESSAGE_LINES + 1)
		self.assertEqual(lines[guard.MAX_MESSAGE_LINES - 1], "row 24")
		self.assertIn("5 more problem", lines[-1])


class _TerminalReadFake:
	"""``frappe.db.sql`` for outstanding_issue_drafts' terminal re-check, by statement.

	``children`` / ``heads``: committed rows; ``locked_rows`` / ``locked_heads``: names another
	transaction is writing (SKIP LOCKED skips them); ``then_rows`` / ``then_heads``: what this
	transaction's snapshot shows (the plain classification reads)."""

	def __init__(
		self,
		children=(),
		heads=(),
		locked_rows=(),
		locked_heads=(),
		then_rows=(),
		then_heads=(),
		scan_error=None,
		read_error=None,
	):
		self.children = [FrappeDict(c) for c in children]
		self.heads = [FrappeDict(h) for h in heads]
		self.locked_rows = set(locked_rows)
		self.locked_heads = set(locked_heads)
		self.then_rows = [FrappeDict(r) for r in then_rows]
		self.then_heads = [FrappeDict(h) for h in then_heads]
		self.scan_error = scan_error
		self.read_error = read_error
		self.statements = []

	def __call__(self, query, values=None, *args, **kwargs):
		q = " ".join(str(query).split())
		self.statements.append((q, values, kwargs))
		if q.startswith("SELECT name FROM `tabEmployee IR Operation` FORCE INDEX"):
			if self.scan_error is not None:
				raise self.scan_error
			wanted = set(values["mwos"])
			return [
				(c.name,) for c in self.children if c.manufacturing_work_order in wanted
			]
		if self.read_error is not None:
			raise self.read_error
		if (
			"FROM `tabEmployee IR Operation` WHERE name IN %(names)s LOCK IN SHARE MODE SKIP LOCKED"
			in q
		):
			return [
				c
				for c in self.children
				if c.name in values["names"] and c.name not in self.locked_rows
			]
		if "FROM `tabEmployee IR Operation` o LEFT JOIN `tabEmployee IR` e" in q:
			return [r for r in self.then_rows if r.name in values["names"]]
		if (
			"FROM `tabEmployee IR` WHERE name IN %(parents)s LOCK IN SHARE MODE SKIP LOCKED"
			in q
		):
			return [
				h
				for h in self.heads
				if h.name in values["parents"] and h.name not in self.locked_heads
			]
		if "FROM `tabEmployee IR` WHERE name IN %(parents)s" in q:
			return [h for h in self.then_heads if h.name in values["parents"]]
		if "o.parent AS draft" in q and "INNER JOIN `tabEmployee IR` e" in q:
			# the plain (snapshot) draft read
			return [FrappeDict(_draft("EIR-PLAIN", "MWO-A"))]
		# A reworded statement must fail as such, not pass as a "draft" row.
		raise AssertionError(f"statement the fake does not know: {q[:200]}")


def _row(name, parent, mwo="MWO-A", docstatus=0, idx=1):
	return dict(
		name=name,
		parent=parent,
		parenttype="Employee IR",
		parentfield="employee_ir_operations",
		manufacturing_work_order=mwo,
		idx=idx,
		docstatus=docstatus,
	)


def _head(name, type="Issue", docstatus=0, owner="maker@example.com"):
	return dict(
		name=name, type=type, docstatus=docstatus, owner=owner, creation=now_datetime()
	)


class TestCurrentOperationGuardDraftQuery(IntegrationTestCase):
	"""outstanding_issue_drafts: a plain read for fast feedback; the terminal re-check -- a
	covered NOWAIT scan of the work orders' rows, then SKIP LOCKED reads by primary key, rows and
	parents another transaction is writing judged by the snapshot -- and a logged plain fallback
	when the index is missing."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	CHILDREN = (
		_row("c-1", "EIR-D-0001", "MWO-A", idx=1),
		_row("c-2", "EIR-SELF", "MWO-A"),
		_row("c-3", "EIR-RECEIVE", "MWO-B", idx=2),
		_row("c-4", "EIR-SUBMITTED", "MWO-B", docstatus=1, idx=3),
	)
	HEADS = (
		_head("EIR-D-0001"),
		_head("EIR-RECEIVE", type="Receive", owner="x@example.com"),
		_head("EIR-SUBMITTED", docstatus=1, owner="x@example.com"),
	)

	def _read(
		self,
		mwos,
		*,
		exclude=None,
		locking=False,
		index="manufacturing_work_order_index",
		**fake,
	):
		fake.setdefault("children", self.CHILDREN)
		fake.setdefault("heads", self.HEADS)
		self.fake = _TerminalReadFake(**fake)
		with (
			patch.object(guard, "_draft_index_name", return_value=index),
			patch.object(guard, "_logged_missing_index", False),
			patch.object(guard.frappe.db, "sql", side_effect=self.fake),
			patch.object(guard.frappe, "log_error") as log_error,
		):
			self.log_error = log_error
			return guard.outstanding_issue_drafts(
				mwos, exclude=exclude, locking=locking
			)

	@property
	def statements(self):
		return self.fake.statements

	def test_no_work_orders_reads_nothing(self):
		for mwos in (None, [], [None, ""]):
			with self.subTest(mwos=mwos):
				self.assertEqual(self._read(mwos, locking=True), [])
				self.assertEqual(self.statements, [])

	def test_plain_read_is_one_non_locking_statement(self):
		drafts = self._read(["MWO-B", "MWO-A", "", None, "MWO-A"])
		self.assertEqual([d.draft for d in drafts], ["EIR-PLAIN"])
		self.assertEqual(len(self.statements), 1)
		query, values, kwargs = self.statements[0]
		self.assertEqual(values, {"mwos": ["MWO-A", "MWO-B"], "exclude": ""})
		self.assertTrue(kwargs.get("as_dict"))
		for clause in (
			"e.type = 'Issue'",
			"e.docstatus = 0",
			"e.name != %(exclude)s",
			# the row, so a hit can be confirmed by primary key (_confirm_drafts)
			"o.name AS row_name",
		):
			self.assertIn(clause, query)
		for clause in ("LOCK IN SHARE MODE", "FOR UPDATE", "FORCE INDEX"):
			self.assertNotIn(clause, query)

	def test_plain_read_excludes_the_document_itself(self):
		self._read(["MWO-A"], exclude="EIR-SELF")
		self.assertEqual(self.statements[0][1]["exclude"], "EIR-SELF")

	def test_terminal_recheck_is_a_covered_scan_then_record_reads_that_never_wait(self):
		self._read(["MWO-B", "MWO-A"], exclude="EIR-SELF", locking=True)
		(
			(scan, scan_values, _k1),
			(rows, rows_values, _k2),
			(heads, heads_values, _k3),
		) = self.statements
		# only `name`: the manufacturing_work_order index covers it, so the scan locks index
		# entries and never the rows another transaction's check_if_latest holds FOR UPDATE
		self.assertEqual(
			scan,
			"SELECT name FROM `tabEmployee IR Operation` FORCE INDEX "
			"(`manufacturing_work_order_index`) WHERE manufacturing_work_order IN %(mwos)s "
			"LOCK IN SHARE MODE NOWAIT",
		)
		self.assertEqual(scan_values, {"mwos": ["MWO-A", "MWO-B"]})
		self.assertTrue(
			rows.endswith("WHERE name IN %(names)s LOCK IN SHARE MODE SKIP LOCKED")
		)
		self.assertEqual(rows_values, {"names": ["c-1", "c-2", "c-3", "c-4"]})
		self.assertIn("FROM `tabEmployee IR` WHERE name IN %(parents)s", heads)
		self.assertTrue(heads.endswith("LOCK IN SHARE MODE SKIP LOCKED"), heads)
		# the Receive draft's parent is read too: its type decides
		self.assertEqual(heads_values, {"parents": ["EIR-D-0001", "EIR-RECEIVE"]})
		for query, _values, _kwargs in self.statements:
			self.assertNotIn("FOR UPDATE", query)

	def test_terminal_recheck_keeps_only_other_employee_issue_drafts(self):
		drafts = self._read(["MWO-A", "MWO-B"], exclude="EIR-SELF", locking=True)
		self.assertEqual(len(drafts), 1)
		self.assertEqual(
			{k: drafts[0][k] for k in ("draft", "mwo", "idx", "owner", "row_name")},
			{
				"draft": "EIR-D-0001",
				"mwo": "MWO-A",
				"idx": 1,
				"owner": "maker@example.com",
				"row_name": "c-1",
			},
		)
		self.assertTrue(drafts[0].creation)

	def test_terminal_recheck_without_other_drafts_skips_the_parent_read(self):
		self.assertEqual(
			self._read(
				["MWO-A"],
				exclude="EIR-SELF",
				locking=True,
				children=(_row("c-2", "EIR-SELF"),),
			),
			[],
		)
		self.assertEqual(len(self.statements), 2)

	def test_rows_a_receive_submit_is_writing_do_not_refuse_the_holder(self):
		"""Race suite (core bug 3): a Receive draft's submit queued behind this block holds its
		rows (check_if_latest). They were a Receive's rows in this snapshot: not drafts now."""
		drafts = self._read(
			["MWO-B"],
			exclude="EIR-SELF",
			locking=True,
			locked_rows={"c-3"},
			then_rows=(
				dict(
					_row("c-3", "EIR-RECEIVE", "MWO-B"),
					type="Receive",
					parent_docstatus=0,
				),
			),
		)
		self.assertEqual(drafts, [])

	def test_a_row_being_written_that_may_be_an_issue_drafts_is_busy(self):
		for label, then_rows in (
			("unknown to the snapshot (committed after it)", ()),
			(
				"an Issue draft's row in the snapshot",
				(dict(_row("c-1", "EIR-D-0001"), type="Issue", parent_docstatus=0),),
			),
		):
			with self.subTest(label):
				with self.assertRaises(guard.WorkOrderBusyError):
					self._read(
						["MWO-A"],
						exclude="EIR-SELF",
						locking=True,
						locked_rows={"c-1"},
						then_rows=then_rows,
					)

	def test_a_row_submitted_or_cancelled_in_the_snapshot_is_never_a_draft_again(self):
		self.assertEqual(
			self._read(
				["MWO-B"],
				locking=True,
				locked_rows={"c-4"},
				then_rows=(
					dict(
						_row("c-4", "EIR-SUBMITTED", "MWO-B", docstatus=1),
						type="Issue",
						parent_docstatus=1,
					),
				),
				children=(_row("c-4", "EIR-SUBMITTED", "MWO-B", docstatus=1),),
			),
			[],
		)

	def test_a_parent_being_written_keeps_its_snapshot_verdict(self):
		"""Typically that draft's own submit, queued behind this block."""
		self.assertEqual(
			[
				d.draft
				for d in self._read(
					["MWO-A"],
					exclude="EIR-SELF",
					locking=True,
					locked_heads={"EIR-D-0001"},
					then_heads=(_head("EIR-D-0001"),),
				)
			],
			["EIR-D-0001"],
		)
		self.assertEqual(
			self._read(
				["MWO-B"],
				locking=True,
				locked_heads={"EIR-RECEIVE"},
				then_heads=(_head("EIR-RECEIVE", type="Receive"),),
			),
			[],
		)
		with self.assertRaises(guard.WorkOrderBusyError):  # unknown to the snapshot
			self._read(
				["MWO-A"], exclude="EIR-SELF", locking=True, locked_heads={"EIR-D-0001"}
			)

	def test_terminal_recheck_never_waits(self):
		"""NOWAIT failure (1205) and a snapshot conflict (1020, mapped to deadlock) say retry."""
		for where, error in (
			("scan", frappe.QueryTimeoutError("1205")),
			("scan", frappe.QueryDeadlockError("1020")),
			("record reads", frappe.QueryDeadlockError("1020")),
		):
			with self.subTest(where=where, error=type(error).__name__):
				with self.assertRaises(guard.WorkOrderBusyError):
					self._read(
						["MWO-A"],
						locking=True,
						scan_error=error if where == "scan" else None,
						read_error=error if where != "scan" else None,
					)

	def test_missing_index_falls_back_to_a_plain_read_logged_once(self):
		statements = []
		log_calls = []
		with (
			patch.object(guard, "_draft_index_name", return_value=None),
			patch.object(guard, "_logged_missing_index", False),
			patch.object(
				guard.frappe.db,
				"sql",
				side_effect=lambda q, *a, **k: statements.append(q) or [],
			),
			patch.object(
				guard.frappe, "log_error", side_effect=_rec(log_calls, "logged")
			),
		):
			guard.outstanding_issue_drafts(["MWO-A"], locking=True)
			guard.outstanding_issue_drafts(["MWO-B"], locking=True)
		self.assertEqual(len(statements), 2)
		for query in statements:
			self.assertNotIn("LOCK IN SHARE MODE", query)
			self.assertNotIn("FORCE INDEX", query)
		self.assertEqual(log_calls, ["logged"])

	def test_plain_read_does_not_probe_the_index(self):
		with patch.object(guard, "_draft_index_name") as probe:
			with patch.object(guard.frappe.db, "sql", return_value=[]):
				guard.outstanding_issue_drafts(["MWO-A"])
		probe.assert_not_called()


class TestCurrentOperationGuardDraftConfirmation(IntegrationTestCase):
	"""_confirm_drafts: a plain-read draft hit only refuses if a current read does not disprove it.

	The plain read comes from the transaction's REPEATABLE READ snapshot, which can predate the
	wait for the lock block (race suite: a Receive that waited on its own Issue's submit was
	refused "already on Employee Issue X (Draft...)" for an X already submitted). Both re-reads
	are SKIP LOCKED: the confirmation never waits and never raises busy."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _hit(self, row="c-1", draft="EIR-D-0001", mwo="MWO-1"):
		hit = FrappeDict(_draft(draft, mwo, "plain@example.com"))
		hit.row_name = row
		return hit

	def _confirm(self, hits, *, children=(), heads=(), mwos=("MWO-1",)):
		"""``children`` / ``heads``: what the SKIP LOCKED reads return (unlocked rows only)."""
		self.statements = []

		def fake_sql(query, values=None, *args, **kwargs):
			self.statements.append((" ".join(query.split()), values))
			if "`tabEmployee IR Operation`" in query:
				return [FrappeDict(c) for c in children]
			return [FrappeDict(h) for h in heads]

		with patch.object(guard.frappe.db, "sql", side_effect=fake_sql):
			return guard._confirm_drafts(hits, set(mwos))

	@staticmethod
	def _child(name="c-1", parent="EIR-D-0001", mwo="MWO-1", docstatus=0):
		return dict(
			name=name,
			parent=parent,
			parenttype="Employee IR",
			manufacturing_work_order=mwo,
			idx=1,
			docstatus=docstatus,
		)

	@staticmethod
	def _head(name="EIR-D-0001", type="Issue", docstatus=0):
		return dict(
			name=name,
			type=type,
			docstatus=docstatus,
			owner="fresh@example.com",
			creation=now_datetime(),
		)

	def test_a_draft_that_is_still_a_draft_is_confirmed_with_its_fresh_owner(self):
		confirmed = self._confirm(
			[self._hit()], children=[self._child()], heads=[self._head()]
		)
		self.assertEqual(
			[(d.draft, d.mwo, d.owner, d.row_name) for d in confirmed],
			[("EIR-D-0001", "MWO-1", "fresh@example.com", "c-1")],
		)

	def test_a_draft_submitted_discarded_or_moved_meanwhile_is_dropped(self):
		for label, children, heads in (
			("submitted", [self._child(docstatus=1)], [self._head(docstatus=1)]),
			("discarded", [self._child(docstatus=2)], [self._head(docstatus=2)]),
			(
				"row moved to another work order",
				[self._child(mwo="MWO-9")],
				[self._head()],
			),
			# the draft's rows were replaced: its row is gone, and nobody is writing the draft
			# (its parent read unlocked), so the row is really gone
			("rows replaced", [], [self._head()]),
			("turned into a Receive", [self._child()], [self._head(type="Receive")]),
			("submitted, row being written", [], [self._head(docstatus=1)]),
		):
			with self.subTest(label=label):
				self.assertEqual(
					self._confirm([self._hit()], children=children, heads=heads), []
				)

	def test_a_document_being_written_right_now_keeps_the_snapshot_verdict(self):
		"""Its own submit, queued behind this block, holds its rows and parent (Frappe's
		check_if_latest loads them FOR UPDATE): skipped, so it is still a draft as far as any
		commit is concerned. (A draft deleted meanwhile looks the same and is refused too: a
		retry passes.)"""
		for label, children in (
			("parent and row locked", []),
			("parent locked, row not yet", [self._child()]),
		):
			with self.subTest(label=label):
				confirmed = self._confirm([self._hit()], children=children, heads=[])
				self.assertEqual([d.draft for d in confirmed], ["EIR-D-0001"])
				self.assertEqual(confirmed[0].owner, "plain@example.com")

	def test_reads_are_record_only_and_never_wait(self):
		self._confirm(
			[self._hit("c-2", "EIR-D-0002"), self._hit("c-1")],
			children=[self._child(), self._child("c-2", "EIR-D-0002")],
			heads=[self._head(), self._head("EIR-D-0002")],
		)
		(children_sql, children_values), (heads_sql, heads_values) = self.statements
		self.assertIn(
			"FROM `tabEmployee IR Operation` WHERE name IN %(rows)s", children_sql
		)
		self.assertTrue(
			children_sql.endswith("LOCK IN SHARE MODE SKIP LOCKED"), children_sql
		)
		self.assertEqual(children_values, {"rows": ["c-1", "c-2"]})
		self.assertIn("FROM `tabEmployee IR` WHERE name IN %(parents)s", heads_sql)
		self.assertTrue(heads_sql.endswith("LOCK IN SHARE MODE SKIP LOCKED"), heads_sql)
		self.assertEqual(heads_values, {"parents": ["EIR-D-0001", "EIR-D-0002"]})
		for query, _values in self.statements:
			for clause in ("FOR UPDATE", "FORCE INDEX", "NOWAIT"):
				self.assertNotIn(clause, query)

	def test_hits_without_a_row_name_read_nothing(self):
		hits = [FrappeDict(_draft())]
		self.assertEqual(self._confirm(hits), hits)
		self.assertEqual(self.statements, [])


class TestCurrentOperationGuardSuccessorConfirmation(IntegrationTestCase):
	"""_confirm_successors: a successor from the plain family read only counts once a current read
	still finds it -- a cancel may have deleted it after this transaction's snapshot."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _confirm(self, family, mops, *, fresh=(), error=None):
		self.statements = []

		def fake_sql(query, values=None, *args, **kwargs):
			self.statements.append((" ".join(query.split()), values))
			if error is not None:
				raise error
			return [FrappeDict(r) for r in fresh]

		with patch.object(guard.frappe.db, "sql", side_effect=fake_sql):
			return guard._confirm_successors(family, mops)

	def test_no_candidate_reads_nothing(self):
		family = _family_of(_op("MOP-1"), _op("MOP-0", status="Finished"))
		self.assertIs(self._confirm(family, {"MOP-1"}), family)
		self.assertEqual(self.statements, [])

	def test_reverted_rows_are_never_candidates(self):
		family = _family_of(
			_op("MOP-1"),
			_op("MOP-R", previous_mop="MOP-1", department_ir_status="Revert"),
		)
		self._confirm(family, {"MOP-1"})
		self.assertEqual(self.statements, [])

	def test_a_successor_deleted_after_the_snapshot_is_dropped(self):
		family = _family_of(_op("MOP-1"), _op("MOP-2", previous_mop="MOP-1"))
		confirmed = self._confirm(family, {"MOP-1"}, fresh=[])
		self.assertEqual([r.name for r in confirmed["MWO-1"]], ["MOP-1"])
		self.assertFalse(guard._has_successor(confirmed["MWO-1"], "MOP-1"))

	def test_a_successor_that_still_exists_is_replaced_by_its_fresh_row(self):
		family = _family_of(_op("MOP-1"), _op("MOP-2", previous_mop="MOP-1"))
		fresh = _op("MOP-2", previous_mop="MOP-1", status="WIP")
		confirmed = self._confirm(family, {"MOP-1"}, fresh=[fresh])
		self.assertTrue(guard._has_successor(confirmed["MWO-1"], "MOP-1"))
		self.assertEqual(
			next(r.status for r in confirmed["MWO-1"] if r.name == "MOP-2"), "WIP"
		)

	def test_a_successor_reverted_meanwhile_stops_counting(self):
		family = _family_of(_op("MOP-1"), _op("MOP-2", previous_mop="MOP-1"))
		fresh = _op("MOP-2", previous_mop="MOP-1", department_ir_status="Revert")
		confirmed = self._confirm(family, {"MOP-1"}, fresh=[fresh])
		self.assertFalse(guard._has_successor(confirmed["MWO-1"], "MOP-1"))

	def test_the_read_is_record_only_and_never_waits(self):
		family = _family_of(
			_op("MOP-1"),
			_op("MOP-3", previous_mop="MOP-1"),
			_op("MOP-2", previous_mop="MOP-1"),
			_op("MOP-9", previous_mop="MOP-8"),
		)
		self._confirm(family, {"MOP-1"})
		((query, values),) = self.statements
		self.assertIn(
			"FROM `tabManufacturing Operation` WHERE name IN %(names)s", query
		)
		self.assertTrue(query.endswith("LOCK IN SHARE MODE NOWAIT"), query)
		self.assertEqual(values, {"names": ["MOP-2", "MOP-3"]})

	def test_a_successor_another_transaction_is_writing_is_busy(self):
		family = _family_of(_op("MOP-1"), _op("MOP-2", previous_mop="MOP-1"))
		with self.assertRaises(guard.WorkOrderBusyError) as cm:
			self._confirm(family, {"MOP-1"}, error=frappe.QueryTimeoutError("1205"))
		self.assertIn("MOP-2", str(cm.exception))


class TestCurrentOperationGuardDraftIndex(IntegrationTestCase):
	"""The terminal re-check must find the manufacturing_work_order index under the name Frappe
	actually gives it: ``<field>`` when it CREATES the table (fresh CI sites) but
	``<field>_index`` when migrate ADDS it to an existing table (every production site; frappe
	database/mariadb/schema.py). MariaDB's SHOW INDEX is emulated over each layout, so the tests
	hold for any lookup that asks the server rather than assuming one name."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _drafts_with_index(self, index_name, probe_error=None):
		indexes = [
			("PRIMARY", "name", 1, 0),
			("parent", "parent", 1, 1),
			("modified", "modified", 1, 1),
			("manufacturing_operation_index", "manufacturing_operation", 1, 1),
		]
		if index_name:
			indexes.append((index_name, "manufacturing_work_order", 1, 1))
		names = {index[0] for index in indexes}
		self.statements = []

		def show_index(query, as_dict):
			rows = [
				FrappeDict(
					Key_name=k,
					Column_name=c,
					Seq_in_index=s,
					Non_unique=n,
					Index_type="BTREE",
				)
				for k, c, s, n in indexes
			]
			for column, pattern, cast in (
				("Key_name", r"Key_name\s*=\s*['\"]([^'\"]+)['\"]", str),
				(
					"Column_name",
					r"Column_name\s*=\s*(?:BINARY\s*)?['\"]([^'\"]+)['\"]",
					str,
				),
				("Seq_in_index", r"Seq_in_index\s*=\s*(\d+)", int),
				("Non_unique", r"Non_unique\s*=\s*(\d+)", int),
			):
				match = re.search(pattern, query)
				if match:
					rows = [row for row in rows if row[column] == cast(match.group(1))]
			return rows if as_dict else [tuple(row.values()) for row in rows]

		terminal = _TerminalReadFake(
			children=(_row("r-1", "EIR-OTHER", "MWO-1"),),
			heads=(_head("EIR-OTHER", owner="m@example.com"),),
		)

		def fake_sql(query, values=None, *args, **kwargs):
			if query.lstrip().upper().startswith("SHOW INDEX"):
				if probe_error is not None:
					raise probe_error
				return show_index(query, kwargs.get("as_dict"))
			self.statements.append(query)
			forced = re.search(r"FORCE INDEX\s*\(\s*`?([^`)\s]+)`?\s*\)", query)
			if forced and forced.group(1) not in names:
				raise AssertionError(
					f"FORCE INDEX names a missing index: {forced.group(1)}"
				)
			if "LOCK IN SHARE MODE" in query:
				return terminal(query, values, *args, **kwargs)
			return [FrappeDict(_draft("EIR-OTHER", "MWO-1", "m@example.com"))]

		with (
			patch.object(guard, "_logged_missing_index", False),
			patch.object(guard.frappe.db, "sql", side_effect=fake_sql),
			patch.object(guard.frappe, "log_error") as log_error,
		):
			drafts = guard.outstanding_issue_drafts(
				["MWO-1"], exclude="EIR-SELF", locking=True
			)
		return drafts, log_error

	def _assert_locking_read(self, drafts, log_error):
		self.assertTrue(
			any("LOCK IN SHARE MODE NOWAIT" in q for q in self.statements),
			f"fell back to a plain read although the index exists: {self.statements}",
		)
		log_error.assert_not_called()
		self.assertEqual([d.draft for d in drafts], ["EIR-OTHER"])

	def test_index_created_with_the_table_is_used(self):
		self._assert_locking_read(*self._drafts_with_index("manufacturing_work_order"))

	def test_index_added_by_migrate_is_used(self):
		"""Production: falling back to the plain read there reopens the REPEATABLE READ race of
		2026-10-01 (two drafts on one work order, neither seeing the other)."""
		self._assert_locking_read(
			*self._drafts_with_index("manufacturing_work_order_index")
		)

	def test_missing_index_falls_back_to_one_plain_read_and_one_error_log(self):
		_drafts, log_error = self._drafts_with_index(None)
		self.assertEqual(len(self.statements), 1)
		self.assertNotIn("LOCK IN SHARE MODE", self.statements[0])
		self.assertNotIn("FORCE INDEX", self.statements[0])
		log_error.assert_called_once()

	def test_failing_index_probe_degrades_to_the_plain_read(self):
		_drafts, log_error = self._drafts_with_index(
			"manufacturing_work_order_index", probe_error=Exception("SHOW INDEX denied")
		)
		self.assertEqual(len(self.statements), 1)
		self.assertNotIn("LOCK IN SHARE MODE", self.statements[0])
		log_error.assert_called_once()


class TestCurrentOperationGuardCheck(IntegrationTestCase):
	"""_check: lock (or read), evaluate, read drafts, remember the block, then warn or raise."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def tearDown(self):
		frappe.clear_messages()
		return super().tearDown()

	def _run(
		self,
		doc,
		phase="save",
		locking=True,
		ops=(),
		wos=(),
		family=None,
		drafts=(),
		confirmed=None,
	):
		"""``confirmed``: what _confirm_drafts keeps (default: every plain hit)."""
		self.calls = []
		rows = ({op.name: op for op in ops}, {wo.name: wo for wo in wos})

		def lock_block(mops, mwos, doc=None):
			self.calls.append(("lock", sorted(mops), sorted(mwos)))
			return rows

		def plain_rows(mops, mwos):
			self.calls.append(("plain", sorted(mops), sorted(mwos)))
			return rows

		def draft_read(mwos, exclude=None, locking=False):
			self.calls.append(("drafts", sorted(mwos), exclude, locking))
			return [FrappeDict(d) for d in drafts]

		def confirm_successors(family, mops):
			self.calls.append(("confirm successors", sorted(mops)))
			return family

		def confirm_drafts(hits, mwos):
			self.calls.append(("confirm drafts", [h.draft for h in hits], sorted(mwos)))
			return hits if confirmed is None else [FrappeDict(d) for d in confirmed]

		def snapshot_check(mop_rows, names):
			self.calls.append(("snapshot check", sorted(names)))

		with (
			patch.object(guard, "_lock_block", side_effect=lock_block),
			patch.object(guard, "_plain_rows", side_effect=plain_rows),
			patch.object(
				guard, "_refuse_if_snapshot_older", side_effect=snapshot_check
			),
			patch.object(
				guard,
				"_after_locks",
				side_effect=lambda d, p: self.calls.append(("after_locks", p)),
			),
			patch.object(
				guard,
				"_family",
				return_value=_family_of(*ops) if family is None else family,
			),
			patch.object(guard, "_confirm_successors", side_effect=confirm_successors),
			patch.object(guard, "_confirm_drafts", side_effect=confirm_drafts),
			patch.object(guard, "_previous_operations", return_value=[]),
			patch.object(guard, "outstanding_issue_drafts", side_effect=draft_read),
			patch.object(guard.frappe, "msgprint") as msgprint,
		):
			self.msgprint = msgprint
			guard._check(doc, phase, locking)

	def test_new_document_locks_then_reads_drafts_and_remembers_the_block(self):
		doc = _eir_doc(new=True)
		self._run(doc, ops=[_op("MOP-1")], wos=[_wo()])
		self.assertEqual(
			self.calls,
			[
				("lock", ["MOP-1"], ["MWO-1"]),
				("after_locks", "save"),
				("confirm successors", ["MOP-1"]),
				("drafts", ["MWO-1"], None, False),
			],
		)
		self.assertEqual(
			doc.flags.current_operation_guarded,
			{"phase": "save", "docstatus": 0, "mwos": ["MWO-1"]},
		)

	def test_locked_check_confirms_draft_hits_before_refusing(self):
		with self.assertRaises(guard.OutstandingDraftError):
			self._run(
				_eir_doc(new=True), ops=[_op("MOP-1")], wos=[_wo()], drafts=[_draft()]
			)
		self.assertEqual(
			self.calls[-2:],
			[
				("drafts", ["MWO-1"], None, False),
				("confirm drafts", ["EIR-D-0001"], ["MWO-1"]),
			],
		)

	def test_a_draft_hit_the_current_read_disproves_does_not_refuse(self):
		"""The race suite's Receive that waited on its own Issue's submit: the snapshot still
		shows the Issue as a draft, the confirmation sees it submitted."""
		doc = _eir_doc(new=True)
		self._run(doc, ops=[_op("MOP-1")], wos=[_wo()], drafts=[_draft()], confirmed=[])
		self.assertEqual(doc.flags.current_operation_guarded["mwos"], ["MWO-1"])

	def test_advisory_and_preflight_checks_confirm_nothing(self):
		with self.assertRaises(guard.OutstandingDraftError):
			self._run(
				_eir_doc(docstatus=1),
				phase="submit",
				locking=False,
				ops=[_op("MOP-1")],
				wos=[_wo()],
				drafts=[_draft()],
			)
		self.assertFalse(
			[c for c in self.calls if c[0] in ("confirm drafts", "confirm successors")]
		)

	def test_warn_mode_takes_no_lock_and_remembers_nothing(self):
		"""The kill switch restores the pre-guard locking: no MOP / MWO lock, no re-check."""
		doc = _eir_doc(new=True)
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(guard.frappe, "log_error"),
		):
			self._run(doc, ops=[_op("MOP-1")], wos=[_wo()], drafts=[_draft()])
		self.assertEqual(self.calls[0], ("plain", ["MOP-1"], ["MWO-1"]))
		self.assertNotIn(("after_locks", "save"), self.calls)
		self.assertFalse([c for c in self.calls if c[0] == "lock"])
		self.assertIsNone(doc.flags.get("current_operation_guarded"))

	def test_department_ir_save_locks_previous_operations_in_the_operation_phase(self):
		"""update_previous_mop_data writes them later in the save (RULE E)."""
		doc = _GuardDoc(
			doctype="Department IR",
			name="DIR-T-0001",
			type="Issue",
			docstatus=0,
			company="Co-T",
			current_department="Dept-T",
			next_department="Dept-B",
			department_ir_operation=[
				FrappeDict(
					idx=1,
					manufacturing_operation="MOP-5",
					manufacturing_work_order="MWO-1",
				)
			],
			flags=FrappeDict(),
			_is_new=True,
		)
		for phase, docstatus, expected in (
			("save", 0, ["MOP-4", "MOP-5"]),
			("submit", 1, ["MOP-5"]),
		):
			with self.subTest(phase=phase):
				doc.docstatus = docstatus
				with patch.object(
					guard, "_previous_operations", return_value=["MOP-4"]
				) as previous:
					calls = []

					def lock_block(mops, mwos, doc=None, calls=calls):
						calls.append(sorted(mops))
						raise _Stop

					with patch.object(guard, "_lock_block", side_effect=lock_block):
						with self.assertRaises(_Stop):
							guard._check(doc, phase, True)
				self.assertEqual(calls, [expected])
				self.assertEqual(previous.call_count, 1 if phase == "save" else 0)

	def test_a_locked_submit_compares_its_snapshot_after_deciding(self):
		"""Only a submit decided under the block, only its ROW operations (a Department IR's
		previous operations are not cloned from), and only once nothing was refused."""
		doc = _eir_doc(docstatus=1, before=_eir_doc())
		self._run(doc, phase="submit", ops=[_op("MOP-1")], wos=[_wo()])
		self.assertEqual(self.calls[-1], ("snapshot check", ["MOP-1"]))
		self.assertLess(
			self.calls.index(("drafts", ["MWO-1"], "EIR-T-0001", False)),
			self.calls.index(("snapshot check", ["MOP-1"])),
		)

	def test_saves_advisory_checks_and_refusals_never_compare_snapshots(self):
		for label, kwargs in (
			("draft save (it clones nothing)", {"doc": _eir_doc(new=True)}),
			(
				"advisory submit check",
				{"doc": _eir_doc(docstatus=1), "phase": "submit", "locking": False},
			),
		):
			with self.subTest(label):
				self._run(ops=[_op("MOP-1")], wos=[_wo()], **kwargs)
				self.assertNotIn("snapshot check", [c[0] for c in self.calls])
		with self.assertRaises(guard.StaleOperationError):
			self._run(
				_eir_doc(docstatus=1),
				phase="submit",
				ops=[_op("MOP-1")],
				wos=[_wo(pointer="MOP-2")],
				family={},
			)
		self.assertNotIn("snapshot check", [c[0] for c in self.calls])

	def test_saved_document_excludes_itself_from_the_draft_read(self):
		doc = _eir_doc(name="EIR-T-0007", before=_eir_doc(name="EIR-T-0007"))
		self._run(doc, ops=[_op("MOP-1")], wos=[_wo()])
		self.assertEqual(self.calls[-1], ("drafts", ["MWO-1"], "EIR-T-0007", False))

	def test_advisory_check_reads_without_locks_and_remembers_nothing(self):
		doc = _eir_doc(docstatus=1)
		self._run(doc, phase="submit", locking=False, ops=[_op("MOP-1")], wos=[_wo()])
		self.assertEqual(self.calls[0], ("plain", ["MOP-1"], ["MWO-1"]))
		self.assertNotIn(("after_locks", "submit"), self.calls)
		self.assertIsNone(doc.flags.get("current_operation_guarded"))

	def test_draft_save_also_locks_the_work_order_a_row_moved_away_from(self):
		before = _eir_doc(rows=(("MOP-1", "MWO-OLD"),))
		doc = _eir_doc(rows=(("MOP-1", "MWO-NEW"),), before=before)
		self._run(
			doc,
			ops=[_op("MOP-1", mwo="MWO-NEW")],
			wos=[_wo("MWO-NEW", "MOP-1"), _wo("MWO-OLD", "MOP-0")],
		)
		self.assertEqual(self.calls[0], ("lock", ["MOP-1"], ["MWO-NEW", "MWO-OLD"]))
		self.assertEqual(self.calls[-1][:2], ("drafts", ["MWO-NEW"]))

	def test_submit_locks_only_the_rows_own_work_orders(self):
		before = _eir_doc(rows=(("MOP-1", "MWO-OLD"),))
		doc = _eir_doc(rows=(("MOP-1", "MWO-NEW"),), before=before, docstatus=1)
		self._run(
			doc,
			phase="submit",
			ops=[_op("MOP-1", mwo="MWO-NEW")],
			wos=[_wo("MWO-NEW", "MOP-1")],
		)
		self.assertEqual(self.calls[0], ("lock", ["MOP-1"], ["MWO-NEW"]))

	def test_row_without_a_work_order_reads_drafts_for_its_operations_work_order(self):
		doc = _eir_doc(rows=(("MOP-1", ""),), new=True)
		self._run(doc, ops=[_op("MOP-1")], wos=[_wo()])
		self.assertEqual(self.calls[0], ("lock", ["MOP-1"], []))
		self.assertEqual(self.calls[-1][:2], ("drafts", ["MWO-1"]))
		self.assertEqual(doc.flags.current_operation_guarded["mwos"], ["MWO-1"])

	def test_document_without_references_reads_nothing(self):
		doc = _eir_doc(rows=(), new=True)
		self._run(doc)
		self.assertEqual(self.calls, [])
		self.assertIsNone(doc.flags.get("current_operation_guarded"))

	def test_outstanding_issue_draft_blocks(self):
		with self.assertRaises(guard.OutstandingDraftError) as cm:
			self._run(
				_eir_doc(new=True), ops=[_op("MOP-1")], wos=[_wo()], drafts=[_draft()]
			)
		for token in ("EIR-D-0001", "MWO-1", "maker@example.com"):
			self.assertIn(token, str(cm.exception))

	def test_stale_row_outranks_a_draft_and_both_are_reported_on_one_line(self):
		with self.assertRaises(guard.StaleOperationError) as cm:
			self._run(
				_eir_doc(new=True),
				ops=[_op("MOP-1")],
				wos=[_wo(pointer="MOP-2")],
				family={},
				drafts=[_draft()],
			)
		message = str(cm.exception)
		self.assertIn("is not the current operation", message)
		self.assertIn("EIR-D-0001", message)
		self.assertIn("<br>", message)
		self.assertNotIn("\n", message)

	def test_one_stale_row_fails_the_whole_document_naming_only_its_work_order(self):
		doc = _eir_doc(rows=(("MOP-1", "MWO-1"), ("MOP-OLD", "MWO-2")), new=True)
		ops = [_op("MOP-1"), _op("MOP-OLD", mwo="MWO-2", status="Finished")]
		with self.assertRaises(guard.StaleOperationError) as cm:
			self._run(doc, ops=ops, wos=[_wo(), _wo("MWO-2", "MOP-NEW")])
		self.assertIn("MWO-2", str(cm.exception))
		self.assertNotIn("MWO-1", str(cm.exception))

	def test_stale_open_sibling_warns_but_lets_the_current_operation_proceed(self):
		family = _family_of(
			_op("MOP-1"),
			_op("MOP-STALE", status="WIP"),
			_op("MOP-DONE", status="Finished"),
		)
		self._run(_eir_doc(new=True), ops=[_op("MOP-1")], wos=[_wo()], family=family)
		self.msgprint.assert_called_once()
		message = self.msgprint.call_args.args[0]
		self.assertIn("MOP-STALE", message)
		self.assertNotIn("MOP-DONE", message)
		self.assertEqual(self.msgprint.call_args.kwargs["indicator"], "orange")

	def test_closed_siblings_raise_no_warning(self):
		family = _family_of(
			_op("MOP-1"), _op("MOP-0", status="Finished"), _op("MOP-R", status="Revert")
		)
		self._run(_eir_doc(new=True), ops=[_op("MOP-1")], wos=[_wo()], family=family)
		self.msgprint.assert_not_called()

	def test_warn_mode_lets_a_stale_row_through_with_an_error_log(self):
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(guard.frappe, "log_error") as log_error,
		):
			self._run(
				_eir_doc(new=True),
				ops=[_op("MOP-1")],
				wos=[_wo(pointer="MOP-2")],
				family={},
			)
		log_error.assert_called_once()
		# Only the warn-mode message: no sibling warning on top of a reported problem.
		self.msgprint.assert_called_once()

	def test_lock_block_takes_operations_then_work_orders(self):
		calls = []

		def lock_ops(names, wait=None):
			calls.append(("operations", sorted(names)))
			return {"MOP-1": _op("MOP-1", mwo="MWO-OF-OP")}

		def lock_wos(names, wait=None):
			calls.append(("work orders", sorted(names)))
			return {name: _wo(name) for name in names}

		with (
			patch.object(guard, "lock_manufacturing_operations", side_effect=lock_ops),
			patch.object(guard, "lock_work_orders", side_effect=lock_wos),
			patch.object(guard, "_lock_wait", return_value=15),
		):
			_mop_rows, mwo_rows = guard._lock_block(["MOP-1"], {"MWO-1", "", None})
		self.assertEqual(
			calls,
			[("operations", ["MOP-1"]), ("work orders", ["MWO-1", "MWO-OF-OP"])],
		)
		self.assertEqual(set(mwo_rows), {"MWO-1", "MWO-OF-OP"})

	def _budget_clauses(self, run, ticks, budget=15):
		"""Run ``run()`` with the REAL lock_order statements recorded (``frappe.db.sql`` faked)
		and a clock reading ``ticks`` in turn, one per statement; return each statement's wait
		clause."""
		statements = []

		def fake_sql(query, values=None, *args, **kwargs):
			statements.append(query)
			name = values[0]
			if "`tabManufacturing Operation`" in query:
				return [_op(name, mwo="MWO-1")]
			return [_wo(name)]

		clock = iter(ticks)
		with (
			patch.object(guard, "_lock_wait", return_value=budget),
			patch.object(guard, "_clock", side_effect=lambda: next(clock)),
			patch.object(frappe.db, "sql", side_effect=fake_sql),
		):
			run()
		clauses = []
		for query in statements:
			self.assertIn("FOR UPDATE", query)
			clauses.append(query.split("FOR UPDATE", 1)[1])
		return clauses

	def test_both_phases_of_the_block_wait_within_one_budget(self):
		"""F3: 15 s for the whole attempt, not per row. Each statement waits what is left
		(whole seconds, at least 1); once the deadline has passed, NOWAIT -- a free row is still
		taken, a held one fails at once (named, by the busy error)."""
		doc = _eir_doc()
		clauses = self._budget_clauses(
			lambda: guard._lock_block(
				["MOP-2", "MOP-1"], {"MWO-1", "MWO-2", "MWO-3"}, doc=doc
			),
			# MOP-1 MOP-2 | MWO-1 MWO-2 MWO-3
			[100.0, 104.2, 114.5, 115.0, 121.0],
		)
		self.assertEqual(
			clauses, [" WAIT 15", " WAIT 11", " WAIT 1", " NOWAIT", " NOWAIT"]
		)
		self.assertEqual(doc.flags[guard.LOCK_DEADLINE_FLAG], 115.0)

	def test_later_guarded_locks_of_the_same_attempt_share_the_budget(self):
		"""A Department IR's previous-operation locks (and a re-entrant second pass) spend the
		same deadline: no attempt waits 15 s more per call."""
		doc = _eir_doc()
		clauses = self._budget_clauses(
			lambda: (
				guard._lock_block(["MOP-1"], {"MWO-1"}, doc=doc),
				guard._lock_previous_operations(["MOP-0"], doc=doc),
			),
			[100.0, 101.0, 112.5],
		)
		self.assertEqual(clauses, [" WAIT 15", " WAIT 14", " WAIT 3"])

	def test_every_attempt_starts_a_budget_of_its_own(self):
		"""begin_attempt forgets the previous attempt's deadline, so the second document of a
		bulk action (one request, several saves) is not left with NOWAIT."""
		first, second = _eir_doc(name="EIR-T-0001"), _eir_doc(name="EIR-T-0002")
		with patch.object(guard, "refuse_if_movement_blocked"):
			guard.begin_attempt(first)
			guard.begin_attempt(second)
		clauses = self._budget_clauses(
			lambda: (
				guard._lock_block(["MOP-1"], set(), doc=first),
				guard._lock_block(["MOP-2"], set(), doc=second),
			),
			# first: MOP-1, MWO-1 | second: MOP-2, MWO-1
			[100.0, 116.0, 130.0, 131.0],
		)
		self.assertEqual(clauses, [" WAIT 15", " NOWAIT", " WAIT 15", " WAIT 14"])
		first.flags[guard.LOCK_DEADLINE_FLAG] = 1.0
		with patch.object(guard, "refuse_if_movement_blocked") as refuse:
			guard.begin_attempt(first, 0)
		self.assertNotIn(guard.LOCK_DEADLINE_FLAG, first.flags)
		refuse.assert_called_once_with(first, 0)

	def test_jobs_wait_the_server_default_and_zero_means_nowait_throughout(self):
		for budget, expected in ((None, ""), (0, " NOWAIT")):
			with self.subTest(budget=budget):
				self.assertEqual(
					self._budget_clauses(
						lambda: guard._lock_block(["MOP-1"], {"MWO-1"}, doc=_eir_doc()),
						[],
						budget=budget,
					),
					[expected, expected],
				)

	def test_busy_operation_lock_is_a_retryable_error(self):
		with (
			patch.object(
				guard,
				"lock_manufacturing_operations",
				side_effect=frappe.QueryTimeoutError("1205"),
			),
			patch.object(guard, "lock_work_orders") as lock_wos,
			patch.object(guard, "_lock_wait", return_value=15),
		):
			with self.assertRaises(guard.WorkOrderBusyError) as cm:
				guard._lock_block(["MOP-1"], {"MWO-1"})
		self.assertIn("MOP-1", str(cm.exception))
		lock_wos.assert_not_called()

	def test_deadlock_or_snapshot_conflict_on_the_work_order_is_a_retryable_error(self):
		with (
			patch.object(guard, "lock_manufacturing_operations", return_value={}),
			patch.object(
				guard, "lock_work_orders", side_effect=frappe.QueryDeadlockError("1020")
			),
			patch.object(guard, "_lock_wait", return_value=15),
		):
			with self.assertRaises(guard.WorkOrderBusyError) as cm:
				guard._lock_block(["MOP-1"], {"MWO-1"})
		self.assertIn("MWO-1", str(cm.exception))

	def test_a_busy_operation_is_named_with_its_work_order(self):
		"""lock_order records which row the failed statement waited for."""
		error = frappe.QueryTimeoutError("1205")
		error.lock_row_name = "MOP-2"
		with (
			patch.object(guard, "lock_manufacturing_operations", side_effect=error),
			patch.object(guard, "_lock_wait", return_value=15),
			patch.object(guard.frappe.db, "get_value", return_value="MWO-2") as read,
		):
			with self.assertRaises(guard.WorkOrderBusyError) as cm:
				guard._lock_block(["MOP-1", "MOP-2", "MOP-3"], {"MWO-1"})
		message = str(cm.exception)
		self.assertTrue(
			message.startswith(
				"Manufacturing Operation <strong>MOP-2</strong> (work order <strong>MWO-2</strong>)"
			),
			message,
		)
		self.assertNotIn("MOP-1", message)
		read.assert_called_once_with(
			"Manufacturing Operation", "MOP-2", "manufacturing_work_order"
		)

	def test_a_busy_work_order_is_named_alone(self):
		error = frappe.QueryTimeoutError("1205")
		error.lock_row_name = "MWO-2"
		with (
			patch.object(guard, "lock_manufacturing_operations", return_value={}),
			patch.object(guard, "lock_work_orders", side_effect=error),
			patch.object(guard, "_lock_wait", return_value=15),
		):
			with self.assertRaises(guard.WorkOrderBusyError) as cm:
				guard._lock_block(["MOP-1"], {"MWO-1", "MWO-2", "MWO-3"})
		message = str(cm.exception)
		self.assertTrue(
			message.startswith("Work order <strong>MWO-2</strong> is"), message
		)
		self.assertNotIn("MWO-1", message)

	def test_interactive_requests_wait_fifteen_seconds_by_default(self):
		with (
			patch.dict(frappe.local.conf, {"current_operation_lock_wait": None}),
			patch.object(frappe.local, "job", None, create=True),
		):
			self.assertEqual(guard._lock_wait(), 15)

	def test_interactive_lock_wait_is_configurable(self):
		with (
			patch.dict(frappe.local.conf, {"current_operation_lock_wait": 5}),
			patch.object(frappe.local, "job", None, create=True),
		):
			self.assertEqual(guard._lock_wait(), 5)

	def test_lock_wait_zero_means_nowait_and_bad_values_fall_back(self):
		"""0 used to become 15 silently (``0 or 15``) and a negative value meant NOWAIT."""
		for value, expected in (
			(0, 0),
			("0", 0),
			(" 7 ", 7),
			("", 15),
			(-3, 15),
			("soon", 15),
		):
			with self.subTest(value=value):
				with (
					patch.dict(
						frappe.local.conf, {"current_operation_lock_wait": value}
					),
					patch.object(frappe.local, "job", None, create=True),
				):
					self.assertEqual(guard._lock_wait(), expected)
		self.assertEqual(lock_order._lock_wait_clause(0), " NOWAIT")

	def test_background_jobs_keep_the_server_lock_wait(self):
		job = SimpleNamespace(lang_resolved=True, user="Administrator")
		with patch.object(frappe.local, "job", job, create=True):
			self.assertIsNone(guard._lock_wait())


class TestCurrentOperationGuardSnapshotCheck(IntegrationTestCase):
	"""_refuse_if_snapshot_older: the block refreshes nothing. A submit / cancel whose row
	operations changed between its snapshot and its locking read would clone balances and
	recompute weights from the old snapshot, so it is refused as busy (the retry reads afresh)."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def tearDown(self):
		frappe.clear_messages()
		return super().tearDown()

	def _check(self, locked, seen, names=None):
		"""``locked``: the block's rows by requested key; ``seen``: ``{name: modified}`` the
		plain read returns."""
		self.reads = []

		def fake_sql(query, values=None, *args, **kwargs):
			self.reads.append((" ".join(query.split()), values))
			return [(n, m) for n, m in seen.items() if n in values["names"]]

		with (
			patch.object(frappe.db, "sql", side_effect=fake_sql),
			patch.object(
				guard,
				"_work_order_hint",
				side_effect=lambda mop: "(work order <strong>MWO-1</strong>)",
			),
		):
			guard._refuse_if_snapshot_older(
				locked, list(locked) if names is None else names
			)

	def test_an_unchanged_operation_passes(self):
		then = now_datetime()
		self._check({"MOP-1": _op("MOP-1", modified=then)}, {"MOP-1": then})
		self.assertEqual(
			self.reads,
			[
				(
					"SELECT name, modified FROM `tabManufacturing Operation` WHERE name IN "
					"%(names)s",
					{"names": ["MOP-1"]},
				)
			],
		)

	def test_an_operation_written_while_this_transaction_waited_is_busy(self):
		then = now_datetime()
		locked = {"MOP-1": _op("MOP-1", modified=add_to_date(then, seconds=2))}
		with self.assertRaises(guard.WorkOrderBusyError) as cm:
			self._check(locked, {"MOP-1": then})
		message = str(cm.exception)
		self.assertIn(
			"<strong>MOP-1</strong> (work order <strong>MWO-1</strong>)", message
		)
		self.assertIn("changed by another transaction while this one waited", message)

	def test_an_operation_the_snapshot_does_not_know_is_busy_too(self):
		with self.assertRaises(guard.WorkOrderBusyError):
			self._check({"MOP-1": _op("MOP-1", modified=now_datetime())}, {})

	def test_rows_are_compared_under_their_own_spelling(self):
		"""The collation ignores case; the block keys rows by the requested spelling."""
		then = now_datetime()
		self._check({"mop-1": _op("MOP-1", modified=then)}, {"MOP-1": then})
		self.assertEqual(self.reads[0][1], {"names": ["MOP-1"]})

	def test_several_changed_operations_are_named_in_one_message(self):
		then = now_datetime()
		later = add_to_date(then, seconds=1)
		locked = {n: _op(n, modified=later) for n in ("MOP-1", "MOP-2", "MOP-3")}
		with self.assertRaises(guard.WorkOrderBusyError) as cm:
			self._check(locked, {"MOP-1": then, "MOP-2": later, "MOP-3": then})
		message = str(cm.exception)
		for token in ("MOP-1", "MOP-3"):
			self.assertIn(token, message)
		self.assertNotIn("MOP-2", message)

	def test_only_the_named_operations_are_compared_and_none_reads_nothing(self):
		then = now_datetime()
		locked = {
			"MOP-1": _op("MOP-1", modified=then),
			"MOP-0": _op("MOP-0", modified=then),
		}
		self._check(locked, {"MOP-1": then}, names=["MOP-1", "MOP-X", None])
		self.assertEqual(self.reads[0][1], {"names": ["MOP-1"]})
		self._check({}, {})
		self.assertEqual(self.reads, [])


class TestCurrentOperationGuardEntryPoints(IntegrationTestCase):
	"""Which phase and lock mode every hook entry point asks _check for, the terminal draft
	re-check and on_discard."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()
		frappe.get_meta("Employee IR")

	def _dispatch(self, entry, doc):
		with patch.object(guard, "_check") as check:
			entry(doc)
		return check

	@staticmethod
	def _prelocked(doc):
		doc.flags[guard.RECEIVE_PRELOCKS_FLAG] = True
		return doc

	def test_before_insert_takes_the_lock_block_for_every_new_document(self):
		cases = (
			("Issue draft", _eir_doc("Issue", new=True), ("save", True)),
			("Issue REST", _eir_doc("Issue", new=True, docstatus=1), ("submit", True)),
			("Receive draft", _eir_doc("Receive", new=True), ("save", True)),
			# REST insert-and-submit of a Receive: the block right after its Tree / Series /
			# Bin pre-locks (taken by the controller just before), still before naming ...
			(
				"Receive REST, pre-locked",
				self._prelocked(_eir_doc("Receive", new=True, docstatus=1)),
				("submit", True),
			),
			# ... and without them only an advisory check (on_submit_receive decides).
			(
				"Receive REST, not pre-locked",
				_eir_doc("Receive", new=True, docstatus=1),
				("submit", False),
			),
		)
		for label, doc, expected in cases:
			with self.subTest(label):
				self.assertEqual(
					_check_args(self._dispatch(guard.on_before_insert, doc)), expected
				)

	def test_entry_points_ignore_other_doctypes(self):
		other = _GuardDoc(
			doctype="Stock Entry", type="Issue", docstatus=1, flags=FrappeDict()
		)
		for entry in (
			guard.on_before_insert,
			guard.on_before_validate,
			guard.check_after_receive_prelocks,
			guard.preflight,
		):
			with self.subTest(entry=entry.__name__):
				with patch.object(guard, "_lock_block") as lock_block, patch.object(
					guard, "_plain_rows"
				) as plain_rows:
					entry(other)
				lock_block.assert_not_called()
				plain_rows.assert_not_called()
		with patch.object(guard, "_lock_block") as lock_block:
			guard.guard_cancel(other)
		lock_block.assert_not_called()

	def test_before_validate_skips_inserts_which_before_insert_already_guarded(self):
		doc = _eir_doc(new=True)
		doc.flags.in_insert = True
		self._dispatch(guard.on_before_validate, doc).assert_not_called()

	def test_before_validate_rechecks_a_draft_only_when_its_references_changed(self):
		self._dispatch(
			guard.on_before_validate, _eir_doc(before=_eir_doc())
		).assert_not_called()
		moved = _eir_doc(rows=(("MOP-2", "MWO-1"),), before=_eir_doc())
		self.assertEqual(
			_check_args(self._dispatch(guard.on_before_validate, moved)), ("save", True)
		)

	def test_before_validate_guards_every_submit(self):
		for label, kind, prelocked, expected in (
			("Issue", "Issue", False, ("submit", True)),
			("Receive after its pre-locks", "Receive", True, ("submit", True)),
			("Receive without them", "Receive", False, ("submit", False)),
		):
			with self.subTest(label):
				doc = _eir_doc(kind, docstatus=1, before=_eir_doc(kind))
				if prelocked:
					self._prelocked(doc)
				self.assertEqual(
					_check_args(self._dispatch(guard.on_before_validate, doc)), expected
				)

	def test_each_attempt_forgets_what_a_previous_attempt_decided(self):
		"""A failed attempt's decision must not let a retry of the same instance skip its checks
		(check_after_receive_prelocks) or run a stale terminal re-check (final_draft_check)."""
		for entry, doc in (
			(guard.on_before_insert, _eir_doc(new=True)),
			(guard.on_before_validate, _eir_doc(before=_eir_doc())),
			(guard.on_before_validate, _eir_doc("Receive", docstatus=1)),
		):
			with self.subTest(entry=entry.__name__, docstatus=doc.docstatus):
				doc.flags[guard.GUARDED_FLAG] = {"phase": "submit", "docstatus": 1}
				self._dispatch(entry, doc)
				self.assertNotIn(guard.GUARDED_FLAG, doc.flags)

	def test_before_validate_leaves_cancelled_documents_to_the_cancel_guard(self):
		self._dispatch(
			guard.on_before_validate, _eir_doc(docstatus=2)
		).assert_not_called()

	def _early_block(self, doc, stored, stored_rows=(("MOP-1", "MWO-1"),), previous=()):
		"""lock_before_own_rows with the stored version read as ``stored`` (``None`` = absent)."""
		self.blocked = []

		def lock_block(mops, mwos, doc=None):
			self.blocked.append((sorted(mops), sorted(mwos)))
			return {}, {}

		def get_value(doctype, name, fields, as_dict=False):
			return None if stored is None else FrappeDict(stored)

		with (
			patch.object(guard.frappe.db, "get_value", side_effect=get_value),
			patch.object(
				guard.frappe,
				"get_all",
				return_value=[
					FrappeDict(
						manufacturing_operation=mop, manufacturing_work_order=mwo
					)
					for mop, mwo in stored_rows
				],
			),
			patch.object(guard, "_previous_operations", return_value=list(previous)),
			patch.object(guard, "_lock_block", side_effect=lock_block),
		):
			guard.lock_before_own_rows(doc)
		return self.blocked

	@staticmethod
	def _stored(**fields):
		values = dict(
			docstatus=0,
			type="Issue",
			company="Co-T",
			department="Dept-T",
			operation="Op-T",
			employee="EMP-T",
			subcontracting="No",
			subcontractor=None,
		)
		values.update(fields)
		return values

	def test_early_block_for_a_submit_before_frappe_locks_the_own_rows(self):
		self.assertEqual(
			self._early_block(_eir_doc(docstatus=1), self._stored()),
			[(["MOP-1"], ["MWO-1"])],
		)

	def test_early_block_for_a_receive_submit_only_after_its_pre_locks(self):
		doc = _eir_doc("Receive", docstatus=1)
		self.assertEqual(self._early_block(doc, self._stored(type="Receive")), [])
		self._prelocked(doc)
		self.assertEqual(
			self._early_block(doc, self._stored(type="Receive")),
			[(["MOP-1"], ["MWO-1"])],
		)

	def test_early_block_for_a_draft_save_only_when_its_references_changed(self):
		self.assertEqual(self._early_block(_eir_doc(), self._stored()), [])
		moved = _eir_doc(rows=(("MOP-2", "MWO-2"),))
		self.assertEqual(
			self._early_block(moved, self._stored()), [(["MOP-2"], ["MWO-1", "MWO-2"])]
		)
		other_employee = _eir_doc(employee="EMP-X")
		self.assertEqual(
			self._early_block(other_employee, self._stored()), [(["MOP-1"], ["MWO-1"])]
		)

	def test_early_block_of_a_department_ir_save_includes_previous_operations(self):
		doc = _GuardDoc(
			doctype="Department IR",
			name="DIR-T-0001",
			type="Issue",
			docstatus=0,
			company="Co-T",
			current_department="Dept-T",
			next_department="Dept-B",
			receive_against=None,
			department_ir_operation=[
				FrappeDict(
					idx=1,
					manufacturing_operation="MOP-5",
					manufacturing_work_order="MWO-1",
				)
			],
			flags=FrappeDict(),
		)
		stored = dict(
			docstatus=0,
			type="Issue",
			company="Co-T",
			current_department="Dept-T",
			next_department="Dept-B",
			receive_against=None,
		)
		self.assertEqual(
			self._early_block(doc, stored, stored_rows=(), previous=["MOP-4"]),
			[(["MOP-4", "MOP-5"], ["MWO-1"])],
		)

	def test_a_department_ir_re_saved_unchanged_locks_only_its_previous_operations(
		self,
	):
		"""update_previous_mop_data still writes them: lock them before the own rows, alone."""
		doc = _GuardDoc(
			doctype="Department IR",
			name="DIR-T-0001",
			type="Issue",
			docstatus=0,
			company="Co-T",
			current_department="Dept-T",
			next_department="Dept-B",
			receive_against=None,
			department_ir_operation=[
				FrappeDict(
					idx=1,
					manufacturing_operation="MOP-5",
					manufacturing_work_order="MWO-1",
				)
			],
			flags=FrappeDict(),
		)
		stored = dict(
			docstatus=0,
			type="Issue",
			company="Co-T",
			current_department="Dept-T",
			next_department="Dept-B",
			receive_against=None,
		)
		with patch.object(guard, "lock_manufacturing_operations") as lock_ops:
			self.assertEqual(
				self._early_block(
					doc, stored, stored_rows=(("MOP-5", "MWO-1"),), previous=["MOP-4"]
				),
				[],
			)
		lock_ops.assert_called_once()
		self.assertEqual(lock_ops.call_args.args[0], ["MOP-4"])
		# a discard writes nothing
		doc._action = "discard"
		with patch.object(guard, "lock_manufacturing_operations") as lock_ops:
			self._early_block(
				doc, stored, stored_rows=(("MOP-5", "MWO-1"),), previous=["MOP-4"]
			)
		lock_ops.assert_not_called()

	def test_no_early_block_for_cancels_new_documents_warn_mode_or_other_doctypes(self):
		for label, doc, stored, conf in (
			("cancel", _eir_doc(docstatus=2), self._stored(docstatus=1), None),
			(
				"update after submit",
				_eir_doc(docstatus=1),
				self._stored(docstatus=1),
				None,
			),
			("gone", _eir_doc(docstatus=1), None, None),
			("new document", _eir_doc(docstatus=1, new=True), self._stored(), None),
			(
				"warn mode",
				_eir_doc(docstatus=1),
				self._stored(),
				{"current_operation_guard": "warn"},
			),
			(
				"other doctype",
				_GuardDoc(doctype="Stock Entry", docstatus=1, flags=FrappeDict()),
				self._stored(),
				None,
			),
		):
			with self.subTest(label), patch.dict(frappe.local.conf, conf or {}):
				self.assertEqual(self._early_block(doc, stored), [])

	def test_receive_prelock_check_is_the_authoritative_locked_submit_check(self):
		check = self._dispatch(
			guard.check_after_receive_prelocks, _eir_doc("Receive", docstatus=1)
		)
		self.assertEqual(_check_args(check), ("submit", True))

	def test_receive_prelock_check_is_skipped_once_decided_under_the_block(self):
		"""before_validate / before_insert already took the block right after the pre-locks."""
		doc = _eir_doc("Receive", docstatus=1)
		doc.flags[guard.GUARDED_FLAG] = {"phase": "submit", "docstatus": 1, "mwos": []}
		self._dispatch(guard.check_after_receive_prelocks, doc).assert_not_called()
		# a draft-save decision does not count for the submit
		doc.flags[guard.GUARDED_FLAG] = {"phase": "save", "docstatus": 0, "mwos": []}
		self._dispatch(guard.check_after_receive_prelocks, doc).assert_called_once()

	def test_warn_mode_takes_no_lock_on_any_save_or_submit_path(self):
		"""Kill switch = the pre-guard locking: no MOP / MWO lock, no NOWAIT re-check, no busy."""
		forbidden = AssertionError("a lock was taken in warn mode")
		docs = (
			(guard.on_before_insert, _eir_doc(new=True)),
			(guard.on_before_insert, _eir_doc("Issue", new=True, docstatus=1)),
			(
				guard.on_before_validate,
				_eir_doc(rows=(("MOP-2", "MWO-1"),), before=_eir_doc()),
			),
			(guard.on_before_validate, _eir_doc(docstatus=1, before=_eir_doc())),
			(
				guard.on_before_validate,
				self._prelocked(_eir_doc("Receive", docstatus=1, before=_eir_doc())),
			),
			(guard.check_after_receive_prelocks, _eir_doc("Receive", docstatus=1)),
		)
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(guard, "lock_manufacturing_operations", side_effect=forbidden),
			patch.object(guard, "lock_work_orders", side_effect=forbidden),
			patch.object(guard, "_confirm_drafts", side_effect=forbidden),
			patch.object(guard, "_confirm_successors", side_effect=forbidden),
			patch.object(
				guard,
				"_plain_rows",
				return_value=({"MOP-1": _op("MOP-1")}, {"MWO-1": _wo()}),
			) as plain,
			patch.object(guard, "_family", return_value={}),
			patch.object(guard, "outstanding_issue_drafts", return_value=[]) as drafts,
			patch.object(guard.frappe, "log_error"),
			patch.object(guard.frappe, "msgprint"),
		):
			for entry, doc in docs:
				with self.subTest(
					entry=entry.__name__, type=doc.type, docstatus=doc.docstatus
				):
					plain.reset_mock()
					entry(doc)
					plain.assert_called_once()
					self.assertNotIn(guard.GUARDED_FLAG, doc.flags)
					for call in drafts.call_args_list:
						self.assertFalse(call.kwargs.get("locking"))
			# and the terminal re-check is off even for a block remembered from enforce mode
			guarded = _eir_doc(docstatus=1)
			guarded.flags[guard.GUARDED_FLAG] = {
				"phase": "submit",
				"docstatus": 1,
				"mwos": ["MWO-1"],
			}
			drafts.reset_mock()
			guard.final_draft_check(guarded)
			drafts.assert_not_called()

	def test_preflight_is_a_lock_free_submit_check(self):
		check = self._dispatch(guard.preflight, _eir_doc(docstatus=1))
		self.assertEqual(_check_args(check), ("submit", False))

	def _final(self, doc, drafts=()):
		with patch.object(
			guard,
			"outstanding_issue_drafts",
			return_value=[FrappeDict(d) for d in drafts],
		) as read:
			guard.final_draft_check(doc)
		return read

	def _guarded(self, docstatus=1, guarded_docstatus=1, name="EIR-T-0009"):
		doc = _eir_doc(name=name, docstatus=docstatus)
		doc.flags.current_operation_guarded = {
			"phase": "submit" if guarded_docstatus else "save",
			"docstatus": guarded_docstatus,
			"mwos": ["MWO-1", "MWO-2"],
		}
		return doc

	def test_final_check_skips_a_document_the_guard_never_locked(self):
		self._final(_eir_doc()).assert_not_called()

	def test_final_check_skips_a_block_taken_for_another_docstatus(self):
		self._final(self._guarded(docstatus=1, guarded_docstatus=0)).assert_not_called()

	def test_final_check_is_a_locking_read_that_excludes_the_document(self):
		read = self._final(self._guarded())
		read.assert_called_once_with(
			["MWO-1", "MWO-2"], exclude="EIR-T-0009", locking=True
		)

	def test_final_check_refuses_when_a_draft_appeared_meanwhile(self):
		with self.assertRaises(guard.OutstandingDraftError) as cm:
			self._final(
				self._guarded(), drafts=[_draft("EIR-LATE", "MWO-2", "o@example.com")]
			)
		self.assertIn("EIR-LATE", str(cm.exception))

	def test_final_check_passes_when_no_draft_appeared(self):
		self._final(
			self._guarded(docstatus=0, guarded_docstatus=0)
		).assert_called_once()

	def test_discard_marks_every_child_table_discarded(self):
		meta = MagicMock()
		meta.get_table_fields.return_value = [
			FrappeDict(
				fieldname="employee_ir_operations", options="Employee IR Operation"
			),
			FrappeDict(
				fieldname="employee_loss_details", options="Employee Loss Details"
			),
		]
		with (
			patch.object(guard.frappe, "get_meta", return_value=meta),
			patch.object(guard.frappe.db, "sql") as sql,
		):
			guard.on_discard(_eir_doc(name="EIR-T-0011"))
		self.assertEqual(sql.call_count, 2)
		first, second = sql.call_args_list
		self.assertIn("UPDATE `tabEmployee IR Operation`", first.args[0])
		self.assertIn("SET docstatus = 2", first.args[0])
		self.assertEqual(
			first.args[1], ("EIR-T-0011", "Employee IR", "employee_ir_operations")
		)
		self.assertIn("UPDATE `tabEmployee Loss Details`", second.args[0])
		self.assertEqual(
			second.args[1], ("EIR-T-0011", "Employee IR", "employee_loss_details")
		)

	def test_discard_covers_the_real_employee_ir_operation_table(self):
		meta = frappe.get_meta(
			"Employee IR"
		)  # cached in setUpClass, before frappe.db is patched
		with (
			patch.object(guard.frappe, "get_meta", return_value=meta),
			patch.object(guard.frappe.db, "sql") as sql,
		):
			guard.on_discard(_eir_doc(name="EIR-T-0012"))
		by_field = {c.args[1][2]: c.args[0] for c in sql.call_args_list}
		self.assertIn("`tabEmployee IR Operation`", by_field["employee_ir_operations"])


class TestCurrentOperationGuardEmployeeCancel(IntegrationTestCase):
	"""guard_cancel: an Employee IR cancel may only undo a transition that is still current."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _cancel(
		self, doc, ops, wos, minted=None, family=None, holding=None, others=None
	):
		"""Run guard_cancel; ``minted`` maps a row operation to the one its Receive minted;
		``holding`` is the submitted Issue a message names for a held operation; ``others``:
		what other_submitted_issues reports (other submitted Issues per operation)."""
		self.locked = []
		self.minted_filters = []
		self.confirmed_for = []
		self.other_reads = []
		self.confirmed_issues = []
		self.snapshot_checks = []

		def other_issues(mops, exclude=None):
			self.other_reads.append((sorted(m for m in mops if m), exclude))
			return others or {}

		def lock_block(mops, mwos, doc=None):
			self.locked.append((sorted(mops), sorted(mwos)))
			return {op.name: op for op in ops}, {wo.name: wo for wo in wos}

		def get_value(doctype, filters=None, *args, **kwargs):
			if doctype == "Manufacturing Operation" and isinstance(filters, dict):
				self.minted_filters.append(filters)
				return (minted or {}).get(filters.get("previous_mop"))
			return None

		def confirm_successors(family, mops):
			self.confirmed_for.append(sorted(mops))
			return family

		with (
			patch.object(guard, "_lock_block", side_effect=lock_block),
			patch.object(guard, "_after_locks"),
			patch.object(
				guard,
				"_family",
				return_value=_family_of(*ops) if family is None else family,
			),
			patch.object(guard, "_confirm_successors", side_effect=confirm_successors),
			patch.object(guard, "_holding_issue", return_value=holding),
			patch.object(guard.frappe.db, "get_value", side_effect=get_value),
			patch.object(guard, "other_submitted_issues", side_effect=other_issues),
			patch.object(
				guard,
				"_confirm_submitted_issues",
				side_effect=lambda by_mop: self.confirmed_issues.append(by_mop)
				or by_mop,
			),
			patch.object(
				guard,
				"_refuse_if_snapshot_older",
				side_effect=lambda rows, names: self.snapshot_checks.append(
					sorted(names)
				),
			),
		):
			guard.guard_cancel(doc)

	def _issue(self, **header):
		return _eir_doc("Issue", name="EIR-I-0001", docstatus=2, **header)

	def _held(self, **fields):
		values = {"status": "WIP", "operation": "Op-T", "employee": "EMP-T"}
		values.update(fields)
		return _op("MOP-1", **values)

	def test_issue_cancel_is_allowed_while_the_operation_is_still_held(self):
		for status in ("WIP", "QC Pending", "QC Completed", "On Hold"):
			with self.subTest(status=status):
				self._cancel(self._issue(), [self._held(status=status)], [_wo()])
				self.assertEqual(self.locked, [(["MOP-1"], ["MWO-1"])])
				self.assertEqual(self.minted_filters, [])

	def test_issue_cancel_is_refused_after_the_operation_was_received(self):
		received = self._held(status="Finished")
		minted = _op("MOP-2", previous_mop="MOP-1")
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._issue(),
				[received],
				[_wo(pointer="MOP-2")],
				family=_family_of(received, minted),
			)
		for token in (
			"EIR-I-0001",
			"MWO-1",
			"MOP-2",
			"would reopen <strong>MOP-1</strong>",
		):
			self.assertIn(token, str(cm.exception))

	def test_issue_cancel_is_refused_once_the_operation_changed_hands(self):
		"""The work order did not move: the message says what happened to the operation (and
		which Issue holds it now) instead of "has moved on to" the very same operation."""
		for label, fields, expected in (
			(
				"other employee",
				{"employee": "EMP-X"},
				"<strong>MOP-1</strong> of work order <strong>MWO-1</strong> is WIP with "
				"<strong>EMP-X</strong> for <strong>Op-T</strong> (Employee IR "
				"<strong>EIR-I-0002</strong>)",
			),
			(
				"other operation",
				{"operation": "Op-X"},
				"is WIP with <strong>EMP-T</strong> for <strong>Op-X</strong>",
			),
			(
				"already unwound",
				{"status": "Not Started", "operation": None, "employee": None},
				"is Not Started in <strong>Dept-T</strong>",
			),
		):
			with self.subTest(label=label):
				with self.assertRaises(guard.HistoryRewriteError) as cm:
					self._cancel(
						self._issue(),
						[self._held(**fields)],
						[_wo()],
						holding="EIR-I-0002",
					)
				message = str(cm.exception)
				self.assertIn(expected, message)
				self.assertNotIn("has moved on", message)
				self.assertIn("would reopen <strong>MOP-1</strong>", message)

	def test_issue_cancel_of_a_double_issued_operation_is_refused(self):
		"""A second submitted Issue still lists the operation (legacy double issue): the cancel
		would reset it to Not Started under that Issue. Only the reviewed repair cancels it."""
		others = {
			"MOP-1": [
				FrappeDict(employee_ir="EIR-I-0009", issue_submitted_on=now_datetime())
			]
		}
		with self.assertRaises(guard.AmbiguousOperationError) as cm:
			self._cancel(self._issue(), [self._held()], [_wo()], others=others)
		message = str(cm.exception)
		for token in ("EIR-I-0001", "MOP-1", "MWO-1", "EIR-I-0009", "audit"):
			self.assertIn(token, message)
		self.assertEqual(self.other_reads, [(["MOP-1"], "EIR-I-0001")])
		self.assertEqual(self.confirmed_issues, [others])
		self.assertEqual(self.snapshot_checks, [])
		# the reviewed repair is the way out: it bypasses the rule (and reads nothing)
		allowed = {guard.REVIEWED_REPAIR_FLAG: {("Employee IR", "EIR-I-0001")}}
		with patch.dict(frappe.local.flags, allowed):
			self._cancel(self._issue(), [self._held()], [_wo()], others=others)
		self.assertEqual(self.other_reads, [])

	def test_a_second_issue_cancelled_meanwhile_is_dropped_by_a_locking_read(self):
		"""other_submitted_issues reads the snapshot; the reviewed repair may have cancelled the
		other Issue while this cancel waited on the block. Only a locking read sees that."""
		by_mop = {
			"MOP-1": [FrappeDict(employee_ir="EIR-I-0009", issue_submitted_on=None)],
			"MOP-2": [FrappeDict(employee_ir="EIR-I-0010", issue_submitted_on=None)],
		}
		with patch.object(guard.frappe.db, "sql", return_value=["EIR-I-0010"]) as sql:
			kept = guard._confirm_submitted_issues(by_mop)
		self.assertEqual(kept, {"MOP-2": by_mop["MOP-2"]})
		query, values = sql.call_args.args[0], sql.call_args.args[1]
		self.assertIn("LOCK IN SHARE MODE NOWAIT", query)
		self.assertIn("docstatus = 1", query)
		self.assertEqual(values, {"names": ["EIR-I-0009", "EIR-I-0010"]})

	def test_a_second_issue_locked_by_another_transaction_is_busy(self):
		by_mop = {
			"MOP-1": [FrappeDict(employee_ir="EIR-I-0009", issue_submitted_on=None)]
		}
		with patch.object(
			guard.frappe.db, "sql", side_effect=frappe.QueryTimeoutError("lock wait")
		):
			with self.assertRaises(guard.WorkOrderBusyError):
				guard._confirm_submitted_issues(by_mop)

	def test_no_second_issue_reads_nothing(self):
		with patch.object(guard.frappe.db, "sql") as sql:
			self.assertEqual(guard._confirm_submitted_issues({}), {})
		sql.assert_not_called()

	def test_only_employee_issue_cancels_look_for_a_second_issue(self):
		received = _op("MOP-1", status="Finished", operation="Op-T", employee="EMP-T")
		minted = _op("MOP-2", previous_mop="MOP-1", employee_ir="EIR-R-0001")
		self._cancel(
			self._receive(),
			[received, minted],
			[_wo(pointer="MOP-2")],
			minted={"MOP-1": "MOP-2"},
		)
		self.assertEqual(self.other_reads, [])

	def test_an_allowed_cancel_compares_its_snapshot_and_a_refused_one_does_not(self):
		self._cancel(self._issue(), [self._held()], [_wo()])
		self.assertEqual(self.snapshot_checks, [["MOP-1"]])
		with self.assertRaises(guard.HistoryRewriteError):
			self._cancel(
				self._issue(),
				[self._held(status="Finished")],
				[_wo(pointer="MOP-2")],
				family={},
			)
		self.assertEqual(self.snapshot_checks, [])

	def test_a_stale_issue_cancel_points_to_the_audit_not_to_a_reversal(self):
		"""The Issue was submitted AFTER the work order moved on (the 2026-10-05 incident): the
		usual "reverse the later transactions first" would destroy legitimate work."""
		submitted = now_datetime()
		reopened = self._held()
		successor = _op(
			"MOP-2",
			previous_mop="MOP-1",
			status="Finished",
			creation=add_to_date(submitted, hours=-30),
		)
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._issue(issue_submitted_on=submitted),
				[reopened],
				[_wo(pointer="MOP-3")],
				family=_family_of(
					reopened,
					successor,
					_op("MOP-3", previous_mop="MOP-2", department="Dept-Z"),
				),
			)
		message = str(cm.exception)
		self.assertIn(
			"it was submitted after work order <strong>MWO-1</strong>", message
		)
		self.assertIn("current-operation audit", message)
		self.assertIn("<strong>MOP-3</strong>", message)
		self.assertNotIn("Reverse the later transactions", message)

	def test_an_issue_received_after_its_submit_keeps_the_reversal_advice(self):
		submitted = now_datetime()
		received = self._held(status="Finished")
		minted = _op(
			"MOP-2", previous_mop="MOP-1", creation=add_to_date(submitted, hours=2)
		)
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._issue(issue_submitted_on=submitted),
				[received],
				[_wo(pointer="MOP-2")],
				family=_family_of(received, minted),
			)
		self.assertIn("Reverse the later transactions first", str(cm.exception))
		self.assertNotIn("submitted after", str(cm.exception))

	def test_issue_cancel_ignores_a_reverted_successor(self):
		"""kggk_uat LIFO: the Receive of this Issue was cancelled, its minted operation kept as
		Revert history -- the Issue cancel must still be allowed."""
		held = self._held()
		reverted = _op(
			"MOP-2",
			previous_mop="MOP-1",
			department_ir_status="Revert",
			status="Not Started",
		)
		self._cancel(self._issue(), [held], [_wo()], family=_family_of(held, reverted))
		self.assertEqual(self.confirmed_for, [["MOP-1"]])

	def test_successors_are_confirmed_for_the_current_operations(self):
		self._cancel(self._issue(), [self._held()], [_wo(pointer="MOP-1")])
		self.assertEqual(self.confirmed_for, [["MOP-1"]])

	def test_subcontracting_issue_cancel_matches_the_subcontractor(self):
		doc = self._issue(subcontracting="Yes", subcontractor="SUP-1", employee=None)
		self._cancel(doc, [self._held(employee=None, subcontractor="SUP-1")], [_wo()])
		with self.assertRaises(guard.HistoryRewriteError):
			self._cancel(
				doc, [self._held(employee=None, subcontractor="SUP-2")], [_wo()]
			)

	def test_issue_cancel_is_refused_when_the_operation_already_has_a_successor(self):
		family = _family_of(self._held(), _op("MOP-2", previous_mop="MOP-1"))
		with self.assertRaises(guard.HistoryRewriteError):
			self._cancel(self._issue(), [self._held()], [_wo()], family=family)

	def test_cancel_guard_ignores_warn_mode(self):
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(guard.frappe, "log_error") as log_error,
		):
			with self.assertRaises(guard.HistoryRewriteError):
				self._cancel(
					self._issue(),
					[self._held(status="Finished")],
					[_wo(pointer="MOP-2")],
					family={},
				)
		log_error.assert_not_called()

	def test_reviewed_repair_bypasses_the_rule_but_still_takes_the_block(self):
		allowed = {guard.REVIEWED_REPAIR_FLAG: {("Employee IR", "EIR-I-0001")}}
		with patch.dict(frappe.local.flags, allowed):
			self._cancel(
				self._issue(),
				[self._held(status="Finished")],
				[_wo(pointer="MOP-2")],
				family={},
			)
		self.assertEqual(self.locked, [(["MOP-1"], ["MWO-1"])])

	def test_reviewed_repair_flag_covers_only_the_documents_it_names(self):
		allowed = {guard.REVIEWED_REPAIR_FLAG: {("Employee IR", "EIR-OTHER")}}
		with patch.dict(frappe.local.flags, allowed):
			with self.assertRaises(guard.HistoryRewriteError):
				self._cancel(
					self._issue(),
					[self._held(status="Finished")],
					[_wo(pointer="MOP-2")],
					family={},
				)

	def _receive(self):
		return _eir_doc("Receive", name="EIR-R-0001", docstatus=2)

	def test_receive_cancel_is_allowed_while_the_minted_operation_is_untouched(self):
		received = _op("MOP-1", status="Finished", operation="Op-T", employee="EMP-T")
		minted = _op("MOP-2", previous_mop="MOP-1", employee_ir="EIR-R-0001")
		self._cancel(
			self._receive(),
			[received, minted],
			[_wo(pointer="MOP-2")],
			minted={"MOP-1": "MOP-2"},
		)
		self.assertEqual(
			self.minted_filters,
			[
				{
					"employee_ir": "EIR-R-0001",
					"previous_mop": "MOP-1",
					"manufacturing_work_order": "MWO-1",
				}
			],
		)
		self.assertEqual(self.locked, [(["MOP-1", "MOP-2"], ["MWO-1"])])

	def test_receive_cancel_is_refused_once_the_minted_operation_was_used(self):
		"""The cancel would delete the minted operation and reopen the SOURCE (row) operation:
		the message names the source, and says what happened to the minted one."""
		received = _op("MOP-1", status="Finished")
		moved_on = _op("MOP-3", previous_mop="MOP-2", department_ir_status="In-Transit")
		for label, minted_fields, pointer, extra, expected in (
			(
				"issued to the next employee",
				{"status": "WIP", "operation": "Op-NEXT", "employee": "EMP-NEXT"},
				"MOP-2",
				(),
				"<strong>MOP-2</strong> of work order <strong>MWO-1</strong> is WIP with "
				"<strong>EMP-NEXT</strong> for <strong>Op-NEXT</strong> (Employee IR "
				"<strong>EIR-I-0009</strong>)",
			),
			(
				"sent to another department",
				{"status": "Finished"},
				"MOP-3",
				(moved_on,),
				"work order <strong>MWO-1</strong> has moved on to <strong>MOP-3</strong>",
			),
			(
				"in transit",
				{
					"department_ir_status": "In-Transit",
					"department_issue_id": "DIR-I-1",
				},
				"MOP-2",
				(),
				"<strong>MOP-2</strong> of work order <strong>MWO-1</strong> is in transit "
				"(<strong>DIR-I-1</strong>)",
			),
		):
			with self.subTest(label=label):
				minted = _op("MOP-2", previous_mop="MOP-1", **minted_fields)
				with self.assertRaises(guard.HistoryRewriteError) as cm:
					self._cancel(
						self._receive(),
						[received, minted],
						[_wo(pointer=pointer)],
						minted={"MOP-1": "MOP-2"},
						family=_family_of(received, minted, *extra),
						holding="EIR-I-0009",
					)
				message = str(cm.exception)
				self.assertIn(expected, message)
				self.assertIn("would reopen <strong>MOP-1</strong>", message)
				self.assertNotIn("would reopen <strong>MOP-2</strong>", message)

	def test_receive_cancel_is_refused_when_its_minted_operation_is_gone(self):
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._receive(), [_op("MOP-1", status="Finished")], [_wo()], minted={}
			)
		self.assertIn("no longer exists", str(cm.exception))
		self.assertIn("would reopen <strong>MOP-1</strong>", str(cm.exception))

	def test_receive_cancel_ignores_a_reverted_successor_of_its_minted_operation(self):
		"""kggk_uat LIFO: a Department Issue of the minted operation was cancelled first and
		left its own minted operation as Revert history, previous_mop = this Receive's."""
		received = _op("MOP-1", status="Finished")
		minted = _op("MOP-2", previous_mop="MOP-1", employee_ir="EIR-R-0001")
		reverted = _op(
			"MOP-3",
			previous_mop="MOP-2",
			department_ir_status="Revert",
			status="Not Started",
		)
		self._cancel(
			self._receive(),
			[received, minted],
			[_wo(pointer="MOP-2")],
			minted={"MOP-1": "MOP-2"},
			family=_family_of(received, minted, reverted),
		)
		# ...while a live one still refuses
		live = _op("MOP-3", previous_mop="MOP-2", department_ir_status="In-Transit")
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._receive(),
				[received, minted],
				[_wo(pointer="MOP-2")],
				minted={"MOP-1": "MOP-2"},
				family=_family_of(received, minted, live),
			)
		self.assertIn("already has a later operation", str(cm.exception))


class TestEmployeeIRCurrentOperationWiring(IntegrationTestCase):
	"""Where EmployeeIR calls the guard, and that nothing moves before it does."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def tearDown(self):
		frappe.clear_messages()
		return super().tearDown()

	def test_before_insert_hands_the_new_document_to_the_guard(self):
		calls = []
		doc = _eir_doc(new=True)
		with (
			patch.object(
				guard, "begin_attempt", side_effect=_rec(calls, "begin attempt")
			),
			patch.object(guard, "on_before_insert", side_effect=_rec(calls, "guard")),
		):
			EmployeeIR.before_insert(doc)
		self.assertEqual(calls, ["begin attempt", "guard"])

	def test_before_validate_guards_a_submit_before_its_docstatus_early_return(self):
		"""The legacy duplicate check never ran at submit; the guard must."""
		calls = []
		with (
			patch.object(guard, "on_before_validate", side_effect=_rec(calls, "guard")),
			patch.object(
				frappe.db, "get_value", side_effect=_rec(calls, "legacy read")
			),
			patch(
				f"{_EIR_MODULE}.validate_duplication_and_gr_wt",
				side_effect=_rec(calls, "legacy check"),
			),
		):
			EmployeeIR.before_validate(_eir_doc(docstatus=1))
		self.assertEqual(calls, ["guard"])

	def test_before_validate_guards_a_draft_save_before_the_legacy_checks(self):
		calls = []
		with (
			patch.object(guard, "on_before_validate", side_effect=_rec(calls, "guard")),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=_rec(calls, "legacy read", raises=_Stop),
			),
		):
			with self.assertRaises(_Stop):
				EmployeeIR.before_validate(_eir_doc())
		self.assertEqual(calls, ["guard", "legacy read"])

	def test_a_guard_rejection_stops_before_validate_cold(self):
		calls = []
		with (
			patch.object(
				guard,
				"on_before_validate",
				side_effect=guard.StaleOperationError("stale"),
			),
			patch.object(
				frappe.db, "get_value", side_effect=_rec(calls, "legacy read")
			),
		):
			with self.assertRaises(guard.StaleOperationError):
				EmployeeIR.before_validate(_eir_doc())
		self.assertEqual(calls, [])

	def test_on_update_runs_the_terminal_draft_check_for_draft_saves_only(self):
		for docstatus, expected in ((0, 1), (1, 0)):
			with self.subTest(docstatus=docstatus):
				with patch.object(guard, "final_draft_check") as final:
					EmployeeIR.on_update(_eir_doc(docstatus=docstatus))
				self.assertEqual(final.call_count, expected)

	def test_submit_ends_with_the_terminal_draft_check(self):
		for kind, subcontracting, expected in (
			("Issue", "No", ["qc warning", "issue", "final draft check"]),
			(
				"Issue",
				"Yes",
				["qc warning", "issue", "subcontracting order", "final draft check"],
			),
			("Receive", "No", ["receive", "final draft check"]),
		):
			with self.subTest(kind=kind, subcontracting=subcontracting):
				calls = []
				doc = _eir_doc(kind, docstatus=1, subcontracting=subcontracting)
				doc.validate_qc = _rec(calls, "qc warning")
				doc.on_submit_issue_new = _rec(calls, "issue")
				doc.create_subcontracting_order = _rec(calls, "subcontracting order")
				doc.on_submit_receive = _rec(calls, "receive")
				with (
					patch(f"{_EIR_MODULE}.validate_loss_gates_left_nothing_to_book"),
					patch(f"{_EIR_MODULE}.validate_loss_tables_required"),
					patch(f"{_EIR_MODULE}.validate_loss_rows_against_gate"),
					patch(f"{_EIR_MODULE}.validate_loss_rows_against_material_gate"),
					patch(f"{_EIR_MODULE}.validate_qc"),
					patch.object(
						guard,
						"final_draft_check",
						side_effect=_rec(calls, "final draft check"),
					),
				):
					EmployeeIR.on_submit(doc)
				self.assertEqual(calls, expected)

	def test_issue_cancel_locks_the_tree_then_guards_before_reversing(self):
		calls = []
		doc = _eir_doc("Issue", docstatus=2)
		doc.on_submit_issue_new = lambda cancel=False: calls.append(
			("reverse issue", cancel)
		)
		with (
			patch(
				f"{_EIR_MODULE}.lock_trees_for_eir",
				side_effect=_rec(calls, "lock trees"),
			),
			patch.object(
				guard, "guard_cancel", side_effect=_rec(calls, "guard cancel")
			),
		):
			EmployeeIR.on_cancel(doc)
		self.assertEqual(calls, ["lock trees", "guard cancel", ("reverse issue", True)])

	def test_refused_issue_cancel_reverses_nothing(self):
		calls = []
		doc = _eir_doc("Issue", docstatus=2)
		doc.on_submit_issue_new = lambda cancel=False: calls.append(
			("reverse issue", cancel)
		)
		with (
			patch(f"{_EIR_MODULE}.lock_trees_for_eir"),
			patch.object(
				guard, "guard_cancel", side_effect=guard.HistoryRewriteError("moved on")
			),
		):
			with self.assertRaises(guard.HistoryRewriteError):
				EmployeeIR.on_cancel(doc)
		self.assertEqual(calls, [])

	def test_receive_cancel_is_guarded_inside_on_submit_receive(self):
		calls = []
		doc = _eir_doc("Receive", docstatus=2)
		doc.on_submit_receive = lambda cancel=False: calls.append(
			("reverse receive", cancel)
		)
		with (
			patch(f"{_EIR_MODULE}.lock_trees_for_eir") as lock_trees,
			patch.object(guard, "guard_cancel") as guard_cancel,
		):
			EmployeeIR.on_cancel(doc)
		self.assertEqual(calls, [("reverse receive", True)])
		lock_trees.assert_not_called()
		guard_cancel.assert_not_called()

	def test_discard_marks_child_rows_through_the_guard(self):
		doc = _eir_doc()
		with patch.object(guard, "on_discard") as hook:
			EmployeeIR.on_discard(doc)
		hook.assert_called_once_with(doc)

	def _run_receive(self, doc, *, cancel=False, ops=(), wos=()):
		"""EmployeeIR.on_submit_receive with every pre-lock and side effect recorded.

		The authoritative guard check runs for real; only its lock helpers and reads are fakes.
		"""
		calls = self.calls = []

		def get_value(doctype, *args, **kwargs):
			if doctype == "Department Operation":
				return 0
			if doctype == "Warehouse":
				return "WH-T"
			if doctype == "Manufacturing Operation":
				return 1.0
			return None

		def lock_ops(names, wait=None):
			calls.append(("lock operations", sorted(names)))
			return {op.name: op for op in ops}

		def lock_wos(names, wait=None):
			calls.append(("lock work orders", sorted(names)))
			return {wo.name: wo for wo in wos}

		def family(mwos):
			calls.append("read family")
			return _family_of(*ops)

		with ExitStack() as stack:
			for patcher in (
				patch(
					f"{_EIR_MODULE}.lock_trees_for_eir",
					side_effect=_rec(calls, "lock trees"),
				),
				patch(
					f"{_EIR_MODULE}.lock_finding_repack_trees",
					side_effect=_rec(calls, "lock finding trees"),
				),
				patch(f"{_EIR_MODULE}.finding_bin_pairs", return_value=[]),
				patch(
					f"{_MAIN_SLIP_INJECT_MODULE}._resolve_source_warehouse_raw_material",
					return_value="WH-MSL",
				),
				patch.object(
					lock_order, "series_stubs", return_value=["stock-entry-stub"]
				),
				patch.object(
					lock_order,
					"preallocate_series_for_docs",
					side_effect=_rec(calls, "lock series"),
				),
				patch.object(
					lock_order, "lock_bins", side_effect=_rec(calls, "lock bins")
				),
				patch.object(
					guard, "lock_manufacturing_operations", side_effect=lock_ops
				),
				patch.object(guard, "lock_work_orders", side_effect=lock_wos),
				patch.object(guard, "_family", side_effect=family),
				patch.object(
					guard,
					"outstanding_issue_drafts",
					side_effect=_rec(calls, "read drafts", result=[]),
				),
				patch.object(
					guard,
					"_refuse_if_snapshot_older",
					side_effect=_rec(calls, "snapshot check"),
				),
				patch.object(
					guard, "guard_cancel", side_effect=_rec(calls, "guard cancel")
				),
				patch(
					f"{_EIR_MODULE}.cancel_loss_stock_entries",
					side_effect=_rec(calls, "cancel loss entries", raises=_Stop()),
				),
				patch(
					f"{_EIR_MODULE}.create_operation_for_next_op",
					side_effect=_rec(calls, "mint next operation", raises=_Stop()),
				),
				patch.object(frappe.db, "get_single_value", return_value=3),
				patch.object(frappe.db, "get_value", side_effect=get_value),
				patch.object(
					frappe.db, "set_value", side_effect=_rec(calls, "set_value")
				),
				patch.object(
					frappe.db, "sql", side_effect=_rec(calls, "sql", result=[])
				),
			):
				stack.enter_context(patcher)
			EmployeeIR.on_submit_receive(doc, cancel=cancel)

	def _receive_doc(self):
		return _eir_doc("Receive", name="EIR-R-0001", docstatus=1)

	def _held(self):
		return _op("MOP-1", status="WIP", operation="Op-T", employee="EMP-T")

	def test_receive_submit_checks_after_tree_series_bin_prelocks_and_before_minting(
		self,
	):
		"""Lock order Tree -> Series -> Bin -> MOP -> MWO (lock_order RULE D/E), then the row loop."""
		doc = self._receive_doc()
		with self.assertRaises(_Stop):
			self._run_receive(doc, ops=[self._held()], wos=[_wo()])
		self.assertEqual(
			self.calls,
			[
				"lock trees",
				"lock finding trees",
				"lock series",
				"lock bins",
				("lock operations", ["MOP-1"]),
				("lock work orders", ["MWO-1"]),
				"read family",
				"read drafts",
				"snapshot check",
				"mint next operation",
			],
		)
		self.assertEqual(
			doc.flags.current_operation_guarded,
			{"phase": "submit", "docstatus": 1, "mwos": ["MWO-1"]},
		)

	def test_stale_receive_submit_mints_and_writes_nothing(self):
		with self.assertRaises(guard.StaleOperationError):
			self._run_receive(
				self._receive_doc(), ops=[self._held()], wos=[_wo(pointer="MOP-2")]
			)
		self.assertNotIn("mint next operation", self.calls)
		self.assertNotIn("set_value", self.calls)
		self.assertNotIn("sql", self.calls)

	def test_receive_cancel_guard_runs_after_the_tree_locks_and_before_any_reversal(
		self,
	):
		doc = _eir_doc("Receive", name="EIR-R-0001", docstatus=2)
		with self.assertRaises(_Stop):
			self._run_receive(doc, cancel=True)
		self.assertEqual(
			self.calls,
			["lock trees", "lock finding trees", "guard cancel", "cancel loss entries"],
		)

	def test_a_prelocked_receive_submit_neither_relocks_nor_rechecks(self):
		"""The attempt took Tree / Series / Bin and then the block in before_validate (or
		before_insert): on_submit_receive goes straight to the row loop."""
		doc = self._receive_doc()
		doc.flags[guard.RECEIVE_PRELOCKS_FLAG] = True
		doc.flags[guard.GUARDED_FLAG] = {
			"phase": "submit",
			"docstatus": 1,
			"mwos": ["MWO-1"],
		}
		with self.assertRaises(_Stop):
			self._run_receive(doc, ops=[self._held()], wos=[_wo()])
		self.assertEqual(self.calls, ["mint next operation"])

	def test_a_receive_cancel_never_counts_as_prelocked(self):
		doc = _eir_doc("Receive", name="EIR-R-0001", docstatus=2)
		doc.flags[guard.RECEIVE_PRELOCKS_FLAG] = True  # left over from a submit attempt
		with self.assertRaises(_Stop):
			self._run_receive(doc, cancel=True)
		self.assertEqual(
			self.calls[:3], ["lock trees", "lock finding trees", "guard cancel"]
		)

	def _hook_calls(self, hook, doc, conf=None):
		"""The hook's calls up to the guard; a draft save's legacy checks are cut off."""
		calls = []
		with (
			patch.dict(frappe.local.conf, conf or {}),
			patch(
				f"{_EIR_MODULE}.take_receive_prelocks",
				side_effect=lambda d: calls.append("receive pre-locks"),
			),
			patch.object(
				guard, "begin_attempt", side_effect=_rec(calls, "begin attempt")
			),
			patch.object(guard, "on_before_insert", side_effect=_rec(calls, "guard")),
			patch.object(guard, "on_before_validate", side_effect=_rec(calls, "guard")),
			patch.object(frappe.db, "get_value", side_effect=_Stop),
		):
			try:
				hook(doc)
			except _Stop:
				pass
		return calls

	def test_a_rest_receive_insert_takes_its_prelocks_right_before_the_guard(self):
		"""Tree -> Series -> Bin, then the guard's MOP -> MWO block, before Frappe names it."""
		doc = _eir_doc("Receive", docstatus=1, new=True)
		self.assertEqual(
			self._hook_calls(EmployeeIR.before_insert, doc),
			["begin attempt", "receive pre-locks", "guard"],
		)

	def test_before_validate_takes_no_prelocks_any_more(self):
		"""A saved draft's submit took them in check_if_latest, an insert in before_insert."""
		for doc in (
			_eir_doc("Receive", docstatus=1),
			_eir_doc("Receive", docstatus=1, new=True),
		):
			with self.subTest(new=doc.is_new()):
				doc.flags[guard.RECEIVE_PRELOCKS_FLAG] = True
				self.assertEqual(
					self._hook_calls(EmployeeIR.before_validate, doc), ["guard"]
				)
				self.assertIs(doc.flags[guard.RECEIVE_PRELOCKS_FLAG], True)

	def test_only_an_enforced_receive_submit_takes_early_prelocks(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			prelock_receive_submit,
		)

		for label, doc, submitting, conf, expected in (
			(
				"Receive submit",
				_eir_doc("Receive", docstatus=1),
				True,
				None,
				["pre-locks"],
			),
			("Receive draft save", _eir_doc("Receive"), True, None, []),
			("Issue submit", _eir_doc(docstatus=1), True, None, []),
			(
				"Receive cancel / update after submit",
				_eir_doc("Receive", docstatus=1),
				False,
				None,
				[],
			),
			(
				"warn mode: on_submit_receive takes them, as before the guard",
				_eir_doc("Receive", docstatus=1),
				True,
				{"current_operation_guard": "warn"},
				[],
			),
		):
			with self.subTest(label):
				calls = []
				doc.flags[guard.RECEIVE_PRELOCKS_FLAG] = True  # a previous attempt's
				with (
					patch.dict(frappe.local.conf, conf or {}),
					patch(
						f"{_EIR_MODULE}.take_receive_prelocks",
						side_effect=lambda d, calls=calls: calls.append("pre-locks"),
					),
				):
					prelock_receive_submit(doc, submitting=submitting)
				self.assertEqual(calls, expected)
				if not expected:
					self.assertIs(doc.flags[guard.RECEIVE_PRELOCKS_FLAG], False)

	def _controller_check_if_latest(self, values, stored_docstatus):
		"""EmployeeIR.check_if_latest on a real (unsaved) controller object; Frappe's own
		check_if_latest -- which loads the stored rows FOR UPDATE -- is recorded instead."""
		from frappe.model.document import Document

		calls = []
		doc = frappe.get_doc({"doctype": "Employee IR", "name": "EIR-T-0099", **values})
		with (
			patch.object(frappe.db, "get_value", return_value=stored_docstatus),
			patch.object(
				guard,
				"begin_attempt",
				side_effect=lambda d, stored=None: calls.append(
					("begin attempt", stored)
				),
			),
			patch(
				f"{_EIR_MODULE}.prelock_receive_submit",
				side_effect=lambda d, submitting=True: calls.append(
					("receive pre-locks", submitting)
				),
			),
			patch.object(
				guard,
				"lock_before_own_rows",
				side_effect=lambda d: calls.append("block"),
			),
			patch.object(
				Document,
				"check_if_latest",
				side_effect=lambda *a, **k: calls.append("own rows (Frappe)"),
			),
		):
			doc.check_if_latest()
		return calls

	def test_check_if_latest_takes_the_block_before_frappe_locks_the_own_rows(self):
		"""begin_attempt (the EOD / reconciliation-window refusal, the budget reset) first."""
		self.assertEqual(
			self._controller_check_if_latest({"type": "Receive", "docstatus": 1}, 0),
			[
				("begin attempt", 0),
				("receive pre-locks", True),
				"block",
				"own rows (Frappe)",
			],
		)
		# a cancel or an update after submit: no Receive pre-locks (the guard ignores it too)
		self.assertEqual(
			self._controller_check_if_latest({"type": "Receive", "docstatus": 2}, 1),
			[
				("begin attempt", 1),
				("receive pre-locks", False),
				"block",
				"own rows (Frappe)",
			],
		)

	def test_check_if_latest_of_a_new_document_leaves_it_to_before_insert(self):
		from frappe.model.document import Document

		doc = frappe.new_doc("Employee IR")
		with (
			patch.object(guard, "begin_attempt") as begin,
			patch.object(guard, "lock_before_own_rows") as block,
			patch(f"{_EIR_MODULE}.prelock_receive_submit") as prelock,
			patch.object(Document, "check_if_latest") as frappe_check,
		):
			doc.check_if_latest()
		begin.assert_not_called()
		block.assert_not_called()
		prelock.assert_not_called()
		frappe_check.assert_called_once()

	def test_take_receive_prelocks_takes_trees_series_then_bins_and_marks_the_attempt(
		self,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			take_receive_prelocks,
		)

		calls = []
		doc = self._receive_doc()
		doc.employee_loss_details = [FrappeDict(item_code="M-GOLD")]
		with (
			patch(
				f"{_EIR_MODULE}.resolve_receive_tree_numbers",
				side_effect=_rec(calls, "resolve trees"),
			),
			patch(
				f"{_EIR_MODULE}.lock_trees_for_eir",
				side_effect=_rec(calls, "lock trees"),
			),
			patch(
				f"{_EIR_MODULE}.lock_finding_repack_trees",
				side_effect=_rec(calls, "lock finding trees"),
			),
			patch(
				f"{_EIR_MODULE}.finding_bin_pairs", return_value=[("F-HOOK", "WH-MSL")]
			),
			patch(
				f"{_MAIN_SLIP_INJECT_MODULE}._resolve_source_warehouse_raw_material",
				return_value="WH-MSL",
			),
			patch.object(lock_order, "series_stubs", return_value=["stock-entry-stub"]),
			patch.object(
				lock_order,
				"preallocate_series_for_docs",
				side_effect=_rec(calls, "lock series"),
			),
			patch.object(
				lock_order, "lock_bins", side_effect=_rec(calls, "lock bins")
			) as bins,
			patch.object(frappe.db, "get_value", return_value="WH-T"),
		):
			take_receive_prelocks(doc)
		self.assertEqual(
			calls,
			[
				"resolve trees",
				"lock trees",
				"lock finding trees",
				"lock series",
				"lock bins",
			],
		)
		self.assertEqual(
			bins.call_args.args[0],
			[("M-GOLD", "WH-T"), ("M-GOLD", "WH-T"), ("F-HOOK", "WH-MSL")],
		)
		self.assertIs(doc.flags[guard.RECEIVE_PRELOCKS_FLAG], True)


class TestCurrentOperationGuardMovementFreeze(IntegrationTestCase):
	"""F2: a save / submit during the EOD sync or an open stock-reconciliation window is refused
	before the attempt takes ANY lock. The before_save / before_submit doc_events refuse it too,
	but only after check_if_latest / before_insert took the Receive pre-locks and the block."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def tearDown(self):
		frappe.clear_messages()
		return super().tearDown()

	def _frozen(self, *, eod=True, window=False):
		return (
			patch(f"{_EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=eod),
			patch(f"{_RECON_WINDOW_MODULE}._enabled", return_value=window),
			patch(
				f"{_RECON_WINDOW_MODULE}._department_window_status", return_value="open"
			),
		)

	def _check_if_latest(self, values, stored, *, action=None, **frozen):
		"""EmployeeIR.check_if_latest on a real controller object; every lock it would take is
		recorded instead (Frappe's own check_if_latest loads the rows FOR UPDATE)."""
		from frappe.model.document import Document

		calls = []
		doc = frappe.get_doc(
			{
				"doctype": "Employee IR",
				"name": "EIR-T-0099",
				"department": "Dept-T",
				**values,
			}
		)
		if action:
			doc._action = action
		with ExitStack() as stack:
			for patcher in (
				*self._frozen(**frozen),
				patch.object(frappe.db, "get_value", return_value=stored),
				patch(
					f"{_EIR_MODULE}.take_receive_prelocks",
					side_effect=_rec(calls, "receive pre-locks"),
				),
				patch.object(
					guard, "lock_before_own_rows", side_effect=_rec(calls, "block")
				),
				patch.object(
					Document,
					"check_if_latest",
					side_effect=_rec(calls, "own rows (Frappe)"),
				),
			):
				stack.enter_context(patcher)
			self.calls = calls
			doc.check_if_latest()
		return calls

	def test_saves_and_submits_of_a_draft_are_refused_before_any_lock(self):
		for label, values in (
			("Receive submit (queued or desk)", {"type": "Receive", "docstatus": 1}),
			("Issue submit", {"type": "Issue", "docstatus": 1}),
			("draft save", {"type": "Issue", "docstatus": 0}),
		):
			with self.subTest(label):
				with self.assertRaisesRegex(
					frappe.ValidationError, "EOD sync is in progress"
				):
					self._check_if_latest(values, 0)
				self.assertEqual(self.calls, [])

	def test_an_open_reconciliation_window_is_refused_before_any_lock_too(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Stock transactions are temporarily blocked"
		):
			self._check_if_latest(
				{"type": "Receive", "docstatus": 1}, 0, eod=False, window=True
			)
		self.assertEqual(self.calls, [])

	def test_cancels_updates_after_submit_and_discards_are_not_refused_here(self):
		"""Exactly what the doc_events gate: a cancel is refused by before_cancel before
		on_cancel takes any guard or tree lock; a discard / update after submit never was."""
		for label, values, stored, action in (
			("cancel", {"type": "Issue", "docstatus": 2}, 1, None),
			("update after submit", {"type": "Issue", "docstatus": 1}, 1, None),
			("discard", {"type": "Issue", "docstatus": 0}, 0, "discard"),
		):
			with self.subTest(label):
				self.assertEqual(
					self._check_if_latest(values, stored, action=action),
					["block", "own rows (Frappe)"],
				)

	def test_the_bypasses_of_the_doc_events_apply(self):
		values = {"type": "Issue", "docstatus": 1}
		with patch.dict(frappe.local.flags, {"in_eod_mop_sync": True}):
			self.assertEqual(
				self._check_if_latest(values, 0), ["block", "own rows (Frappe)"]
			)
		doc = _eir_doc(docstatus=1)
		doc.flags.ignore_validate = True
		with ExitStack() as stack:
			for patcher in self._frozen():
				stack.enter_context(patcher)
			guard.refuse_if_movement_blocked(doc, 0)  # no error

	def test_a_new_document_is_refused_before_its_receive_pre_locks_and_block(self):
		calls = []
		doc = _eir_doc("Receive", docstatus=1, new=True)
		with ExitStack() as stack:
			for patcher in (
				*self._frozen(),
				patch(
					f"{_EIR_MODULE}.take_receive_prelocks",
					side_effect=_rec(calls, "receive pre-locks"),
				),
				patch.object(
					guard, "on_before_insert", side_effect=_rec(calls, "guard")
				),
			):
				stack.enter_context(patcher)
			with self.assertRaisesRegex(
				frappe.ValidationError, "EOD sync is in progress"
			):
				EmployeeIR.before_insert(doc)
		self.assertEqual(calls, [])

	def test_without_a_freeze_the_attempt_locks_as_before(self):
		self.assertEqual(
			self._check_if_latest({"type": "Receive", "docstatus": 1}, 0, eod=False),
			["receive pre-locks", "block", "own rows (Frappe)"],
		)


class TestEmployeeIssueCancelRestoresPreIssueState(IntegrationTestCase):
	"""An Employee Issue cancel puts each row operation back as it was before the Issue: Not
	Started, no holder -- and, new, no running timer (started_time) and no open time log of the
	Issue's holder."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _cancel(self, doc):
		calls = self.calls = []
		self.bulk = {}

		def bulk_update(doctype, updates, **kwargs):
			calls.append("bulk update")
			self.bulk = {name: dict(values) for name, values in updates.items()}

		doc._refresh_msl_tracking = lambda: calls.append("msl")
		with (
			patch.object(frappe.db, "sql", return_value=[]),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.cancel_voucher_mop_logs",
				side_effect=_rec(calls, "cancel MOP Logs", result=[]),
			),
			patch.object(frappe.db, "set_value", side_effect=_rec(calls, "set_value")),
			patch.object(frappe.db, "get_value", return_value="WH-T"),
			patch.object(frappe.db, "bulk_update", side_effect=bulk_update),
			patch(
				f"{_EIR_MODULE}._delete_issue_open_time_logs",
				side_effect=_rec(calls, "delete open time logs"),
			),
			patch(f"{_EIR_MODULE}.unlink_tree_on_issue_cancel"),
		):
			EmployeeIR.on_submit_issue_new(doc, cancel=True)

	def test_cancel_clears_the_timer_and_drops_the_open_time_log_before_the_reset(self):
		doc = _eir_doc(
			"Issue", docstatus=2, rows=(("MOP-1", "MWO-1"), ("MOP-2", "MWO-2"))
		)
		self._cancel(doc)
		self.assertEqual(
			self.calls,
			["cancel MOP Logs", "delete open time logs", "bulk update", "msl"],
		)
		for mop in ("MOP-1", "MOP-2"):
			values = self.bulk[mop]
			self.assertEqual(
				{
					k: values[k]
					for k in (
						"status",
						"operation",
						"employee",
						"start_time",
						"started_time",
					)
				},
				{
					"status": "Not Started",
					"operation": None,
					"employee": None,
					"start_time": None,
					"started_time": None,
				},
			)

	def test_subcontracting_cancel_resets_the_timer_too(self):
		doc = _eir_doc(
			"Issue",
			docstatus=2,
			subcontracting="Yes",
			subcontractor="SUP-1",
			employee=None,
		)
		self._cancel(doc)
		self.assertEqual(self.bulk["MOP-1"]["started_time"], None)
		self.assertEqual(self.bulk["MOP-1"]["subcontractor"], None)

	def test_a_submit_does_not_touch_started_time_through_the_bulk_update(self):
		"""The submit's timer comes from batch_add_time_logs (reset_timer_value), not the bulk."""
		doc = _eir_doc("Issue", docstatus=1)
		doc.employee_ir_operations[0].rpt_wt_issue = 0
		bulk = {}
		with (
			patch.object(frappe.db, "get_value", return_value="WH-T"),
			patch.object(
				frappe.db,
				"bulk_update",
				side_effect=lambda dt, updates, **kw: bulk.update(updates),
			),
			patch(f"{_EIR_MODULE}.creste_mop_log_for_employee_ir"),
			patch(f"{_EIR_MODULE}.batch_add_time_logs"),
			patch(f"{_EIR_MODULE}.create_tree_on_issue"),
			patch(f"{_EIR_MODULE}._delete_issue_open_time_logs") as delete_logs,
		):
			doc._refresh_msl_tracking = lambda: None
			EmployeeIR.on_submit_issue_new(doc)
		self.assertNotIn("started_time", bulk["MOP-1"])
		delete_logs.assert_not_called()

	def _delete(self, doc, *, held=None, open_logs=(("TL-1", "MOP-1"),), others=None):
		"""``_delete_issue_open_time_logs`` with its SQL faked: ``open_logs`` are the holder's
		open rows ``(name, operation[, seconds after the Issue's submit])`` the locking read
		returns; ``others`` what other_submitted_issues reports."""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			_delete_issue_open_time_logs,
		)

		self.statements = []
		self.other_reads = []
		submitted = doc.get("issue_submitted_on") or now_datetime()

		def fake_sql(query, values=None, *args, **kwargs):
			self.statements.append((" ".join(query.split()), values))
			if query.lstrip().startswith("SELECT"):
				return [
					FrappeDict(
						name=log[0],
						parent=log[1],
						from_time=add_to_date(
							submitted, seconds=log[2] if len(log) > 2 else 0.2
						),
					)
					for log in open_logs
				]
			return None

		def other_issues(mops, exclude=None):
			self.other_reads.append((list(mops), exclude))
			return others or {}

		with (
			patch.object(frappe.db, "sql", side_effect=fake_sql),
			patch.object(
				lock_order,
				"lock_manufacturing_operations",
				return_value={op.name: op for op in held or ()},
			) as relock,
			patch.object(guard, "other_submitted_issues", side_effect=other_issues),
			patch.object(frappe, "clear_document_cache") as clear_cache,
		):
			self.relock = relock
			self.clear_cache = clear_cache
			return _delete_issue_open_time_logs(doc)

	def test_in_house_cancel_deletes_open_time_logs_of_the_issues_employee(self):
		doc = _eir_doc(
			"Issue",
			docstatus=2,
			rows=(("MOP-2", "MWO-2"), ("MOP-1", "MWO-1")),
			issue_submitted_on=now_datetime(),
		)
		self.assertEqual(
			self._delete(doc, open_logs=(("TL-1", "MOP-1"), ("TL-2", "MOP-2"))),
			["TL-1", "TL-2"],
		)
		(select_sql, select_values), (delete_sql, delete_values) = self.statements
		for clause in (
			"SELECT name, parent, from_time FROM `tabManufacturing Operation Time Log`",
			"WHERE parent IN %(mops)s",
			"parentfield = 'time_logs'",
			"to_time IS NULL",
			"IFNULL(employee, '') = %(employee)s",
		):
			self.assertIn(clause, select_sql)
		# a plain read (guard_cancel holds the operations and checked they did not change):
		# no gap locks on the time-log parent index
		self.assertNotIn("FOR UPDATE", select_sql)
		self.assertEqual(
			select_values, {"mops": ["MOP-1", "MOP-2"], "employee": "EMP-T"}
		)
		self.assertEqual(self.other_reads, [(["MOP-1", "MOP-2"], "EIR-T-0001")])
		self.assertEqual(
			delete_sql,
			"DELETE FROM `tabManufacturing Operation Time Log` "
			"WHERE name IN %(names)s AND to_time IS NULL",
		)
		self.assertEqual(delete_values, {"names": ["TL-1", "TL-2"]})
		self.relock.assert_not_called()
		self.assertEqual(
			sorted(c.args for c in self.clear_cache.call_args_list),
			[
				("Manufacturing Operation", "MOP-1"),
				("Manufacturing Operation", "MOP-2"),
			],
		)

	def test_a_double_issued_operation_loses_only_this_issues_own_row(self):
		"""Legacy: two submitted Issues of the same employee on one operation, both timers
		open (only the reviewed repair cancels one of them). Whichever is cancelled, the other's
		running timer stays -- even with the two submits nine minutes apart."""
		submitted = now_datetime()
		for label, gap_to_other, own, other in (
			("seconds apart", 2, "TL-OWN", "TL-OTHER"),
			("nine minutes later", 561, "TL-OWN", "TL-OTHER"),
			("nine minutes earlier", -561, "TL-OWN", "TL-OTHER"),
		):
			with self.subTest(label):
				doc = _eir_doc(
					"Issue", docstatus=2, name="EIR-I-L", issue_submitted_on=submitted
				)
				others = {
					"MOP-1": [
						FrappeDict(
							employee_ir="EIR-I-D",
							issue_submitted_on=add_to_date(
								submitted, seconds=gap_to_other
							),
						)
					]
				}
				deleted = self._delete(
					doc,
					open_logs=(
						(own, "MOP-1", 0.3),
						(other, "MOP-1", gap_to_other + 0.3),
					),
					others=others,
				)
				self.assertEqual(deleted, [own])

	def test_subcontracting_cancel_matches_the_holder_recorded_on_the_operation(self):
		"""A subcontracting Issue's time log has no employee: only operations this subcontractor
		holds are touched, and only their employee-less open rows."""
		doc = _eir_doc(
			"Issue",
			docstatus=2,
			subcontracting="Yes",
			subcontractor="SUP-1",
			employee=None,
			rows=(("MOP-1", "MWO-1"), ("MOP-2", "MWO-2"), ("MOP-3", "MWO-3")),
		)
		held = [
			_op("MOP-1", for_subcontracting=1, subcontractor="SUP-1"),
			_op("MOP-2", mwo="MWO-2", for_subcontracting=1, subcontractor="SUP-2"),
			_op("MOP-3", mwo="MWO-3", for_subcontracting=0, subcontractor="SUP-1"),
		]
		self._delete(doc, held=held)
		self.relock.assert_called_once_with(["MOP-1", "MOP-2", "MOP-3"])
		self.assertEqual(self.statements[0][1], {"mops": ["MOP-1"], "employee": ""})

	def test_nothing_open_deletes_nothing(self):
		self.assertEqual(self._delete(_eir_doc("Issue", docstatus=2), open_logs=()), [])
		self.assertEqual(len(self.statements), 1)
		self.clear_cache.assert_not_called()
		self.assertEqual(self._delete(_eir_doc("Issue", docstatus=2, rows=())), [])
		self.assertEqual(self.statements, [])


class TestIssueOwnOpenTimeLogs(IntegrationTestCase):
	"""issue_own_open_time_logs: which of the holder's open time logs on an operation an
	Employee Issue's own submit opened -- what its cancel may delete (F4)."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def setUp(self):
		super().setUp()
		self.submitted = now_datetime()

	def _row(self, name, seconds):
		return FrappeDict(
			name=name, from_time=add_to_date(self.submitted, seconds=seconds)
		)

	def _issue(self, name="EIR-I-L", submitted=True):
		return _eir_doc(
			"Issue",
			name=name,
			docstatus=2,
			issue_submitted_on=self.submitted if submitted else None,
		)

	def _other(self, name, seconds):
		return FrappeDict(
			employee_ir=name,
			issue_submitted_on=add_to_date(self.submitted, seconds=seconds)
			if seconds is not None
			else None,
		)

	def test_held_alone_its_own_row_and_a_resumed_timer_go_an_older_leftover_stays(
		self,
	):
		rows = [
			self._row("TL-OWN", 0.25),
			self._row("TL-RESUMED", 1800),  # Resume Job after a Pause, 30 min later
			self._row("TL-EARLY", -3),  # inside the 5 s tolerance
			self._row("TL-LEFTOVER", -86400),  # a pre-guard cancel left it running
		]
		self.assertEqual(
			guard.issue_own_open_time_logs(self._issue(), rows, []),
			["TL-EARLY", "TL-OWN", "TL-RESUMED"],
		)

	def test_without_a_submit_time_only_a_lone_row_of_a_lone_issue_goes(self):
		issue = self._issue(submitted=False)
		self.assertEqual(
			guard.issue_own_open_time_logs(issue, [self._row("TL-1", 5)], None),
			["TL-1"],
		)
		self.assertEqual(
			guard.issue_own_open_time_logs(
				issue, [self._row("TL-1", 5), self._row("TL-2", 9)], None
			),
			[],
		)
		self.assertEqual(
			guard.issue_own_open_time_logs(
				issue, [self._row("TL-1", 5)], [self._other("EIR-I-D", 4)]
			),
			[],
		)

	def test_a_double_issue_hands_each_row_to_the_closest_submit(self):
		"""The 31998 / 31995 shape: same employee, submits 9 min 21 s apart. A plain window
		around either submit would take both rows."""
		rows = [self._row("TL-L", 0.29), self._row("TL-D", 561.04)]
		others = [self._other("EIR-I-D", 560.75)]
		self.assertEqual(
			guard.issue_own_open_time_logs(self._issue("EIR-I-L"), rows, others),
			["TL-L"],
		)
		# cancelling the later one instead
		later = self._issue("EIR-I-D")
		later.issue_submitted_on = add_to_date(self.submitted, seconds=560.75)
		self.assertEqual(
			guard.issue_own_open_time_logs(later, rows, [self._other("EIR-I-L", 0)]),
			["TL-D"],
		)

	def test_a_double_issue_never_takes_an_unattributable_row(self):
		rows = [self._row("TL-RESUMED", 1800)]
		self.assertEqual(
			guard.issue_own_open_time_logs(
				self._issue(), rows, [self._other("EIR-I-D", 2)]
			),
			[],
		)
		# two rows that both come to this Issue: only the one closest to its submit
		rows = [self._row("TL-A", 0.4), self._row("TL-B", 3.0)]
		self.assertEqual(
			guard.issue_own_open_time_logs(
				self._issue(), rows, [self._other("EIR-I-D", 400)]
			),
			["TL-A"],
		)

	def test_the_opener_rule_is_the_audits(self):
		"""The cancel and the repair manifests must attribute every row alike."""
		from jewellery_erpnext import mop_lineage_audit as audit

		self.assertEqual(guard.TIME_LOG_OPENER_EARLY_SECONDS, audit._CO_TIME_LOG_EARLY)
		self.assertEqual(
			guard.TIME_LOG_OPENER_WINDOW_SECONDS, audit._CO_TIME_LOG_WINDOW
		)
		issues = [
			self._other("EIR-A", 0),
			self._other("EIR-B", 300),
			self._other("EIR-C", 300.5),
			self._other("EIR-NONE", None),
		]
		for seconds in (
			-6,
			-5,
			-4.999,
			0,
			0.2,
			150,
			150.25,
			300.2,
			300.3,
			900,
			900.6,
			901,
		):
			with self.subTest(seconds=seconds):
				from_time = add_to_date(self.submitted, seconds=seconds)
				ours = guard.time_log_opener(from_time, issues)
				theirs = audit._co_time_log_opener(from_time, issues)
				self.assertEqual(
					ours and ours.employee_ir, theirs and theirs.employee_ir
				)


class TestSubmissionQueueCurrentOperationPreflight(IntegrationTestCase):
	"""CustomSubmissionQueue.insert refuses a stale Employee / Department IR submit in the user's
	own request, before any queue row exists; other doctypes and actions are untouched."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _insert(self, ref_doctype, action, preflight=None, doc=None):
		row = frappe.new_doc("Submission Queue")
		self.assertIsInstance(row, CustomSubmissionQueue)
		row.ref_doctype = ref_doctype
		row.ref_docname = "DOC-T-0001"
		self.doc = doc or _GuardDoc(
			doctype=ref_doctype, name="DOC-T-0001", docstatus=1, flags=FrappeDict()
		)
		with (
			patch.object(frappe.db, "get_value", return_value=None) as queue_lookup,
			patch.object(SubmissionQueue, "insert") as frappe_insert,
		):
			self.queue_lookup = queue_lookup
			self.frappe_insert = frappe_insert
			if preflight is None:
				row.insert(self.doc, action)
			else:
				with patch.object(
					guard, "preflight", side_effect=preflight
				) as self.preflight:
					row.insert(self.doc, action)

	def test_employee_and_department_ir_submits_are_preflighted(self):
		for ref_doctype in ("Employee IR", "Department IR"):
			for action in ("Submit", "submit"):
				with self.subTest(ref_doctype=ref_doctype, action=action):
					self._insert(ref_doctype, action, preflight=lambda doc: None)
					self.preflight.assert_called_once_with(self.doc)
					self.frappe_insert.assert_called_once_with(self.doc, action)

	def test_other_actions_and_doctypes_are_not_preflighted(self):
		for ref_doctype, action in (
			("Employee IR", "Cancel"),
			("Employee IR", "Update"),
			("Employee IR", None),
			("Department IR", "Cancel"),
			("Stock Entry", "Submit"),
			("Main Slip", "submit"),
		):
			with self.subTest(ref_doctype=ref_doctype, action=action):
				self._insert(ref_doctype, action, preflight=lambda doc: None)
				self.preflight.assert_not_called()
				self.frappe_insert.assert_called_once_with(self.doc, action)

	def test_a_refused_preflight_creates_no_queue_row(self):
		with self.assertRaises(guard.StaleOperationError):
			self._insert(
				"Employee IR", "Submit", preflight=guard.StaleOperationError("stale")
			)
		self.queue_lookup.assert_not_called()
		self.frappe_insert.assert_not_called()

	def test_preflight_runs_the_lock_free_guard_on_the_queued_document(self):
		doc = _eir_doc(rows=(("MOP-OLD", "MWO-1"),), docstatus=1, name="DOC-T-0001")
		mop_rows = {"MOP-OLD": _op("MOP-OLD", status="Finished")}
		with (
			patch(f"{_EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=False),
			patch(f"{_RECON_WINDOW_MODULE}._enabled", return_value=False),
			patch.object(
				guard,
				"_plain_rows",
				return_value=(mop_rows, {"MWO-1": _wo(pointer="MOP-NEW")}),
			),
			patch.object(guard, "_family", return_value={}),
			patch.object(guard, "outstanding_issue_drafts", return_value=[]),
			patch.object(
				guard,
				"_lock_block",
				side_effect=AssertionError("preflight must not lock"),
			),
		):
			with self.assertRaises(guard.StaleOperationError):
				self._insert("Employee IR", "Submit", doc=doc)
		self.queue_lookup.assert_not_called()
		self.frappe_insert.assert_not_called()

	def test_a_submit_queued_during_the_eod_sync_is_refused_inline(self):
		"""The worker would only fail on it later; no queue row, no current-operation read."""
		doc = _eir_doc(docstatus=1, name="DOC-T-0001")
		with (
			patch(f"{_EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=True),
			patch.object(guard, "_check", side_effect=AssertionError("checked")),
		):
			with self.assertRaisesRegex(
				frappe.ValidationError, "EOD sync is in progress"
			):
				self._insert("Employee IR", "Submit", doc=doc)
		self.frappe_insert.assert_not_called()


class TestIssueVoucherResolverAmbiguity(IntegrationTestCase):
	"""resolve_employee_ir_issue_voucher_for_receive: one submitted Issue per operation, or an
	error -- never "the latest modified", which returned the stale 43405."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	DOC = FrappeDict(name="EIR-R-0001", emp_ir_id=None)
	ROW = FrappeDict(name="row-1", manufacturing_operation="MOP-1")

	def _resolve(self, rows):
		with patch.object(frappe.db, "sql", return_value=rows) as sql:
			self.sql = sql
			return mop_log_module.resolve_employee_ir_issue_voucher_for_receive(
				self.DOC, self.ROW
			)

	def test_two_submitted_issues_on_one_operation_are_ambiguous(self):
		with self.assertRaises(guard.AmbiguousOperationError) as cm:
			self._resolve((("EIR-NEW",), ("EIR-STALE",)))
		for token in ("MOP-1", "EIR-NEW", "EIR-STALE"):
			self.assertIn(token, str(cm.exception))

	def test_single_submitted_issue_is_returned(self):
		self.assertEqual(self._resolve((("EIR-ONLY",),)), "EIR-ONLY")

	def test_no_submitted_issue_returns_none(self):
		self.assertIsNone(self._resolve(()))

	def test_query_counts_distinct_issues_instead_of_picking_the_latest(self):
		self._resolve((("EIR-ONLY",),))
		query, value = self.sql.call_args.args[:2]
		self.assertIn("GROUP BY eir.name", query)
		self.assertIn("LIMIT 2", query)
		self.assertNotIn("LIMIT 1", query)
		self.assertEqual(value, "MOP-1")

	def test_named_issue_still_short_circuits_the_search(self):
		doc = FrappeDict(name="EIR-R-0001", emp_ir_id="EIR-NAMED")

		def get_value(doctype, *args, **kwargs):
			return (
				FrappeDict(type="Issue", docstatus=1)
				if doctype == "Employee IR"
				else None
			)

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "sql") as sql,
		):
			result = mop_log_module.resolve_employee_ir_issue_voucher_for_receive(
				doc, self.ROW
			)
		self.assertEqual(result, "EIR-NAMED")
		sql.assert_not_called()


class _MOPDoc(FrappeDict):
	def is_new(self):
		return bool(self.get("_is_new"))

	def get_doc_before_save(self):
		return self.get("_before")


def _mop_doc(status, before_status, *, name="MOP-OLD", mwo="MWO-1", new=False):
	return _MOPDoc(
		doctype="Manufacturing Operation",
		name=name,
		manufacturing_work_order=mwo,
		status=status,
		flags=FrappeDict(),
		_is_new=new,
		_before=FrappeDict(status=before_status) if before_status is not None else None,
	)


class TestManualReopenGuard(IntegrationTestCase):
	"""validate_manual_reopen: a desk / API save may not reopen an operation the work order has
	moved past (Finished/Revert -> open on a non-current operation)."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	def _validate(self, mop_doc, pointer="MOP-NEW"):
		def get_value(doctype, *args, **kwargs):
			return pointer if doctype == "Manufacturing Work Order" else None

		with patch.object(guard.frappe.db, "get_value", side_effect=get_value) as read:
			self.read = read
			guard.validate_manual_reopen(mop_doc)

	def test_reopening_a_finished_non_current_operation_is_refused(self):
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._validate(_mop_doc("WIP", "Finished"))
		for token in ("MOP-OLD", "MWO-1", "MOP-NEW"):
			self.assertIn(token, str(cm.exception))
		self.assertEqual(
			self.read.call_args.args,
			("Manufacturing Work Order", "MWO-1", "manufacturing_operation"),
		)

	def test_every_open_status_counts_as_a_reopen(self):
		# Literal lists, not the guard's constants: a status dropped there must fail here.
		for before in ("Finished", "Revert"):
			for status in (
				"Not Started",
				"On Hold",
				"WIP",
				"QC Pending",
				"QC Completed",
			):
				with self.subTest(before=before, status=status):
					with self.assertRaises(guard.HistoryRewriteError):
						self._validate(_mop_doc(status, before))

	def test_reopening_the_current_operation_is_allowed(self):
		self._validate(_mop_doc("WIP", "Finished", name="MOP-NEW"))

	def test_saves_that_do_not_reopen_read_nothing(self):
		for label, mop_doc in (
			("new", _mop_doc("WIP", "Finished", new=True)),
			("no work order", _mop_doc("WIP", "Finished", mwo=None)),
			("no saved version", _mop_doc("WIP", None)),
			("closing", _mop_doc("Finished", "WIP")),
			("still closed", _mop_doc("Finished", "Finished")),
			("open to open", _mop_doc("QC Pending", "WIP")),
		):
			with self.subTest(label=label):
				self._validate(mop_doc)
				self.read.assert_not_called()

	def test_warn_mode_logs_the_reopen_instead_of_refusing(self):
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(guard.frappe, "log_error") as log_error,
			patch.object(guard.frappe, "msgprint"),
		):
			self._validate(_mop_doc("WIP", "Finished"))
		log_error.assert_called_once()

	def test_manufacturing_operation_validate_runs_the_reopen_guard_on_saves(self):
		calls = []
		mop_doc = _mop_doc("WIP", "Finished")
		mop_doc.set_start_finish_time = _rec(calls, "set start/finish time")
		mop_doc.validate_operation = _rec(calls, "validate operation")
		with patch.object(
			guard, "validate_manual_reopen", side_effect=_rec(calls, "reopen guard")
		):
			ManufacturingOperation.validate(mop_doc)
		self.assertEqual(
			calls, ["reopen guard", "set start/finish time", "validate operation"]
		)

	def test_internal_and_new_operation_saves_skip_the_reopen_guard(self):
		internal = _mop_doc("WIP", "Finished")
		internal.flags.ignore_validation = True
		new = _mop_doc("Not Started", None, new=True)
		for label, mop_doc in (("ignore_validation", internal), ("new", new)):
			with self.subTest(label=label):
				mop_doc.set_start_finish_time = MagicMock()
				with patch.object(guard, "validate_manual_reopen") as reopen_guard:
					ManufacturingOperation.validate(mop_doc)
				reopen_guard.assert_not_called()


class TestEODStaleCurrentOperationHold(IntegrationTestCase):
	"""MOP EOD sync holds a work order whose unsynced logs sit on an OPEN operation that is not
	its current one (EA26688 / EA26685 after 43405), and nothing else."""

	@classmethod
	def setUpClass(cls):
		_warm_frappe_caches()

	@staticmethod
	def _md(name, status, logs=1):
		return {
			"mop_name": name,
			"mop_doc": FrappeDict(name=name, status=status),
			"logs": [
				FrappeDict(
					item_code=f"M-{i}",
					batch_no=f"B-{i}",
					qty_after_transaction_batch_based=1.5,
				)
				for i in range(logs)
			],
		}

	def _open(self, data, mwo_row=None, mwo="MWO-1"):
		def get_value(doctype, *args, **kwargs):
			return mwo_row if doctype == "Manufacturing Work Order" else None

		with patch.object(frappe.db, "get_value", side_effect=get_value) as read:
			self.read = read
			return mop_eod_sync._open_non_pointer_operations(mwo, data)

	def test_open_operation_beside_the_current_one_is_reported(self):
		for status in ("Not Started", "On Hold", "WIP", "QC Pending", "QC Completed"):
			with self.subTest(status=status):
				stale = self._md("MOP-STALE", status)
				data = [self._md("MOP-PTR", "WIP"), stale]
				pointer, ops = self._open(
					data, FrappeDict(docstatus=1, manufacturing_operation="MOP-PTR")
				)
				self.assertEqual(pointer, "MOP-PTR")
				self.assertEqual(ops, [stale])

	def test_logs_on_a_finished_predecessor_are_normal(self):
		data = [self._md("MOP-PREV", "Finished"), self._md("MOP-PTR", "Not Started")]
		pointer, ops = self._open(
			data, FrappeDict(docstatus=1, manufacturing_operation="MOP-PTR")
		)
		self.assertEqual((pointer, ops), ("MOP-PTR", []))
		self.assertEqual(
			self.read.call_args.args,
			(
				"Manufacturing Work Order",
				"MWO-1",
				["docstatus", "manufacturing_operation"],
			),
		)
		self.assertTrue(self.read.call_args.kwargs.get("as_dict"))

	def test_unestablished_answers_keep_todays_behaviour(self):
		stale = [self._md("MOP-PTR", "WIP"), self._md("MOP-STALE", "WIP")]
		submitted = FrappeDict(docstatus=1, manufacturing_operation="MOP-PTR")
		for label, mwo, data, row in (
			("no work order name", None, stale, submitted),
			("no operations", "MWO-1", [], submitted),
			("work order missing", "MWO-1", stale, None),
			(
				"draft work order",
				"MWO-1",
				stale,
				FrappeDict(docstatus=0, manufacturing_operation="MOP-PTR"),
			),
			(
				"cancelled work order",
				"MWO-1",
				stale,
				FrappeDict(docstatus=2, manufacturing_operation="MOP-PTR"),
			),
			(
				"no current operation",
				"MWO-1",
				stale,
				FrappeDict(docstatus=1, manufacturing_operation=None),
			),
			("mocked database", "MWO-1", stale, MagicMock()),
			(
				"status unknown",
				"MWO-1",
				[self._md("MOP-PTR", "WIP"), self._md("MOP-STALE", None)],
				submitted,
			),
			(
				"no operation row",
				"MWO-1",
				[self._md("MOP-PTR", "WIP"), {"mop_name": "MOP-X", "logs": []}],
				submitted,
			),
		):
			with self.subTest(label=label):
				self.assertEqual(self._open(data, row, mwo=mwo), (None, []))

	def test_no_work_order_or_operations_reads_nothing(self):
		self._open([self._md("MOP-PTR", "WIP")], mwo=None)
		self.read.assert_not_called()
		self._open([], FrappeDict(docstatus=1, manufacturing_operation="MOP-PTR"))
		self.read.assert_not_called()

	def _plan(self, data, mwo_row):
		failures, stats = [], {"failed_mwos": 0, "processed_mwos": 0}
		items = []

		def get_value(doctype, *args, **kwargs):
			return mwo_row if doctype == "Manufacturing Work Order" else None

		with (
			patch.object(mop_eod_sync, "_mwo_realized_by_artifact", return_value=None),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(
				mop_eod_sync,
				"_insert_sync_log_item",
				side_effect=lambda log, row: items.append(row),
			),
			patch.object(mop_eod_sync, "_mark_all_mwo_mop_logs_synced") as mark_synced,
			patch.object(
				mop_eod_sync, "_find_last_operation", side_effect=_Stop
			) as find_last,
		):
			try:
				result = mop_eod_sync._plan_mwo_group(
					("Co-T", "MWO-1"),
					data,
					failures,
					stats,
					sync_log_name="SYNC-T-0001",
				)
			except _Stop:
				result = _Stop
		return result, failures, stats, items, mark_synced, find_last

	def test_planner_holds_the_work_order_with_an_ambiguous_current_operation(self):
		data = [self._md("MOP-PTR", "WIP"), self._md("MOP-STALE", "WIP", logs=2)]
		result, failures, stats, items, mark_synced, find_last = self._plan(
			data, FrappeDict(docstatus=1, manufacturing_operation="MOP-PTR")
		)
		self.assertIsNone(result)
		self.assertEqual(len(failures), 1)
		self.assertEqual(failures[0]["step"], "ambiguous_current_operation")
		self.assertEqual(failures[0]["last_mop"], "MOP-PTR")
		self.assertEqual(failures[0]["affected_mops"], ["MOP-PTR", "MOP-STALE"])
		self.assertIn("MOP-STALE", failures[0]["error_message"])
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertEqual(len(items), 2)
		for item in items:
			self.assertEqual(item["manufacturing_operation"], "MOP-STALE")
			self.assertEqual(item["status"], "Failed")
			self.assertEqual(item["sync_stage"], "Collect MOP Log")
		# Held: the logs stay unsynced and no transfer is planned.
		mark_synced.assert_not_called()
		find_last.assert_not_called()

	def test_planner_carries_on_for_a_healthy_work_order(self):
		data = [self._md("MOP-PREV", "Finished"), self._md("MOP-PTR", "Not Started")]
		result, failures, stats, items, _mark, find_last = self._plan(
			data, FrappeDict(docstatus=1, manufacturing_operation="MOP-PTR")
		)
		self.assertIs(result, _Stop)
		find_last.assert_called_once()
		self.assertEqual((failures, stats["failed_mwos"], items), ([], 0, []))
