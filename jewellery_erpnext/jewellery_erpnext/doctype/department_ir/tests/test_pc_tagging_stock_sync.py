"""
Unit tests for pc_tagging_stock_sync.py
Run with: bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.department_ir.tests.test_pc_tagging_stock_sync
"""
import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
	SCENARIO_PC_TO_TAGGING_ISSUE,
	SCENARIO_TAGGING_TO_PC_RECEIVE,
	_build_sre_info_by_key,
	_norm,
	_requires_pcs,
	_resolve_scenario,
)


def _make_doc(type_, current, nxt=None, prev=None):
	doc = MagicMock()
	doc.type = type_
	doc.current_department = current
	doc.next_department = nxt or ""
	doc.previous_department = prev or ""
	doc.company = "Test Company"
	doc.name = "DIR-TEST-001"
	return doc


class TestNorm(unittest.TestCase):
	def test_strips_company_suffix(self):
		self.assertEqual(_norm("Product Certification - GEPL"), "Product Certification")

	def test_no_suffix_unchanged(self):
		self.assertEqual(_norm("Tagging"), "Tagging")

	def test_empty_string(self):
		self.assertEqual(_norm(""), "")


class TestRequiresPcs(unittest.TestCase):
	def test_diamond_item(self):
		self.assertTrue(_requires_pcs("D-18KT-1.00"))

	def test_gemstone_item(self):
		self.assertTrue(_requires_pcs("G-RU-OVAL"))

	def test_metal_item(self):
		self.assertFalse(_requires_pcs("M-18KT-750"))

	def test_finding_item(self):
		self.assertFalse(_requires_pcs("F-HOOK"))

	def test_empty_string(self):
		self.assertFalse(_requires_pcs(""))

	def test_none_string(self):
		self.assertFalse(_requires_pcs(None))


class TestResolveScenario(unittest.TestCase):
	def test_issue_pc_to_tagging(self):
		doc = _make_doc("Issue", "Product Certification - GEPL", nxt="Tagging - GEPL")
		self.assertEqual(_resolve_scenario(doc), SCENARIO_PC_TO_TAGGING_ISSUE)

	def test_receive_pc_to_tagging(self):
		doc = _make_doc(
			"Receive", "Tagging - GEPL", prev="Product Certification - GEPL"
		)
		self.assertEqual(_resolve_scenario(doc), SCENARIO_TAGGING_TO_PC_RECEIVE)

	def test_issue_different_dept_returns_none(self):
		doc = _make_doc("Issue", "Waxing - GEPL", nxt="Polishing - GEPL")
		self.assertIsNone(_resolve_scenario(doc))

	def test_receive_different_dept_returns_none(self):
		doc = _make_doc("Receive", "Polishing - GEPL", prev="Waxing - GEPL")
		self.assertIsNone(_resolve_scenario(doc))

	def test_issue_pc_to_non_tagging_returns_none(self):
		doc = _make_doc("Issue", "Product Certification - GEPL", nxt="Polishing - GEPL")
		self.assertIsNone(_resolve_scenario(doc))

	def test_receive_non_pc_to_tagging_returns_none(self):
		doc = _make_doc("Receive", "Tagging - GEPL", prev="Waxing - GEPL")
		self.assertIsNone(_resolve_scenario(doc))


class TestBuildSreInfoByKey(unittest.TestCase):
	def _make_sre(
		self,
		name,
		item_code,
		warehouse,
		reserved_qty,
		delivered_qty=0.0,
		has_batch_no=1,
		reservation_based_on="Serial and Batch",
	):
		return {
			"name": name,
			"item_code": item_code,
			"warehouse": warehouse,
			"reserved_qty": reserved_qty,
			"delivered_qty": delivered_qty,
			"has_batch_no": has_batch_no,
			"reservation_based_on": reservation_based_on,
		}

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_sre_batch_entries"
	)
	def test_batch_sre_mapped_by_item_and_batch(self, mock_batch_entries):
		sre = self._make_sre("SRE-001", "D-18KT-1.00", "PC WO - GEPL", 2.36)
		mock_batch_entries.return_value = {
			"SRE-001": [
				{"batch_no": "BATCH-001", "qty": 2.36, "warehouse": "PC WO - GEPL"}
			]
		}
		keys = {("D-18KT-1.00", "BATCH-001")}
		result = _build_sre_info_by_key([sre], keys)
		self.assertIn(("D-18KT-1.00", "BATCH-001"), result)
		name, wh, qty = result[("D-18KT-1.00", "BATCH-001")]
		self.assertEqual(name, "SRE-001")
		self.assertEqual(wh, "PC WO - GEPL")
		self.assertAlmostEqual(qty, 2.36)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_sre_batch_entries"
	)
	def test_non_batch_sre_mapped_by_item(self, mock_batch_entries):
		sre = self._make_sre(
			"SRE-002",
			"M-18KT-750",
			"PC WO - GEPL",
			5.0,
			has_batch_no=0,
			reservation_based_on="Qty",
		)
		mock_batch_entries.return_value = {}
		keys = {("M-18KT-750", None)}
		result = _build_sre_info_by_key([sre], keys)
		self.assertIn(("M-18KT-750", None), result)
		_, wh, qty = result[("M-18KT-750", None)]
		self.assertEqual(wh, "PC WO - GEPL")
		self.assertAlmostEqual(qty, 5.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_sre_batch_entries"
	)
	def test_key_not_in_sres_not_in_result(self, mock_batch_entries):
		sre = self._make_sre("SRE-003", "D-18KT-1.00", "PC WO - GEPL", 2.0)
		mock_batch_entries.return_value = {
			"SRE-003": [
				{"batch_no": "BATCH-AAA", "qty": 2.0, "warehouse": "PC WO - GEPL"}
			]
		}
		keys = {("D-18KT-1.00", "BATCH-BBB")}  # different batch
		result = _build_sre_info_by_key([sre], keys)
		self.assertNotIn(("D-18KT-1.00", "BATCH-BBB"), result)


class TestProcessRowIssueValidation(unittest.TestCase):
	"""Tests that Issue scenario correctly validates and builds SE + SRE."""

	def _make_log(
		self,
		item_code,
		batch_no,
		qty,
		pcs=0,
		from_wh="PC WO - GEPL",
		to_wh="Tagging Transit - GEPL",
	):
		return {
			"name": f"MOP-LOG-{item_code}",
			"item_code": item_code,
			"batch_no": batch_no,
			"qty_after_transaction_batch_based": qty,
			"pcs_after_transaction_batch_based": pcs,
			"from_warehouse": from_wh,
			"to_warehouse": to_wh,
			"manufacturing_operation": "MOP-001",
			"manufacturing_work_order": "MWO-001",
		}

	def _make_sre_row(self, name, item_code, wh, reserved=2.36, has_batch=1):
		return {
			"name": name,
			"item_code": item_code,
			"warehouse": wh,
			"reserved_qty": reserved,
			"delivered_qty": 0.0,
			"has_batch_no": has_batch,
			"reservation_based_on": "Serial and Batch" if has_batch else "Qty",
			"voucher_type": "Sales Order",
			"voucher_no": "SO-001",
			"voucher_detail_no": "SO-ITEM-001",
			"company": "GEPL",
			"stock_uom": "Gram",
			"manufacturing_operation": "MOP-001",
			"manufacturing_work_order": "MWO-001",
		}

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync.frappe"
	)
	def test_no_mop_logs_returns_early(self, mock_frappe):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		mock_frappe.db.get_all.return_value = []  # no MOP Logs
		dept_ir = _make_doc(
			"Issue", "Product Certification - GEPL", nxt="Tagging - GEPL"
		)
		row = MagicMock()
		row.manufacturing_work_order = "MWO-001"
		row.manufacturing_operation = "MOP-001"
		row.name = "DIR-ROW-001"

		_process_row(dept_ir, row, SCENARIO_PC_TO_TAGGING_ISSUE)
		# Should not create any SE — returns early
		mock_frappe.new_doc.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._resolve_dept_transit_wh"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_active_sres_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_dept_ir_mop_logs"
	)
	def test_receive_sre_not_at_transit_raises(
		self, mock_logs, mock_sres, mock_transit_wh
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		mock_logs.return_value = [self._make_log("D-18KT-1.00", "BATCH-001", 2.36)]
		# transit_wh resolved from current_department = Tagging
		mock_transit_wh.return_value = "Tagging Transit - GEPL"

		# SRE is still at PC WO (Issue not yet submitted)
		sre_at_wrong_wh = self._make_sre_row("SRE-001", "D-18KT-1.00", "PC WO - GEPL")
		mock_sres.return_value = [sre_at_wrong_wh]

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_sre_batch_entries"
		) as mock_be:
			mock_be.return_value = {
				"SRE-001": [
					{"batch_no": "BATCH-001", "qty": 2.36, "warehouse": "PC WO - GEPL"}
				]
			}
			# current_department = Tagging (that's what Receive sees)
			dept_ir = _make_doc(
				"Receive", "Tagging - GEPL", prev="Product Certification - GEPL"
			)
			row = MagicMock()
			row.manufacturing_work_order = "MWO-001"
			row.manufacturing_operation = "MOP-001"
			row.name = "DIR-ROW-001"

			with self.assertRaises(frappe.exceptions.ValidationError):
				_process_row(dept_ir, row, SCENARIO_TAGGING_TO_PC_RECEIVE)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._resolve_dept_transit_wh"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_active_sres_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_dept_ir_mop_logs"
	)
	def test_receive_no_sre_raises(self, mock_logs, mock_sres, mock_transit_wh):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		mock_logs.return_value = [self._make_log("D-18KT-1.00", "BATCH-001", 2.36)]
		mock_transit_wh.return_value = "Tagging Transit - GEPL"
		mock_sres.return_value = []  # no active SREs

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._get_sre_batch_entries"
		) as mock_be:
			mock_be.return_value = {}
			dept_ir = _make_doc(
				"Receive", "Tagging - GEPL", prev="Product Certification - GEPL"
			)
			row = MagicMock()
			row.manufacturing_work_order = "MWO-001"
			row.manufacturing_operation = "MOP-001"
			row.name = "DIR-ROW-001"

			with self.assertRaises(frappe.exceptions.ValidationError):
				_process_row(dept_ir, row, SCENARIO_TAGGING_TO_PC_RECEIVE)


class TestProcessPcTaggingStockSync(unittest.TestCase):
	"""Tests for the top-level process_pc_tagging_stock_sync dispatcher."""

	def test_non_pc_tagging_scenario_no_op(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			process_pc_tagging_stock_sync,
		)

		doc = _make_doc("Issue", "Waxing - GEPL", nxt="Polishing - GEPL")
		doc.department_ir_operation = []
		# Should return without doing anything — no error
		process_pc_tagging_stock_sync(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._process_row"
	)
	def test_issue_calls_process_row_for_each_operation(self, mock_process):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			process_pc_tagging_stock_sync,
		)

		doc = _make_doc("Issue", "Product Certification - GEPL", nxt="Tagging - GEPL")
		row1 = MagicMock()
		row2 = MagicMock()
		doc.department_ir_operation = [row1, row2]

		process_pc_tagging_stock_sync(doc, cancel=False)

		self.assertEqual(mock_process.call_count, 2)
		mock_process.assert_any_call(doc, row1, SCENARIO_PC_TO_TAGGING_ISSUE)
		mock_process.assert_any_call(doc, row2, SCENARIO_PC_TO_TAGGING_ISSUE)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync._handle_cancel_row"
	)
	def test_cancel_calls_handle_cancel_row(self, mock_cancel):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			process_pc_tagging_stock_sync,
		)

		doc = _make_doc("Issue", "Product Certification - GEPL", nxt="Tagging - GEPL")
		row1 = MagicMock()
		doc.department_ir_operation = [row1]

		process_pc_tagging_stock_sync(doc, cancel=True)

		mock_cancel.assert_called_once_with(doc, row1, SCENARIO_PC_TO_TAGGING_ISSUE)


if __name__ == "__main__":
	unittest.main()
