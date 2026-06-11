# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the EOD gap-fill additions:

- Loss item resolution through Variant Loss Table (get_item_loss_item).
- last_eod_sync_on stamp on Manufacturing Operation success path.
- Audit-first SRE reconciliation (_reconcile_reservations_for_mwo).
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _loss_item_doc(name, variant_of="ML"):
	doc = MagicMock()
	doc.name = name
	doc.variant_of = variant_of
	return doc


class TestItemLossItemResolution(FrappeTestCase):
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
			get_item_loss_item,
		)

		mock_get_value.side_effect = ["ML", "HSN-1"]
		mock_get_all.side_effect = [
			[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
			[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
		]

		resolved_item = _loss_item_doc("ML-G-22KT-91.9-Y", variant_of="ML")
		with patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
		):
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M", "Loss")

		self.assertEqual(result, "ML-G-22KT-91.9-Y")
		resolved_item.save.assert_called_once()

		args, _kwargs = mock_get_value.call_args_list[0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1]["variant"], "M")
		self.assertEqual(args[1]["loss_type"], "Loss")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value"
	)
	def test_throws_clear_error_when_mapping_missing(self, mock_get_value):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_item_loss_item,
		)

		mock_get_value.return_value = None

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			get_item_loss_item("Test Co", "D-X", "D", loss_type="Missing")

		self.assertIn("Variant Loss Table", str(ctx.exception))
		self.assertNotIn("MOP Settings", str(ctx.exception))

	def test_without_loss_type_falls_back_to_source_variant(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_item_loss_item,
		)

		resolved_item = _loss_item_doc("M-G-22KT-91.9-Y", variant_of="M")

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=[None, "HSN-1"],
		) as mock_get_value, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
		):
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M")

		self.assertEqual(result, "M-G-22KT-91.9-Y")
		args, _kwargs = mock_get_value.call_args_list[0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1], {"variant": "M"})

	def test_throws_when_target_loss_variant_unresolvable(self):
		"""Mapping resolves to a loss_variant template, then creates the missing variant."""
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_item_loss_item,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			return_value="ML",
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=None,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_loss_item",
			return_value="ML-G-22KT-91.9-Y",
		) as mock_create:
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M", "Loss")

		self.assertEqual(result, "ML-G-22KT-91.9-Y")
		mock_create.assert_called_once_with("ML", {"Metal Type": "Gold"})


class TestLossMappingMatrix(FrappeTestCase):
	"""Variant + loss_type combinations described in the spec must all
	resolve via Variant Loss Table.
	"""

	def _run_resolution(self, source_item, variant_of, loss_type, expected_template):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_item_loss_item,
		)

		resolved_item = _loss_item_doc(
			f"{expected_template}-VARIANT", variant_of=expected_template
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=[expected_template, "HSN-1"],
		) as mock_get_value, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
		):
			result = get_item_loss_item("Test Co", source_item, variant_of, loss_type)

		# Result should be the resolved variant.
		self.assertEqual(result, f"{expected_template}-VARIANT")
		# The Variant Loss Table query was scoped to the right
		# (variant, loss_type) combo.
		args = mock_get_value.call_args_list[0][0]
		self.assertEqual(args[0], "Variant Loss Table")
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
		"""Defense check: the helper must not read MOP Settings dust_item.
		If anyone re-introduces that path, this test fails.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_item_loss_item,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=["ML", "HSN-1"],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_single_value"
		) as mock_single, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=_loss_item_doc("ML-VARIANT", variant_of="ML"),
		):
			get_item_loss_item("Test Co", "M-X", "M", "Loss")

		# Helper must not read MOP Settings.dust_item in the resolution path.
		mock_single.assert_not_called()


class TestSyncStamp(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._recreate_sres_at"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._cancel_sre_snapshots"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._snapshot_mwo_sres_for_relocation",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._save_draft_eod_se",
		return_value="STE-1",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_source_batch_stock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_items_for_mwo_reservation"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._preload_sre_warehouse_map",
		return_value={("M-X", "B1"): "WH-A"},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.utils.now",
		return_value="2026-05-04 12:00:00",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	def test_eod_sync_stamps_last_eod_sync_on_after_success(
		self,
		mock_get_doc,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_set_value,
		_mock_now,
		_mock_artifact,
		_mock_sre_map,
		_mock_validate_reservation,
		_mock_validate_stock,
		_mock_save_draft,
		_mock_snapshot,
		_mock_cancel_sres,
		_mock_recreate_sres,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_process_mwo_group,
		)

		stock_entry = MagicMock()
		mock_get_doc.return_value = stock_entry
		mop_data_list = [
			{
				"mop_name": "MOP-1",
				"mop_doc": frappe._dict(
					{"manufacturing_order": "MO-1", "manufacturer": "Shubh"}
				),
				"logs": [
					frappe._dict(
						{
							"item_code": "M-X",
							"batch_no": "B1",
							"qty_after_transaction_batch_based": 5.0,
							"to_warehouse": "WH-B",
							"flow_index": 1,
							"creation": "2026-05-04 10:00:00",
						}
					)
				],
			},
			{
				"mop_name": "MOP-2",
				"mop_doc": frappe._dict(
					{"manufacturing_order": "MO-1", "manufacturer": "Shubh"}
				),
				"logs": [],
			},
		]
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}

		_process_mwo_group(("CO", "MWO-1"), mop_data_list, failures, stats)

		# Stamp once per MOP in the successful group.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(len(stamp_calls), 2)
		# update_modified=False is part of the contract.
		for c in stamp_calls:
			self.assertEqual(c[1].get("update_modified"), False)
		self.assertEqual(failures, [])
		self.assertEqual(stats["processed_mwos"], 1)
		self.assertEqual(stats["submitted_ses"], ["STE-1"])
		stock_entry.submit.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._recreate_sres_at"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._cancel_sre_snapshots"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._snapshot_mwo_sres_for_relocation",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._save_draft_eod_se",
		return_value="STE-DRAFT",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_source_batch_stock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_items_for_mwo_reservation"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._preload_sre_warehouse_map",
		return_value={("M-X", "B1"): "WH-A"},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	def test_failed_group_does_not_stamp(
		self,
		mock_get_doc,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_set_value,
		_mock_artifact,
		_mock_sre_map,
		_mock_validate_reservation,
		_mock_validate_stock,
		_mock_save_draft,
		_mock_snapshot,
		_mock_cancel_sres,
		_mock_recreate_sres,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_process_mwo_group,
		)

		stock_entry = MagicMock()
		stock_entry.submit.side_effect = Exception("boom")
		mock_get_doc.return_value = stock_entry
		mop_data_list = [
			{
				"mop_name": "MOP-FAIL",
				"mop_doc": frappe._dict(
					{"manufacturing_order": "MO-1", "manufacturer": "Shubh"}
				),
				"logs": [
					frappe._dict(
						{
							"item_code": "M-X",
							"batch_no": "B1",
							"qty_after_transaction_batch_based": 5.0,
							"to_warehouse": "WH-B",
							"flow_index": 1,
							"creation": "2026-05-04 10:00:00",
						}
					)
				],
			}
		]
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}

		_process_mwo_group(("CO", "MWO-1"), mop_data_list, failures, stats)

		# The exception path skipped the stamp block entirely.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(stamp_calls, [])
		self.assertEqual(stats["processed_mwos"], 0)
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertEqual(stats["draft_ses"], ["STE-DRAFT"])


class TestSreReconciliationDryRun(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.logger"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_dry_run_default_does_not_cancel(
		self, mock_get_all, _mock_balance, mock_get_doc, mock_logger
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_reconcile_reservations_for_mwo,
		)

		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-zero",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		log = MagicMock()
		mock_logger.return_value = log

		_reconcile_reservations_for_mwo("MWO-1")

		# Dry run: NEVER call frappe.get_doc to cancel.
		mock_get_doc.assert_not_called()
		# Logged the would-cancel decision.
		log.info.assert_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.logger"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_destructive_cancels_zero_balance(
		self,
		mock_get_all,
		_mock_balance,
		mock_get_doc,
		_mock_logger,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_reconcile_reservations_for_mwo,
		)

		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-cancel-me",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		sre_doc = MagicMock()
		mock_get_doc.return_value = sre_doc

		_reconcile_reservations_for_mwo("MWO-1", dry_run=False)

		mock_get_doc.assert_called_once_with("Stock Reservation Entry", "SRE-cancel-me")
		sre_doc.cancel.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_ambiguous_balance_never_cancels(
		self, mock_get_all, mock_balance, mock_get_doc
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_reconcile_reservations_for_mwo,
		)

		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-partial",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 10.0,
					"delivered_qty": 2.0,
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		# Latest balance is positive — partial coverage; reconcile must skip.
		mock_balance.return_value = [
			{"item_code": "M-X", "to_warehouse": "WH-Y", "qty": 4.0}
		]

		_reconcile_reservations_for_mwo("MWO-1", dry_run=False)

		mock_get_doc.assert_not_called()


class TestEodSyncIdempotentRerun(FrappeTestCase):
	"""When `_get_unsynced_mop_groups` returns an empty dict, EOD must be a
	no-op — no Stock Entry created, no `last_eod_sync_on` stamps, no
	reconciliation calls. This is the steady-state second run.
	"""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.recalculate_sync_log_totals"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.release_eod_sync_lock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.set_eod_sync_running"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc",
		return_value=frappe._dict({"eod_sync_work_order_filter": []}),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups",
		return_value={},
	)
	def test_second_run_is_noop_when_no_unsynced_logs(
		self,
		_mock_groups,
		mock_new_doc,
		_mock_settings,
		_mock_set_running,
		_mock_release_lock,
		mock_set_value,
		_mock_recalculate,
		mock_reconcile,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		sync_log = MagicMock()
		sync_log.name = "MOP-EOD-SYNC-LOG-1"
		mock_new_doc.return_value = sync_log

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 0)
		self.assertEqual(out["stock_entries"], [])
		# No MOPs were touched; no stamps and no reconcile calls.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(stamp_calls, [])
		mock_reconcile.assert_not_called()
