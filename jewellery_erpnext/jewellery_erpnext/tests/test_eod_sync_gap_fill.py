# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the EOD gap-fill additions:

- Manufacturer-scoped loss item resolution (get_loss_item_from_manufacturer_mapping).
- last_eod_sync_on stamp on Manufacturing Operation success path.
- Audit-first SRE reconciliation (_reconcile_reservations_for_mwo).
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


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

		# 1) Item.variant_of -> "M"
		# 2) Variant Loss Table mapping (parent=Manufacturer, variant=M, loss_type=Loss) -> "ML"
		mock_get_value.side_effect = ["M", "ML"]
		mock_get_all.return_value = [
			{"item_attribute": "Metal Type", "attribute_value": "Gold"}
		]

		# set_items_from_attribute returns a doc-like object with .name
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

		# Ensure the per-Manufacturer query was used.
		# Second invocation: doctype is "Variant Loss Table", filters scoped to Manufacturer.
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

		# variant_of resolves, but mapping query returns None.
		mock_get_value.side_effect = ["D", None]

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			get_loss_item_from_manufacturer_mapping("D-X", "Shubh", loss_type="Missing")

		# Error message points to Manufacturer config, NOT MOP Settings.
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
		"""Mapping resolves to a loss_variant template, but no Item variant
		exists for the source attributes.
		"""
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
			# Error path mentions the unresolved variant template.
			self.assertIn("ML", str(ctx.exception))


class TestManufacturerLossMappingMatrix(FrappeTestCase):
	"""Variant + loss_type combinations described in the spec must all
	resolve via the Manufacturer's custom_variant_loss_table.
	"""

	def _run_resolution(self, source_item, variant_of, loss_type, expected_template):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		# get_value side_effects: (1) Item.variant_of, (2) Variant Loss Table loss_variant.
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

		# Result should be the resolved variant.
		self.assertIsNotNone(result)
		# The Variant Loss Table query was scoped to the right
		# (variant, loss_type) combo and parented to Manufacturer.
		_args, _kwargs = mock_get_value.call_args_list[1]
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
		"""Defense check: the new helper must not read MOP Settings dust_item.
		If anyone re-introduces that path, this test fails.
		"""
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

		# Helper must not read MOP Settings.dust_item in the resolution path.
		mock_single.assert_not_called()


class TestSyncStamp(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
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
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._sync_consolidated_group",
		return_value=(["STE-1"], 1),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups"
	)
	def test_eod_sync_stamps_last_eod_sync_on_after_success(
		self,
		mock_groups,
		_mock_consolidated,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_set_value,
		_mock_now,
		_mock_reconcile,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		mock_groups.return_value = {
			("CO", "MWO-1", "WH-A", "WH-B"): [
				{"mop_name": "MOP-1", "mop_doc": MagicMock(), "logs": []},
				{"mop_name": "MOP-2", "mop_doc": MagicMock(), "logs": []},
			]
		}

		out = sync_mop_logs()

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
		self.assertEqual(out["processed"], 1)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.log_error"
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
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._sync_consolidated_group",
		side_effect=Exception("boom"),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups"
	)
	def test_failed_group_does_not_stamp(
		self,
		mock_groups,
		_mock_consolidated,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_set_value,
		_mock_log,
		_mock_reconcile,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		mock_groups.return_value = {
			("CO", "MWO-1", "WH-A", "WH-B"): [
				{"mop_name": "MOP-FAIL", "mop_doc": MagicMock(), "logs": []},
			]
		}

		sync_mop_logs()

		# The exception path skipped the stamp block entirely.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(stamp_calls, [])


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
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._sync_consolidated_group"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups",
		return_value={},
	)
	def test_second_run_is_noop_when_no_unsynced_logs(
		self, _mock_groups, mock_sync, mock_set_value, mock_reconcile
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 0)
		self.assertEqual(out["stock_entries"], [])
		mock_sync.assert_not_called()
		# No MOPs were touched; no stamps and no reconcile calls.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(stamp_calls, [])
		mock_reconcile.assert_not_called()


class TestEodLossResolutionRequiresManufacturerMapping(FrappeTestCase):
	"""The EOD loss path must resolve the loss item via Manufacturer mapping.
	If the helper throws, _create_loss_entries propagates — there is no
	silent skip, no MOP Settings fallback.
	"""

	def test_eod_loss_propagates_manufacturer_mapping_error(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_create_loss_entries,
		)

		# Stand up a Stock Entry mock that records appended items.
		stock_entry = MagicMock()
		stock_entry.items = []

		def append(_, payload):
			row = MagicMock()
			row.item_code = payload.get("item_code")
			row.qty = payload.get("qty")
			stock_entry.items.append(row)

		stock_entry.append.side_effect = append

		variant_loss_dict = frappe._dict(
			{
				"loss_warehouse": "WH-LOSS",
				"consider_department_warehouse": 0,
				"warehouse_type": None,
			}
		)
		mop = frappe._dict(
			{
				"company": "Test Co",
				"manufacturer": "Shubh",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
				"department": "Waxing - GEPL",
			}
		)
		latest_logs = [
			frappe._dict(
				{
					"item_code": "M-X",
					"batch_no": "B1",
					"qty_after_transaction_batch_based": 5.0,
				}
			)
		]

		# Build all patches inline so frappe.exceptions.ValidationError is
		# instantiated lazily (after frappe.connect, when the test body runs).
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc",
			return_value=stock_entry,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_value",
			return_value=variant_loss_dict,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_loss_item_from_manufacturer_mapping",
			side_effect=frappe.exceptions.ValidationError(
				"Loss Item could not be resolved for Item M-X. "
				"Configure Manufacturer Shubh -> Variant Loss Table."
			),
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
				_create_loss_entries(mop, "MOP-1", latest_logs, "WH-END", 0.5)
			self.assertIn("Manufacturer", str(ctx.exception))
