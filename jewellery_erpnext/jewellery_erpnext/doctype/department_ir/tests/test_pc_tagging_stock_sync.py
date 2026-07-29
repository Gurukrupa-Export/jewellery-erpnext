"""
Unit tests for pc_tagging_stock_sync.py
Run with: bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.department_ir.tests.test_pc_tagging_stock_sync
"""
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
	SCENARIO_PC_TO_TAGGING_ISSUE,
	SCENARIO_TAGGING_TO_PC_RECEIVE,
	_build_sre_info_by_key,
	_find_row_stock_entry,
	_find_stock_entry_source_warehouse,
	_norm,
	_physical_batch_qty,
	_pick_source_warehouse,
	_requires_pcs,
	_resolve_scenario,
	_row_mop_names,
	_warehouses_with_physical_batch,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync"


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

	@patch(f"{MOD}._get_sre_batch_entries")
	def test_non_batch_sre_does_not_claim_a_batched_key(self, mock_batch_entries):
		"""has_batch_no=0 can only satisfy a non-batch line.

		It used to claim every key of its item, so a batched line inherited an
		unrelated warehouse.
		"""
		sre = self._make_sre(
			"SRE-004",
			"M-18KT-750",
			"PC WO - GEPL",
			5.0,
			has_batch_no=0,
			reservation_based_on="Qty",
		)
		mock_batch_entries.return_value = {}
		keys = {("M-18KT-750", None), ("M-18KT-750", "BATCH-X")}
		result = _build_sre_info_by_key([sre], keys)
		self.assertIn(("M-18KT-750", None), result)
		self.assertNotIn(("M-18KT-750", "BATCH-X"), result)

	@patch(f"{MOD}._get_sre_batch_entries")
	def test_qty_reserved_batch_item_still_maps_to_batched_key(
		self, mock_batch_entries
	):
		"""A batch-tracked item reserved on "Qty" has no Serial and Batch Entry rows to
		match on, so it must still map to its item's batched keys (unchanged)."""
		sre = self._make_sre(
			"SRE-005",
			"M-18KT-750",
			"PC WO - GEPL",
			5.0,
			has_batch_no=1,
			reservation_based_on="Qty",
		)
		mock_batch_entries.return_value = {}
		keys = {("M-18KT-750", "BATCH-X")}
		result = _build_sre_info_by_key([sre], keys)
		self.assertIn(("M-18KT-750", "BATCH-X"), result)
		_, wh, qty = result[("M-18KT-750", "BATCH-X")]
		self.assertEqual(wh, "PC WO - GEPL")
		self.assertAlmostEqual(qty, 5.0)


class TestRowMopNames(unittest.TestCase):
	"""The row key: Manufacturing Operations owned by ONE Department IR row."""

	def test_collects_unique_operations_from_logs(self):
		logs = [
			{"manufacturing_operation": "MOP-75ZO8"},
			{"manufacturing_operation": "MOP-6P9U8"},
			{"manufacturing_operation": "MOP-75ZO8"},
		]
		self.assertEqual(_row_mop_names(logs), ["MOP-6P9U8", "MOP-75ZO8"])

	def test_discards_none_operations(self):
		logs = [{"manufacturing_operation": None}, {"manufacturing_operation": "MOP-A"}]
		self.assertEqual(_row_mop_names(logs), ["MOP-A"])

	def test_falls_back_when_no_operation_on_logs(self):
		self.assertEqual(_row_mop_names([{}], fallback="MOP-PC"), ["MOP-PC"])

	def test_no_fallback_yields_empty(self):
		self.assertEqual(_row_mop_names([{}]), [])

	def test_returns_sorted_for_stable_filters(self):
		logs = [
			{"manufacturing_operation": "MOP-Z"},
			{"manufacturing_operation": "MOP-A"},
		]
		self.assertEqual(_row_mop_names(logs), ["MOP-A", "MOP-Z"])


class TestFindRowStockEntry(unittest.TestCase):
	"""The row-scoped duplicate-SE guard lookup."""

	@patch(f"{MOD}.frappe")
	def test_no_se_names_returns_none(self, mock_frappe):
		self.assertIsNone(_find_row_stock_entry([], ["MOP-A"]))
		mock_frappe.db.get_all.assert_not_called()

	@patch(f"{MOD}.frappe")
	def test_no_row_mops_returns_none(self, mock_frappe):
		self.assertIsNone(_find_row_stock_entry(["MAT-STE-00721"], []))
		mock_frappe.db.get_all.assert_not_called()

	@patch(f"{MOD}.frappe")
	def test_returns_parent_when_this_rows_operation_is_present(self, mock_frappe):
		mock_frappe.db.get_all.return_value = [{"parent": "MAT-STE-00721"}]
		result = _find_row_stock_entry(["MAT-STE-00721"], ["MOP-75ZO8"])
		self.assertEqual(result, "MAT-STE-00721")
		filters = mock_frappe.db.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["manufacturing_operation"], ["in", ["MOP-75ZO8"]])
		self.assertEqual(filters["parent"], ["in", ["MAT-STE-00721"]])
		self.assertEqual(filters["parenttype"], "Stock Entry")

	@patch(f"{MOD}.frappe")
	def test_sibling_rows_stock_entry_does_not_match(self, mock_frappe):
		"""Department-IR-Labh-2026-00185: MAT-STE-00721 covers MOP-75ZO8 (row idx1).

		Row idx2 (MOP-6P9U8) must NOT match it -- that is the whole bug.
		"""
		mock_frappe.db.get_all.return_value = []
		self.assertIsNone(_find_row_stock_entry(["MAT-STE-00721"], ["MOP-6P9U8"]))


class TestFindStockEntrySourceWarehouse(unittest.TestCase):
	"""The SE-Detail source-warehouse fallback, now scoped to the row."""

	@patch(f"{MOD}.frappe")
	def test_scoped_to_row_operations(self, mock_frappe):
		mock_frappe.db.get_all.return_value = [{"s_warehouse": "PC WO - GEPL"}]
		result = _find_stock_entry_source_warehouse(
			["MAT-STE-00721"], ["MOP-6P9U8"], "M-18KT-750", "BATCH-001"
		)
		self.assertEqual(result, "PC WO - GEPL")
		kwargs = mock_frappe.db.get_all.call_args.kwargs
		filters = kwargs["filters"]
		self.assertEqual(filters["manufacturing_operation"], ["in", ["MOP-6P9U8"]])
		self.assertEqual(filters["s_warehouse"], ["is", "set"])
		self.assertEqual(filters["batch_no"], "BATCH-001")
		self.assertEqual(kwargs["order_by"], "creation desc")

	@patch(f"{MOD}.frappe")
	def test_batch_filter_omitted_for_non_batch_lines(self, mock_frappe):
		mock_frappe.db.get_all.return_value = []
		_find_stock_entry_source_warehouse(
			["MAT-STE-00721"], ["MOP-6P9U8"], "M-18KT-750", None
		)
		self.assertNotIn("batch_no", mock_frappe.db.get_all.call_args.kwargs["filters"])

	@patch(f"{MOD}.frappe")
	def test_returns_none_without_se_names_or_row_mops(self, mock_frappe):
		self.assertIsNone(
			_find_stock_entry_source_warehouse([], ["MOP-A"], "M-18KT-750", None)
		)
		self.assertIsNone(
			_find_stock_entry_source_warehouse(["MAT-STE-1"], [], "M-18KT-750", None)
		)
		mock_frappe.db.get_all.assert_not_called()


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


class TestPickSourceWarehouse(unittest.TestCase):
	"""The physical-stock-aware source warehouse picker (the fix's core decision)."""

	def test_empty_candidates_returns_none(self):
		self.assertIsNone(_pick_source_warehouse("D-1", "B1", 0.5, []))

	def test_non_batch_returns_first_candidate_without_physical_check(self):
		with patch(f"{MOD}._physical_batch_qty") as mock_phys:
			wh = _pick_source_warehouse("M-18KT-750", None, 5.0, ["WH-A", "WH-B"])
		self.assertEqual(wh, "WH-A")
		mock_phys.assert_not_called()

	def test_first_candidate_with_stock_chosen_no_regression(self):
		# MOP-Log warehouse (candidate 0) physically covers -> chosen, SRE untouched.
		with patch(f"{MOD}._physical_batch_qty", return_value=1.0):
			wh = _pick_source_warehouse(
				"D-1", "B1", 0.036, ["PC WO - GEPL", "Diamond Setting WO - GEPL"]
			)
		self.assertEqual(wh, "PC WO - GEPL")

	def test_falls_through_to_warehouse_with_stock(self):
		# Core fix: candidate 0 is physically empty, candidate 1 (SRE WH) covers.
		def physical(item, batch, w):
			return {"PC WO - GEPL": 0.0, "Diamond Setting WO - GEPL": 0.036}[w]

		with patch(f"{MOD}._physical_batch_qty", side_effect=physical):
			wh = _pick_source_warehouse(
				"D-1", "B1", 0.036, ["PC WO - GEPL", "Diamond Setting WO - GEPL"]
			)
		self.assertEqual(wh, "Diamond Setting WO - GEPL")

	def test_partial_candidate_rejected_in_favor_of_covering_one(self):
		def physical(item, batch, w):
			return {"PC WO - GEPL": 0.02, "Diamond Setting WO - GEPL": 0.036}[w]

		with patch(f"{MOD}._physical_batch_qty", side_effect=physical):
			wh = _pick_source_warehouse(
				"D-1", "B1", 0.036, ["PC WO - GEPL", "Diamond Setting WO - GEPL"]
			)
		self.assertEqual(wh, "Diamond Setting WO - GEPL")

	def test_none_when_no_candidate_covers(self):
		with patch(f"{MOD}._physical_batch_qty", return_value=0.0):
			self.assertIsNone(
				_pick_source_warehouse(
					"D-1", "B1", 0.036, ["PC WO - GEPL", "Diamond Setting WO - GEPL"]
				)
			)

	def test_within_tolerance_is_accepted(self):
		# physical 0.0359 + TOLERANCE (0.0001) >= requested 0.036
		with patch(f"{MOD}._physical_batch_qty", return_value=0.0359):
			wh = _pick_source_warehouse("D-1", "B1", 0.036, ["PC WO - GEPL"])
		self.assertEqual(wh, "PC WO - GEPL")


class TestPhysicalBatchHelpers(unittest.TestCase):
	def test_physical_batch_qty_none_for_missing_batch_or_warehouse(self):
		self.assertIsNone(_physical_batch_qty("D-1", None, "WH-A"))
		self.assertIsNone(_physical_batch_qty("D-1", "B1", None))

	@patch(f"{MOD}.get_batch_qty")
	def test_physical_batch_qty_ignores_reserved_stock(self, mock_gbq):
		mock_gbq.return_value = 0.036
		result = _physical_batch_qty("D-NT-RO-5-+0-1", "B1", "WH-A")
		self.assertAlmostEqual(result, 0.036)
		mock_gbq.assert_called_once_with(
			"B1", "WH-A", "D-NT-RO-5-+0-1", ignore_reserved_stock=True
		)

	@patch(f"{MOD}.get_batch_qty")
	def test_physical_batch_qty_swallows_errors(self, mock_gbq):
		mock_gbq.side_effect = Exception("boom")
		self.assertEqual(_physical_batch_qty("D-1", "B1", "WH-A"), 0.0)

	def test_warehouses_with_physical_batch_empty_for_no_batch(self):
		self.assertEqual(_warehouses_with_physical_batch("D-1", None), [])

	@patch(f"{MOD}.get_batch_qty")
	def test_warehouses_with_physical_batch_filters_and_sorts(self, mock_gbq):
		mock_gbq.return_value = [
			{"batch_no": "B1", "warehouse": "WH-A", "qty": 0.02},
			{
				"batch_no": "B1",
				"warehouse": "WH-B",
				"qty": 0.0,
			},  # filtered: <= TOLERANCE
			{"batch_no": "B1", "warehouse": "WH-C", "qty": 0.05},
			{"batch_no": "OTHER", "warehouse": "WH-D", "qty": 0.5},  # different batch
		]
		out = _warehouses_with_physical_batch("D-1", "B1")
		self.assertEqual(out, [("WH-C", 0.05), ("WH-A", 0.02)])


class TestProcessRowPhysicalReconcile(unittest.TestCase):
	"""Integration of the physical-stock-aware resolution inside _process_row."""

	def _log(self, item_code, batch_no, qty, pcs=0):
		return {
			"name": f"MOP-LOG-{item_code}",
			"item_code": item_code,
			"batch_no": batch_no,
			"qty_after_transaction_batch_based": qty,
			"pcs_after_transaction_batch_based": pcs,
			"from_warehouse": "Product Certification WO - GEPL",
			"to_warehouse": "Tagging Transit - GEPL",
			"manufacturing_operation": "MOP-TAG",
			"manufacturing_work_order": "MWO-001",
			"flow_index": 1,
		}

	def _row(self):
		row = MagicMock()
		row.manufacturing_work_order = "MWO-001"
		row.manufacturing_operation = "MOP-001"
		row.name = "DIR-ROW-001"
		return row

	@patch("jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs")
	@patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins")
	@patch(f"{MOD}._build_sre_from_context")
	@patch(f"{MOD}.get_batch_qty")
	@patch(f"{MOD}._find_stock_entry_source_warehouse")
	@patch(f"{MOD}._build_sre_info_by_key")
	@patch(f"{MOD}._get_active_sres_for_mwo")
	@patch(f"{MOD}._get_dept_ir_mop_logs")
	@patch(f"{MOD}.frappe")
	def test_core_fix_sources_from_reserved_warehouse(
		self,
		mock_frappe,
		mock_logs,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_gbq,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		mock_frappe.db.get_all.return_value = []  # duplicate-SE guard: none exists
		mock_frappe._ = lambda s: s
		mock_frappe.get_cached_value.return_value = "Carat"
		se = MagicMock()
		mock_frappe.new_doc.return_value = se
		sre_doc = MagicMock()
		sre_doc.docstatus = 1
		mock_frappe.get_doc.return_value = sre_doc

		mock_logs.return_value = [
			self._log("D-NT-RO-5-+0-1", "BATCH-001", 0.036, pcs=6)
		]
		mock_sres.return_value = [{"name": "SRE-001"}]
		mock_sre_info.return_value = {
			("D-NT-RO-5-+0-1", "BATCH-001"): (
				"SRE-001",
				"Diamond Setting WO - GEPL",
				0.036,
			)
		}
		mock_find_se.return_value = None

		def gbq(*args, **kwargs):
			# _physical_batch_qty(batch_no, warehouse, item_code, ignore_reserved_stock=True)
			if args:
				return 0.0 if args[1] == "Product Certification WO - GEPL" else 0.036
			return []  # _warehouses_with_physical_batch (not reached on the happy path)

		mock_gbq.side_effect = gbq
		mock_build_sre.return_value = MagicMock()

		dept_ir = _make_doc(
			"Issue", "Product Certification - GEPL", nxt="Tagging - GEPL"
		)
		dept_ir.name = "DIR-TEST-CORE"

		_process_row(dept_ir, self._row(), SCENARIO_PC_TO_TAGGING_ISSUE)

		# SE built sourcing from the reserved (physically-stocked) warehouse.
		item_appends = [c for c in se.append.call_args_list if c.args[0] == "items"]
		self.assertEqual(len(item_appends), 1)
		item_row = item_appends[0].args[1]
		self.assertEqual(item_row["s_warehouse"], "Diamond Setting WO - GEPL")
		self.assertAlmostEqual(item_row["qty"], 0.036)
		self.assertEqual(item_row["batch_no"], "BATCH-001")
		se.submit.assert_called_once()
		# Old SRE cancelled, replacement created.
		sre_doc.cancel.assert_called_once()
		mock_build_sre.return_value.submit.assert_called_once()

	@patch(f"{MOD}._build_sre_from_context")
	@patch(f"{MOD}.get_batch_qty")
	@patch(f"{MOD}._find_stock_entry_source_warehouse")
	@patch(f"{MOD}._build_sre_info_by_key")
	@patch(f"{MOD}._get_active_sres_for_mwo")
	@patch(f"{MOD}._get_dept_ir_mop_logs")
	@patch(f"{MOD}.frappe")
	def test_fail_fast_when_no_warehouse_has_physical_stock(
		self,
		mock_frappe,
		mock_logs,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_gbq,
		mock_build_sre,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		mock_frappe.db.get_all.return_value = []
		mock_frappe._ = lambda s: s
		mock_frappe.throw.side_effect = frappe.exceptions.ValidationError

		mock_logs.return_value = [
			self._log("D-NT-RO-5-+0-1", "BATCH-001", 0.036, pcs=6)
		]
		mock_sres.return_value = []  # no SRE -> only the MOP-Log warehouse is a candidate
		mock_sre_info.return_value = {}
		mock_find_se.return_value = None

		def gbq(*args, **kwargs):
			if args:  # _physical_batch_qty -> MOP-Log warehouse is empty
				return 0.0
			# _warehouses_with_physical_batch (diagnostic): stock is elsewhere
			return [
				{
					"batch_no": "BATCH-001",
					"warehouse": "Diamond Setting WO - GEPL",
					"qty": 0.036,
				}
			]

		mock_gbq.side_effect = gbq

		dept_ir = _make_doc(
			"Issue", "Product Certification - GEPL", nxt="Tagging - GEPL"
		)
		dept_ir.name = "DIR-TEST-FAIL"

		with self.assertRaises(frappe.exceptions.ValidationError):
			_process_row(dept_ir, self._row(), SCENARIO_PC_TO_TAGGING_ISSUE)

		# No SE created, no SRE cancelled — reservations left intact for resubmit.
		mock_frappe.new_doc.assert_not_called()
		mock_frappe.get_doc.assert_not_called()
		# Diagnostic names the item, batch, qty, empty source WH and where stock is.
		msg = mock_frappe.throw.call_args.args[0]
		self.assertIn("D-NT-RO-5-+0-1", msg)
		self.assertIn("BATCH-001", msg)
		self.assertIn("0.036", msg)
		self.assertIn("Product Certification WO - GEPL", msg)
		self.assertIn("Diamond Setting WO - GEPL", msg)


class _MultiRowHarness(unittest.TestCase):
	"""Shared rig for driving process_pc_tagging_stock_sync over several rows."""

	LOCK_MOD = "jewellery_erpnext.jewellery_erpnext.lock_order"

	def _log(
		self, mop, item_code, batch_no, qty, from_wh="Product Certification WO - GEPL"
	):
		return {
			"name": f"MOP-LOG-{mop}-{item_code}",
			"item_code": item_code,
			"batch_no": batch_no,
			"qty_after_transaction_batch_based": qty,
			"pcs_after_transaction_batch_based": 0,
			"from_warehouse": from_wh,
			"to_warehouse": "Tagging Transit - GEPL",
			"manufacturing_operation": mop,
			"manufacturing_work_order": f"MWO-{mop}",
			"flow_index": 1,
		}

	def _row(self, name, mop, idx):
		row = MagicMock()
		row.name = name
		row.idx = idx
		row.manufacturing_operation = mop
		row.manufacturing_work_order = f"MWO-{mop}"
		return row

	def _doc(self, rows):
		doc = _make_doc("Issue", "Product Certification - GEPL", nxt="Tagging - GEPL")
		doc.name = "Department-IR-Labh-2026-00185"
		doc.manufacturer = "Labh"
		doc.department_ir_operation = rows
		return doc

	def _created_stock_entries(self, mock_frappe):
		"""new_doc() also mints the naming-series stub; only real SEs get submitted."""
		return [d for d in self._new_docs if d.submit.called]

	def _wire(self, mock_frappe):
		self._new_docs = []

		def new_doc(*_a, **_kw):
			doc = MagicMock()
			self._new_docs.append(doc)
			return doc

		mock_frappe.new_doc.side_effect = new_doc
		mock_frappe.get_cached_value.return_value = "Gram"
		mock_frappe.get_doc.return_value = MagicMock(docstatus=1)


@patch(f"{_MultiRowHarness.LOCK_MOD}.preallocate_series_for_docs")
@patch(f"{_MultiRowHarness.LOCK_MOD}.lock_bins")
@patch(f"{MOD}._build_sre_from_context")
@patch(f"{MOD}._physical_batch_qty")
@patch(f"{MOD}._find_stock_entry_source_warehouse")
@patch(f"{MOD}._build_sre_info_by_key")
@patch(f"{MOD}._get_active_sres_for_mwo")
@patch(f"{MOD}._find_row_stock_entry")
@patch(f"{MOD}._submitted_se_names_for_dir")
@patch(f"{MOD}._get_dept_ir_mop_logs")
@patch(f"{MOD}.frappe")
class TestProcessRowMultiRow(_MultiRowHarness):
	"""Regression: Department-IR-Labh-2026-00185.

	The duplicate-SE guard was scoped to the whole Department IR while _process_row
	creates one Stock Entry per row, so row 1 created MAT-STE-00721 and row 2 returned
	silently. Row 2's MOP Log claimed its 3.234 g had moved to Tagging Transit while it
	was still physically at Product Certification WO with no reservation, which then
	blocked the Receive (Department-IR-Labh-2026-00186).
	"""

	ROWS = (
		("76h458ro2r", "MOP-75ZO8", 1, 3.214),
		("76h4em435s", "MOP-6P9U8", 2, 3.234),
	)

	def _setup(
		self, mock_frappe, mock_logs, mock_sres, mock_sre_info, mock_find_se, mock_phys
	):
		self._wire(mock_frappe)
		logs_by_row = {
			row_name: [self._log(mop, "M-G-22KT-91.9-Y", "BATCH-METAL", qty)]
			for row_name, mop, _idx, qty in self.ROWS
		}
		mock_logs.side_effect = lambda _dir, row_name: logs_by_row[row_name]
		mock_sres.return_value = []
		mock_sre_info.return_value = {}
		mock_find_se.return_value = None
		mock_phys.return_value = 100.0  # every candidate covers the line
		return self._doc([self._row(n, m, i) for n, m, i, _q in self.ROWS])

	def test_two_row_department_ir_creates_a_stock_entry_for_each_row(
		self,
		mock_frappe,
		mock_logs,
		mock_se_names,
		mock_find_row_se,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_phys,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			process_pc_tagging_stock_sync,
		)

		doc = self._setup(
			mock_frappe, mock_logs, mock_sres, mock_sre_info, mock_find_se, mock_phys
		)
		mock_se_names.return_value = []
		mock_find_row_se.return_value = None
		mock_build_sre.return_value = MagicMock()

		process_pc_tagging_stock_sync(doc)

		ses = self._created_stock_entries(mock_frappe)
		self.assertEqual(len(ses), 2, "each Department IR row must move its own stock")

		moved = []
		for se in ses:
			appends = [c for c in se.append.call_args_list if c.args[0] == "items"]
			self.assertEqual(len(appends), 1)
			item_row = appends[0].args[1]
			moved.append((item_row["manufacturing_operation"], item_row["qty"]))
			self.assertEqual(item_row["s_warehouse"], "Product Certification WO - GEPL")
			self.assertEqual(item_row["t_warehouse"], "Tagging Transit - GEPL")

		self.assertEqual(sorted(moved), [("MOP-6P9U8", 3.234), ("MOP-75ZO8", 3.214)])
		# Manufacturer comes from the document, not from the submitting user's session
		# defaults -- otherwise the sync only works from the Desk.
		for se in ses:
			self.assertEqual(se.manufacturer, "Labh")
		mock_frappe.msgprint.assert_not_called()
		mock_frappe.log_error.assert_not_called()

	def test_regression_first_rows_stock_entry_does_not_block_the_second(
		self,
		mock_frappe,
		mock_logs,
		mock_se_names,
		mock_find_row_se,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_phys,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		"""Replaying the sync must complete only the row that was skipped."""
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			process_pc_tagging_stock_sync,
		)

		doc = self._setup(
			mock_frappe, mock_logs, mock_sres, mock_sre_info, mock_find_se, mock_phys
		)
		mock_se_names.return_value = ["MAT-STE-00721"]
		mock_find_row_se.side_effect = (
			lambda _names, row_mops: "MAT-STE-00721"
			if "MOP-75ZO8" in row_mops
			else None
		)
		mock_build_sre.return_value = MagicMock()

		process_pc_tagging_stock_sync(doc)

		ses = self._created_stock_entries(mock_frappe)
		self.assertEqual(len(ses), 1, "only the skipped row should be re-synced")
		item_row = [c for c in ses[0].append.call_args_list if c.args[0] == "items"][
			0
		].args[1]
		self.assertEqual(item_row["manufacturing_operation"], "MOP-6P9U8")
		self.assertAlmostEqual(item_row["qty"], 3.234)

		# The covered row is skipped loudly now, not silently.
		self.assertEqual(mock_frappe.msgprint.call_count, 1)
		self.assertEqual(mock_frappe.log_error.call_count, 1)
		self.assertIn("MAT-STE-00721", mock_frappe.log_error.call_args.args[0])

	def test_guard_runs_before_receive_validation(
		self,
		mock_frappe,
		mock_logs,
		mock_se_names,
		mock_find_row_se,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_phys,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		"""An already-synced row must not trip the Receive transit checks on replay."""
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		self._wire(mock_frappe)
		mock_logs.return_value = [
			self._log("MOP-75ZO8", "M-G-22KT-91.9-Y", "BATCH-METAL", 3.214)
		]
		mock_se_names.return_value = ["MAT-STE-00721"]
		mock_find_row_se.return_value = "MAT-STE-00721"
		# Would throw "the stock reservation is at ..., not <transit>" if reached.
		mock_sres.return_value = [{"name": "SRE-001"}]
		mock_sre_info.return_value = {
			("M-G-22KT-91.9-Y", "BATCH-METAL"): ("SRE-001", "PC WO - GEPL", 3.214)
		}

		doc = _make_doc(
			"Receive", "Tagging - GEPL", prev="Product Certification - GEPL"
		)
		_process_row(
			doc, self._row("76h458ro2r", "MOP-75ZO8", 1), SCENARIO_TAGGING_TO_PC_RECEIVE
		)

		mock_frappe.throw.assert_not_called()
		self.assertEqual(self._created_stock_entries(mock_frappe), [])

	def test_guard_uses_raw_logs_not_deduped(
		self,
		mock_frappe,
		mock_logs,
		mock_se_names,
		mock_find_row_se,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_phys,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		"""The flow_index dedup keeps one log per (item, batch); the guard must still see
		every operation the row ever wrote."""
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		self._wire(mock_frappe)
		older = self._log("MOP-OLD", "M-G-22KT-91.9-Y", "BATCH-METAL", 3.2)
		older["flow_index"] = 1
		newer = self._log("MOP-NEW", "M-G-22KT-91.9-Y", "BATCH-METAL", 3.214)
		newer["flow_index"] = 2
		mock_logs.return_value = [older, newer]
		mock_se_names.return_value = ["MAT-STE-00721"]
		mock_find_row_se.return_value = "MAT-STE-00721"

		doc = self._doc([])
		_process_row(
			doc, self._row("76h458ro2r", "MOP-NEW", 1), SCENARIO_PC_TO_TAGGING_ISSUE
		)

		self.assertEqual(mock_find_row_se.call_args.args[1], ["MOP-NEW", "MOP-OLD"])


@patch(f"{_MultiRowHarness.LOCK_MOD}.preallocate_series_for_docs")
@patch(f"{_MultiRowHarness.LOCK_MOD}.lock_bins")
@patch(f"{MOD}._build_sre_from_context")
@patch(f"{MOD}._warehouses_with_physical_batch")
@patch(f"{MOD}._physical_batch_qty")
@patch(f"{MOD}._find_stock_entry_source_warehouse")
@patch(f"{MOD}._build_sre_info_by_key")
@patch(f"{MOD}._get_active_sres_for_mwo")
@patch(f"{MOD}._find_row_stock_entry")
@patch(f"{MOD}._submitted_se_names_for_dir")
@patch(f"{MOD}._get_dept_ir_mop_logs")
@patch(f"{MOD}.frappe")
class TestProcessRowCandidateDedup(_MultiRowHarness):
	"""Candidate warehouses are ordered, de-duplicated and falsy-stripped."""

	def test_duplicate_candidate_warehouse_listed_once_in_diagnostic(
		self,
		mock_frappe,
		mock_logs,
		mock_se_names,
		mock_find_row_se,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_phys,
		mock_where,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		self._wire(mock_frappe)
		mock_frappe.throw.side_effect = frappe.exceptions.ValidationError
		mock_logs.return_value = [
			self._log("MOP-75ZO8", "M-G-22KT-91.9-Y", "BATCH-METAL", 3.214)
		]
		mock_se_names.return_value = []
		mock_find_row_se.return_value = None
		mock_sres.return_value = [{"name": "SRE-001"}]
		# MOP-Log warehouse == SE-Detail warehouse == SRE warehouse.
		mock_sre_info.return_value = {
			("M-G-22KT-91.9-Y", "BATCH-METAL"): (
				"SRE-001",
				"Product Certification WO - GEPL",
				3.214,
			)
		}
		mock_find_se.return_value = "Product Certification WO - GEPL"
		mock_phys.return_value = 0.0
		mock_where.return_value = [("Waxing WO - GEPL", 568.072)]

		doc = self._doc([])
		with self.assertRaises(frappe.exceptions.ValidationError):
			_process_row(
				doc,
				self._row("76h458ro2r", "MOP-75ZO8", 1),
				SCENARIO_PC_TO_TAGGING_ISSUE,
			)

		msg = mock_frappe.throw.call_args.args[0]
		self.assertEqual(msg.count("Product Certification WO - GEPL (physical"), 1)
		self.assertIn("Waxing WO - GEPL", msg)

	def test_falsy_candidates_are_stripped(
		self,
		mock_frappe,
		mock_logs,
		mock_se_names,
		mock_find_row_se,
		mock_sres,
		mock_sre_info,
		mock_find_se,
		mock_phys,
		mock_where,
		mock_build_sre,
		_mock_lock,
		_mock_series,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
			_process_row,
		)

		self._wire(mock_frappe)
		log = self._log("MOP-75ZO8", "M-G-22KT-91.9-Y", "BATCH-METAL", 3.214)
		log["from_warehouse"] = None
		mock_logs.return_value = [log]
		mock_se_names.return_value = []
		mock_find_row_se.return_value = None
		mock_sres.return_value = [{"name": "SRE-001"}]
		mock_sre_info.return_value = {
			("M-G-22KT-91.9-Y", "BATCH-METAL"): (
				"SRE-001",
				"Tagging Transit - GEPL",
				3.214,
			)
		}
		mock_find_se.return_value = None
		mock_phys.return_value = 100.0

		doc = self._doc([])
		with patch(f"{MOD}._pick_source_warehouse") as mock_pick:
			mock_pick.return_value = "Tagging Transit - GEPL"
			_process_row(
				doc,
				self._row("76h458ro2r", "MOP-75ZO8", 1),
				SCENARIO_PC_TO_TAGGING_ISSUE,
			)
		self.assertEqual(mock_pick.call_args.args[3], ["Tagging Transit - GEPL"])


if __name__ == "__main__":
	unittest.main()
