# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_create_loss_entries,
	_get_unsynced_mop_groups,
	_latest_flow_logs,
	_mark_synced,
	_mop_manufacturer_label,
	_resolve_warehouses,
	_sync_consolidated_group,
	_validate_eod_source_batch_stock,
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


class FlakyFakeStockEntry(FakeStockEntry):
	"""Simulate consolidated multi-line SE failing on submit; single-line SEs succeed."""

	def submit(self):
		if len(self.items) > 1:
			raise RuntimeError("consolidated submit failed")
		super().submit()


class TestMopEodSyncGrouping(FrappeTestCase):
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all")
	def test_get_unsynced_mop_groups_groups_by_routing_hop(self, mock_get_all, mock_get_value):
		mock_get_all.return_value = [
			_log(name="LOG-1", manufacturing_operation="MOP-A", flow_index=1, from_warehouse="WH-1", to_warehouse="WH-2", manufacturing_work_order="MWO-1"),
			_log(name="LOG-2", manufacturing_operation="MOP-B", flow_index=1, from_warehouse="WH-1", to_warehouse="WH-2", manufacturing_work_order="MWO-1"),
			_log(name="LOG-3", manufacturing_operation="MOP-C", flow_index=1, from_warehouse="WH-2", to_warehouse="WH-3", manufacturing_work_order="MWO-1"),
		]
		
		# Mocking the MOP metadata fetches
		def mock_mop_value(dt, name, fields, as_dict):
			return FrappeDict({
				"company": "Company",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
				"department": "Dept",
				"loss_wt": 0,
			})
		mock_get_value.side_effect = mock_mop_value

		out = _get_unsynced_mop_groups()

		group_keys = sorted(list(out.keys()))
		self.assertEqual(len(group_keys), 2)
		
		key_wh1_wh2 = ("Company", "MWO-1", "WH-1", "WH-2")
		key_wh2_wh3 = ("Company", "MWO-1", "WH-2", "WH-3")
		
		self.assertIn(key_wh1_wh2, group_keys)
		self.assertIn(key_wh2_wh3, group_keys)
		
		# MOP A and B should be in the WH1->WH2 bucket
		self.assertEqual(len(out[key_wh1_wh2]), 2)
		self.assertEqual(out[key_wh1_wh2][0]["mop_name"], "MOP-A")
		self.assertEqual(out[key_wh1_wh2][1]["mop_name"], "MOP-B")


class TestMopEodSyncHelpers(FrappeTestCase):
	def test_resolve_warehouses_empty_logs_returns_none_pair(self):
		first_wh, last_wh = _resolve_warehouses([])
		self.assertIsNone(first_wh)
		self.assertIsNone(last_wh)

	def test_latest_flow_logs_empty_returns_empty_list(self):
		self.assertEqual(_latest_flow_logs([]), [])

	def test_mop_manufacturer_label_dict_attr_and_none(self):
		self.assertIsNone(_mop_manufacturer_label(None))
		self.assertEqual(_mop_manufacturer_label(FrappeDict({"manufacturer": "MF-DICT"})), "MF-DICT")

		class _MopObj:
			manufacturer = "MF-ATTR"

		self.assertEqual(_mop_manufacturer_label(_MopObj()), "MF-ATTR")

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


class TestSyncConsolidatedGroup(FrappeTestCase):
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_synced")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._create_loss_entries", return_value=[])
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc")
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=100.0)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_undelivered_batch_qty",
		return_value=999.0,
	)
	def test_sync_consolidated_group_creates_grouped_transfer(
		self,
		_mock_sre_audit,
		mock_gbq,
		mock_new_doc,
		mock_get_value,
		_loss_entries,
		mock_mark_synced,
	):
		se = FakeStockEntry("SE-TEST-001")
		mock_new_doc.return_value = se

		def _get_value_side_effect(doctype, name, field, *args, **kwargs):
			if doctype == "Item" and name == "M-1" and field == ["has_batch_no", "has_serial_no"]:
				return (0, 0)
			return None

		mock_get_value.side_effect = _get_value_side_effect

		group_key = ("Test Co", "MWO-1", "WH-START", "WH-END")
		
		mop_doc = FrappeDict({
			"manufacturing_order": "MO-1",
			"manufacturing_work_order": "MWO-1",
			"loss_wt": 0,
		})
		
		logs_a = [_log(name="LOG-1", flow_index=1, item_code="M-1", batch_no="B1", qty_after_transaction_batch_based=1.0)]
		logs_b = [_log(name="LOG-2", flow_index=1, item_code="M-1", batch_no="B1", qty_after_transaction_batch_based=2.5)]
		
		mop_data_list = [
			{"mop_name": "MOP-A", "mop_doc": mop_doc, "logs": logs_a},
			{"mop_name": "MOP-B", "mop_doc": mop_doc, "logs": logs_b},
		]

		out_names, count = _sync_consolidated_group(group_key, mop_data_list)

		self.assertTrue(mock_gbq.call_args.kwargs.get("ignore_reserved_stock"))
		self.assertEqual(count, 2)
		self.assertEqual(out_names, ["SE-TEST-001"])
		self.assertEqual(se.stock_entry_type, "Material Transfer to Department")
		self.assertTrue(se.saved)
		self.assertTrue(se.submitted)
		self.assertEqual(len(se.items), 2)
		self.assertEqual(se.items[0].qty, 1.0)
		self.assertEqual(se.items[0].manufacturing_operation, "MOP-A")
		self.assertEqual(se.items[1].qty, 2.5)
		self.assertEqual(se.items[1].manufacturing_operation, "MOP-B")
		
		mock_mark_synced.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_synced")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._create_loss_entries", return_value=[])
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc")
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=100.0)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_undelivered_batch_qty",
		return_value=999.0,
	)
	def test_fallback_per_mop_when_consolidated_submit_fails(
		self,
		_mock_sre_audit,
		mock_gbq,
		mock_new_doc,
		mock_get_value,
		_loss_entries,
		mock_mark_synced,
		mock_log_error,
	):
		created = []

		def _new_doc(dt):
			self.assertEqual(dt, "Stock Entry")
			se = FlakyFakeStockEntry(f"SE-{len(created) + 1}")
			created.append(se)
			return se

		mock_new_doc.side_effect = _new_doc

		def _get_value_side_effect(doctype, name, field, *args, **kwargs):
			if doctype == "Item" and name == "M-1" and field == ["has_batch_no", "has_serial_no"]:
				return (0, 0)
			return None

		mock_get_value.side_effect = _get_value_side_effect

		group_key = ("Test Co", "MWO-1", "WH-START", "WH-END")
		mop_doc = FrappeDict(
			{
				"manufacturing_order": "MO-1",
				"manufacturing_work_order": "MWO-1",
				"loss_wt": 0,
				"manufacturer": "MF-FALLBACK",
			}
		)
		logs_a = [
			_log(name="LOG-1", flow_index=1, item_code="M-1", batch_no="B1", qty_after_transaction_batch_based=1.0)
		]
		logs_b = [
			_log(name="LOG-2", flow_index=1, item_code="M-1", batch_no="B1", qty_after_transaction_batch_based=2.5)
		]
		mop_data_list = [
			{"mop_name": "MOP-A", "mop_doc": mop_doc, "logs": logs_a},
			{"mop_name": "MOP-B", "mop_doc": mop_doc, "logs": logs_b},
		]

		out_names, count = _sync_consolidated_group(group_key, mop_data_list)

		self.assertEqual(len(created), 3)
		self.assertFalse(created[0].submitted)
		self.assertTrue(created[1].submitted)
		self.assertTrue(created[2].submitted)
		self.assertEqual(created[1].manufacturing_operation, "MOP-A")
		self.assertEqual(created[1].manufacturer, "MF-FALLBACK")
		self.assertEqual(created[2].manufacturing_operation, "MOP-B")
		self.assertEqual(created[2].manufacturer, "MF-FALLBACK")
		self.assertEqual(out_names, ["SE-2", "SE-3"])
		self.assertEqual(count, 2)
		mock_log_error.assert_called_once()
		mock_mark_synced.assert_called_once()
		self.assertTrue(mock_gbq.call_args.kwargs.get("ignore_reserved_stock"))


class TestValidateEodSourceBatchStock(FrappeTestCase):
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_passes_when_physical_batch_stock_sufficient(self, mock_gbq):
		items = [
			{
				"item_code": "IT-1",
				"batch_no": "B1",
				"s_warehouse": "WH-S",
				"qty": 3.0,
			},
			{
				"item_code": "IT-1",
				"batch_no": "B1",
				"s_warehouse": "WH-S",
				"qty": 2.0,
			},
		]
		_validate_eod_source_batch_stock(items)
		mock_gbq.assert_called()
		_, kwargs = mock_gbq.call_args
		self.assertTrue(kwargs.get("ignore_reserved_stock"))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._format_batch_short_diagnostics",
		return_value="(diagnostics)",
	)
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=1.0)
	def test_raises_when_physical_batch_stock_short(self, mock_gbq, _mock_diag):
		items = [
			{
				"item_code": "IT-1",
				"batch_no": "B1",
				"s_warehouse": "WH-S",
				"qty": 3.0,
			},
		]
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_source_batch_stock(items)
		_, kwargs = mock_gbq.call_args
		self.assertTrue(kwargs.get("ignore_reserved_stock"))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._list_open_sre_other_warehouses",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._list_open_sre_for_batch",
		return_value=[],
	)
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=1.0)
	def test_validation_error_includes_manufacturer_from_mop_doc(
		self, _mock_gbq, _mock_sre_here, _mock_sre_other
	):
		items = [
			{
				"item_code": "IT-1",
				"batch_no": "B1",
				"s_warehouse": "WH-S",
				"qty": 3.0,
			},
		]
		mop_doc = FrappeDict({"manufacturer": "MF-EOD-TEST"})
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": mop_doc}]
		with self.assertRaises(frappe.ValidationError) as ctx:
			_validate_eod_source_batch_stock(
				items,
				manufacturing_work_order="MWO-1",
				mop_data_list=mop_data_list,
				company="Test Co",
			)
		self.assertIn("Manufacturer: MF-EOD-TEST", str(ctx.exception))

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_undelivered_batch_qty",
		return_value=0.5,
	)
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_reservation_audit_logs_when_undelivered_sre_below_req(
		self, _mock_gbq, _mock_sre_qty, mock_log_error
	):
		"""Physical qty passes, but open SRE undelivered is below MOP line — audit only, no throw."""
		items = [
			{
				"item_code": "IT-1",
				"batch_no": "B1",
				"s_warehouse": "WH-S",
				"qty": 3.0,
			},
		]
		_validate_eod_source_batch_stock(items, manufacturing_work_order="MWO-1")
		mock_log_error.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_sre_undelivered_batch_qty",
		return_value=5.0,
	)
	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_no_audit_log_when_sre_covers_req(self, _mock_gbq, _mock_sre, mock_log):
		items = [
			{
				"item_code": "IT-1",
				"batch_no": "B1",
				"s_warehouse": "WH-S",
				"qty": 3.0,
			},
		]
		_validate_eod_source_batch_stock(items, manufacturing_work_order="MWO-1")
		mock_log.assert_not_called()


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
		self.assertEqual(len(se.items), 3)


class TestSyncMopLogsEntryPoint(FrappeTestCase):
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._sync_consolidated_group")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups")
	def test_sync_mop_logs_continues_after_transaction_failure(
		self, mock_get_groups, mock_sync_group, mock_log_error, mock_rollback, mock_release, mock_savepoint
	):
		logs_a = [{"mop_name": "MOP-A", "logs": []}]
		logs_b = [{"mop_name": "MOP-B", "logs": []}]
		
		key_a = ("Co", "MWO", "WH1", "WH2")
		key_b = ("Co", "MWO", "WH2", "WH3")
		
		mock_get_groups.return_value = {key_a: logs_a, key_b: logs_b}

		def sync_side_effect(group_key, data):
			if group_key == key_a:
				return (["SE-A"], 1)
			raise Exception("boom")

		mock_sync_group.side_effect = sync_side_effect

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 1)
		self.assertEqual(out["stock_entries"], ["SE-A"])
		
		self.assertEqual(mock_savepoint.call_count, 2)
		mock_release.assert_called_once()
		mock_rollback.assert_called_once()
		mock_log_error.assert_called_once()

