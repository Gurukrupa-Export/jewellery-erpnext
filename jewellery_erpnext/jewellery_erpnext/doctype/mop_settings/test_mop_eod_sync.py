# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_create_loss_entries,
	_get_unsynced_mop_groups,
	_mark_synced,
	_resolve_warehouses,
	_sync_single_mop,
	sync_mop_logs,
)


def _log(**overrides):
	base = {
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


class TestMopEodSyncGrouping(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_get_unsynced_mop_groups_groups_by_mop(self, mock_get_all):
		mock_get_all.return_value = [
			_log(name="LOG-1", manufacturing_operation="MOP-A", flow_index=1),
			_log(name="LOG-2", manufacturing_operation="MOP-A", flow_index=2),
			_log(name="LOG-3", manufacturing_operation="MOP-B", flow_index=1),
		]

		out = _get_unsynced_mop_groups()

		self.assertEqual(sorted(out.keys()), ["MOP-A", "MOP-B"])
		self.assertEqual([row.name for row in out["MOP-A"]], ["LOG-1", "LOG-2"])
		self.assertEqual([row.name for row in out["MOP-B"]], ["LOG-3"])


class TestMopEodSyncHelpers(FrappeTestCase):
	def test_resolve_warehouses_uses_first_from_and_last_to(self):
		logs = [
			_log(flow_index=1, from_warehouse="WH-START", to_warehouse="WH-TRANSIT"),
			_log(flow_index=2, from_warehouse="WH-TRANSIT", to_warehouse="WH-END"),
		]

		first_wh, last_wh = _resolve_warehouses(logs)

		self.assertEqual(first_wh, "WH-START")
		self.assertEqual(last_wh, "WH-END")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	def test_mark_synced_marks_all_log_names(self, mock_set_value):
		logs = [_log(name="LOG-1"), _log(name="LOG-2")]

		_mark_synced(logs)

		mock_set_value.assert_called_once_with(
			"MOP Log",
			{"name": ["in", ["LOG-1", "LOG-2"]]},
			"is_synced",
			1,
		)


class TestSyncSingleMop(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_synced"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._create_loss_entries",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	def test_sync_single_mop_creates_direct_transfer_from_latest_flow(
		self, mock_get_value, mock_new_doc, _loss_entries, mock_mark_synced
	):
		mock_get_value.return_value = FrappeDict(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
				"department": "Waxing - GEPL",
				"loss_wt": 0,
			}
		)
		se = FakeStockEntry("SE-TEST-001")
		mock_new_doc.return_value = se
		logs = [
			_log(
				name="LOG-1",
				flow_index=1,
				item_code="M-OLD",
				batch_no="B1",
				qty_after_transaction_batch_based=0.5,
				from_warehouse="WH-START",
				to_warehouse="WH-TRANSIT",
			),
			_log(
				name="LOG-2",
				flow_index=2,
				item_code="M-NEW",
				batch_no="B2",
				qty_after_transaction_batch_based=1.25,
				from_warehouse="WH-TRANSIT",
				to_warehouse="WH-END",
			),
		]

		out = _sync_single_mop("MOP-TEST-001", logs)

		self.assertEqual(out, ["SE-TEST-001"])
		self.assertEqual(se.stock_entry_type, "Material Transfer to Department")
		self.assertTrue(se.saved)
		self.assertTrue(se.submitted)
		self.assertEqual(len(se.items), 1)
		self.assertEqual(se.items[0].item_code, "M-NEW")
		self.assertEqual(se.items[0].qty, 1.25)
		self.assertEqual(se.items[0].s_warehouse, "WH-START")
		self.assertEqual(se.items[0].t_warehouse, "WH-END")
		self.assertEqual(se.items[0].manufacturing_operation, "MOP-TEST-001")
		mock_mark_synced.assert_called_once_with(logs)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_synced"
	)
	def test_sync_single_mop_skips_when_warehouses_unresolved(
		self, mock_mark_synced, mock_new_doc, mock_get_value, mock_log_error
	):
		mock_get_value.return_value = FrappeDict(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
				"department": "Waxing - GEPL",
				"loss_wt": 0,
			}
		)
		logs = [_log(flow_index=1, from_warehouse=None, to_warehouse=None)]

		out = _sync_single_mop("MOP-TEST-001", logs)

		self.assertEqual(out, [])
		mock_new_doc.assert_not_called()
		mock_mark_synced.assert_not_called()
		mock_log_error.assert_called_once()


class TestCreateLossEntries(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item",
		return_value="LOSS-M-ITEM",
	)
	def test_create_loss_entries_builds_repack_entry(
		self, _get_item_loss_item, mock_get_value, mock_new_doc
	):
		mock_get_value.return_value = FrappeDict(
			{
				"loss_warehouse": "LOSS-WH",
				"consider_department_warehouse": 0,
				"warehouse_type": None,
			}
		)
		se = FakeStockEntry("SE-LOSS-001")
		mock_new_doc.return_value = se
		mop = FrappeDict(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
				"department": "Waxing - GEPL",
			}
		)
		latest_logs = [
			_log(item_code="M-ONE", batch_no="B1", qty_after_transaction_batch_based=2.0),
			_log(item_code="F-TWO", batch_no="B2", qty_after_transaction_batch_based=1.0),
			_log(item_code="D-THREE", batch_no="BD1", qty_after_transaction_batch_based=9.0),
		]

		out = _create_loss_entries(mop, "MOP-TEST-001", latest_logs, "WH-END", 0.9)

		self.assertEqual(out, ["SE-LOSS-001"])
		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertTrue(se.saved)
		self.assertTrue(se.submitted)
		self.assertEqual(len(se.items), 3)
		self.assertEqual(se.items[0].item_code, "M-ONE")
		self.assertEqual(se.items[1].item_code, "F-TWO")
		self.assertEqual(se.items[2].item_code, "LOSS-M-ITEM")
		self.assertEqual(se.items[2].t_warehouse, "LOSS-WH")


class TestSyncMopLogsEntryPoint(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.commit"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._sync_single_mop"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups"
	)
	def test_sync_mop_logs_continues_after_per_mop_failure(
		self, mock_get_groups, mock_sync_single_mop, mock_log_error, mock_commit
	):
		logs_a = [_log(name="LOG-A1", manufacturing_operation="MOP-A")]
		logs_b = [
			_log(name="LOG-B1", manufacturing_operation="MOP-B"),
			_log(name="LOG-B2", manufacturing_operation="MOP-B"),
		]
		mock_get_groups.return_value = {"MOP-A": logs_a, "MOP-B": logs_b}

		def sync_side_effect(mop_name, logs):
			if mop_name == "MOP-A":
				return ["SE-A"]
			raise Exception("boom")

		mock_sync_single_mop.side_effect = sync_side_effect

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 1)
		self.assertEqual(out["stock_entries"], ["SE-A"])
		mock_log_error.assert_called_once()
		mock_commit.assert_called_once()
