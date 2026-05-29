# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the SRE-based MWO sync (mop_eod_sync.py).

Covers:
- _get_mwos_with_unsynced_logs grouping
- _get_last_mop_for_mwo selection
- _build_item_t_warehouse_map from latest flow logs
- _latest_flow_logs
- _mark_synced
- _validate_eod_items_for_mwo_reservation
- _validate_eod_source_batch_stock
- _sync_mwo_via_sre orchestration
- sync_mop_logs entry point (no-op, failure isolation)
- delete_cancelled_stock_reservations Sunday gate
"""

import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"


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
			"voucher_type": "Department IR",
			"voucher_no": "DIR-1",
			"serial_no": None,
		}
	)
	base.update(overrides)
	return base


# ---------------------------------------------------------------------------
# _get_mwos_with_unsynced_logs
# ---------------------------------------------------------------------------


class TestGetMwosWithUnsyncedLogs(FrappeTestCase):
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_groups_by_mwo_and_mop(self, mock_get_all):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_get_mwos_with_unsynced_logs,
		)

		mock_get_all.return_value = [
			_log(
				name="L1",
				manufacturing_work_order="MWO-1",
				manufacturing_operation="MOP-A",
			),
			_log(
				name="L2",
				manufacturing_work_order="MWO-1",
				manufacturing_operation="MOP-B",
			),
			_log(
				name="L3",
				manufacturing_work_order="MWO-2",
				manufacturing_operation="MOP-C",
			),
		]
		result = _get_mwos_with_unsynced_logs()

		self.assertIn("MWO-1", result)
		self.assertIn("MWO-2", result)
		self.assertIn("MOP-A", result["MWO-1"])
		self.assertIn("MOP-B", result["MWO-1"])
		self.assertIn("MOP-C", result["MWO-2"])
		self.assertEqual(len(result["MWO-1"]["MOP-A"]), 1)

	@patch(f"{_MOD}.frappe.db.get_all", return_value=[])
	def test_empty_when_no_unsynced_logs(self, _):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_get_mwos_with_unsynced_logs,
		)

		self.assertEqual(_get_mwos_with_unsynced_logs(), {})

	@patch(f"{_MOD}.frappe.db.get_all")
	def test_skips_logs_without_mwo_or_mop(self, mock_get_all):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_get_mwos_with_unsynced_logs,
		)

		mock_get_all.return_value = [
			_log(
				name="L-NO-MWO",
				manufacturing_work_order="",
				manufacturing_operation="MOP-A",
			),
			_log(
				name="L-NO-MOP",
				manufacturing_work_order="MWO-1",
				manufacturing_operation="",
			),
		]
		result = _get_mwos_with_unsynced_logs()
		self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _get_last_mop_for_mwo
# ---------------------------------------------------------------------------


class TestGetLastMopForMwo(FrappeTestCase):
	def test_single_mop_returns_it(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_get_last_mop_for_mwo,
		)

		self.assertEqual(_get_last_mop_for_mwo(["MOP-ONLY"]), "MOP-ONLY")

	def test_empty_list_returns_none(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_get_last_mop_for_mwo,
		)

		self.assertIsNone(_get_last_mop_for_mwo([]))

	@patch(f"{_MOD}.frappe.db.get_all")
	def test_returns_most_recent_by_creation(self, mock_get_all):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_get_last_mop_for_mwo,
		)

		mock_get_all.return_value = [
			FrappeDict({"name": "MOP-B", "creation": "2026-01-02"})
		]
		result = _get_last_mop_for_mwo(["MOP-A", "MOP-B"])
		self.assertEqual(result, "MOP-B")
		_, kwargs = mock_get_all.call_args
		self.assertEqual(kwargs.get("order_by"), "creation desc")
		self.assertEqual(kwargs.get("limit"), 1)


# ---------------------------------------------------------------------------
# _build_item_t_warehouse_map
# ---------------------------------------------------------------------------


class TestBuildItemTWarehouseMap(FrappeTestCase):
	def test_uses_highest_flow_index(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_build_item_t_warehouse_map,
		)

		logs = [
			_log(item_code="M-1", batch_no="B1", to_warehouse="WH-OLD", flow_index=1),
			_log(item_code="M-1", batch_no="B1", to_warehouse="WH-NEW", flow_index=2),
		]
		result = _build_item_t_warehouse_map(logs)
		self.assertEqual(result[("M-1", "B1")], "WH-NEW")

	def test_empty_logs_returns_empty_map(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_build_item_t_warehouse_map,
		)

		self.assertEqual(_build_item_t_warehouse_map([]), {})

	def test_no_batch_keyed_with_empty_string(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_build_item_t_warehouse_map,
		)

		logs = [_log(item_code="M-1", batch_no=None, to_warehouse="WH-X", flow_index=1)]
		result = _build_item_t_warehouse_map(logs)
		self.assertIn(("M-1", ""), result)

	def test_skips_logs_without_to_warehouse(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_build_item_t_warehouse_map,
		)

		logs = [_log(item_code="M-1", batch_no="B1", to_warehouse=None, flow_index=1)]
		result = _build_item_t_warehouse_map(logs)
		self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _latest_flow_logs
# ---------------------------------------------------------------------------


class TestLatestFlowLogs(FrappeTestCase):
	def test_empty_returns_empty(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_latest_flow_logs,
		)

		self.assertEqual(_latest_flow_logs([]), [])

	def test_returns_only_highest_flow_index_logs(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_latest_flow_logs,
		)

		logs = [
			_log(name="L1", flow_index=1),
			_log(name="L2", flow_index=2),
			_log(name="L3", flow_index=2),
		]
		result = _latest_flow_logs(logs)
		self.assertEqual(len(result), 2)
		names = {l.name for l in result}
		self.assertEqual(names, {"L2", "L3"})

	def test_single_log_returns_it(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_latest_flow_logs,
		)

		logs = [_log(name="ONLY", flow_index=5)]
		result = _latest_flow_logs(logs)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0].name, "ONLY")


# ---------------------------------------------------------------------------
# _mark_synced
# ---------------------------------------------------------------------------


class TestMarkSynced(FrappeTestCase):
	@patch(f"{_MOD}.frappe.db.set_value")
	def test_marks_all_log_names(self, mock_set_value):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_mark_synced,
		)

		logs = [_log(name="LOG-1"), _log(name="LOG-2")]
		_mark_synced(logs)
		mock_set_value.assert_called_once_with(
			"MOP Log",
			{"name": ["in", ["LOG-1", "LOG-2"]]},
			"is_synced",
			1,
		)

	@patch(f"{_MOD}.frappe.db.set_value")
	def test_empty_logs_no_db_call(self, mock_set_value):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_mark_synced,
		)

		_mark_synced([])
		mock_set_value.assert_not_called()


# ---------------------------------------------------------------------------
# _validate_eod_items_for_mwo_reservation
# ---------------------------------------------------------------------------


class TestValidateEodItemsForMwoReservation(FrappeTestCase):
	@patch(f"{_MOD}.frappe.db.get_value", return_value=(1, 0))
	def test_raises_for_batch_item_missing_batch_no(self, _):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_validate_eod_items_for_mwo_reservation,
		)

		items = [
			{
				"item_code": "M-BATCH",
				"qty": 1.0,
				"batch_no": None,
				"manufacturing_operation": "MOP-1",
			}
		]
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_items_for_mwo_reservation(items)

	@patch(f"{_MOD}.frappe.db.get_value", return_value=(0, 0))
	def test_passes_for_non_batch_item_without_batch_no(self, _):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_validate_eod_items_for_mwo_reservation,
		)

		items = [{"item_code": "M-PLAIN", "qty": 1.0, "batch_no": None}]
		# Must not raise
		_validate_eod_items_for_mwo_reservation(items)

	def test_skips_zero_qty_items(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_validate_eod_items_for_mwo_reservation,
		)

		# Zero qty item with batch flag missing: should not raise
		items = [{"item_code": "M-BATCH", "qty": 0.0, "batch_no": None}]
		_validate_eod_items_for_mwo_reservation(items)


# ---------------------------------------------------------------------------
# _validate_eod_source_batch_stock
# ---------------------------------------------------------------------------


class TestValidateEodSourceBatchStock(FrappeTestCase):
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_passes_when_physical_stock_sufficient(self, mock_gbq):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_validate_eod_source_batch_stock,
		)

		items = [
			{"item_code": "IT-1", "batch_no": "B1", "s_warehouse": "WH-S", "qty": 3.0}
		]
		_validate_eod_source_batch_stock(items)
		self.assertTrue(mock_gbq.call_args.kwargs.get("ignore_reserved_stock"))

	@patch(f"{_MOD}._list_open_sre_for_batch", return_value=[])
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=1.0)
	def test_raises_when_physical_batch_stock_short(self, _mock_gbq, _mock_sre):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_validate_eod_source_batch_stock,
		)

		items = [
			{"item_code": "IT-1", "batch_no": "B1", "s_warehouse": "WH-S", "qty": 5.0}
		]
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_source_batch_stock(items)

	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_skips_items_without_batch_no(self, mock_gbq):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_validate_eod_source_batch_stock,
		)

		# Non-batch items (no batch_no) should be skipped entirely
		items = [
			{
				"item_code": "IT-NO-BATCH",
				"batch_no": None,
				"s_warehouse": "WH-S",
				"qty": 5.0,
			}
		]
		_validate_eod_source_batch_stock(items)
		mock_gbq.assert_not_called()


# ---------------------------------------------------------------------------
# _sync_mwo_via_sre
# ---------------------------------------------------------------------------


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
		if fieldname == "items":
			self.items.append(FrappeDict(value))

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class TestSyncMwoViaSre(FrappeTestCase):
	def _make_sre(
		self, name="SRE-1", item_code="M-1", warehouse="WH-SRE", mop="MOP-LAST"
	):
		return FrappeDict(
			{
				"name": name,
				"item_code": item_code,
				"warehouse": warehouse,
				"reserved_qty": 5.0,
				"delivered_qty": 0.0,
				"manufacturing_work_order": "MWO-1",
				"manufacturing_operation": mop,
				"voucher_type": "Sales Order",
				"voucher_no": "SO-1",
				"voucher_detail_no": "SO-1-row-1",
				"voucher_qty": 5.0,
				"company": "Test Co",
				"stock_uom": "Nos",
				"reservation_based_on": "Qty",
			}
		)

	@patch(f"{_MOD}._get_active_sres_for_mop", return_value=[])
	@patch(f"{_MOD}.frappe.db.get_value")
	@patch(f"{_MOD}._get_last_mop_for_mwo", return_value="MOP-LAST")
	def test_no_sres_returns_empty_and_no_se(
		self, _mock_last, mock_get_value, _mock_sres
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_sync_mwo_via_sre,
		)

		mock_get_value.return_value = FrappeDict(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
			}
		)
		logs_by_mop = {
			"MOP-LAST": [
				_log(item_code="M-1", batch_no="B1", to_warehouse="WH-TO", flow_index=1)
			]
		}
		result = _sync_mwo_via_sre("MWO-1", logs_by_mop, "Test Co")
		self.assertEqual(result, [])

	@patch(f"{_MOD}._relocate_sre")
	@patch(f"{_MOD}._validate_eod_source_batch_stock")
	@patch(f"{_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(f"{_MOD}.frappe.new_doc")
	@patch(f"{_MOD}._get_active_sres_for_mop")
	@patch(f"{_MOD}.frappe.db.get_value")
	@patch(f"{_MOD}._get_last_mop_for_mwo", return_value="MOP-LAST")
	def test_creates_se_when_s_and_t_differ(
		self,
		_mock_last,
		mock_get_value,
		mock_sres,
		mock_new_doc,
		_mock_validate_items,
		_mock_validate_stock,
		mock_relocate,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_sync_mwo_via_sre,
		)

		mock_get_value.return_value = FrappeDict(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
			}
		)
		sre = self._make_sre(item_code="M-1", warehouse="WH-CURRENT")
		mock_sres.return_value = [sre]
		se = FakeStockEntry("SE-001")
		mock_new_doc.return_value = se

		logs_by_mop = {
			"MOP-LAST": [
				_log(
					item_code="M-1", batch_no="B1", to_warehouse="WH-DEST", flow_index=1
				)
			]
		}
		result = _sync_mwo_via_sre("MWO-1", logs_by_mop, "Test Co")

		self.assertEqual(result, ["SE-001"])
		self.assertTrue(se.saved)
		self.assertTrue(se.submitted)
		self.assertEqual(se.stock_entry_type, "Material Transfer to Department")
		mock_relocate.assert_called_once()
		_, kwargs = mock_relocate.call_args
		# t_warehouse passed as second positional arg
		call_args = mock_relocate.call_args[0]
		self.assertEqual(call_args[1], "WH-DEST")

	@patch(f"{_MOD}._get_active_sres_for_mop")
	@patch(f"{_MOD}.frappe.db.get_value")
	@patch(f"{_MOD}._get_last_mop_for_mwo", return_value="MOP-LAST")
	def test_same_warehouse_no_se_created(self, _mock_last, mock_get_value, mock_sres):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_sync_mwo_via_sre,
		)

		mock_get_value.return_value = FrappeDict(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
			}
		)
		# SRE warehouse == log to_warehouse → no movement needed
		sre = self._make_sre(item_code="M-1", warehouse="WH-SAME")
		mock_sres.return_value = [sre]

		logs_by_mop = {
			"MOP-LAST": [
				_log(
					item_code="M-1", batch_no="B1", to_warehouse="WH-SAME", flow_index=1
				)
			]
		}
		result = _sync_mwo_via_sre("MWO-1", logs_by_mop, "Test Co")
		self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# sync_mop_logs entry point
# ---------------------------------------------------------------------------


class TestSyncMopLogsEntryPoint(FrappeTestCase):
	@patch(f"{_MOD}._get_mwos_with_unsynced_logs", return_value={})
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.commit")
	def test_empty_groups_is_noop(self, _mock_commit, _mock_set, _mock_groups):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		out = sync_mop_logs()
		self.assertEqual(out["processed"], 0)
		self.assertEqual(out["stock_entries"], [])

	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}.frappe.log_error")
	@patch(f"{_MOD}._sync_mwo_via_sre")
	@patch(f"{_MOD}._mark_synced")
	@patch(f"{_MOD}._resolve_company_for_mwo", return_value="Test Co")
	@patch(f"{_MOD}._get_mwos_with_unsynced_logs")
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.commit")
	def test_continues_after_one_mwo_failure(
		self,
		_mock_commit,
		_mock_set,
		mock_groups,
		_mock_company,
		mock_mark,
		mock_sync,
		mock_log_error,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		logs_ok = [_log(name="L-OK")]
		logs_fail = [_log(name="L-FAIL")]
		mock_groups.return_value = {
			"MWO-OK": {"MOP-A": logs_ok},
			"MWO-FAIL": {"MOP-B": logs_fail},
		}

		def sync_side(mwo, logs_by_mop, company):
			if mwo == "MWO-FAIL":
				raise Exception("boom")
			return ["SE-OK"]

		mock_sync.side_effect = sync_side

		out = sync_mop_logs()

		# Only MWO-OK contributed
		self.assertEqual(out["stock_entries"], ["SE-OK"])
		# MWO-OK's log was marked synced
		mock_mark.assert_called_once()
		# Error was logged for MWO-FAIL
		mock_log_error.assert_called_once()


# ---------------------------------------------------------------------------
# delete_cancelled_stock_reservations
# ---------------------------------------------------------------------------


class TestDeleteCancelledStockReservations(FrappeTestCase):
	@patch(f"{_MOD}.frappe.delete_doc")
	@patch(f"{_MOD}.frappe.db.get_all", return_value=[])
	@patch(f"{_MOD}.datetime")
	def test_skips_on_non_sunday(self, mock_dt, _mock_get_all, mock_delete):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			delete_cancelled_stock_reservations,
		)

		mock_dt.datetime.now.return_value = MagicMock(
			weekday=MagicMock(return_value=0)
		)  # Monday
		delete_cancelled_stock_reservations()
		mock_delete.assert_not_called()

	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}.frappe.delete_doc")
	@patch(f"{_MOD}.frappe.db.get_all")
	@patch(f"{_MOD}.datetime")
	def test_deletes_all_cancelled_sres_on_sunday(
		self,
		mock_dt,
		mock_get_all,
		mock_delete,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			delete_cancelled_stock_reservations,
		)

		mock_dt.datetime.now.return_value = MagicMock(
			weekday=MagicMock(return_value=6)
		)  # Sunday
		mock_get_all.return_value = ["SRE-CANCEL-1", "SRE-CANCEL-2"]

		delete_cancelled_stock_reservations()

		self.assertEqual(mock_delete.call_count, 2)
		calls = [c[0] for c in mock_delete.call_args_list]
		self.assertEqual(calls[0], ("Stock Reservation Entry", "SRE-CANCEL-1"))
		self.assertEqual(calls[1], ("Stock Reservation Entry", "SRE-CANCEL-2"))


# ---------------------------------------------------------------------------
# maybe_sync_mop_logs
# ---------------------------------------------------------------------------


class TestMaybeSyncMopLogs(FrappeTestCase):
	@patch(f"{_MOD}.sync_mop_logs")
	@patch(f"{_MOD}.frappe.db.get_single_value", return_value=None)
	def test_no_sync_time_is_noop(self, _mock_get, mock_sync):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			maybe_sync_mop_logs,
		)

		maybe_sync_mop_logs()
		mock_sync.assert_not_called()

	@patch(f"{_MOD}.sync_mop_logs")
	@patch(f"{_MOD}.datetime")
	@patch(f"{_MOD}.frappe.db.get_single_value", return_value="21:00:00")
	def test_wrong_hour_is_noop(self, _mock_get, mock_dt, mock_sync):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			maybe_sync_mop_logs,
		)

		mock_dt.datetime.now.return_value = MagicMock(hour=22)
		maybe_sync_mop_logs()
		mock_sync.assert_not_called()

	@patch(f"{_MOD}.sync_mop_logs")
	@patch(f"{_MOD}.datetime")
	@patch(f"{_MOD}.frappe.db.get_single_value", return_value="21:00:00")
	def test_matching_hour_calls_sync(self, _mock_get, mock_dt, mock_sync):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			maybe_sync_mop_logs,
		)

		mock_dt.datetime.now.return_value = MagicMock(hour=21)
		maybe_sync_mop_logs()
		mock_sync.assert_called_once()
