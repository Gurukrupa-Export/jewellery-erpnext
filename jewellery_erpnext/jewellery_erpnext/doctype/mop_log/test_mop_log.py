# # Copyright (c) 2026, Nirali and Contributors
# # See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	mop_log_creation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	_get_mop_logs_for_employee_ir_issue,
	create_mop_log_for_department_ir,
	create_mop_log_for_employee_ir_receive,
	create_mop_log_for_stock_transfer_to_mo,
	creste_mop_log_for_employee_ir,
	drop_pre_refining_rows,
	get_current_mop_balance_rows,
	get_mop_opening_balances,
	get_mwo_balance_rows,
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


class TestCurrentMOPBalanceRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_get_current_balance_rows_keeps_latest_per_item_batch(self):
		rows = [
			_sample_log(
				name="LOG-NEW-D",
				creation="2026-04-17 10:00:00",
				item_code="D-A",
				batch_no="BD1",
				qty_after_transaction_batch_based=1.0,
			),
			_sample_log(
				name="LOG-NEW-M",
				creation="2026-04-17 09:00:00",
				item_code="M-A",
				batch_no=None,
				qty_after_transaction_batch_based=5.0,
			),
			_sample_log(
				name="LOG-OLD-D",
				creation="2026-04-17 08:00:00",
				item_code="D-A",
				batch_no="BD1",
				qty_after_transaction_batch_based=0.4,
			),
		]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=rows,
		):
			out = get_current_mop_balance_rows(
				"MOP-TEST-001",
				include_fields=[
					"item_code",
					"batch_no",
					"qty_after_transaction_batch_based",
				],
			)

		self.assertEqual(len(out), 2)
		out_by_key = {(row.item_code, row.batch_no): row for row in out}
		self.assertEqual(
			out_by_key[("D-A", "BD1")].qty_after_transaction_batch_based, 1.0
		)
		self.assertEqual(
			out_by_key[("M-A", None)].qty_after_transaction_batch_based, 5.0
		)


class TestMwoBalanceRows(IntegrationTestCase):
	"""``get_mwo_balance_rows`` — the work-order-scoped balance reader.

	``qty_after_transaction_batch_based`` is a PER-OPERATION running balance
	(see ``get_mop_opening_balances``); it used to be an MWO-wide running sum,
	which folded residue stranded on one operation into the next operation's
	opening balance. This reader deliberately spans the work order for callers
	that want the whole MWO's picture rather than one operation's.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_scopes_by_mwo_never_by_operation(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=[],
		) as mock_get_all:
			get_mwo_balance_rows("MWO-TEST-001")

		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["manufacturing_work_order"], "MWO-TEST-001")
		self.assertEqual(filters["is_cancelled"], 0)
		self.assertNotIn("manufacturing_operation", filters)

	def test_keeps_latest_row_per_item_batch_across_operations(self):
		"""The handoff shape: the source operation is zeroed, then the
		destination carries the balance forward. Latest wins, and it is the
		carry-forward row — not the zeroed one."""
		rows = [
			_sample_log(
				name="LOG-CARRY-FORWARD",
				creation="2026-04-17 10:05:19.066796",
				item_code="F-A",
				batch_no="B1",
				manufacturing_operation="MOP-NEW",
				qty_after_transaction_batch_based=0.770,
			),
			_sample_log(
				name="LOG-SOURCE-ZEROED",
				creation="2026-04-17 10:05:19.031109",
				item_code="F-A",
				batch_no="B1",
				manufacturing_operation="MOP-OLD",
				qty_after_transaction_batch_based=0.0,
			),
			_sample_log(
				name="LOG-SOURCE-PRE-HANDOFF",
				creation="2026-04-17 10:05:19.018149",
				item_code="F-A",
				batch_no="B1",
				manufacturing_operation="MOP-OLD",
				qty_after_transaction_batch_based=0.770,
			),
		]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=rows,
		):
			out = get_mwo_balance_rows("MWO-TEST-001")

		self.assertEqual(len(out), 1)
		self.assertEqual(out[0].name, "LOG-CARRY-FORWARD")
		self.assertEqual(out[0].qty_after_transaction_batch_based, 0.770)

	def test_narrows_by_item_codes_when_keys_given(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=[],
		) as mock_get_all:
			get_mwo_balance_rows("MWO-TEST-001", keys={("M-A", None), ("D-A", "B1")})

		self.assertEqual(
			mock_get_all.call_args.kwargs["filters"]["item_code"],
			["in", ["D-A", "M-A"]],
		)

	def test_empty_key_set_short_circuits_without_querying(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=[],
		) as mock_get_all:
			self.assertEqual(
				get_mwo_balance_rows("MWO-TEST-001", keys={(None, None)}), []
			)
		mock_get_all.assert_not_called()


class TestCurrentMopBalanceRowsUnchanged(IntegrationTestCase):
	"""Lock on the promise made when the MWO-scoped reader was added: it is
	additive. Fourteen production call sites legitimately want per-operation
	semantics, so ``get_current_mop_balance_rows`` must keep filtering by
	``manufacturing_operation`` and nothing else."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_still_scopes_by_operation_not_by_mwo(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			return_value=[],
		) as mock_get_all:
			get_current_mop_balance_rows("MOP-TEST-001")

		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["manufacturing_operation"], "MOP-TEST-001")
		self.assertEqual(filters["is_cancelled"], 0)
		self.assertNotIn("manufacturing_work_order", filters)


class TestEmployeeIRIssueMOPLogSource(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestResolveEmployeeIRIssueVoucher(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_resolve_uses_emp_ir_id_when_valid(self):
		doc = MagicMock()
		doc.emp_ir_id = "EMP-IR-ISSUE-01"
		row = MockRow()
		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
				return_value=FrappeDict({"docstatus": 1, "type": "Issue"}),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
				return_value=True,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.sql",
			) as mock_sql,
		):
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


class TestDepartmentIRIdempotency(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists"
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index"
	)
	def test_department_ir_idempotency_safe(
		self, mock_last_index, mock_get_all, mock_new_doc, mock_exists
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_department_ir,
		)

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

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists"
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index"
	)
	def test_department_ir_receive_lineage(
		self, mock_last_index, mock_get_all, mock_new_doc, mock_exists
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_department_ir,
		)

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

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists"
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_last_mop_index"
	)
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

		self.assertEqual(mock_new_doc.call_count, 2)
		self.assertEqual(mock_log.item_code, "M-NEW")
		mock_last_index.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_site_config"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_department_ir_receive_strict_blocks_tail_without_issue_logs(
		self, mock_new_doc, mock_get_all, mock_exists, mock_site_cfg
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_department_ir,
		)

		mock_site_cfg.return_value = {"department_ir_receive_strict_lineage": True}
		mock_exists.return_value = False
		mock_get_all.return_value = []

		doc = MockDepartmentIR()
		row = MockRow()
		mock_log = MagicMock()

		def mock_get_all_side_effect(doctype, filters, *args, **kwargs):
			if filters.get("voucher_no") == "DIR-ISSUE-001":
				return [
					_sample_log(flow_index=1, item_code="M-OLD"),
					_sample_log(flow_index=2, item_code="M-NEW"),
				]
			return []

		mock_get_all.side_effect = mock_get_all_side_effect
		mock_new_doc.return_value = mock_log
		create_mop_log_for_department_ir(doc, row, "T-WH", "F-WH", "MOP-OP")

		mock_get_all.assert_called()
		mock_new_doc.assert_called()


class MockMainSlipIssueEIR:
	doctype = "Employee IR"
	name = "EIR-MS-ISSUE-001"
	is_raw_material = 1


class MockMainSlipReceiveEIR:
	doctype = "Employee IR"
	name = "EIR-MS-RECV-001"
	is_raw_material = 1
	emp_ir_id = None
	employee_loss_details = []
	manually_book_loss_details = []

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestMainSlipEmployeeIRRelaxations(IntegrationTestCase):
	"""Regressions for the is_raw_material gate in mop_log writers."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.resolve_employee_ir_issue_voucher_for_receive",
		return_value="EIR-ISSUE-001",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.log_error"
	)
	def test_receive_tolerates_missing_keys_when_raw_material(
		self,
		mock_log_error,
		mock_resolve,
		mock_get_all,
		mock_new_doc,
	):
		# Issue snapshot: only metal.
		mock_get_all.return_value = [
			_sample_log(item_code="M-X", batch_no="MBATCH"),
		]
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log

		doc = MockMainSlipReceiveEIR()
		row = MockRow()
		# Should NOT raise and should still create the receive audit clone.
		create_mop_log_for_employee_ir_receive(doc, row, "FROM", "TO")
		self.assertEqual(mock_new_doc.call_count, 4)
		self.assertEqual(mock_log.save.call_count, 4)


class TestMOPLog(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

	def test_mop_log_creation(self):
		pmo = create_pmo(self)
		mwo_list = frappe.get_all(
			"Manufacturing Work Order",
			filters={"manufacturing_order": pmo.name},
			fields=["name", "department", "manufacturing_order"],
		)
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
					"Manufacturing Plan & Management - T", "Tagging - T", mo_man
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
		"""The header is derived from the BATCH tier, not stamped from the row.

		``qty_after_transaction`` is the family-wide tier and is deliberately set to a
		wrong value here: only ``*_batch_based`` may reach the header.
		"""
		mo = create_test_manufacturing_operation()

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "D-001"
		mop_log.qty_after_transaction = 999.0
		mop_log.pcs_after_transaction = 999
		mop_log.qty_after_transaction_batch_based = 10.0
		mop_log.pcs_after_transaction_batch_based = 20
		mop_log.manufacturing_operation = mo.name

		mop_log.validate()

		updated_mo = frappe.get_doc("Manufacturing Operation", mo.name)
		self.assertEqual(updated_mo.diamond_wt, 10.0)
		self.assertEqual(updated_mo.diamond_wt_in_gram, 10.0 * 0.2)  # 2.0
		self.assertEqual(updated_mo.diamond_pcs, 20)

	def test_mop_log_validate_only_touches_its_own_family(self):
		"""A per-row save must not rewrite a bucket authored outside MOP Log.

		``create_manufacturing_operation`` seeds diamond/gemstone weights from the
		MWO before any stone is issued. An unnarrowed recompute on the first metal
		row would zero them.
		"""
		mo = create_test_manufacturing_operation()

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "M-001"
		mop_log.qty_after_transaction_batch_based = 7.0
		mop_log.manufacturing_operation = mo.name
		mop_log.validate()

		updated_mo = frappe.get_doc("Manufacturing Operation", mo.name)
		self.assertEqual(updated_mo.net_wt, 7.0)
		# Seeded by create_test_manufacturing_operation, no D/G ledger rows exist.
		self.assertEqual(updated_mo.diamond_wt_in_gram, 2.0)
		self.assertEqual(updated_mo.gemstone_wt_in_gram, 1.5)

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


MOP_LOG = "jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log"
MFG_OP = (
	"jewellery_erpnext.jewellery_erpnext.doctype."
	"manufacturing_operation.manufacturing_operation"
)


class MockStockEntry:
	doctype = "Stock Entry"
	stock_entry_type = "Material Transfer (WORK ORDER)"
	name = "MAT-STE-TEST-001"

	def __init__(self, mwo="MWO-TEST-001"):
		self._mwo = mwo

	def get(self, key, default=None):
		if key == "manufacturing_work_order":
			return self._mwo
		return default


def _se_row(**overrides):
	base = {
		"name": "se-child-1",
		"item_code": "M-G-22KT-91.75-Y",
		"batch_no": "BATCH-A",
		"qty": 3.21,
		"pcs": 0,
		"s_warehouse": "WH-Employee",
		"t_warehouse": "WH-Dept",
		"manufacturing_operation": "MOP-FRESH",
		"serial_and_batch_bundle": None,
	}
	base.update(overrides)
	return FrappeDict(base)


class TestMopOpeningBalances(IntegrationTestCase):
	"""``get_mop_opening_balances`` — the per-operation opening balance.

	Regression cover for the MWO-wide sum: residue stranded on an earlier
	operation of the same work order must not become the next operation's
	opening balance.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_fresh_operation_opens_at_zero(self):
		with patch(f"{MOP_LOG}.frappe.db.get_all", return_value=[]):
			out = get_mop_opening_balances(
				"MOP-FRESH", "M-G-22KT-91.75-Y", "BATCH-A", "MWO-TEST-001"
			)
		self.assertEqual(out["qty_batch"], 0.0)
		self.assertEqual(out["qty_item"], 0.0)
		self.assertEqual(out["qty_prefix"], 0.0)

	def test_scopes_by_operation_never_by_work_order(self):
		"""The bug in one assertion: the query must not span the work order."""
		with patch(f"{MOP_LOG}.frappe.db.get_all", return_value=[]) as mock_get_all:
			get_mop_opening_balances(
				"MOP-FRESH", "M-G-22KT-91.75-Y", "BATCH-A", "MWO-TEST-001"
			)
		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["manufacturing_operation"], "MOP-FRESH")
		self.assertNotIn("manufacturing_work_order", filters)

	def test_tiers_split_by_prefix_item_and_batch(self):
		rows = [
			_sample_log(
				name="L1",
				creation="2026-08-24 10:00:00",
				item_code="M-G-22KT-91.75-Y",
				batch_no="BATCH-A",
				qty_after_transaction_batch_based=3.24,
			),
			_sample_log(
				name="L2",
				creation="2026-08-24 10:00:01",
				item_code="M-G-22KT-91.75-Y",
				batch_no="BATCH-B",
				qty_after_transaction_batch_based=0.01,
			),
			_sample_log(
				name="L3",
				creation="2026-08-24 10:00:02",
				item_code="F-FINDING",
				batch_no="BATCH-C",
				qty_after_transaction_batch_based=9.0,
			),
		]
		with (
			patch(f"{MOP_LOG}.frappe.db.get_all", return_value=rows),
			patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=None),
		):
			out = get_mop_opening_balances(
				"MOP-X", "M-G-22KT-91.75-Y", "BATCH-A", "MWO-TEST-001"
			)
		# batch tier: only BATCH-A. item tier: both batches of that item.
		# prefix tier: every M- item. The F- row belongs to none of them.
		self.assertEqual(out["qty_batch"], 3.24)
		self.assertEqual(out["qty_item"], 3.25)
		self.assertEqual(out["qty_prefix"], 3.25)

	def test_pre_refining_rows_are_ignored(self):
		"""Refining kills the MWO; a surviving pre-refining row must not revive it."""
		rows = [
			_sample_log(
				name="L-PRE",
				creation="2026-08-24 10:01:46",
				item_code="M-G-22KT-91.75-Y",
				batch_no="BATCH-A",
				qty_after_transaction_batch_based=0.01,
			)
		]
		with (
			patch(f"{MOP_LOG}.frappe.db.get_all", return_value=rows),
			patch(
				f"{MOP_LOG}.get_mwo_refining_cutoff",
				return_value="2026-08-24 10:03:18",
			),
		):
			out = get_mop_opening_balances(
				"MOP-I5D24", "M-G-22KT-91.75-Y", "BATCH-A", "MWO-TEST-001"
			)
		self.assertEqual(out["qty_batch"], 0.0)


class TestStockTransferBalanceIsPerOperation(IntegrationTestCase):
	"""The reported defect, end to end through the writer.

	`MWO-KGJPL-RI02163-054-1-91.75-Y-01`: 3.26 issued, 0.02 booked as loss,
	3.23 returned, leaving 0.01 stranded. The next casting issued 3.21 and the
	operation opened at 3.22 because the balance was summed across the work
	order. It must open at 3.21.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _write(self, opening_rows):
		created = MagicMock()
		with (
			patch(f"{MOP_LOG}.frappe.db.get_all", return_value=opening_rows),
			patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=None),
			patch(f"{MOP_LOG}.get_last_mop_index", return_value=None),
			patch(f"{MOP_LOG}.frappe.new_doc", return_value=created),
		):
			create_mop_log_for_stock_transfer_to_mo(MockStockEntry(), _se_row())
		return created

	def test_fresh_operation_records_only_what_was_issued(self):
		created = self._write([])
		self.assertEqual(created.qty_change, 3.21)
		self.assertEqual(created.qty_after_transaction_batch_based, 3.21)
		self.assertEqual(created.qty_after_transaction_item_based, 3.21)
		self.assertEqual(created.qty_after_transaction, 3.21)
		created.save.assert_called_once()

	def test_operation_with_own_balance_adds_to_it(self):
		"""Per-operation carry-forward still works — only the MWO-wide leak is gone."""
		created = self._write(
			[
				_sample_log(
					name="L-OWN",
					creation="2026-08-24 10:00:00",
					item_code="M-G-22KT-91.75-Y",
					batch_no="BATCH-A",
					qty_after_transaction_batch_based=1.0,
				)
			]
		)
		self.assertEqual(created.qty_after_transaction_batch_based, 4.21)


class TestNewMopBaselineWrite(IntegrationTestCase):
	"""``update_new_mop_wtg`` must write the inherited baseline as an absolute value.

	The new operation has no rows of its own, so a computed running balance
	would open it at ``-loss_qty`` instead of ``baseline - loss_qty``.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, loss_qty):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			update_new_mop_wtg,
		)

		new_mop = FrappeDict(
			{"name": "MOP-NEW", "previous_mop": "MOP-SRC", "gross_wt": 0}
		)
		eir = FrappeDict({"name": "EIR-1", "doctype": "Employee IR"})
		eir_row = FrappeDict(
			{
				"name": "eir-row-1",
				"manufacturing_operation": "MOP-SRC",
				"manufacturing_work_order": "MWO-TEST-001",
			}
		)
		baseline = _sample_log(
			name="L-BASE",
			creation="2026-08-24 10:00:00",
			item_code="M-G-22KT-91.75-Y",
			batch_no="BATCH-A",
			qty_after_transaction=3.24,
			qty_after_transaction_item_based=3.24,
			qty_after_transaction_batch_based=3.24,
			pcs_after_transaction=0,
			pcs_after_transaction_item_based=0,
			pcs_after_transaction_batch_based=0,
			manufacturing_operation="MOP-SRC",
			manufacturing_work_order="MWO-TEST-001",
			row_name="src-row",
			from_warehouse="WH-A",
			to_warehouse="WH-B",
		)
		loss_map = {}
		if loss_qty:
			loss_map[("MOP-SRC", "MWO-TEST-001", "M-G-22KT-91.75-Y", "BATCH-A")] = {
				"loss_qty": loss_qty,
				"loss_pcs": 0,
			}

		created = MagicMock()
		with (
			patch(f"{MFG_OP}.get_last_mop_index", return_value=None),
			patch(f"{MFG_OP}.get_employee_ir_loss_map", return_value=loss_map),
			patch(f"{MFG_OP}.get_current_mop_balance_rows", return_value=[baseline]),
			patch(f"{MFG_OP}._float_tolerance", return_value=0.001),
			patch(f"{MFG_OP}.frappe.new_doc", return_value=created),
		):
			update_new_mop_wtg(
				new_mop,
				employee_ir_doc=eir,
				employee_ir_operation_row=eir_row,
				from_warehouse="WH-Emp",
				to_warehouse="WH-Dept",
			)
		return created

	def test_baseline_cloned_verbatim_when_no_loss(self):
		created = self._run(0)
		self.assertEqual(created.qty_after_transaction_batch_based, 3.24)
		self.assertEqual(created.qty_change, 0)
		self.assertEqual(created.flow_index, 0)
		self.assertEqual(created.manufacturing_operation, "MOP-NEW")

	def test_baseline_reduced_by_loss_not_replaced_by_it(self):
		created = self._run(0.01)
		self.assertEqual(created.qty_after_transaction_batch_based, 3.23)
		self.assertEqual(created.qty_change, -0.01)


class TestNewMopBaselineFamilyTotals(IntegrationTestCase):
	"""Two items of one family losing weight in one Employee IR (MOP-7Q48F).

	``qty_after_transaction`` is the family-wide tier and ``*_item_based`` the
	per-item tier. Deriving them as ``source_value - loss_qty`` netted out only the
	row's OWN loss, so whichever row saved last stamped a header that had silently
	dropped every sibling's movement. Live data: findings 0.622 (SOP) and 1.353
	(PSS) sharing a family total of 1.975, losing 0.014 and 0.030. The header read
	1.945 == 0.622 + 1.323 -- the SOP finding at its PRE-loss weight -- against a
	true batch-tier sum of 0.608 + 1.323 = 1.931.
	"""

	SOP = "F-G-22KT-91.75-Y-PO-SOP-1.60*10.00 MM"
	PSS = "F-G-22KT-91.75-Y-SW-PSS-8.00 MM"

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, order):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			update_new_mop_wtg,
		)

		new_mop = FrappeDict(
			{"name": "MOP-NEW", "previous_mop": "MOP-SRC", "gross_wt": 0}
		)
		eir = FrappeDict({"name": "EIR-1", "doctype": "Employee IR"})
		eir_row = FrappeDict(
			{
				"name": "eir-row-1",
				"manufacturing_operation": "MOP-SRC",
				"manufacturing_work_order": "MWO-TEST-001",
			}
		)

		def _finding(item_code, batch_no, batch_qty, family_qty, name):
			# Every clone carries the family tier as of ITS own write, which is
			# exactly how the stale snapshots reach the new operation.
			return _sample_log(
				name=name,
				creation="2026-08-24 17:27:34",
				item_code=item_code,
				batch_no=batch_no,
				qty_after_transaction=family_qty,
				qty_after_transaction_item_based=batch_qty,
				qty_after_transaction_batch_based=batch_qty,
				pcs_after_transaction=0,
				pcs_after_transaction_item_based=0,
				pcs_after_transaction_batch_based=0,
				manufacturing_operation="MOP-SRC",
				manufacturing_work_order="MWO-TEST-001",
				row_name="src-row",
				from_warehouse="WH-A",
				to_warehouse="WH-B",
			)

		rows = [
			_finding(self.SOP, "B-SOP", 0.622, 1.992, "L-SOP"),
			_finding(self.PSS, "B-PSS", 1.353, 1.975, "L-PSS"),
		]
		if order == "reversed":
			rows.reverse()

		loss_map = {
			("MOP-SRC", "MWO-TEST-001", self.SOP, "B-SOP"): {
				"loss_qty": 0.014,
				"loss_pcs": 0,
			},
			("MOP-SRC", "MWO-TEST-001", self.PSS, "B-PSS"): {
				"loss_qty": 0.030,
				"loss_pcs": 0,
			},
		}

		created = []

		def _new_doc(_doctype):
			doc = MagicMock()
			created.append(doc)
			return doc

		with (
			patch(f"{MFG_OP}.get_last_mop_index", return_value=None),
			patch(f"{MFG_OP}.get_employee_ir_loss_map", return_value=loss_map),
			patch(f"{MFG_OP}.get_current_mop_balance_rows", return_value=rows),
			patch(f"{MFG_OP}._float_tolerance", return_value=0.001),
			patch(f"{MFG_OP}.frappe.new_doc", side_effect=_new_doc),
		):
			update_new_mop_wtg(
				new_mop,
				employee_ir_doc=eir,
				employee_ir_operation_row=eir_row,
				from_warehouse="WH-Emp",
				to_warehouse="WH-Dept",
			)
		return created

	def test_family_tier_is_a_running_total_not_a_per_row_subtraction(self):
		created = self._run("as-written")
		self.assertEqual(len(created), 2)
		sop, pss = created

		# Batch tier: each row's own balance, baseline minus its own loss.
		self.assertAlmostEqual(sop.qty_after_transaction_batch_based, 0.608, places=3)
		self.assertAlmostEqual(pss.qty_after_transaction_batch_based, 1.323, places=3)

		# Family tier: a running total, so the LAST row of the family -- the one
		# whose value reaches the header -- carries the whole family balance.
		self.assertAlmostEqual(pss.qty_after_transaction, 1.931, places=3)
		self.assertAlmostEqual(
			pss.qty_after_transaction,
			sop.qty_after_transaction_batch_based
			+ pss.qty_after_transaction_batch_based,
			places=3,
		)
		# The exact number the bug produced: SOP carried at its pre-loss 0.622.
		self.assertNotAlmostEqual(pss.qty_after_transaction, 1.945, places=3)

		# Distinct item codes, so each item tier is its own balance.
		self.assertAlmostEqual(sop.qty_after_transaction_item_based, 0.608, places=3)
		self.assertAlmostEqual(pss.qty_after_transaction_item_based, 1.323, places=3)

	def test_family_total_is_independent_of_row_order(self):
		"""Last-writer-wins is exactly what the old code was sensitive to."""
		created = self._run("reversed")
		last = created[-1]
		self.assertAlmostEqual(last.qty_after_transaction, 1.931, places=3)
		self.assertAlmostEqual(
			sum(c.qty_after_transaction_batch_based for c in created),
			1.931,
			places=3,
		)


class TestDepartmentIRCloneDedupes(IntegrationTestCase):
	"""Cloning every historical row doubled the ledger at each Department IR hop.

	Observed on the reported MWO: 2 rows -> 4 -> 8, with stale pre-loss
	balances interleaved with the true one.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_issue_clones_latest_snapshot_only(self):
		row = FrappeDict(
			{
				"name": "dir-row-1",
				"manufacturing_operation": "MOP-SRC",
				"manufacturing_work_order": "MWO-TEST-001",
			}
		)
		dir_doc = FrappeDict(
			{"name": "DIR-1", "type": "Issue", "receive_against": None}
		)
		snapshot = [
			_sample_log(
				name="L-LATEST",
				creation="2026-08-24 10:00:02",
				item_code="M-G-22KT-91.75-Y",
				batch_no="BATCH-A",
				qty_after_transaction_batch_based=0.01,
				flow_index=5,
			)
		]
		created = MagicMock()
		with (
			patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=None),
			patch(
				f"{MOP_LOG}.get_current_mop_balance_rows", return_value=snapshot
			) as mock_snapshot,
			patch(f"{MOP_LOG}.frappe.new_doc", return_value=created),
		):
			create_mop_log_for_department_ir(
				dir_doc, row, "WH-Dept", "WH-Transit", "MOP-DEST"
			)

		mock_snapshot.assert_called_once()
		self.assertEqual(mock_snapshot.call_args.args[0], "MOP-SRC")
		self.assertEqual(created.save.call_count, 1)
		self.assertEqual(created.qty_after_transaction_batch_based, 0.01)
		self.assertEqual(created.manufacturing_operation, "MOP-DEST")
		self.assertEqual(created.flow_index, 6)


class TestRepairPatchClassification(IntegrationTestCase):
	"""The repair script must never guess, and never invent metal.

	Only findings it can prove are written; a shortfall (positive delta) points at a
	different cause than this defect and would mean adding gold to the ledger, so it is
	gated behind an explicit opt-in.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _finding(**overrides):
		base = {
			"manufacturing_work_order": "MWO-TEST-001",
			"manufacturing_operation": "MOP-A",
			"item_code": "M-G-22KT-91.75-Y",
			"batch_no": "BATCH-A",
			"expected": 3.21,
			"actual": 3.22,
			"delta": -0.01,
			"straddles": False,
			"already_repaired": False,
			"latest_row": "row-1",
			"latest_voucher": "Employee IR EIR-1",
			"refined_on": "2026-08-24 10:03:18",
		}
		base.update(overrides)
		return base

	def _execute(self, findings, **kwargs):
		from jewellery_erpnext.patches import (
			repair_mwo_wide_mop_log_balances as patch_mod,
		)

		with (
			patch.object(
				patch_mod, "audit_post_refining_contamination", return_value=findings
			),
			patch.object(patch_mod, "recalculate_manufacturing_operation_weights"),
			patch.object(patch_mod.frappe.db, "commit"),
			patch.object(patch_mod, "_append_correction") as appended,
		):
			out = patch_mod.execute(**kwargs)
		return out, appended

	def test_dry_run_writes_nothing(self):
		out, appended = self._execute([self._finding()])
		self.assertEqual(len(out["corrections"]), 1)
		appended.assert_not_called()

	def test_inflation_is_repaired(self):
		out, appended = self._execute([self._finding()], dry_run=False)
		self.assertEqual(len(out["written"]), 1)
		self.assertEqual(appended.call_count, 1)

	def test_shortfall_needs_explicit_opt_in(self):
		shortfall = self._finding(expected=2.36, actual=0.0, delta=2.36)
		out, appended = self._execute([shortfall], dry_run=False)
		self.assertEqual(out["corrections"], [])
		self.assertEqual(len(out["review"]), 1)
		appended.assert_not_called()

		out, appended = self._execute([shortfall], dry_run=False, allow_increase=True)
		self.assertEqual(len(out["corrections"]), 1)
		self.assertEqual(appended.call_count, 1)

	def test_straddling_and_already_repaired_are_never_written(self):
		findings = [
			self._finding(manufacturing_operation="MOP-STRADDLE", straddles=True),
			self._finding(manufacturing_operation="MOP-DONE", already_repaired=True),
		]
		out, appended = self._execute(findings, dry_run=False)
		self.assertEqual(out["corrections"], [])
		self.assertEqual(len(out["review"]), 2)
		appended.assert_not_called()


class TestRefinedMwoCloneKeepsRecastBalance(IntegrationTestCase):
	"""Refining must drop the PRE-refining balance, not the recast that follows it.

	The clone writers used to early-return on ``is_mwo_refined``, so a Department IR
	handoff after a recast wrote no ledger rows at all: the receiving operation opened
	at 0 while the metal stayed stranded on the source. Observed on kg-gk as
	``MOP-N632Z`` at 0.000 against 2.34 held on ``MOP-5W4B8``.
	"""

	CUTOFF = "2026-08-24 12:40:47"

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _rows():
		return [
			_sample_log(
				name="L-PRE",
				creation="2026-08-24 12:30:00",
				item_code="M-G-22KT-91.75-Y",
				batch_no="BATCH-OLD",
				qty_after_transaction_batch_based=3.25,
			),
			_sample_log(
				name="L-POST",
				creation="2026-08-24 12:58:18",
				item_code="M-G-22KT-91.75-Y",
				batch_no="BATCH-A",
				qty_after_transaction_batch_based=2.34,
			),
		]

	def test_unrefined_mwo_keeps_every_row(self):
		with patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=None):
			out = drop_pre_refining_rows(self._rows(), "MWO-TEST-001")
		self.assertEqual(len(out), 2)

	def test_pre_refining_row_dropped_recast_kept(self):
		with patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=self.CUTOFF):
			out = drop_pre_refining_rows(self._rows(), "MWO-TEST-001")
		self.assertEqual([r.name for r in out], ["L-POST"])
		self.assertEqual(out[0].qty_after_transaction_batch_based, 2.34)

	def test_only_pre_refining_rows_drops_everything(self):
		rows = [r for r in self._rows() if r.name == "L-PRE"]
		with patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=self.CUTOFF):
			out = drop_pre_refining_rows(rows, "MWO-TEST-001")
		self.assertEqual(out, [])

	def test_department_ir_clones_the_recast_balance(self):
		"""The end of the reported chain: the handoff must carry 2.34, not nothing."""
		row = FrappeDict(
			{
				"name": "dir-row-1",
				"manufacturing_operation": "MOP-5W4B8",
				"manufacturing_work_order": "MWO-KGJPL-NP00004-003-5-91.75-Y-01",
			}
		)
		dir_doc = FrappeDict(
			{"name": "DIR-658", "type": "Issue", "receive_against": None}
		)
		created = MagicMock()
		with (
			patch(f"{MOP_LOG}.get_mwo_refining_cutoff", return_value=self.CUTOFF),
			patch(f"{MOP_LOG}.get_current_mop_balance_rows", return_value=self._rows()),
			patch(f"{MOP_LOG}.frappe.new_doc", return_value=created),
		):
			create_mop_log_for_department_ir(
				dir_doc, row, "WH-Dept", "WH-Transit", "MOP-N632Z"
			)
		# exactly one clone -- the post-refining row -- onto the receiving operation
		self.assertEqual(created.save.call_count, 1)
		self.assertEqual(created.qty_after_transaction_batch_based, 2.34)
		self.assertEqual(created.manufacturing_operation, "MOP-N632Z")
