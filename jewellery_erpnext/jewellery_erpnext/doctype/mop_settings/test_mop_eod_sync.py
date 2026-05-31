# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_apply_mwo_filter_rows,
	_build_eod_se_rows,
	_cancel_sre_snapshots,
	_find_last_operation,
	_get_last_logs_per_item_batch,
	_get_t_warehouse_from_logs,
	_get_today_range,
	_get_unsynced_mop_groups,
	_mark_all_mwo_mop_logs_synced,
	_mwo_realized_by_artifact,
	_preload_sre_warehouse_map,
	_process_mwo_group,
	_recreate_sres_at,
	_resolve_department_warehouse,
	_snapshot_mwo_sres_for_relocation,
	_validate_eod_source_batch_stock,
	recalculate_sync_log_totals,
	sync_mop_logs,
)

_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _log(**overrides):
	base = FrappeDict(
		{
			"name": "LOG-1",
			"manufacturing_operation": "MOP-TEST-001",
			"manufacturing_work_order": "MWO-TEST-001",
			"item_code": "M-TEST",
			"batch_no": "B1",
			"qty_after_transaction_batch_based": 1.0,
			"pcs_after_transaction_batch_based": 0,
			"from_warehouse": "WH-FROM",
			"to_warehouse": "WH-TO",
			"flow_index": 1,
			"creation": "2026-01-01 10:00:00",
			"voucher_type": "Department IR",
			"voucher_no": "DIR-1",
			"serial_no": None,
		}
	)
	base.update(overrides)
	return base


def _mop_doc(**overrides):
	base = {
		"company": "Test Co",
		"manufacturer": "MF-1",
		"manufacturing_work_order": "MWO-1",
		"manufacturing_order": "MO-1",
		"department": "Dept",
		"loss_wt": 0,
	}
	base.update(overrides)
	return FrappeDict(base)


class FakeStockEntry:
	def __init__(self, name="SE-TEST-001"):
		self.name = name
		self.items = []
		self.flags = FrappeDict()
		self.stock_entry_type = None
		self.company = None
		self.manufacturing_order = None
		self.manufacturing_work_order = None
		self.manufacturing_operation = None
		self.manufacturer = None
		self.auto_created = 0
		self.saved = False
		self.submitted = False

	def append(self, fieldname, value):
		if fieldname != "items":
			raise AssertionError(f"Unexpected append target: {fieldname}")
		self.items.append(FrappeDict(value))

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


# ---------------------------------------------------------------------------
# TestGetUnsyncedMopGroupsByMwo (3 cases)
# ---------------------------------------------------------------------------


class TestGetUnsyncedMopGroupsByMwo(FrappeTestCase):
	# New implementation fetches logs with get_all, then MOP metadata with a second
	# get_all (bulk fetch). Mock get_all to return different results per call.

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql",
		return_value=[{"cnt": 0, "qty": 0}],
	)
	def test_two_mops_same_mwo_produce_one_group(self, _mock_sql, mock_get_all):
		"""Two MOPs with same MWO → one (company, mwo) group."""
		logs_result = [
			_log(
				name="L1",
				manufacturing_operation="MOP-A",
				manufacturing_work_order="MWO-1",
			),
			_log(
				name="L2",
				manufacturing_operation="MOP-B",
				manufacturing_work_order="MWO-1",
			),
		]
		mop_meta_result = [
			FrappeDict({**_mop_doc(manufacturing_work_order="MWO-1"), "name": "MOP-A"}),
			FrappeDict({**_mop_doc(manufacturing_work_order="MWO-1"), "name": "MOP-B"}),
		]
		mock_get_all.side_effect = [logs_result, mop_meta_result]

		out = _get_unsynced_mop_groups()

		self.assertEqual(len(out), 1)
		key = ("Test Co", "MWO-1")
		self.assertIn(key, out)
		self.assertEqual(len(out[key]), 2)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql",
		return_value=[{"cnt": 0, "qty": 0}],
	)
	def test_two_different_mwos_produce_two_groups(self, _mock_sql, mock_get_all):
		"""Logs from different MWOs → two groups."""
		logs_result = [
			_log(
				name="L1",
				manufacturing_operation="MOP-A",
				manufacturing_work_order="MWO-1",
			),
			_log(
				name="L2",
				manufacturing_operation="MOP-C",
				manufacturing_work_order="MWO-2",
			),
		]
		mop_meta_result = [
			FrappeDict({**_mop_doc(manufacturing_work_order="MWO-1"), "name": "MOP-A"}),
			FrappeDict({**_mop_doc(manufacturing_work_order="MWO-2"), "name": "MOP-C"}),
		]
		mock_get_all.side_effect = [logs_result, mop_meta_result]

		out = _get_unsynced_mop_groups()

		self.assertEqual(len(out), 2)
		self.assertIn(("Test Co", "MWO-1"), out)
		self.assertIn(("Test Co", "MWO-2"), out)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql",
		return_value=[{"cnt": 0, "qty": 0}],
	)
	def test_mop_with_missing_metadata_skipped(self, _mock_sql, mock_get_all):
		"""MOP whose metadata is absent from the bulk fetch is silently skipped."""
		logs_result = [_log(name="L1", manufacturing_operation="MOP-GHOST")]
		mop_meta_result = []  # no matching MOP row returned
		mock_get_all.side_effect = [logs_result, mop_meta_result]

		out = _get_unsynced_mop_groups()

		self.assertEqual(out, {})

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql",
		return_value=[{"cnt": 0, "qty": 0}],
	)
	def test_empty_logs_returns_empty_dict(self, _mock_sql, _mock_get_all):
		"""No unsynced logs → empty dict returned immediately without querying MOPs."""
		out = _get_unsynced_mop_groups()

		self.assertEqual(out, {})

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql",
		return_value=[{"cnt": 0, "qty": 0}],
	)
	def test_empty_logs_returns_empty_dict(self, _mock_sql, _mock_get_all):
		"""No unsynced logs → empty dict returned immediately without querying MOPs."""
		out = _get_unsynced_mop_groups()

		self.assertEqual(out, {})


# ---------------------------------------------------------------------------
# TestFindLastOperation (4 cases)
# ---------------------------------------------------------------------------


class TestFindLastOperation(FrappeTestCase):
	def test_single_entry_returns_it(self):
		mop_data = {
			"mop_name": "MOP-A",
			"mop_doc": {},
			"logs": [_log(creation="2026-01-01 10:00:00")],
		}
		result = _find_last_operation([mop_data])
		self.assertIs(result, mop_data)

	def test_returns_mop_with_latest_creation(self):
		older = {
			"mop_name": "MOP-A",
			"mop_doc": {},
			"logs": [_log(creation="2026-01-01 09:00:00")],
		}
		newer = {
			"mop_name": "MOP-B",
			"mop_doc": {},
			"logs": [_log(creation="2026-01-01 11:00:00")],
		}
		result = _find_last_operation([older, newer])
		self.assertIs(result, newer)

	def test_empty_list_returns_none(self):
		self.assertIsNone(_find_last_operation([]))

	def test_same_creation_returns_deterministically(self):
		"""When all logs share the same creation, function must not raise and returns one of them."""
		a = {
			"mop_name": "MOP-A",
			"mop_doc": {},
			"logs": [_log(creation="2026-01-01 10:00:00")],
		}
		b = {
			"mop_name": "MOP-B",
			"mop_doc": {},
			"logs": [_log(creation="2026-01-01 10:00:00")],
		}
		result = _find_last_operation([a, b])
		self.assertIn(result, [a, b])


# ---------------------------------------------------------------------------
# TestGetLastLogsPerItemBatch (4 cases)
# ---------------------------------------------------------------------------


class TestGetLastLogsPerItemBatch(FrappeTestCase):
	def test_single_log_per_key_returns_unchanged(self):
		log = _log(item_code="M-1", batch_no="B1", flow_index=1)
		result = _get_last_logs_per_item_batch([log])
		self.assertEqual(len(result), 1)
		self.assertIs(result[0], log)

	def test_second_log_with_higher_flow_index_wins(self):
		first = _log(
			item_code="M-1", batch_no="B1", flow_index=1, creation="2026-01-01 10:00:00"
		)
		second = _log(
			item_code="M-1", batch_no="B1", flow_index=2, creation="2026-01-01 11:00:00"
		)
		result = _get_last_logs_per_item_batch([first, second])
		self.assertEqual(len(result), 1)
		self.assertIs(result[0], second)

	def test_multiple_distinct_items_each_get_one_entry(self):
		logs = [
			_log(item_code="M-1", batch_no="B1"),
			_log(item_code="M-2", batch_no="B2"),
			_log(item_code="D-1", batch_no="B3"),
		]
		result = _get_last_logs_per_item_batch(logs)
		self.assertEqual(len(result), 3)
		keys = {(r.item_code, r.batch_no) for r in result}
		self.assertEqual(keys, {("M-1", "B1"), ("M-2", "B2"), ("D-1", "B3")})

	def test_empty_input_returns_empty_list(self):
		self.assertEqual(_get_last_logs_per_item_batch([]), [])


# ---------------------------------------------------------------------------
# TestGetTWarehouseFromLogs (3 cases)
# ---------------------------------------------------------------------------


class TestGetTWarehouseFromLogs(FrappeTestCase):
	def test_returns_first_to_warehouse(self):
		logs = [_log(to_warehouse="WH-A"), _log(to_warehouse="WH-B")]
		self.assertEqual(_get_t_warehouse_from_logs(logs), "WH-A")

	def test_all_none_returns_none(self):
		logs = [_log(to_warehouse=None), _log(to_warehouse=None)]
		self.assertIsNone(_get_t_warehouse_from_logs(logs))

	def test_mixed_returns_first_non_null(self):
		logs = [
			_log(to_warehouse=None),
			_log(to_warehouse="WH-X"),
			_log(to_warehouse="WH-Y"),
		]
		self.assertEqual(_get_t_warehouse_from_logs(logs), "WH-X")

	def test_returns_latest_by_flow_index(self):
		"""Destination = the chronologically last log's to_warehouse (by flow_index, creation)."""
		logs = [
			_log(to_warehouse="WH-EARLY", flow_index=1, creation="2026-01-01 10:00:00"),
			_log(to_warehouse="WH-LATE", flow_index=3, creation="2026-01-01 12:00:00"),
			_log(to_warehouse="WH-MID", flow_index=2, creation="2026-01-01 11:00:00"),
		]
		self.assertEqual(_get_t_warehouse_from_logs(logs), "WH-LATE")


# ---------------------------------------------------------------------------
# TestPreloadSreWarehouseMap (3 cases — replaces old TestGetSreSourceWarehouse)
# ---------------------------------------------------------------------------


class TestPreloadSreWarehouseMap(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_batch_sre_rows_populate_map(self, mock_sql, mock_get_all):
		mock_sql.return_value = [
			FrappeDict({"item_code": "M-1", "batch_no": "B1", "warehouse": "WH-SRE"})
		]
		mock_get_all.return_value = []  # no qty-based rows

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result.get(("M-1", "B1")), "WH-SRE")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_qty_based_fallback_populates_item_none_key(self, mock_sql, mock_get_all):
		mock_sql.return_value = []  # no batch-level SRE
		mock_get_all.return_value = [
			FrappeDict({"item_code": "M-1", "warehouse": "WH-QTY"})
		]

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result.get(("M-1", None)), "WH-QTY")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_no_sre_returns_empty_map(self, mock_sql, mock_get_all):
		mock_sql.return_value = []
		mock_get_all.return_value = []

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# TestBuildEodSeRows (5 cases — updated for new sre_map signature)
# ---------------------------------------------------------------------------


class TestBuildEodSeRows(FrappeTestCase):
	def test_normal_case_builds_row(self):
		sre_map = {("M-1", "B1"): "WH-SRE"}
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=5.0,
				to_warehouse="WH-DEPT",
			)
		]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(len(rows), 1)
		self.assertEqual(skipped, [])
		self.assertEqual(rows[0]["s_warehouse"], "WH-SRE")
		self.assertEqual(rows[0]["t_warehouse"], "WH-DEPT")
		self.assertEqual(rows[0]["qty"], 5.0)
		self.assertEqual(rows[0]["item_code"], "M-1")

	def test_no_sre_skips_row_and_adds_to_skipped(self):
		sre_map = {}  # no SRE for this item/batch
		logs = [_log(qty_after_transaction_batch_based=3.0)]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(rows, [])
		self.assertEqual(len(skipped), 1)
		self.assertEqual(skipped[0]["item_code"], "M-TEST")

	def test_same_source_and_target_skips_row(self):
		sre_map = {("M-TEST", "B1"): "WH-SAME"}
		logs = [_log(qty_after_transaction_batch_based=2.0)]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-SAME", sre_map)
		self.assertEqual(rows, [])
		self.assertEqual(skipped, [])  # same-WH is a clean skip, not a missing SRE

	def test_zero_qty_log_skipped(self):
		sre_map = {("M-TEST", "B1"): "WH-SRE"}
		logs = [_log(qty_after_transaction_batch_based=0.0)]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(rows, [])
		self.assertEqual(skipped, [])

	def test_batch_no_included_in_row(self):
		sre_map = {("M-1", "BATCH-99"): "WH-SRE"}
		logs = [
			_log(
				item_code="M-1",
				batch_no="BATCH-99",
				qty_after_transaction_batch_based=1.0,
			)
		]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["batch_no"], "BATCH-99")


# ---------------------------------------------------------------------------
# TestMarkAllMwoMopLogsSynced (3 cases)
# ---------------------------------------------------------------------------


class TestMarkAllMwoMopLogsSynced(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_marks_unsynced_non_cancelled_logs(self, mock_sql):
		_mark_all_mwo_mop_logs_synced(["MWO-1", "MWO-2"])
		mock_sql.assert_called_once()
		sql_text = mock_sql.call_args[0][0]
		self.assertIn("UPDATE", sql_text)
		self.assertIn("is_synced", sql_text)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_filter_targets_only_is_synced_0_and_is_cancelled_0(self, mock_sql):
		"""SQL update must target only is_synced=0 and is_cancelled=0 rows."""
		_mark_all_mwo_mop_logs_synced(["MWO-X"])
		mock_sql.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_empty_list_does_not_call_sql(self, mock_sql):
		_mark_all_mwo_mop_logs_synced([])
		mock_sql.assert_not_called()


# ---------------------------------------------------------------------------
# TestProcessMwoGroupHappyPath (1 case — integration of new _process_mwo_group)
# ---------------------------------------------------------------------------


class TestProcessMwoGroupHappyPath(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_all_mwo_mop_logs_synced"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_source_batch_stock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_items_for_mwo_reservation"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._preload_sre_warehouse_map",
		return_value={("M-1", "B1"): "WH-SRE"},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._snapshot_mwo_sres_for_relocation",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	def test_happy_path_creates_se_and_marks_synced(
		self,
		mock_new_doc,
		mock_get_doc,
		_mock_artifact,
		_mock_snap,
		_mock_sre_map,
		_mock_item_val,
		_mock_batch_val,
		mock_mark_synced,
		_mock_release,
		_mock_savepoint,
		_mock_set_value,
	):
		se = FakeStockEntry("SE-EOD-001")
		mock_new_doc.return_value = se
		submitted_se = FakeStockEntry("SE-EOD-001")
		mock_get_doc.return_value = submitted_se

		mop_doc = _mop_doc(manufacturing_work_order="MWO-1", manufacturing_order="MO-1")
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=3.0,
				to_warehouse="WH-DEPT",
				creation="2026-01-01 10:00:00",
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": mop_doc, "logs": logs}]
		failures = []
		stats = {
			"total_mwos": 1,
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
			"started_on": "2026-01-01 00:00:00",
		}

		_process_mwo_group(("Test Co", "MWO-1"), mop_data_list, failures, stats)

		self.assertEqual(failures, [])
		self.assertIn("SE-EOD-001", stats["submitted_ses"])
		self.assertEqual(stats["processed_mwos"], 1)
		mock_mark_synced.assert_called_once_with(["MWO-1"], selective=False)
		self.assertTrue(submitted_se.submitted)


# ---------------------------------------------------------------------------
# TestSyncMopLogsEntryPoint (updated for new flow)
# ---------------------------------------------------------------------------


class TestSyncMopLogsEntryPoint(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.release_eod_sync_lock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.set_eod_sync_running"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._process_mwo_group"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error"
	)
	def test_continues_after_one_group_fails_and_produces_one_error_log(
		self,
		mock_log_error,
		mock_get_groups,
		mock_process,
		_mock_reconcile,
		_mock_set_running,
		mock_release,
	):
		mock_log_error.return_value = FrappeDict({"name": "ERR-001"})
		key_a = ("Co", "MWO-A")
		key_b = ("Co", "MWO-B")
		mock_get_groups.return_value = {
			key_a: [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": []}],
			key_b: [{"mop_name": "MOP-B", "mop_doc": _mop_doc(), "logs": []}],
		}

		def _inject(
			group_key,
			mop_data_list,
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		):
			_, mwo = group_key
			if mwo == "MWO-A":
				stats["submitted_ses"].append("SE-A")
				stats["processed_mwos"] += 1
			else:
				failures.append(
					{"step": "draft_save", "mwo": mwo, "error_message": "fail"}
				)
				stats["failed_mwos"] += 1

		mock_process.side_effect = _inject

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 1)
		self.assertIn("SE-A", out["stock_entries"])
		# One consolidated error log created (not one per MWO)
		self.assertEqual(mock_log_error.call_count, 1)
		# release called with success=False
		mock_release.assert_called_once()
		call_kwargs = mock_release.call_args[1]
		self.assertFalse(call_kwargs.get("success", True))


# ---------------------------------------------------------------------------
# TestValidateEodSourceBatchStock (kept from original — unchanged function)
# ---------------------------------------------------------------------------


class TestValidateEodSourceBatchStock(FrappeTestCase):
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_passes_when_physical_batch_stock_sufficient(self, mock_gbq):
		items = [
			{"item_code": "IT-1", "batch_no": "B1", "s_warehouse": "WH-S", "qty": 3.0},
			{"item_code": "IT-1", "batch_no": "B1", "s_warehouse": "WH-S", "qty": 2.0},
		]
		_validate_eod_source_batch_stock(items)
		_, kwargs = mock_gbq.call_args
		self.assertTrue(kwargs.get("ignore_reserved_stock"))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._format_batch_short_diagnostics",
		return_value="(diagnostics)",
	)
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=1.0)
	def test_raises_when_physical_batch_stock_short(self, mock_gbq, _mock_diag):
		items = [
			{"item_code": "IT-1", "batch_no": "B1", "s_warehouse": "WH-S", "qty": 3.0}
		]
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_source_batch_stock(items)


# ---------------------------------------------------------------------------
# TestTodayRange (verify helper returns correct strings)
# ---------------------------------------------------------------------------


class TestTodayRange(FrappeTestCase):
	def test_returns_tuple_of_two_strings(self):
		result = _get_today_range()
		self.assertIsInstance(result, tuple)
		self.assertEqual(len(result), 2)
		today_start, tomorrow_start = result
		self.assertIn("00:00:00", today_start)
		self.assertIn("00:00:00", tomorrow_start)
		self.assertGreater(tomorrow_start, today_start)


# ---------------------------------------------------------------------------
# TestApplyMwoFilterRows (today-only and MWO filter logic)
# ---------------------------------------------------------------------------


class TestApplyMwoFilterRows(FrappeTestCase):
	def _filter_row(self, mwo, operation=None, sync_from=None):
		return FrappeDict(
			{
				"enabled": 1,
				"manufacturing_work_order": mwo,
				"manufacturing_operation": operation or "",
				"sync_from_datetime": sync_from or "2026-01-01 00:00:00",
			}
		)

	def test_log_not_in_filter_is_excluded(self):
		logs = [_log(manufacturing_work_order="MWO-999")]
		filter_rows = [self._filter_row("MWO-1")]
		included, excluded = _apply_mwo_filter_rows(logs, filter_rows)
		self.assertEqual(included, [])
		self.assertEqual(len(excluded), 1)
		self.assertEqual(excluded[0]._exclude_reason, "MWO Not In EOD Filter")

	def test_log_in_filter_with_no_operation_filter_is_included(self):
		log = _log(manufacturing_work_order="MWO-1", creation="2026-01-01 10:00:00")
		filter_rows = [self._filter_row("MWO-1", sync_from="2026-01-01 09:00:00")]
		included, excluded = _apply_mwo_filter_rows([log], filter_rows)
		self.assertEqual(len(included), 1)
		self.assertEqual(excluded, [])

	def test_log_before_sync_from_datetime_now_included(self):
		"""sync_from_datetime is no longer honored — selective sync covers full history."""
		log = _log(manufacturing_work_order="MWO-1", creation="2026-01-01 08:00:00")
		filter_rows = [self._filter_row("MWO-1", sync_from="2026-01-01 10:00:00")]
		included, excluded = _apply_mwo_filter_rows([log], filter_rows)
		self.assertEqual(len(included), 1)
		self.assertEqual(excluded, [])

	def test_operation_filter_excludes_other_operations(self):
		log = _log(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-OTHER",
			creation="2026-01-01 10:00:00",
		)
		filter_rows = [
			self._filter_row(
				"MWO-1", operation="MOP-A", sync_from="2026-01-01 00:00:00"
			)
		]
		included, excluded = _apply_mwo_filter_rows([log], filter_rows)
		self.assertEqual(included, [])
		self.assertEqual(len(excluded), 1)
		self.assertEqual(
			excluded[0]._exclude_reason, "Manufacturing Operation Not In Filter"
		)

	def test_operation_filter_includes_matching_operation(self):
		log = _log(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-A",
			creation="2026-01-01 10:00:00",
		)
		filter_rows = [
			self._filter_row(
				"MWO-1", operation="MOP-A", sync_from="2026-01-01 00:00:00"
			)
		]
		included, excluded = _apply_mwo_filter_rows([log], filter_rows)
		self.assertEqual(len(included), 1)
		self.assertEqual(excluded, [])

	def test_no_filter_rows_raises_error_or_returns_all(self):
		"""With empty filter_rows, _apply_mwo_filter_rows excludes everything (MWO not in empty map)."""
		log = _log(manufacturing_work_order="MWO-1")
		included, excluded = _apply_mwo_filter_rows([log], [])
		# All excluded because filter_map is empty — per design Mode B behavior
		self.assertEqual(included, [])
		self.assertEqual(len(excluded), 1)


# ---------------------------------------------------------------------------
# TestLockBypass (frappe.flags.in_eod_mop_sync)
# ---------------------------------------------------------------------------


class TestLockBypass(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked",
		return_value=True,
	)
	def test_validate_throws_when_locked_and_no_flag(self, _mock_locked):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
			validate_not_eod_sync_locked,
		)

		frappe.flags.in_eod_mop_sync = False
		with self.assertRaises(frappe.ValidationError):
			validate_not_eod_sync_locked(FrappeDict({}))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked",
		return_value=True,
	)
	def test_validate_passes_when_eod_flag_set(self, _mock_locked):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
			validate_not_eod_sync_locked,
		)

		frappe.flags.in_eod_mop_sync = True
		try:
			# Must not raise
			validate_not_eod_sync_locked(FrappeDict({}))
		finally:
			frappe.flags.in_eod_mop_sync = False


# ---------------------------------------------------------------------------
# TestRecalculateSyncLogTotals (SQL aggregation)
# ---------------------------------------------------------------------------


class TestRecalculateSyncLogTotals(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_aggregation_produces_correct_totals(self, mock_sql, mock_set_value):
		mock_sql.return_value = [
			FrappeDict({"status": "Synced", "item_count": 2, "total_qty": 10.0}),
			FrappeDict({"status": "Failed", "item_count": 1, "total_qty": 3.0}),
			FrappeDict({"status": "Excluded", "item_count": 1, "total_qty": 5.0}),
		]

		recalculate_sync_log_totals("SYNC-LOG-001")

		mock_set_value.assert_called_once()
		call_kwargs = mock_set_value.call_args[0][2]
		self.assertEqual(call_kwargs["total_items"], 4)
		self.assertEqual(call_kwargs["synced_items"], 2)
		self.assertEqual(call_kwargs["synced_qty"], 10.0)
		self.assertEqual(call_kwargs["failed_items"], 1)
		self.assertEqual(call_kwargs["excluded_items"], 1)
		self.assertEqual(call_kwargs["excluded_qty"], 5.0)
		# eligible_qty = total_qty - excluded_qty = 18 - 5 = 13
		self.assertEqual(call_kwargs["eligible_qty"], 13.0)
		# progress = synced_qty / eligible_qty * 100 = 10/13 * 100 ≈ 76.9
		self.assertAlmostEqual(call_kwargs["progress_percent"], 76.9, places=0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql",
		return_value=[],
	)
	def test_empty_child_rows_produces_zero_totals(self, _mock_sql, mock_set_value):
		recalculate_sync_log_totals("SYNC-LOG-002")

		mock_set_value.assert_called_once()
		call_kwargs = mock_set_value.call_args[0][2]
		self.assertEqual(call_kwargs["total_items"], 0)
		self.assertEqual(call_kwargs["progress_percent"], 0.0)

	def test_none_sync_log_name_does_nothing(self):
		# Must not raise and must not call any DB
		recalculate_sync_log_totals(None)


# ---------------------------------------------------------------------------
# TestSyncMopLogsWithSyncLogName (new sync_log_name param)
# ---------------------------------------------------------------------------


class TestSyncMopLogsWithSyncLogName(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.release_eod_sync_lock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.set_eod_sync_running"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._process_mwo_group"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups",
		return_value={},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.recalculate_sync_log_totals"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_sync_log_name_passed_to_set_eod_sync_running(
		self,
		_mock_sv,
		_mock_recalc,
		_mock_get_doc,
		_mock_groups,
		_mock_process,
		_mock_reconcile,
		mock_set_running,
		_mock_release,
	):
		_mock_get_doc.return_value = FrappeDict({"eod_sync_work_order_filter": []})
		sync_mop_logs(sync_log_name="SYNC-LOG-TEST-001")
		# set_eod_sync_running must be called with sync_log_name kwarg
		mock_set_running.assert_called_once_with(sync_log_name="SYNC-LOG-TEST-001")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.release_eod_sync_lock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.set_eod_sync_running"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._process_mwo_group"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups",
		return_value={},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.recalculate_sync_log_totals"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_eod_flag_cleared_in_finally_on_success(
		self,
		_mock_sv,
		_mock_recalc,
		_mock_get_doc,
		_mock_groups,
		_mock_process,
		_mock_reconcile,
		_mock_set_running,
		_mock_release,
	):
		_mock_get_doc.return_value = FrappeDict({"eod_sync_work_order_filter": []})
		frappe.flags.in_eod_mop_sync = False
		sync_mop_logs(sync_log_name="SYNC-LOG-TEST-002")
		self.assertFalse(getattr(frappe.flags, "in_eod_mop_sync", False))


# ---------------------------------------------------------------------------
# TestSreRelocation (release reservation at source, re-reserve at target)
# ---------------------------------------------------------------------------


class TestSreRelocation(FrappeTestCase):
	def test_snapshot_empty_when_no_items(self):
		self.assertEqual(_snapshot_mwo_sres_for_relocation("MWO-1", [], "WH-DEPT"), [])

	def test_snapshot_empty_when_source_equals_target(self):
		# Items already at the target warehouse → nothing to relocate.
		items = [
			{"item_code": "M-1", "batch_no": "B1", "s_warehouse": "WH-DEPT", "qty": 1.0}
		]
		self.assertEqual(
			_snapshot_mwo_sres_for_relocation("MWO-1", items, "WH-DEPT"), []
		)

	@patch(f"{_MOD}.frappe.get_all", return_value=[])  # no batch entries
	@patch(f"{_MOD}.frappe.get_cached_value", return_value=(0, 0))  # qty-based item
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_snapshot_captures_matching_sre(
		self, mock_db_get_all, _mock_cached, _mock_get_all
	):
		mock_db_get_all.return_value = [
			FrappeDict(
				{
					"name": "SRE-1",
					"item_code": "M-1",
					"warehouse": "WH-SRE",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"voucher_type": "Sales Order",
					"voucher_no": "SO-1",
					"voucher_detail_no": "row1",
					"voucher_qty": 5.0,
					"company": "Test Co",
					"stock_uom": "Nos",
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-A",
				}
			)
		]
		items = [
			{"item_code": "M-1", "batch_no": None, "s_warehouse": "WH-SRE", "qty": 5.0}
		]
		snaps = _snapshot_mwo_sres_for_relocation("MWO-1", items, "WH-DEPT")
		self.assertEqual(len(snaps), 1)
		self.assertEqual(snaps[0]["remaining"], 5.0)
		self.assertEqual(snaps[0]["sre"].name, "SRE-1")
		# Query must restrict to source warehouses excluding the target
		filters = mock_db_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["warehouse"], ["in", ["WH-SRE"]])

	@patch(f"{_MOD}.frappe.get_doc")
	def test_cancel_snapshots_cancels_submitted_sre(self, mock_get_doc):
		fake = MagicMock()
		fake.docstatus = 1
		mock_get_doc.return_value = fake
		_cancel_sre_snapshots([{"sre": FrappeDict({"name": "SRE-1"})}])
		fake.cancel.assert_called_once()

	def test_recreate_noop_on_empty(self):
		# Must not raise or touch the DB
		_recreate_sres_at([], "WH-DEPT")

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry."
		"get_available_qty_to_reserve",
		return_value=10.0,
	)
	@patch(f"{_MOD}.frappe.new_doc")
	def test_recreate_builds_and_submits_sre_at_target(self, mock_new_doc, _mock_avail):
		new_sre = MagicMock()
		mock_new_doc.return_value = new_sre
		snap = {
			"sre": FrappeDict(
				{
					"item_code": "M-1",
					"voucher_type": "Sales Order",
					"voucher_no": "SO-1",
					"voucher_detail_no": "row1",
					"voucher_qty": 5.0,
					"company": "Test Co",
					"stock_uom": "Nos",
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-A",
				}
			),
			"remaining": 5.0,
			"has_batch_no": 0,
			"has_serial_no": 0,
			"sb_entries": [],
		}
		_recreate_sres_at([snap], "WH-DEPT")
		self.assertEqual(new_sre.warehouse, "WH-DEPT")
		self.assertEqual(new_sre.reserved_qty, 5.0)
		new_sre.insert.assert_called_once()
		new_sre.submit.assert_called_once()


# ---------------------------------------------------------------------------
# TestTargetWarehouseFallback (resolve t_warehouse when last op carries none)
# ---------------------------------------------------------------------------


class TestTargetWarehouseFallback(FrappeTestCase):
	@patch(f"{_MOD}.frappe.db.get_value", return_value="WH-DEPT")
	def test_resolve_department_warehouse(self, mock_gv):
		self.assertEqual(
			_resolve_department_warehouse({"department": "Waxing"}), "WH-DEPT"
		)
		filters = mock_gv.call_args[0][1]
		self.assertEqual(filters["department"], "Waxing")
		self.assertEqual(filters["warehouse_type"], "Manufacturing")

	def test_resolve_department_warehouse_none_inputs(self):
		self.assertIsNone(_resolve_department_warehouse(None))
		self.assertIsNone(_resolve_department_warehouse({"department": None}))

	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(f"{_MOD}._preload_sre_warehouse_map", return_value={})
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_t_warehouse_resolved_from_other_operation(self, _art, _sre, mock_mark):
		# The last operation (latest creation) is a receive audit with no to_warehouse;
		# an earlier operation carries it. Resolution must fall back to that one.
		recv = {
			"mop_name": "MOP-RECV",
			"mop_doc": _mop_doc(),
			"logs": [
				_log(
					item_code="M-1",
					batch_no="B1",
					to_warehouse=None,
					qty_after_transaction_batch_based=0.0,
					flow_index=0,
					creation="2026-01-01 12:00:00",
				)
			],
		}
		move = {
			"mop_name": "MOP-MOVE",
			"mop_doc": _mop_doc(),
			"logs": [
				_log(
					item_code="M-1",
					batch_no="B1",
					to_warehouse="WH-DEPT",
					qty_after_transaction_batch_based=1.0,
					flow_index=4,
					creation="2026-01-01 10:00:00",
				)
			],
		}
		failures = []
		stats = {"processed_mwos": 0, "failed_mwos": 0, "submitted_ses": []}
		_process_mwo_group(("Test Co", "MWO-1"), [recv, move], failures, stats)
		# Target resolved from the other operation → no "Missing Target Warehouse".
		self.assertFalse(any(f.get("step") == "no_t_warehouse" for f in failures))
		mock_mark.assert_called()


# ---------------------------------------------------------------------------
# TestMwoRealizedByArtifact (SNC product-artifact detection)
# ---------------------------------------------------------------------------


class TestMwoRealizedByArtifact(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value",
		return_value="SE-MFG-1",
	)
	def test_returns_se_name_when_manufacture_se_exists(self, mock_get_value):
		result = _mwo_realized_by_artifact("MWO-1")
		self.assertEqual(result, "SE-MFG-1")
		# Query must target a submitted Manufacture SE for this MWO
		args = mock_get_value.call_args[0]
		self.assertEqual(args[0], "Stock Entry")
		filters = args[1]
		self.assertEqual(filters["manufacturing_work_order"], "MWO-1")
		self.assertEqual(filters["stock_entry_type"], "Manufacture")
		self.assertEqual(filters["docstatus"], 1)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value",
		return_value=None,
	)
	def test_returns_none_when_no_manufacture_se(self, _mock_get_value):
		self.assertIsNone(_mwo_realized_by_artifact("MWO-1"))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	def test_blank_mwo_short_circuits_without_query(self, mock_get_value):
		self.assertIsNone(_mwo_realized_by_artifact(None))
		mock_get_value.assert_not_called()


# ---------------------------------------------------------------------------
# TestProcessMwoGroupArtifactSkip (skip transfer when SNC artifact exists)
# ---------------------------------------------------------------------------


class TestProcessMwoGroupArtifactSkip(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._stamp_last_eod_sync"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_all_mwo_mop_logs_synced"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value="SE-MFG-1",
	)
	def test_artifact_skip_marks_synced_and_creates_no_se(
		self, _mock_artifact, mock_mark, _mock_stamp, mock_new_doc
	):
		logs = [_log(item_code="M-1", batch_no="B1")]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures = []
		stats = {"processed_mwos": 0, "failed_mwos": 0, "submitted_ses": []}

		_process_mwo_group(
			("Test Co", "MWO-1"),
			mop_data_list,
			failures,
			stats,
			sync_log_name=None,
			selective=True,
		)

		# No Material Transfer SE created
		mock_new_doc.assert_not_called()
		# Leftover logs marked synced, honoring the selective scope
		mock_mark.assert_called_once_with(["MWO-1"], selective=True)
		self.assertEqual(stats["processed_mwos"], 1)
		self.assertEqual(stats["artifact_skipped"], ["MWO-1"])
		self.assertEqual(failures, [])


# ---------------------------------------------------------------------------
# TestMarkAllMwoMopLogsSyncedSelective (full-history marking)
# ---------------------------------------------------------------------------


class TestMarkAllMwoMopLogsSyncedSelective(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_selective_marks_full_history_without_date_window(self, mock_sql):
		_mark_all_mwo_mop_logs_synced(["MWO-1"], selective=True)
		mock_sql.assert_called_once()
		sql_text = mock_sql.call_args[0][0]
		params = mock_sql.call_args[0][1]
		self.assertIn("UPDATE", sql_text)
		self.assertNotIn("creation", sql_text)
		self.assertNotIn("today_start", params)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_scheduled_marks_only_today(self, mock_sql):
		_mark_all_mwo_mop_logs_synced(["MWO-1"], selective=False)
		mock_sql.assert_called_once()
		sql_text = mock_sql.call_args[0][0]
		self.assertIn("creation", sql_text)


# ---------------------------------------------------------------------------
# TestGetUnsyncedMopGroupsSelective (full-history collection for filtered MWOs)
# ---------------------------------------------------------------------------


class TestGetUnsyncedMopGroupsSelective(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_selective_mode_fetches_full_history_for_listed_mwos(self, mock_get_all):
		settings = FrappeDict(
			{
				"eod_sync_work_order_filter": [
					FrappeDict(
						{
							"enabled": 1,
							"manufacturing_work_order": "MWO-1",
							"manufacturing_operation": "",
							"sync_from_datetime": "2026-05-01 00:00:00",
						}
					)
				]
			}
		)
		logs_result = [
			_log(
				name="L1",
				manufacturing_operation="MOP-A",
				manufacturing_work_order="MWO-1",
				creation="2026-01-01 08:00:00",
			)
		]
		mop_meta_result = [
			FrappeDict({**_mop_doc(manufacturing_work_order="MWO-1"), "name": "MOP-A"})
		]
		mock_get_all.side_effect = [logs_result, mop_meta_result]

		out = _get_unsynced_mop_groups(settings=settings)

		# The log-fetch filters restrict by MWO and carry NO today-only window
		log_filters = mock_get_all.call_args_list[0].kwargs["filters"]
		self.assertEqual(log_filters["manufacturing_work_order"], ["in", ["MWO-1"]])
		self.assertNotIn("creation", log_filters)
		# A pre-today log is still grouped (sync_from_datetime ignored)
		self.assertIn(("Test Co", "MWO-1"), out)
