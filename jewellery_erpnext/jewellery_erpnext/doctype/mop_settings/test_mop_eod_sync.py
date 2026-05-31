# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_build_eod_se_rows,
	_find_last_operation,
	_get_last_logs_per_item_batch,
	_get_sre_source_warehouse,
	_get_t_warehouse_from_logs,
	_get_unsynced_mop_groups,
	_mark_all_mwo_mop_logs_synced,
	_sync_mwo_group,
	_validate_eod_source_batch_stock,
	sync_mop_logs,
)

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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_two_mops_same_mwo_produce_one_group(self, mock_get_all, mock_get_value):
		"""Two MOPs with same MWO → one (company, mwo) group."""
		mock_get_all.return_value = [
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
		mock_get_value.return_value = _mop_doc(manufacturing_work_order="MWO-1")

		out = _get_unsynced_mop_groups()

		self.assertEqual(len(out), 1)
		key = ("Test Co", "MWO-1")
		self.assertIn(key, out)
		self.assertEqual(len(out[key]), 2)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_two_different_mwos_produce_two_groups(self, mock_get_all, mock_get_value):
		"""Logs from different MWOs → two groups."""
		mock_get_all.return_value = [
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

		def _side(dt, name, fields, as_dict):
			mwo = "MWO-1" if name == "MOP-A" else "MWO-2"
			return _mop_doc(manufacturing_work_order=mwo)

		mock_get_value.side_effect = _side

		out = _get_unsynced_mop_groups()

		self.assertEqual(len(out), 2)
		self.assertIn(("Test Co", "MWO-1"), out)
		self.assertIn(("Test Co", "MWO-2"), out)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_mop_with_missing_metadata_skipped(self, mock_get_all, mock_get_value):
		"""MOP whose metadata fetch returns None is silently skipped."""
		mock_get_all.return_value = [
			_log(name="L1", manufacturing_operation="MOP-GHOST"),
		]
		mock_get_value.return_value = None  # missing MOP doc

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


# ---------------------------------------------------------------------------
# TestGetSreSourceWarehouse (3 cases)
# ---------------------------------------------------------------------------


class TestGetSreSourceWarehouse(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_batch_sre_match_returns_warehouse(self, mock_sql):
		mock_sql.return_value = [{"warehouse": "WH-SRE"}]
		result = _get_sre_source_warehouse("MWO-1", "M-1", "B1")
		self.assertEqual(result, "WH-SRE")
		mock_sql.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_no_batch_sre_falls_back_to_qty_based(self, mock_sql, mock_get_value):
		mock_sql.return_value = []  # no batch SRE
		mock_get_value.return_value = "WH-QTY"
		result = _get_sre_source_warehouse("MWO-1", "M-1", "B1")
		self.assertEqual(result, "WH-QTY")
		mock_get_value.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_no_sre_at_all_returns_none(self, mock_sql, mock_get_value):
		mock_sql.return_value = []
		mock_get_value.return_value = None
		result = _get_sre_source_warehouse("MWO-1", "M-1", "B1")
		self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestBuildEodSeRows (5 cases)
# ---------------------------------------------------------------------------


class TestBuildEodSeRows(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_source_warehouse",
		return_value="WH-SRE",
	)
	def test_normal_case_builds_row(self, _mock_sre):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=5.0,
				to_warehouse="WH-DEPT",
			)
		]
		rows = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["s_warehouse"], "WH-SRE")
		self.assertEqual(rows[0]["t_warehouse"], "WH-DEPT")
		self.assertEqual(rows[0]["qty"], 5.0)
		self.assertEqual(rows[0]["item_code"], "M-1")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_source_warehouse",
		return_value=None,
	)
	def test_no_sre_skips_row_and_logs_error(self, _mock_sre, mock_log_error):
		logs = [_log(qty_after_transaction_batch_based=3.0)]
		rows = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT")
		self.assertEqual(rows, [])
		mock_log_error.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_source_warehouse",
		return_value="WH-SAME",
	)
	def test_same_source_and_target_skips_row(self, _mock_sre):
		logs = [_log(qty_after_transaction_batch_based=2.0)]
		rows = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-SAME")
		self.assertEqual(rows, [])

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_source_warehouse",
		return_value="WH-SRE",
	)
	def test_zero_qty_log_skipped(self, _mock_sre):
		logs = [_log(qty_after_transaction_batch_based=0.0)]
		rows = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT")
		self.assertEqual(rows, [])

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_source_warehouse",
		return_value="WH-SRE",
	)
	def test_batch_no_included_in_row(self, _mock_sre):
		logs = [
			_log(
				item_code="M-1",
				batch_no="BATCH-99",
				qty_after_transaction_batch_based=1.0,
			)
		]
		rows = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["batch_no"], "BATCH-99")


# ---------------------------------------------------------------------------
# TestMarkAllMwoMopLogsSynced (3 cases)
# ---------------------------------------------------------------------------


class TestMarkAllMwoMopLogsSynced(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_marks_unsynced_non_cancelled_logs(self, mock_set_value):
		_mark_all_mwo_mop_logs_synced(["MWO-1", "MWO-2"])
		mock_set_value.assert_called_once_with(
			"MOP Log",
			{
				"manufacturing_work_order": ["in", ["MWO-1", "MWO-2"]],
				"is_synced": 0,
				"is_cancelled": 0,
			},
			"is_synced",
			1,
		)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_filter_targets_only_is_synced_0_and_is_cancelled_0(self, mock_set_value):
		"""The filter dict passed to set_value must explicitly gate on is_synced=0 and is_cancelled=0."""
		_mark_all_mwo_mop_logs_synced(["MWO-X"])
		_, filter_arg, field, val = mock_set_value.call_args.args
		self.assertEqual(filter_arg.get("is_synced"), 0)
		self.assertEqual(filter_arg.get("is_cancelled"), 0)
		self.assertEqual(field, "is_synced")
		self.assertEqual(val, 1)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_empty_list_does_not_call_set_value(self, mock_set_value):
		_mark_all_mwo_mop_logs_synced([])
		mock_set_value.assert_not_called()


# ---------------------------------------------------------------------------
# TestSyncMwoGroupHappyPath (1 case — integration of all new functions)
# ---------------------------------------------------------------------------


class TestSyncMwoGroupHappyPath(FrappeTestCase):
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
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_source_warehouse",
		return_value="WH-SRE",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	def test_happy_path_creates_se_and_marks_synced(
		self,
		mock_new_doc,
		_mock_sre,
		_mock_item_val,
		_mock_batch_val,
		mock_mark_synced,
	):
		se = FakeStockEntry("SE-EOD-001")
		mock_new_doc.return_value = se

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

		se_names, count = _sync_mwo_group(("Test Co", "MWO-1"), mop_data_list)

		self.assertEqual(se_names, ["SE-EOD-001"])
		self.assertEqual(count, 1)
		self.assertTrue(se.submitted)
		self.assertEqual(se.stock_entry_type, "Material Transfer to Department")
		self.assertEqual(len(se.items), 1)
		self.assertEqual(se.items[0].s_warehouse, "WH-SRE")
		self.assertEqual(se.items[0].t_warehouse, "WH-DEPT")
		mock_mark_synced.assert_called_once_with(["MWO-1"])


# ---------------------------------------------------------------------------
# TestSyncMopLogsEntryPoint (1 case)
# ---------------------------------------------------------------------------


class TestSyncMopLogsEntryPoint(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._sync_mwo_group"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_continues_after_one_group_fails(
		self,
		_mock_set_value,
		_mock_reconcile,
		mock_get_groups,
		mock_sync_group,
		mock_log_error,
		mock_rollback,
		mock_release,
		mock_savepoint,
	):
		key_a = ("Co", "MWO-A")
		key_b = ("Co", "MWO-B")
		mock_get_groups.return_value = {
			key_a: [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": []}],
			key_b: [{"mop_name": "MOP-B", "mop_doc": _mop_doc(), "logs": []}],
		}

		def _side(key, data):
			if key == key_a:
				return (["SE-A"], 1)
			raise RuntimeError("simulated failure")

		mock_sync_group.side_effect = _side

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 1)
		self.assertEqual(out["stock_entries"], ["SE-A"])
		self.assertEqual(mock_savepoint.call_count, 2)
		mock_release.assert_called_once()
		mock_rollback.assert_called_once()
		mock_log_error.assert_called()


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
