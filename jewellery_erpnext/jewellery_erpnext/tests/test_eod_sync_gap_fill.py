# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests covering the SRE-based MWO sync and Manufacturer loss-item resolution.

- Manufacturer-scoped loss item resolution (get_loss_item_from_manufacturer_mapping).
- _sync_mwo_via_sre error isolation: one failing MWO must not block others.
- _relocate_sre: cancel old SRE and recreate at new warehouse.
- sync_mop_logs idempotency: no-op when no unsynced logs.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"


# ---------------------------------------------------------------------------
# Manufacturer-scoped loss item resolution (tests main_slip.py helper)
# ---------------------------------------------------------------------------


class TestLossItemFromManufacturerMapping(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value"
	)
	def test_resolves_metal_to_ml(self, mock_get_value, mock_get_all, _mock_set):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		mock_get_value.side_effect = ["M", "ML"]
		mock_get_all.return_value = [
			{"item_attribute": "Metal Type", "attribute_value": "Gold"}
		]

		resolved_item = MagicMock()
		resolved_item.name = "ML-G-22KT-91.9-Y"
		with patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
		):
			result = get_loss_item_from_manufacturer_mapping(
				"M-G-22KT-91.9-Y", "Shubh", loss_type="Loss"
			)

		self.assertEqual(result, "ML-G-22KT-91.9-Y")

		args, kwargs = mock_get_value.call_args_list[1]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1]["parenttype"], "Manufacturer")
		self.assertEqual(args[1]["parent"], "Shubh")
		self.assertEqual(args[1]["parentfield"], "custom_variant_loss_table")
		self.assertEqual(args[1]["variant"], "M")
		self.assertEqual(args[1]["loss_type"], "Loss")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value"
	)
	def test_throws_clear_error_when_mapping_missing(self, mock_get_value):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		mock_get_value.side_effect = ["D", None]

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			get_loss_item_from_manufacturer_mapping("D-X", "Shubh", loss_type="Missing")

		self.assertIn("Manufacturer", str(ctx.exception))
		self.assertNotIn("MOP Settings", str(ctx.exception))

	def test_throws_when_manufacturer_missing(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with self.assertRaises(frappe.exceptions.ValidationError):
			get_loss_item_from_manufacturer_mapping("M-X", manufacturer=None)

	def test_throws_when_item_has_no_variant_of(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			return_value=None,
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
				get_loss_item_from_manufacturer_mapping(
					"X-NOT-A-VARIANT", "Shubh", "Loss"
				)
			self.assertIn("variant", str(ctx.exception).lower())

	def test_throws_when_target_loss_variant_unresolvable(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=["M", "ML"],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			return_value=[],
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=None,
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
				get_loss_item_from_manufacturer_mapping(
					"M-G-22KT-91.9-Y", "Shubh", "Loss"
				)
			self.assertIn("ML", str(ctx.exception))


class TestManufacturerLossMappingMatrix(FrappeTestCase):
	"""Variant + loss_type combinations must all resolve via custom_variant_loss_table."""

	def _run_resolution(self, source_item, variant_of, loss_type, expected_template):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=[variant_of, expected_template],
		) as mock_get_value, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			return_value=[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=MagicMock(name=f"{expected_template}-VARIANT"),
		):
			result = get_loss_item_from_manufacturer_mapping(
				source_item, "Shubh", loss_type
			)

		self.assertIsNotNone(result)
		args = mock_get_value.call_args_list[1][0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1]["parenttype"], "Manufacturer")
		self.assertEqual(args[1]["parent"], "Shubh")
		self.assertEqual(args[1]["parentfield"], "custom_variant_loss_table")
		self.assertEqual(args[1]["variant"], variant_of)
		self.assertEqual(args[1]["loss_type"], loss_type)

	def test_metal_loss_uses_ML(self):
		self._run_resolution("M-G-22KT-91.9-Y", "M", "Loss", "ML")

	def test_finding_loss_uses_FL(self):
		self._run_resolution("F-G-18KT-75.4-Y-X", "F", "Loss", "FL")

	def test_diamond_loss_uses_DL(self):
		self._run_resolution("D-X", "D", "Loss", "DL")

	def test_diamond_missing_uses_DM(self):
		self._run_resolution("D-X", "D", "Missing", "DM")

	def test_diamond_burn_uses_DB(self):
		self._run_resolution("D-X", "D", "Burn", "DB")

	def test_diamond_broken_uses_DBK(self):
		self._run_resolution("D-X", "D", "Broken", "DBK")

	def test_gemstone_loss_uses_GL(self):
		self._run_resolution("G-X", "G", "Loss", "GL")

	def test_gemstone_broken_uses_GB(self):
		self._run_resolution("G-X", "G", "Broken", "GB")

	def test_other_loss_uses_OL(self):
		self._run_resolution("O-X", "O", "Loss", "OL")

	def test_eod_loss_resolution_does_not_consult_mop_settings(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=["M", "ML"],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			return_value=[],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_single_value"
		) as mock_single, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=MagicMock(name="ML-VARIANT"),
		):
			get_loss_item_from_manufacturer_mapping("M-X", "Shubh", "Loss")

		mock_single.assert_not_called()


# ---------------------------------------------------------------------------
# sync_mop_logs: no-op when there are no unsynced logs
# ---------------------------------------------------------------------------


class TestEodSyncNoop(FrappeTestCase):
	@patch(f"{_MOD}._get_mwos_with_unsynced_logs", return_value={})
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.commit")
	def test_second_run_is_noop_when_no_unsynced_logs(
		self, _mock_commit, mock_set_value, _mock_groups
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 0)
		self.assertEqual(out["stock_entries"], [])
		# No Manufacturing Operation stamps
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if len(c[0]) >= 3 and c[0][0] == "Manufacturing Operation"
		]
		self.assertEqual(stamp_calls, [])


# ---------------------------------------------------------------------------
# sync_mop_logs: failure in one MWO must not block others
# ---------------------------------------------------------------------------


class TestSyncMwoErrorIsolation(FrappeTestCase):
	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}.frappe.log_error")
	@patch(f"{_MOD}._mark_synced")
	@patch(f"{_MOD}._sync_mwo_via_sre")
	@patch(f"{_MOD}._resolve_company_for_mwo", return_value="Test Co")
	@patch(f"{_MOD}._get_mwos_with_unsynced_logs")
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.commit")
	def test_failed_mwo_does_not_stop_successful_one(
		self,
		_mock_commit,
		_mock_set,
		mock_groups,
		_mock_company,
		mock_sync,
		mock_mark,
		mock_log_error,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		log_ok = frappe._dict(
			{
				"name": "L-OK",
				"manufacturing_work_order": "MWO-OK",
				"manufacturing_operation": "MOP-1",
			}
		)
		log_fail = frappe._dict(
			{
				"name": "L-FAIL",
				"manufacturing_work_order": "MWO-FAIL",
				"manufacturing_operation": "MOP-2",
			}
		)

		mock_groups.return_value = {
			"MWO-OK": {"MOP-1": [log_ok]},
			"MWO-FAIL": {"MOP-2": [log_fail]},
		}

		def sync_side(mwo, logs_by_mop, company):
			if mwo == "MWO-FAIL":
				raise Exception("intentional failure")
			return ["SE-OK"]

		mock_sync.side_effect = sync_side

		out = sync_mop_logs()

		self.assertIn("SE-OK", out["stock_entries"])
		self.assertEqual(out["processed"], 1)
		mock_mark.assert_called_once()
		mock_log_error.assert_called_once()

	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}.frappe.log_error")
	@patch(f"{_MOD}._mark_synced")
	@patch(f"{_MOD}._sync_mwo_via_sre", side_effect=Exception("boom"))
	@patch(f"{_MOD}._resolve_company_for_mwo", return_value="Test Co")
	@patch(f"{_MOD}._get_mwos_with_unsynced_logs")
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.commit")
	def test_failed_mwo_does_not_stamp_mark_synced(
		self,
		_mock_commit,
		_mock_set,
		mock_groups,
		_mock_company,
		_mock_sync,
		mock_mark,
		mock_log_error,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		log_fail = frappe._dict(
			{
				"name": "L-FAIL",
				"manufacturing_work_order": "MWO-FAIL",
				"manufacturing_operation": "MOP-1",
			}
		)
		mock_groups.return_value = {"MWO-FAIL": {"MOP-1": [log_fail]}}

		sync_mop_logs()

		mock_mark.assert_not_called()
		mock_log_error.assert_called_once()


# ---------------------------------------------------------------------------
# _relocate_sre: cancel + recreate at new warehouse
# ---------------------------------------------------------------------------


class TestRelocateSre(FrappeTestCase):
	def _make_mock_sre(self, warehouse="WH-OLD", reserved_qty=5.0, delivered_qty=0.0):
		sre_doc = MagicMock()
		sre_doc.docstatus = 1
		sre_doc.name = "SRE-TEST-001"
		sre_doc.item_code = "M-GOLD-22K"
		sre_doc.warehouse = warehouse
		sre_doc.reserved_qty = reserved_qty
		sre_doc.delivered_qty = delivered_qty
		sre_doc.voucher_type = "Sales Order"
		sre_doc.voucher_no = "SO-001"
		sre_doc.voucher_detail_no = "SO-001-row-1"
		sre_doc.voucher_qty = 10.0
		sre_doc.company = "Test Co"
		sre_doc.stock_uom = "Nos"
		sre_doc.reservation_based_on = "Qty"
		sre_doc.manufacturing_work_order = "MWO-001"
		sre_doc.manufacturing_operation = "MOP-001"
		sre_doc.ignore_permissions = False
		return sre_doc

	@patch(f"{_MOD}.frappe.get_doc")
	def test_skips_non_submitted_sre(self, mock_get_doc):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_relocate_sre,
		)

		mock_sre = self._make_mock_sre()
		mock_sre.docstatus = 0  # Draft
		mock_get_doc.return_value = mock_sre

		_relocate_sre(frappe._dict({"name": "SRE-TEST-001"}), "WH-NEW")

		mock_sre.cancel.assert_not_called()

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=5.0,
	)
	@patch(f"{_MOD}.frappe.new_doc")
	@patch(f"{_MOD}.frappe.get_all", return_value=[])
	@patch(f"{_MOD}.frappe.get_cached_value", return_value=(0, 0))
	@patch(f"{_MOD}.frappe.get_doc")
	def test_cancels_old_sre_and_creates_new_at_new_warehouse(
		self, mock_get_doc, _mock_cached, _mock_get_all, mock_new_doc, _mock_avail
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_relocate_sre,
		)

		mock_sre = self._make_mock_sre(
			warehouse="WH-OLD", reserved_qty=5.0, delivered_qty=0.0
		)
		mock_get_doc.return_value = mock_sre

		new_sre = MagicMock()
		new_sre.sb_entries = []
		mock_new_doc.return_value = new_sre

		_relocate_sre(frappe._dict({"name": "SRE-TEST-001"}), "WH-NEW")

		mock_sre.cancel.assert_called_once()
		new_sre.insert.assert_called_once()
		new_sre.submit.assert_called_once()
		# New SRE warehouse must be WH-NEW
		self.assertEqual(new_sre.warehouse, "WH-NEW")
		self.assertEqual(new_sre.reserved_qty, 5.0)

	@patch(f"{_MOD}.frappe.get_doc")
	def test_fully_delivered_sre_only_cancelled_not_recreated(self, mock_get_doc):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_relocate_sre,
		)

		# reserved_qty == delivered_qty → remaining = 0
		mock_sre = self._make_mock_sre(reserved_qty=3.0, delivered_qty=3.0)
		mock_get_doc.return_value = mock_sre

		with patch(f"{_MOD}.frappe.get_cached_value", return_value=(0, 0)):
			_relocate_sre(frappe._dict({"name": "SRE-TEST-001"}), "WH-NEW")

		mock_sre.cancel.assert_called_once()
		# No new SRE should be created (new_doc never called)

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=2.325,
	)
	@patch(f"{_MOD}.frappe.new_doc")
	@patch(f"{_MOD}.frappe.get_all")
	@patch(f"{_MOD}.frappe.get_cached_value", return_value=(1, 0))
	@patch(f"{_MOD}.frappe.get_doc")
	def test_batch_entries_rebuilt_in_new_sre(
		self, mock_get_doc, _mock_cached, mock_get_all, mock_new_doc, _mock_avail
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_relocate_sre,
		)

		mock_sre = self._make_mock_sre(reserved_qty=2.350, delivered_qty=0.0)
		mock_get_doc.return_value = mock_sre

		# Old SRE has one batch entry
		mock_get_all.return_value = [
			frappe._dict({"batch_no": "BATCH-001", "qty": 2.350, "delivered_qty": 0.0})
		]

		new_sre = MagicMock()
		appended = []
		new_sre.sb_entries = appended

		def _append(fieldname, payload):
			if fieldname == "sb_entries":
				appended.append(payload)

		new_sre.append.side_effect = _append
		mock_new_doc.return_value = new_sre

		_relocate_sre(frappe._dict({"name": "SRE-TEST-001"}), "WH-NEW")

		self.assertEqual(len(appended), 1)
		self.assertEqual(appended[0]["batch_no"], "BATCH-001")
		self.assertAlmostEqual(appended[0]["qty"], 2.350, places=3)
		self.assertEqual(new_sre.warehouse, "WH-NEW")
