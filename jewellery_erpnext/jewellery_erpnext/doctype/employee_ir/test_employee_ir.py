# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.types.frappedict import _dict as FrappeDict
from frappe.utils import add_to_date, flt, now_datetime

from jewellery_erpnext.jewellery_erpnext.doc_events import mwo_mop_link
from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_mop_link import (
	GUARD_KEY,
	MWOMOPLinkError,
	refetch_from_work_order,
	set_current_operation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.test_department_ir import (
	EOD_LOCK_CHECK,
	mo_creation,
	other_work_order,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.receive_state import (
	rows_to_check,
	state_problem,
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
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	FOREIGN_STAMP_LOG_TITLE,
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


class TestEmployeeIR(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

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
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		eir = frappe.new_doc("Employee IR")
		eir.company = "Test_Company"
		eir.type = "Issue"
		eir.department = "Waxing - T"
		eir.operation = "Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC"
		eir.employee = "HR-EMP-00002"
		eir.subcontracting = "Yes"
		eir.subcontractor = "Test_Supplier"
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
			"Test_Supplier",
			"MOP must carry the subcontractor name after Issue.",
		)

	def test_on_submit_issue_new_sets_subcontracting_values(self):
		create_pmo(self)
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		mo.reload()
		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		dir_for_receive(dir_issue)
		mo_wax.reload()

		eir = frappe.new_doc("Employee IR")
		eir.type = "Issue"
		eir.department = "Waxing - T"
		eir.company = "Test_Company"
		eir.operation = "Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC"
		eir.employee = "HR-EMP-00002"
		eir.subcontracting = "Yes"
		eir.subcontractor = "Test_Supplier"
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


# ----------------------------------------------------------------------------------------------
# Work order <-> operation link integrity (doc_events/mwo_mop_link.py) and the Receive-state
# check (doc_events/receive_state.py)
# ----------------------------------------------------------------------------------------------

EIR_DEPARTMENT = "Waxing - T"
EIR_OPERATION = "Wax Pull Out"
EIR_EMPLOYEE = "HR-EMP-00001"
OTHER_EMPLOYEE = "HR-EMP-00002"
EIR_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"
# mwo_mop_link.report titles every warn-mode Error Log "<this> <doctype> <document name>".
GUARD_LOG_PREFIX = "MWO-MOP link guard (warn mode):"
EIR_CHAIN_SAVEPOINT = "mwo_mop_link_eir_chain"
EIR_TEST_SAVEPOINT = "mwo_mop_link_eir_test"


def issued_operation_chain(holder):
	"""The newest PMO's work order (``create_pmo`` first), its operation moved from Manufacturing Plan
	& Management to Waxing (a Department Issue and its Receive) and then issued to ``EIR_EMPLOYEE`` at
	``EIR_OPERATION``.

	Returns ``frappe._dict(mwo, mop, other_mwo)``: ``mop`` is ``mwo``'s WIP operation in Waxing,
	``other_mwo`` a sibling work order of the same PMO -- the shape of the production defect, where
	a row paired an operation with its sibling's work order. Nothing here commits.
	"""
	# A Receive may follow the Issue at once (another test here sets a 5-minute delay on this
	# operation and leaves it behind).
	frappe.db.set_value(
		"Department Operation", EIR_OPERATION, "employee_ir_receive_delay", 0
	)
	mo = mo_creation()
	dir_issue = dir_for_issue("Manufacturing Plan & Management - T", EIR_DEPARTMENT, mo)
	dir_for_receive(dir_issue)
	mop = frappe.db.get_value(
		"Manufacturing Operation",
		{"department_issue_id": dir_issue.name, "previous_mop": mo.name},
		"name",
	)

	issue = frappe.new_doc("Employee IR")
	issue.company = "Test_Company"
	issue.type = "Issue"
	issue.department = EIR_DEPARTMENT
	issue.operation = EIR_OPERATION
	issue.employee = EIR_EMPLOYEE
	issue.scan_mwo = mo.manufacturing_work_order
	scan_mwo_eir(issue)
	issue.save()
	issue.submit()

	return frappe._dict(
		mwo=mo.manufacturing_work_order,
		mop=mop,
		other_mwo=other_work_order(holder, mo),
	)


def _receive(chain, mwo, employee=EIR_EMPLOYEE):
	"""An UNSAVED Employee IR Receive of the chain's operation, received at its full weight (no
	loss, no gain), its row carrying work order ``mwo`` (``None`` for a blank cell).

	``gross_wt`` is set the way the desk row carries it: an insert without link validation does
	not fetch it, and validate_duplication_and_gr_wt compares it before refreshing it."""
	gross_wt = flt(
		frappe.db.get_value("Manufacturing Operation", chain.mop, "gross_wt")
	)
	doc = frappe.new_doc("Employee IR")
	doc.company = "Test_Company"
	doc.type = "Receive"
	doc.department = EIR_DEPARTMENT
	doc.operation = EIR_OPERATION
	doc.employee = employee
	doc.append(
		"employee_ir_operations",
		{
			"manufacturing_operation": chain.mop,
			"manufacturing_work_order": mwo,
			"gross_wt": gross_wt,
			"received_gross_wt": gross_wt,
			"rpt_wt_issue": 0,
		},
	)
	return doc


def _eir_row_mwo(doc):
	return frappe.db.get_value(
		"Employee IR Operation",
		doc.employee_ir_operations[0].name,
		"manufacturing_work_order",
	)


def _pointer(mwo):
	return frappe.db.get_value(
		"Manufacturing Work Order", mwo, "manufacturing_operation"
	)


def _successor(mop, employee_ir):
	return frappe.db.get_value(
		"Manufacturing Operation",
		{"previous_mop": mop, "employee_ir": employee_ir},
		"name",
	)


def _guard_logs():
	"""The Error Log entries a mismatched row can leave: the guard's warn-mode notes and, when such a
	row reaches the ledger, MOP Log's note that it re-derived the stamp (mop_log.FOREIGN_STAMP_LOG_TITLE)."""
	return set(
		frappe.get_all(
			"Error Log",
			or_filters=[
				["method", "like", f"{GUARD_LOG_PREFIX}%"],
				["method", "=", FOREIGN_STAMP_LOG_TITLE],
			],
			pluck="name",
		)
	)


def _guard_log_texts(names):
	if not names:
		return []
	return frappe.get_all(
		"Error Log", filters={"name": ("in", sorted(names))}, pluck="error"
	)


def _drop_guard_logs_since(before):
	"""Error Log is MyISAM: rolling back leaves its rows behind, so a test deletes what it wrote."""
	for name in _guard_logs() - before:
		frappe.db.delete("Error Log", {"name": name})


class TestEmployeeIRWorkOrderOperationLink(IntegrationTestCase):
	"""An Employee IR row must carry the work order that OWNS its operation, a Receive may only
	receive an operation that is with it, and the successor a Receive mints is pointed to from its
	owner -- never from the row's work order.

	One chain (see ``issued_operation_chain``) is built per class. IntegrationTestCase would
	register a class rollback in setUpClass, but every class in this module skips that bootstrap
	(it builds ERPNext masters these suites do not need) and ``bench run-tests`` commits whatever is
	left when the run ends. ``create_pmo`` itself COMMITS -- gke_customization's Timesheet and Order
	hooks call frappe.db.commit() while the Sales Order behind the PMO is built -- so the PMO
	persists, like every create_pmo fixture in these modules, and the class SAVEPOINT is taken right
	after it: the rest of the chain is rolled back by the class cleanup, and every test runs inside a
	SAVEPOINT of its own. Nothing on the Employee IR / Department IR paths commits, so both survive
	until they are rolled back.
	"""

	@classmethod
	def setUpClass(cls):
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)
		# An EOD lock leaked by another suite on a shared site would refuse every save here.
		eod = patch(EOD_LOCK_CHECK, return_value=False)
		eod.start()
		cls.addClassCleanup(eod.stop)
		create_pmo(cls)
		frappe.db.savepoint(EIR_CHAIN_SAVEPOINT)
		cls.addClassCleanup(frappe.db.rollback, save_point=EIR_CHAIN_SAVEPOINT)
		cls.chain = issued_operation_chain(cls)

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(EIR_TEST_SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=EIR_TEST_SAVEPOINT)

	# -- the row rule ---------------------------------------------------------------------------

	def test_row_with_another_work_order_is_refused_on_insert(self):
		"""Without link validation -- an import or API write that skips fetch_from -- the server check
		is what refuses a row pairing the operation with a sibling's work order."""
		doc = _receive(self.chain, self.chain.other_mwo)
		with self.assertRaises(MWOMOPLinkError) as cm:
			doc.insert(ignore_links=True)
		message = str(cm.exception)
		self.assertIn("belongs to work order", message)
		self.assertIn(self.chain.mwo, message)
		self.assertIn(self.chain.other_mwo, message)

	def test_draft_save_takes_the_work_order_from_the_operation(self):
		"""The row's work order is a read-only fetch_from WITHOUT fetch_if_empty, so every draft save
		re-derives it from the operation: a foreign value posted with the row does not survive. Nor
		does the finding flag fetched from it: Frappe fetches it from the POSTED work order before the
		operation's fetch replaces that work order, so before_validate reads it again from the owner
		(here the sibling is made a finding work order, the owner is not)."""
		owner_flag = frappe.db.get_value(
			"Manufacturing Work Order", self.chain.mwo, "is_finding_mwo"
		)
		self.assertFalse(owner_flag)
		frappe.db.set_value(
			"Manufacturing Work Order", self.chain.other_mwo, "is_finding_mwo", 1
		)
		frappe.db.value_cache.clear()
		doc = _receive(self.chain, self.chain.other_mwo)
		doc.insert()
		self.assertEqual(_eir_row_mwo(doc), self.chain.mwo)
		self.assertEqual(
			frappe.db.get_value(
				"Employee IR Operation",
				doc.employee_ir_operations[0].name,
				"is_finding_mwo",
			),
			0,
		)

	def test_row_tampered_after_save_is_refused_on_submit(self):
		"""fetch_from is not re-applied on the submit transition (and before_validate returns once
		docstatus is 1), so a row re-pointed after its last save reaches validate as it is."""
		doc = _receive(self.chain, self.chain.mwo)
		doc.insert()
		frappe.db.set_value(
			"Employee IR Operation",
			doc.employee_ir_operations[0].name,
			"manufacturing_work_order",
			self.chain.other_mwo,
		)
		with self.assertRaises(MWOMOPLinkError) as cm:
			frappe.get_doc("Employee IR", doc.name).submit()
		self.assertIn(self.chain.other_mwo, str(cm.exception))
		# Refused before any side effect: the operation is still open and has no successor.
		self.assertEqual(
			frappe.db.get_value("Manufacturing Operation", self.chain.mop, "status"),
			"WIP",
		)
		self.assertFalse(_successor(self.chain.mop, doc.name))

	def test_client_copy_with_another_work_order_is_refused_on_submit(self):
		"""Desk -- and the Submission Queue worker after it -- submits the CLIENT's copy of the
		document. A cell re-pointed in the browser never reached the database and is refused all the
		same."""
		doc = _receive(self.chain, self.chain.mwo)
		doc.insert()
		payload = frappe.get_doc("Employee IR", doc.name).as_dict()
		payload["employee_ir_operations"][0][
			"manufacturing_work_order"
		] = self.chain.other_mwo
		with self.assertRaises(MWOMOPLinkError):
			frappe.get_doc(payload).submit()
		self.assertEqual(_eir_row_mwo(doc), self.chain.mwo)

	def test_blank_work_order_is_filled_from_the_operation(self):
		"""A row that arrives with only its operation gets the operation's own work order. Inserted
		without link validation, so it is the server check -- not fetch_from -- that fills it."""
		doc = _receive(self.chain, None)
		doc.insert(ignore_links=True)
		self.assertEqual(_eir_row_mwo(doc), self.chain.mwo)

	def test_manual_loss_row_with_another_work_order_is_refused(self):
		"""A loss row reaches the operation's ledger only through the operation row with the SAME
		pair (update_new_mop_wtg), so a manual loss row naming a sibling's work order would post its
		loss entry and never reach the ledger: it is refused like an operation row."""
		doc = _receive(self.chain, self.chain.mwo)
		doc.append(
			"manually_book_loss_details",
			{
				"manufacturing_operation": self.chain.mop,
				"manufacturing_work_order": self.chain.other_mwo,
			},
		)
		with self.assertRaises(MWOMOPLinkError) as cm:
			doc.insert(ignore_links=True)
		message = str(cm.exception)
		self.assertIn("Manually Book Loss Details", message)
		self.assertIn(self.chain.mwo, message)
		self.assertIn(self.chain.other_mwo, message)

	def test_warn_mode_logs_instead_of_refusing(self):
		"""``"mwo_mop_link_guard": "warn"`` in site_config is the kill switch: the mismatch goes to
		the Error Log and the save goes through, the row left as it was posted."""
		before = _guard_logs()
		self.addCleanup(_drop_guard_logs_since, before)
		doc = _receive(self.chain, self.chain.other_mwo)
		with patch.dict(frappe.local.conf, {GUARD_KEY: "warn"}):
			doc.insert(ignore_links=True)
		logged = _guard_logs() - before
		self.assertTrue(logged, "warn mode must leave an Error Log entry")
		self.assertTrue(
			any("belongs to work order" in text for text in _guard_log_texts(logged))
		)
		self.assertEqual(_eir_row_mwo(doc), self.chain.other_mwo)

	# -- the current-operation pointer ----------------------------------------------------------

	def test_receive_points_the_operation_owner_at_the_successor(self):
		doc = _receive(self.chain, self.chain.mwo)
		doc.insert()
		with patch(
			f"{EIR_MODULE}.set_current_operation", wraps=set_current_operation
		) as pointer_write:
			frappe.get_doc("Employee IR", doc.name).submit()

		successor = _successor(self.chain.mop, doc.name)
		self.assertTrue(successor)
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Operation", successor, "manufacturing_work_order"
			),
			self.chain.mwo,
		)
		self.assertEqual(_pointer(self.chain.mwo), successor)
		pointer_write.assert_called_once()
		self.assertEqual(pointer_write.call_args.args[0], successor)
		self.assertEqual(
			pointer_write.call_args.kwargs.get("expected_mwo"), self.chain.mwo
		)

	def test_receive_cancel_points_the_owner_back_at_the_received_operation(self):
		"""The cancel puts the pointer back BEFORE deleting the successor (the work order's link
		would otherwise block the delete), on the operation's owner."""
		doc = _receive(self.chain, self.chain.mwo)
		doc.insert()
		frappe.get_doc("Employee IR", doc.name).submit()
		successor = _successor(self.chain.mop, doc.name)
		self.assertEqual(_pointer(self.chain.mwo), successor)

		frappe.get_doc("Employee IR", doc.name).cancel()

		self.assertEqual(_pointer(self.chain.mwo), self.chain.mop)
		self.assertFalse(frappe.db.exists("Manufacturing Operation", successor))

	def test_warn_mode_receive_still_points_the_owner_not_the_rows_work_order(self):
		"""The production defect end to end: a Receive whose row carries a sibling's work order. The
		kill switch lets it through (logged), yet the successor is pointed to from the operation's
		OWNER, and the sibling's pointer is left alone -- the old code moved the sibling's pointer
		and left the owner on its Finished operation."""
		doc = _receive(self.chain, self.chain.mwo)
		doc.insert()
		frappe.db.set_value(
			"Employee IR Operation",
			doc.employee_ir_operations[0].name,
			"manufacturing_work_order",
			self.chain.other_mwo,
		)
		sibling_pointer = _pointer(self.chain.other_mwo)
		before = _guard_logs()
		self.addCleanup(_drop_guard_logs_since, before)

		with patch.dict(frappe.local.conf, {GUARD_KEY: "warn"}):
			frappe.get_doc("Employee IR", doc.name).submit()

		successor = _successor(self.chain.mop, doc.name)
		self.assertTrue(successor)
		self.assertEqual(_pointer(self.chain.mwo), successor)
		self.assertEqual(_pointer(self.chain.other_mwo), sibling_pointer)
		self.assertTrue(
			any(
				"refusing to move the current operation" in text
				for text in _guard_log_texts(_guard_logs() - before)
			)
		)

	# -- the Receive-state check -----------------------------------------------------------------

	def test_receive_of_an_operation_issued_to_another_employee_is_refused(self):
		doc = _receive(self.chain, self.chain.mwo, employee=OTHER_EMPLOYEE)
		with self.assertRaises(MWOMOPLinkError) as cm:
			doc.insert()
		message = str(cm.exception)
		self.assertIn("does not match this Receive", message)
		self.assertIn("employee", message)
		self.assertIn(EIR_EMPLOYEE, message)

	def test_receive_of_an_operation_never_issued_is_refused(self):
		"""Not Started, no operation, no employee: an operation taken straight from its Department
		Receive without an Employee Issue. The desk never offers one; the server now refuses it."""
		frappe.db.set_value(
			"Manufacturing Operation",
			self.chain.mop,
			{"status": "Not Started", "operation": None, "employee": None},
		)
		doc = _receive(self.chain, self.chain.mwo)
		with self.assertRaises(MWOMOPLinkError) as cm:
			doc.insert()
		message = str(cm.exception)
		self.assertIn("status Not Started", message)
		self.assertIn("operation", message)

	def test_draft_that_went_stale_saves_but_does_not_submit(self):
		"""An open draft may go stale (here its operation went into QC); re-saving it unchanged is not
		judged, so the operator can still edit it -- but it cannot be submitted."""
		doc = _receive(self.chain, self.chain.mwo)
		doc.insert()
		frappe.db.set_value(
			"Manufacturing Operation", self.chain.mop, "status", "QC Pending"
		)
		frappe.get_doc("Employee IR", doc.name).save()
		with self.assertRaises(MWOMOPLinkError) as cm:
			frappe.get_doc("Employee IR", doc.name).submit()
		self.assertIn("status QC Pending", str(cm.exception))


class TestLossRowsNameTheOperationsOwnWorkOrder(UnitTestCase):
	"""EmployeeIR.validate_loss_work_orders: both loss tables are held to the operation rows' rule,
	each problem named with its table. The owner lookup is stubbed; no database."""

	OWNERS = "jewellery_erpnext.jewellery_erpnext.doc_events.mwo_mop_link.mop_owners"

	@staticmethod
	def _row(idx, mop, mwo):
		return FrappeDict(
			idx=idx, manufacturing_operation=mop, manufacturing_work_order=mwo
		)

	def test_rows_of_another_work_order_are_refused_per_table(self):
		doc = FrappeDict(
			manually_book_loss_details=[self._row(1, "MOP-A", "MWO-B")],
			employee_loss_details=[
				self._row(1, "MOP-A", "MWO-A"),
				# Matched case-insensitively, as MariaDB matches the operation.
				self._row(2, "mop-a", "MWO-C"),
			],
		)
		with patch(self.OWNERS, return_value={"MOP-A": "MWO-A"}):
			with self.assertRaises(MWOMOPLinkError) as cm:
				EmployeeIR.validate_loss_work_orders(doc)
		message = str(cm.exception)
		self.assertIn("Manually Book Loss Details: Row 1:", message)
		self.assertIn("Employee Loss Details: Row 2:", message)
		self.assertIn("MWO-B", message)
		self.assertIn("MWO-C", message)
		self.assertNotIn("Employee Loss Details: Row 1:", message)

	def test_consistent_and_blank_rows_pass_untouched(self):
		"""A blank work order is not filled here: the manual table requires one, and the automatic
		table is always written with one."""
		blank = self._row(2, "MOP-A", None)
		doc = FrappeDict(
			manually_book_loss_details=[self._row(1, "MOP-A", "mwo-a"), blank],
			employee_loss_details=[],
		)
		with patch(self.OWNERS, return_value={"MOP-A": "MWO-A"}):
			EmployeeIR.validate_loss_work_orders(doc)
		self.assertIsNone(blank.manufacturing_work_order)


class TestSetCurrentOperationWithoutAnOwner(UnitTestCase):
	"""mwo_mop_link.set_current_operation for an operation with no owner -- missing, or saved without a
	work order. It goes through ``report`` like every other check, so the kill switch covers it, and
	the message says which of the two it is. The database seams are stubbed."""

	@staticmethod
	def _call(owner=None, exists=True, warn=False, expected_mwo="MWO-ROW"):
		with (
			patch.dict(frappe.local.conf, {GUARD_KEY: "warn" if warn else "enforce"}),
			patch.object(mwo_mop_link, "owner_of", return_value=owner),
			patch.object(mwo_mop_link.frappe.db, "exists", return_value=exists),
			patch.object(mwo_mop_link.frappe.db, "set_value") as set_value,
			patch.object(mwo_mop_link.frappe, "log_error") as log_error,
			patch.object(mwo_mop_link.frappe, "msgprint"),
		):
			result = set_current_operation(
				"MOP-NEW",
				expected_mwo=expected_mwo,
				doc=FrappeDict(doctype="Employee IR"),
			)
		return result, set_value, log_error

	def test_an_operation_without_a_work_order_is_refused_and_named_as_such(self):
		with self.assertRaises(MWOMOPLinkError) as cm:
			self._call(exists=True)
		self.assertIn("has no work order", str(cm.exception))
		self.assertIn("MOP-NEW", str(cm.exception))

	def test_a_missing_operation_is_refused_and_named_as_such(self):
		with self.assertRaises(MWOMOPLinkError) as cm:
			self._call(exists=False)
		self.assertIn("does not exist", str(cm.exception))

	def test_warn_mode_logs_and_points_the_callers_work_order_as_before(self):
		result, set_value, log_error = self._call(warn=True)
		self.assertEqual(result, "MWO-ROW")
		set_value.assert_called_once_with(
			"Manufacturing Work Order", "MWO-ROW", "manufacturing_operation", "MOP-NEW"
		)
		log_error.assert_called_once()
		self.assertIn("has no work order", log_error.call_args.kwargs["message"])

	def test_warn_mode_without_a_callers_work_order_writes_nothing(self):
		result, set_value, log_error = self._call(warn=True, expected_mwo=None)
		self.assertIsNone(result)
		set_value.assert_not_called()
		log_error.assert_called_once()

	def test_an_owned_operation_still_points_its_owner(self):
		result, set_value, log_error = self._call(
			owner="MWO-OWNER", expected_mwo="mwo-owner"
		)
		self.assertEqual(result, "MWO-OWNER")
		set_value.assert_called_once_with(
			"Manufacturing Work Order",
			"MWO-OWNER",
			"manufacturing_operation",
			"MOP-NEW",
		)
		log_error.assert_not_called()


class TestRefetchFromWorkOrder(UnitTestCase):
	"""mwo_mop_link.refetch_from_work_order re-reads the fields an IR row fetches FROM its work order,
	once the row's work order is final. One query; rows without a work order, or with one that does
	not exist, are left alone; names match case-insensitively."""

	def test_fields_follow_the_rows_final_work_order(self):
		owner = FrappeDict(idx=1, manufacturing_work_order="MWO-A", is_finding_mwo=1)
		other_case = FrappeDict(
			idx=2, manufacturing_work_order="mwo-b", is_finding_mwo=0
		)
		blank = FrappeDict(idx=3, manufacturing_work_order=None, is_finding_mwo=1)
		unknown = FrappeDict(
			idx=4, manufacturing_work_order="MWO-GONE", is_finding_mwo=1
		)
		answer = [
			FrappeDict(name="MWO-A", is_finding_mwo=0),
			FrappeDict(name="MWO-B", is_finding_mwo=1),
		]
		with patch.object(
			mwo_mop_link.frappe, "get_all", return_value=answer
		) as get_all:
			refetch_from_work_order(
				[owner, other_case, blank, unknown],
				{"is_finding_mwo": "is_finding_mwo"},
			)
		get_all.assert_called_once()
		self.assertEqual(
			sorted(get_all.call_args.kwargs["filters"]["name"][1]),
			["MWO-A", "MWO-GONE", "mwo-b"],
		)
		self.assertEqual(
			[row.is_finding_mwo for row in (owner, other_case, blank, unknown)],
			[0, 1, 1, 1],
		)

	def test_no_row_with_a_work_order_runs_no_query(self):
		with patch.object(mwo_mop_link.frappe, "get_all") as get_all:
			refetch_from_work_order(
				[FrappeDict(manufacturing_work_order="")],
				{"is_finding_mwo": "is_finding_mwo"},
			)
		get_all.assert_not_called()


class _ReceiveDoc(FrappeDict):
	"""Just enough Document surface for receive_state: ``get``, ``is_new`` and
	``get_doc_before_save``."""

	def is_new(self):
		return bool(self.get("_new"))

	def get_doc_before_save(self):
		return self.get("_before")


class TestReceiveStateRules(UnitTestCase):
	"""The pure rules of doc_events/receive_state.py: what a Receive row is compared with, and which
	rows a save judges. No database."""

	@staticmethod
	def _doc(*mops, **fields):
		doc = _ReceiveDoc(
			type="Receive",
			department="DEPT-1",
			operation="OP-1",
			employee="EMP-1",
			subcontracting="No",
			subcontractor=None,
			docstatus=0,
			employee_ir_operations=[
				FrappeDict(idx=idx, manufacturing_operation=mop)
				for idx, mop in enumerate(mops, 1)
			],
		)
		doc.update(fields)
		return doc

	@staticmethod
	def _operation(**fields):
		operation = FrappeDict(
			name="MOP-1",
			manufacturing_work_order="MWO-1",
			status="WIP",
			department="DEPT-1",
			operation="OP-1",
			employee="EMP-1",
			subcontractor=None,
		)
		operation.update(fields)
		return operation

	def test_operation_with_this_receive_passes(self):
		self.assertIsNone(state_problem(self._doc("MOP-1"), 1, self._operation()))

	def test_every_mismatch_is_named_in_one_message(self):
		problem = state_problem(
			self._doc("MOP-1"),
			1,
			self._operation(
				status="Not Started", department="DEPT-2", operation=None, employee=None
			),
		)
		for part in (
			"Row 1:",
			"does not match this Receive",
			"status Not Started",
			"department",
			"operation",
			"employee",
		):
			self.assertIn(part, problem)

	def test_only_on_hold_wip_and_qc_completed_are_receivable(self):
		doc = self._doc("MOP-1")
		for status in ("On Hold", "WIP", "QC Completed"):
			self.assertIsNone(state_problem(doc, 1, self._operation(status=status)))
		for status in ("Not Started", "QC Pending", "Finished"):
			self.assertIn(
				f"status {status}",
				state_problem(doc, 1, self._operation(status=status)),
			)

	def test_names_compare_case_insensitively(self):
		doc = self._doc(
			"MOP-1", department="dept-1", operation="op-1", employee="emp-1"
		)
		self.assertIsNone(state_problem(doc, 1, self._operation()))

	def test_subcontracting_receive_compares_the_subcontractor_not_the_employee(self):
		"""A subcontracting Issue stamps the subcontractor, never an employee, on the operation."""
		doc = self._doc(
			"MOP-1", subcontracting="Yes", subcontractor="SUP-1", employee="EMP-9"
		)
		self.assertIsNone(
			state_problem(doc, 1, self._operation(employee=None, subcontractor="SUP-1"))
		)
		self.assertIn(
			"subcontractor",
			state_problem(
				doc, 1, self._operation(employee=None, subcontractor="SUP-2")
			),
		)

	def test_insert_and_submit_judge_every_row(self):
		self.assertEqual(len(rows_to_check(self._doc("MOP-1", "MOP-2", _new=True))), 2)
		submitted = self._doc(
			"MOP-1", "MOP-2", docstatus=1, _before=self._doc("MOP-1", "MOP-2")
		)
		self.assertEqual(len(rows_to_check(submitted)), 2)

	def test_plain_draft_resave_judges_nothing(self):
		resave = self._doc("MOP-1", "MOP-2", _before=self._doc("MOP-1", "MOP-2"))
		self.assertEqual(rows_to_check(resave), [])

	def test_draft_resave_judges_only_the_rows_it_adds(self):
		resave = self._doc("MOP-1", "MOP-2", _before=self._doc("mop-1"))
		self.assertEqual(
			[row.manufacturing_operation for row in rows_to_check(resave)], ["MOP-2"]
		)

	def test_draft_resave_that_changes_the_filter_judges_every_row(self):
		"""An Issue turned into a Receive keeps its rows, but every one of them is judged again."""
		resave = self._doc("MOP-1", _before=self._doc("MOP-1", type="Issue"))
		self.assertEqual(len(rows_to_check(resave)), 1)
