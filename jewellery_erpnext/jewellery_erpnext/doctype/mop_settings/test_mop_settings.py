# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.types.frappedict import _dict as FD
from frappe.types.frappedict import _dict as FrappeDict
from frappe.utils import add_to_date, now_datetime

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
	is_eod_sync_locked,
	release_eod_sync_lock,
	release_expired_eod_sync_lock,
	set_eod_sync_queued,
	set_eod_sync_running,
	validate_not_eod_sync_locked,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_apply_mwo_filter_rows,
	_build_eod_se_rows,
	_cancel_sre_snapshots,
	_check_eod_source_batch_stock,
	_collect_mop_names,
	_commit_company_issues_se,
	_commit_company_main_se,
	_eod_base_mr_voucher_qty,
	_find_last_operation,
	_format_batch_short_diagnostics,
	_get_last_logs_per_item_batch,
	_get_sync_range,
	_get_t_warehouse_from_logs,
	_get_unsynced_mop_groups,
	_hard_delete_cancelled_snapshots,
	_mark_all_mwo_mop_logs_synced,
	_mop_manufacturer_label,
	_mwo_realized_by_artifact,
	_plan_mwo_group,
	_preload_sre_warehouse_map,
	_process_mwo_group,
	_reconcile_reservations_for_mwo,
	_reserve_batch_at_physical_warehouse,
	_reserve_sres_from_eod_se_rows,
	_resolve_department_warehouse,
	_resolve_eod_manufacturer_label,
	_resolve_run_range,
	_save_draft_eod_se,
	_snapshot_mwo_sres_for_relocation,
	_today_range,
	_validate_eod_items_for_mwo_reservation,
	_validate_eod_source_batch_stock,
	recalculate_sync_log_totals,
	sync_mop_logs,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler import (
	check_and_enqueue_eod_sync,
)


class TestMOPSettings(IntegrationTestCase):
	"""Tests for MOP Settings validation and helpers."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_validate_warns_when_reservation_types_incomplete(self):
		doc = frappe.get_doc("MOP Settings")
		doc.stock_entry_type_to_reservation = []
		doc.append(
			"stock_entry_type_to_reservation",
			{"stock_entry_type_to_reservation": "Material Transfer (WORK ORDER)"},
		)
		with patch.object(frappe, "msgprint") as mock_msgprint:
			doc.validate()
		mock_msgprint.assert_called_once()
		kwargs = mock_msgprint.call_args[1]
		self.assertEqual(kwargs.get("indicator"), "orange")

	def test_validate_silent_when_all_eir_types_configured(self):
		# _RESERVATION_TYPES_FOR_EIR requires three SE types: Repack,
		# Material Transfer (WORK ORDER), Material Receive (WORK ORDER).
		doc = frappe.get_doc("MOP Settings")
		doc.stock_entry_type_to_reservation = []
		for se_type in (
			"Material Transfer (WORK ORDER)",
			"Repack",
			"Material Receive (WORK ORDER)",
		):
			doc.append(
				"stock_entry_type_to_reservation",
				{"stock_entry_type_to_reservation": se_type},
			)
		with patch.object(frappe, "msgprint") as mock_msgprint:
			doc.validate()
		mock_msgprint.assert_not_called()


_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock"


def _settings(running=0, lock_until=None):
	future = add_to_date(now_datetime(), hours=2)
	return FrappeDict(
		{
			"eod_sync_running": running,
			"eod_sync_lock_until": lock_until
			if lock_until is not None
			else (future if running else None),
		}
	)


# ---------------------------------------------------------------------------
# is_eod_sync_locked
# ---------------------------------------------------------------------------


class TestIsEodSyncLocked(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.get_value")
	def test_returns_false_when_not_running(self, mock_gv):
		mock_gv.return_value = FrappeDict(
			{"eod_sync_running": 0, "eod_sync_lock_until": None}
		)
		self.assertFalse(is_eod_sync_locked())

	@patch(f"{_MOD}.frappe.db.get_value")
	def test_returns_true_when_running_and_lock_in_future(self, mock_gv):
		mock_gv.return_value = _settings(running=1)
		self.assertTrue(is_eod_sync_locked())

	@patch(f"{_MOD}.frappe.db.get_value")
	def test_returns_false_when_running_but_lock_expired(self, mock_gv):
		past = add_to_date(now_datetime(), hours=-3)
		mock_gv.return_value = FrappeDict(
			{"eod_sync_running": 1, "eod_sync_lock_until": past}
		)
		self.assertFalse(is_eod_sync_locked())

	@patch(f"{_MOD}.frappe.db.get_value")
	def test_returns_false_when_no_row(self, mock_gv):
		mock_gv.return_value = None
		self.assertFalse(is_eod_sync_locked())


# ---------------------------------------------------------------------------
# set_eod_sync_running
# ---------------------------------------------------------------------------


class TestSetEodSyncRunning(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	def test_sets_running_and_commits(self, mock_sv, mock_commit):
		set_eod_sync_running()
		mock_sv.assert_called_once()
		_, _, values = mock_sv.call_args.args
		self.assertEqual(values["eod_sync_running"], 1)
		self.assertEqual(values["eod_sync_status"], "Running")
		self.assertIn("eod_sync_lock_until", values)
		mock_commit.assert_called_once()


# ---------------------------------------------------------------------------
# set_eod_sync_queued
# ---------------------------------------------------------------------------


class TestSetEodSyncQueued(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	def test_sets_queued_status_and_commits(self, mock_sv, mock_commit):
		set_eod_sync_queued()
		mock_sv.assert_called_once()
		_, _, values = mock_sv.call_args.args
		self.assertEqual(values["eod_sync_status"], "Queued")
		# Must NOT set running=1
		self.assertNotIn("eod_sync_running", values)
		mock_commit.assert_called_once()


# ---------------------------------------------------------------------------
# release_eod_sync_lock
# ---------------------------------------------------------------------------


class TestReleaseEodSyncLock(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	def test_success_release_sets_completed(self, mock_sv, mock_commit):
		release_eod_sync_lock(success=True)
		_, _, values = mock_sv.call_args.args
		self.assertEqual(values["eod_sync_running"], 0)
		self.assertEqual(values["eod_sync_status"], "Completed")
		self.assertIn("eod_sync_last_completed_on", values)
		mock_commit.assert_called_once()

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	def test_failure_release_sets_failed_with_error_log(self, mock_sv, mock_commit):
		release_eod_sync_lock(success=False, error_log_name="ERR-001")
		_, _, values = mock_sv.call_args.args
		self.assertEqual(values["eod_sync_running"], 0)
		self.assertEqual(values["eod_sync_status"], "Failed")
		self.assertEqual(values["eod_sync_last_error_log"], "ERR-001")
		mock_commit.assert_called_once()


# ---------------------------------------------------------------------------
# release_expired_eod_sync_lock
# ---------------------------------------------------------------------------


class TestReleaseExpiredEodSyncLock(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.get_value")
	def test_clears_lock_when_expired(self, mock_gv, mock_sv, mock_commit):
		past = add_to_date(now_datetime(), hours=-3)
		mock_gv.return_value = FrappeDict(
			{"eod_sync_running": 1, "eod_sync_lock_until": past}
		)
		release_expired_eod_sync_lock()
		mock_sv.assert_called_once()
		_, _, values = mock_sv.call_args.args
		self.assertEqual(values["eod_sync_running"], 0)
		self.assertEqual(values["eod_sync_status"], "Timeout Released")
		mock_commit.assert_called_once()

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.get_value")
	def test_does_nothing_when_not_running(self, mock_gv, mock_sv, mock_commit):
		mock_gv.return_value = FrappeDict(
			{"eod_sync_running": 0, "eod_sync_lock_until": None}
		)
		release_expired_eod_sync_lock()
		mock_sv.assert_not_called()
		mock_commit.assert_not_called()

	@patch(f"{_MOD}.frappe.db.commit")
	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.get_value")
	def test_does_nothing_when_lock_still_active(self, mock_gv, mock_sv, mock_commit):
		future = add_to_date(now_datetime(), hours=1)
		mock_gv.return_value = FrappeDict(
			{"eod_sync_running": 1, "eod_sync_lock_until": future}
		)
		release_expired_eod_sync_lock()
		mock_sv.assert_not_called()
		mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# validate_not_eod_sync_locked
# ---------------------------------------------------------------------------


class TestValidateNotEodSyncLocked(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.is_eod_sync_locked", return_value=True)
	def test_throws_when_locked(self, _mock):
		doc = MagicMock()
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_not_eod_sync_locked(doc)

	@patch(f"{_MOD}.is_eod_sync_locked", return_value=True)
	def test_bypasses_when_in_eod_flag_set(self, mock_locked):
		frappe.flags.in_eod_mop_sync = True
		try:
			doc = MagicMock()
			validate_not_eod_sync_locked(doc)  # must not raise
		finally:
			frappe.flags.in_eod_mop_sync = False
		mock_locked.assert_not_called()

	@patch(f"{_MOD}.is_eod_sync_locked", return_value=False)
	def test_does_not_throw_when_not_locked(self, _mock):
		doc = MagicMock()
		validate_not_eod_sync_locked(doc)  # must not raise


# ---------------------------------------------------------------------------
# Scheduler: check_and_enqueue_eod_sync
# ---------------------------------------------------------------------------

_SCHED_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler"


class TestCheckAndEnqueueEodSync(IntegrationTestCase):
	"""Tests for the per-minute scheduler entry point.

	Mocks are started in setUp (keyed dict) to avoid decorator-ordering pitfalls.
	``new_doc``/``set_value``/``exists`` keep the Sync-Log creation block inert so
	the time-gate logic is asserted in isolation.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		patchers = {
			"rel": patch(f"{_SCHED_MOD}.release_expired_eod_sync_lock"),
			"now": patch(f"{_SCHED_MOD}.now_datetime"),
			"gv": patch(f"{_SCHED_MOD}.frappe.db.get_value"),
			"exists": patch(f"{_SCHED_MOD}.frappe.db.exists"),
			"new_doc": patch(f"{_SCHED_MOD}.frappe.new_doc"),
			"set_value": patch(f"{_SCHED_MOD}.frappe.db.set_value"),
			"queued": patch(f"{_SCHED_MOD}.set_eod_sync_queued"),
			"enq": patch(f"{_SCHED_MOD}.frappe.enqueue"),
		}
		self.m = {k: p.start() for k, p in patchers.items()}
		for p in patchers.values():
			self.addCleanup(p.stop)
		# Defaults: configured time 02:00, idle, no prior Scheduler attempt today.
		self.m["exists"].return_value = False
		self.m["gv"].return_value = self._make_settings()

	def _make_settings(self, **overrides):
		base = FrappeDict(
			{
				"eod_sync_time": "02:00:00",
				"eod_sync_running": 0,
				"eod_sync_lock_until": None,
				"eod_sync_last_completed_on": None,
				"eod_sync_status": "Idle",
			}
		)
		base.update(overrides)
		return base

	def _run(self):
		check_and_enqueue_eod_sync()

	def _assert_enqueued(self):
		self.m["queued"].assert_called_once()
		self.m["enq"].assert_called_once()

	def _assert_not_enqueued(self):
		self.m["queued"].assert_not_called()
		self.m["enq"].assert_not_called()

	def test_enqueues_when_time_matches(self):
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 0, 0)
		self._run()
		self._assert_enqueued()

	def test_enqueues_with_catchup_when_minute_missed(self):
		# Bug A regression: tick lands 17 minutes AFTER the configured 02:00 minute.
		# Old exact-equality skipped the whole day; catch-up must still enqueue once.
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 17, 0)
		self._run()
		self._assert_enqueued()

	def test_no_enqueue_when_not_yet_due(self):
		# 01:59 is before the configured 02:00 → not yet due.
		self.m["now"].return_value = datetime(2026, 5, 31, 1, 59, 0)
		self._run()
		self._assert_not_enqueued()

	def test_no_enqueue_when_already_attempted_today(self):
		# Once-per-day: a Scheduler log already exists for today → skip even though due.
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 17, 0)
		self.m["exists"].return_value = True
		self._run()
		self._assert_not_enqueued()

	def test_failed_run_does_not_refire(self):
		# A prior run FAILED today (last_completed_on still None, so the completed-today
		# guard passes), but a Scheduler log exists → the attempt guard stops the re-run.
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 17, 0)
		self.m["gv"].return_value = self._make_settings(eod_sync_last_completed_on=None)
		self.m["exists"].return_value = True
		self._run()
		self._assert_not_enqueued()

	def test_enqueues_for_hour_below_ten_timedelta(self):
		# Bug B: Time field reads back as timedelta; hours < 10 must still fire.
		self.m["now"].return_value = datetime(2026, 5, 31, 9, 5, 0)
		self.m["gv"].return_value = self._make_settings(
			eod_sync_time=timedelta(hours=9)
		)
		self._run()
		self._assert_enqueued()

	def test_enqueues_for_hour_below_ten_string(self):
		# Bug B parity: same time as a string form.
		self.m["now"].return_value = datetime(2026, 5, 31, 9, 5, 0)
		self.m["gv"].return_value = self._make_settings(eod_sync_time="09:00:00")
		self._run()
		self._assert_enqueued()

	def test_no_enqueue_when_already_queued(self):
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 0, 0)
		self.m["gv"].return_value = self._make_settings(eod_sync_status="Queued")
		self._run()
		self._assert_not_enqueued()

	def test_no_enqueue_when_already_ran_today(self):
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 0, 0)
		self.m["gv"].return_value = self._make_settings(
			eod_sync_last_completed_on=datetime(2026, 5, 31, 0, 0, 0)
		)
		self._run()
		self._assert_not_enqueued()

	def test_no_enqueue_when_running(self):
		self.m["now"].return_value = datetime(2026, 5, 31, 2, 0, 0)
		self.m["gv"].return_value = self._make_settings(eod_sync_running=1)
		self._run()
		self._assert_not_enqueued()


# ---------------------------------------------------------------------------
# Permission tests for MOPSettings controller
# ---------------------------------------------------------------------------


_SETTINGS_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_settings"


class TestMOPSettingsPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SETTINGS_MOD}.frappe.get_roles", return_value=["System Manager", "All"])
	def test_system_manager_can_change_eod_sync_time(self, _mock_roles):
		doc = frappe.get_doc("MOP Settings")
		old = MagicMock()
		old.eod_sync_time = "00:00:00"
		doc.eod_sync_time = "02:00:00"
		with patch.object(doc, "get_doc_before_save", return_value=old):
			with patch.object(doc, "add_comment"):
				doc._validate_eod_sync_time_permission()  # must not raise

	@patch(f"{_SETTINGS_MOD}.frappe.get_roles", return_value=["Accounts User", "All"])
	def test_non_system_manager_cannot_change_eod_sync_time(self, _mock_roles):
		doc = frappe.get_doc("MOP Settings")
		old = MagicMock()
		old.eod_sync_time = "00:00:00"
		doc.eod_sync_time = "03:00:00"
		with patch.object(doc, "get_doc_before_save", return_value=old):
			with self.assertRaises(frappe.exceptions.ValidationError):
				doc._validate_eod_sync_time_permission()

	@patch(f"{_SETTINGS_MOD}.frappe.get_roles", return_value=["Accounts User", "All"])
	def test_non_system_manager_cannot_call_sync_mop_log(self, _mock_roles):
		doc = frappe.get_doc("MOP Settings")
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.sync_mop_log()

	@patch(f"{_SETTINGS_MOD}.frappe.enqueue")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.set_eod_sync_queued"
	)
	@patch(f"{_SETTINGS_MOD}.frappe.get_roles", return_value=["System Manager", "All"])
	def test_system_manager_can_trigger_sync(
		self, _mock_roles, _mock_queued, _mock_locked, mock_enq
	):
		doc = frappe.get_doc("MOP Settings")
		with patch.object(frappe, "msgprint"):
			doc.sync_mop_log()
		mock_enq.assert_called_once()


# ---------------------------------------------------------------------------
# Sync behavior: draft SE preserved on submit failure
# ---------------------------------------------------------------------------


_SYNC_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"


class TestDraftSePreservedOnSubmitFailure(IntegrationTestCase):
	"""Validates Phase 2 savepoint design: draft SE must survive a submit failure."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SYNC_MOD}.frappe.db.set_value")
	@patch(f"{_SYNC_MOD}.frappe.db.savepoint")
	@patch(f"{_SYNC_MOD}.frappe.db.release_savepoint")
	@patch(f"{_SYNC_MOD}.frappe.db.rollback")
	@patch(f"{_SYNC_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(f"{_SYNC_MOD}._check_eod_source_batch_stock", return_value={})
	@patch(f"{_SYNC_MOD}._validate_eod_source_batch_stock")
	@patch(f"{_SYNC_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(
		f"{_SYNC_MOD}._preload_sre_warehouse_map",
		return_value={("M-1", "B1"): ["WH-SRE"]},
	)
	@patch(f"{_SYNC_MOD}.frappe.new_doc")
	@patch(f"{_SYNC_MOD}.frappe.get_doc")
	def test_draft_se_name_in_failures_and_logs_not_synced(
		self,
		mock_get_doc,
		mock_new_doc,
		_mock_sre_map,
		_mock_item_val,
		_mock_batch_val,
		_mock_check_batch,
		mock_mark_synced,
		mock_rollback,
		mock_release,
		mock_savepoint,
		mock_set_value,
	):
		class FakeSE:
			name = "SE-DRAFT-001"
			items = []
			flags = FD()
			stock_entry_type = None
			company = None
			manufacturing_order = None
			manufacturing_work_order = None
			manufacturing_operation = None
			manufacturer = None
			auto_created = 0

			def append(self, fn, v):
				self.items.append(FD(v))

			def save(self):
				pass

			def submit(self):
				raise frappe.ValidationError("Warehouse not allowed")

		mock_new_doc.return_value = FakeSE()
		mock_get_doc.return_value = FakeSE()

		mop_doc = FD(
			{
				"company": "Test Co",
				"manufacturer": "MF-1",
				"manufacturing_work_order": "MWO-1",
				"manufacturing_order": "MO-1",
				"department": "Dept",
				"loss_wt": 0,
			}
		)
		logs = [
			FD(
				{
					"name": "L1",
					"item_code": "M-1",
					"batch_no": "B1",
					"qty_after_transaction_batch_based": 2.0,
					"to_warehouse": "WH-DEPT",
					"from_warehouse": "WH-FROM",
					"flow_index": 1,
					"creation": "2026-01-01 10:00:00",
					"serial_no": None,
					"manufacturing_operation": "MOP-A",
					"manufacturing_work_order": "MWO-1",
				}
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
			"started_on": frappe.utils.now_datetime(),
		}

		_process_mwo_group(("Test Co", "MWO-1"), mop_data_list, failures, stats)

		# Submit failed → logs must NOT be marked synced
		mock_mark_synced.assert_not_called()
		# The draft SE name must appear in failures and stats
		submit_failure = next((f for f in failures if f.get("step") == "submit"), None)
		self.assertIsNotNone(submit_failure, "Expected a 'submit' failure entry")
		self.assertEqual(submit_failure.get("draft_se"), "SE-DRAFT-001")
		self.assertIn("SE-DRAFT-001", stats["draft_ses"])
		# Phase 2 savepoint was rolled back
		rollback_calls = [str(c) for c in mock_rollback.call_args_list]
		self.assertTrue(
			any("eod_submit_phase" in c for c in rollback_calls),
			f"Expected eod_submit_phase rollback; got: {rollback_calls}",
		)


class TestConsolidatedErrorLog(IntegrationTestCase):
	"""Only one Error Log is created for the full sync, not one per MWO."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SYNC_MOD}.release_eod_sync_lock")
	@patch(f"{_SYNC_MOD}.set_eod_sync_running")
	@patch(f"{_SYNC_MOD}._reconcile_reservations_for_mwo")
	@patch(f"{_SYNC_MOD}._plan_mwo_group")
	@patch(f"{_SYNC_MOD}._get_unsynced_mop_groups")
	@patch(f"{_SYNC_MOD}.frappe.log_error")
	def test_single_error_log_for_multiple_mwo_failures(
		self,
		mock_log_error,
		mock_get_groups,
		mock_plan,
		_mock_reconcile,
		_mock_set_running,
		_mock_release,
	):
		mock_log_error.return_value = FD({"name": "ERR-CONSOLIDATED-001"})
		mock_get_groups.return_value = {
			("Co", "MWO-A"): [],
			("Co", "MWO-B"): [],
		}

		# Both MWOs fail in planning → no consolidated SE, two failures, one error log.
		def _inject_failures(
			group_key,
			mop_data_list,
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		):
			_, mwo = group_key
			failures.append(
				{
					"step": "no_sre_warehouse",
					"mwo": mwo,
					"error_message": f"Simulated failure for {mwo}",
				}
			)
			stats["failed_mwos"] += 1
			return {
				"kind": "failed",
				"company": "Co",
				"manufacturer": "MF-1",
				"issues_rows": [],
			}

		mock_plan.side_effect = _inject_failures

		sync_mop_logs()

		# Exactly one Error Log must be created (the consolidated one)
		self.assertEqual(mock_log_error.call_count, 1)
		title_arg = (
			mock_log_error.call_args[1].get("title") or mock_log_error.call_args[0][0]
		)
		self.assertIn("MOP EOD Sync Failed", title_arg)


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


class TestGetUnsyncedMopGroupsByMwo(IntegrationTestCase):
	# New implementation fetches logs with get_all, then MOP metadata with a second
	# get_all (bulk fetch). Mock get_all to return different results per call.
	@classmethod
	def setUpClass(cls):
		pass

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


# ---------------------------------------------------------------------------
# TestFindLastOperation (4 cases)
# ---------------------------------------------------------------------------


class TestFindLastOperation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestGetLastLogsPerItemBatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestGetTWarehouseFromLogs(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestPreloadSreWarehouseMap(IntegrationTestCase):
	# The map value is now an ORDERED, de-duplicated LIST of candidate warehouses (active
	# SREs first); both the batch-keyed and qty-fallback queries run through frappe.db.sql.
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_batch_sre_rows_populate_map(self, mock_sql):
		mock_sql.side_effect = [
			[FrappeDict({"item_code": "M-1", "batch_no": "B1", "warehouse": "WH-SRE"})],
			[],  # qty-based fallback
		]

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result.get(("M-1", "B1")), ["WH-SRE"])

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_qty_based_fallback_populates_item_none_key(self, mock_sql):
		mock_sql.side_effect = [
			[],  # no batch-level SRE
			[FrappeDict({"item_code": "M-1", "warehouse": "WH-QTY"})],
		]

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result.get(("M-1", None)), ["WH-QTY"])

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_no_sre_returns_empty_map(self, mock_sql):
		mock_sql.side_effect = [[], []]

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result, {})

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.sql"
	)
	def test_multiple_sres_for_key_listed_active_first_and_deduped(self, mock_sql):
		# The SQL ORDER BY lists active SREs ahead of stale (Delivered/Cancelled) ones; the
		# function preserves that order and de-dups repeats. The stale warehouse is kept as
		# a low-priority candidate (the physical-stock check, not status, is the decider).
		mock_sql.side_effect = [
			[
				FrappeDict(
					{"item_code": "M-1", "batch_no": "B1", "warehouse": "WH-ACTIVE"}
				),
				FrappeDict(
					{"item_code": "M-1", "batch_no": "B1", "warehouse": "WH-STALE"}
				),
				FrappeDict(
					{"item_code": "M-1", "batch_no": "B1", "warehouse": "WH-ACTIVE"}
				),
			],
			[],
		]

		result = _preload_sre_warehouse_map("MWO-1")

		self.assertEqual(result.get(("M-1", "B1")), ["WH-ACTIVE", "WH-STALE"])


# ---------------------------------------------------------------------------
# TestBuildEodSeRows (5 cases — updated for new sre_map signature)
# ---------------------------------------------------------------------------


class TestBuildEodSeRows(IntegrationTestCase):
	# sre_map values are now candidate LISTS. With no physical stock anywhere (the default
	# for these fake item/batch names), source resolution falls back to the first candidate
	# — preserving the legacy row-building behaviour these cases assert.
	@classmethod
	def setUpClass(cls):
		pass

	def test_normal_case_builds_row(self):
		sre_map = {("M-1", "B1"): ["WH-SRE"]}
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
		sre_map = {("M-TEST", "B1"): ["WH-SAME"]}
		logs = [_log(qty_after_transaction_batch_based=2.0)]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-SAME", sre_map)
		self.assertEqual(rows, [])
		self.assertEqual(skipped, [])  # same-WH is a clean skip, not a missing SRE

	def test_zero_qty_log_skipped(self):
		sre_map = {("M-TEST", "B1"): ["WH-SRE"]}
		logs = [_log(qty_after_transaction_batch_based=0.0)]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(rows, [])
		self.assertEqual(skipped, [])

	def test_batch_no_included_in_row(self):
		sre_map = {("M-1", "BATCH-99"): ["WH-SRE"]}
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

	@patch(f"{_MOD}._eod_physical_batch_qty")
	def test_batch_already_at_target_is_noop(self, mock_phys):
		# Reproduces MOP-EOD-SYNC-2026-03647: the SRE-mapped source warehouse is stale
		# (0 physical) because the batch already moved to the target department warehouse.
		# Source resolves to the target -> clean no-op (no row, no skip; the MWO can sync).
		mock_phys.side_effect = lambda i, b, w: 5.0 if w == "WH-DEPT" else 0.0
		sre_map = {("M-1", "B1"): ["WH-STALE"]}
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=2.065,
				to_warehouse="WH-DEPT",
			)
		]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(rows, [])
		self.assertEqual(skipped, [])

	@patch(f"{_MOD}._eod_physical_batch_qty")
	def test_picks_warehouse_with_physical_stock(self, mock_phys):
		# Two candidate source warehouses; only the second physically holds the batch.
		mock_phys.side_effect = lambda i, b, w: 5.0 if w == "WH-B" else 0.0
		sre_map = {("M-1", "B1"): ["WH-A", "WH-B"]}
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=3.0,
				to_warehouse="WH-DEPT",
			)
		]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["s_warehouse"], "WH-B")
		self.assertEqual(skipped, [])

	@patch(f"{_MOD}._eod_physical_batch_qty", return_value=0.0)
	def test_missing_everywhere_builds_row_for_batch_short(self, _mock_phys):
		# Batch is missing at the target AND every candidate -> NOT a no-op. A row is still
		# built against the first candidate so the downstream batch-stock check reports a
		# real batch_short instead of silently burying the MOP Log as synced.
		sre_map = {("M-1", "B1"): ["WH-A"]}
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=2.0,
				to_warehouse="WH-DEPT",
			)
		]
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["s_warehouse"], "WH-A")
		self.assertEqual(skipped, [])


# ---------------------------------------------------------------------------
# TestMarkAllMwoMopLogsSynced (3 cases)
# ---------------------------------------------------------------------------


class TestMarkAllMwoMopLogsSynced(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestProcessMwoGroupHappyPath(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._check_eod_source_batch_stock",
		return_value={},
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
		_mock_check_batch,
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


class TestProcessMwoGroupMissingSreNotSynced(IntegrationTestCase):
	"""A row with no resolvable SRE warehouse must fail the MWO, never mark it synced.

	Regression for the silent data-loss bug: when every row is skipped for Missing
	SRE, ``items`` is empty — the old code marked the MWO's MOP Logs ``is_synced=1``
	and counted it as processed, burying them as done while nothing moved.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_all_mwo_mop_logs_synced"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._preload_sre_warehouse_map",
		return_value={},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value=None,
	)
	def test_missing_sre_fails_mwo_without_marking_synced(
		self, _mock_artifact, _mock_sre_map, mock_mark_synced, mock_new_doc
	):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=3.0,
				to_warehouse="WH-DEPT",
				creation="2026-01-01 10:00:00",
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures = []
		stats = {"processed_mwos": 0, "failed_mwos": 0, "submitted_ses": []}

		_process_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)

		# MWO counted as failed, never as synced/processed
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertEqual(stats["processed_mwos"], 0)
		# Logs left retryable — not marked synced
		mock_mark_synced.assert_not_called()
		# No Stock Entry created
		mock_new_doc.assert_not_called()
		# The skipped row was recorded as a failure for the consolidated error log
		self.assertEqual(len(failures), 1)
		self.assertEqual(failures[0]["step"], "no_sre_warehouse")


# ---------------------------------------------------------------------------
# TestSyncMopLogsEntryPoint (updated for new flow)
# ---------------------------------------------------------------------------


class TestSyncMopLogsEntryPoint(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._commit_company_main_se"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._plan_mwo_group"
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
		mock_plan,
		mock_commit,
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

		# MWO-A plans as resolvable (committed into one SE); MWO-B fails in planning.
		def _plan(
			group_key,
			mop_data_list,
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		):
			_, mwo = group_key
			if mwo == "MWO-A":
				return {
					"kind": "resolvable",
					"company": "Co",
					"manufacturer": "MF-1",
					"mwo": mwo,
					"items": [{"item_code": "M-1"}],
					"t_warehouse": "WH",
					"mop_data_list": mop_data_list,
					"last_mop_name": "MOP-A",
					"child_row_names": [],
				}
			failures.append(
				{"step": "no_sre_warehouse", "mwo": mwo, "error_message": "fail"}
			)
			stats["failed_mwos"] += 1
			return {
				"kind": "failed",
				"company": "Co",
				"manufacturer": "MF-1",
				"issues_rows": [],
			}

		def _commit(
			company,
			manufacturer,
			main_mwos,
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		):
			stats["submitted_ses"].append("SE-A")
			stats["processed_mwos"] += len(main_mwos)

		mock_plan.side_effect = _plan
		mock_commit.side_effect = _commit

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


class TestValidateEodSourceBatchStock(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
# TestSyncRange (window resolution: today default, custom From/To, flag-aware)
# ---------------------------------------------------------------------------


class TestSyncRange(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_today_range_is_start_and_end_of_today(self):
		start, end = _today_range()
		self.assertIn("00:00:00", start)
		self.assertIn("23:59:59", end)
		self.assertEqual(start[:10], end[:10])
		self.assertGreater(end, start)

	def test_resolve_run_range_defaults_to_today_when_unset(self):
		self.assertEqual(_resolve_run_range(None, None), _today_range())
		# A single bound is not enough to override; falls back to today.
		self.assertEqual(
			_resolve_run_range("2026-01-01 00:00:00", None), _today_range()
		)

	def test_resolve_run_range_uses_custom_window_when_both_set(self):
		rng = _resolve_run_range("2026-01-01 08:00:00", "2026-01-01 20:00:00")
		self.assertEqual(rng, ("2026-01-01 08:00:00", "2026-01-01 20:00:00"))

	def test_get_sync_range_reads_run_flag(self):
		frappe.flags.eod_sync_range = ("2026-02-02 00:00:00", "2026-02-02 23:59:59")
		try:
			self.assertEqual(
				_get_sync_range(), ("2026-02-02 00:00:00", "2026-02-02 23:59:59")
			)
		finally:
			frappe.flags.eod_sync_range = None
		# Without the flag it falls back to today.
		self.assertEqual(_get_sync_range(), _today_range())


# ---------------------------------------------------------------------------
# TestApplyMwoFilterRows (today-only and MWO filter logic)
# ---------------------------------------------------------------------------


class TestApplyMwoFilterRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestLockBypass(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestRecalculateSyncLogTotals(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestSyncMopLogsWithSyncLogName(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
# TestSyncMopLogsWindow (manual From/To window flows into the run flag)
# ---------------------------------------------------------------------------


class TestSyncMopLogsWindow(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run_capturing_window(self, **range_kwargs):
		"""Run sync_mop_logs with the heavy deps mocked, capturing the window flag
		that _get_unsynced_mop_groups would see at call time. Returns that window."""
		captured = {}

		def _capture(*_a, **_k):
			captured["range"] = getattr(frappe.flags, "eod_sync_range", None)
			return {}

		patches = [
			patch(f"{_MOD}.release_eod_sync_lock"),
			patch(f"{_MOD}.set_eod_sync_running"),
			patch(f"{_MOD}._reconcile_reservations_for_mwo"),
			patch(f"{_MOD}._process_mwo_group"),
			patch(
				f"{_MOD}.frappe.get_doc",
				return_value=FrappeDict({"eod_sync_work_order_filter": []}),
			),
			patch(f"{_MOD}.recalculate_sync_log_totals"),
			patch(f"{_MOD}.frappe.db.set_value"),
			patch(f"{_MOD}._get_unsynced_mop_groups", side_effect=_capture),
		]
		for p in patches:
			p.start()
		try:
			sync_mop_logs(sync_log_name="SYNC-LOG-WIN", **range_kwargs)
		finally:
			for p in patches:
				p.stop()
		return captured.get("range")

	def test_custom_window_flows_into_run_flag(self):
		window = self._run_capturing_window(
			from_datetime="2026-01-01 08:00:00", to_datetime="2026-01-01 20:00:00"
		)
		self.assertEqual(window, ("2026-01-01 08:00:00", "2026-01-01 20:00:00"))
		# Cleared in finally after the run.
		self.assertIsNone(getattr(frappe.flags, "eod_sync_range", None))

	def test_no_args_defaults_to_today_window(self):
		window = self._run_capturing_window()
		self.assertEqual(window, _today_range())
		self.assertIsNone(getattr(frappe.flags, "eod_sync_range", None))


# ---------------------------------------------------------------------------
# TestSreRelocation (release reservation at source, re-reserve at target)
# ---------------------------------------------------------------------------


class TestSreRelocation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


# ---------------------------------------------------------------------------
# TestReserveFromEodSeRows (build SO-anchored SREs from the submitted SE's rows)
# ---------------------------------------------------------------------------


class TestReserveFromEodSeRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _reserve(
		self,
		rows,
		*,
		avail=10.0,
		batch_free=None,
		so_reserved=0.0,
		base_mr=None,
		sales_order="SO-1",
		sales_order_item="SOI-1",
		mo="MO-1",
		mfr="MF-1",
		has_batch_no=0,
		has_serial_no=0,
		stock_uom="Nos",
	):
		"""Run _reserve_sres_from_eod_se_rows with every external mocked.

		Returns (created_names, sre_docs); sre_docs is one MagicMock per frappe.new_doc
		call (i.e. per SRE actually built).
		"""
		sre_docs = []

		def _new_doc(_doctype):
			m = MagicMock()
			sre_docs.append(m)
			return m

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return (sales_order, sales_order_item, mfr)
			if doctype == "Item":
				return (has_batch_no, has_serial_no, stock_uom)
			raise AssertionError(f"unexpected get_cached_value doctype: {doctype}")

		ep = "erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry."
		with patch(f"{_MOD}.frappe.new_doc", side_effect=_new_doc), patch(
			f"{_MOD}.frappe.get_cached_value", side_effect=_cached
		), patch(f"{_MOD}.frappe.db.get_value", return_value=(mo, mfr)), patch(
			f"{_MOD}._eod_base_mr_voucher_qty", return_value=base_mr
		), patch(ep + "get_available_qty_to_reserve", return_value=avail), patch(
			# Batched rows resolve free qty via the SBB-aware helper (ERPNext's
			# batch-keyed availability reads 0 under v16); mirror `avail` unless the
			# test sets batch_free to diverge them.
			f"{_MOD}._free_batch_qty_to_reserve",
			return_value=(avail if batch_free is None else batch_free),
		), patch(
			ep + "get_sre_reserved_qty_for_voucher_detail_no", return_value=so_reserved
		):
			created = _reserve_sres_from_eod_se_rows("Co", rows)
		return created, sre_docs

	def test_empty_items_returns_empty(self):
		self.assertEqual(_reserve_sres_from_eod_se_rows("Co", []), [])

	def test_skips_row_without_mwo(self):
		rows = [{"item_code": "M-1", "t_warehouse": "WH-D1", "qty": 1.0}]
		_created, sres = self._reserve(rows)
		self.assertEqual(sres, [])

	def test_qty_item_so_anchored_at_target(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 5.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		_created, sres = self._reserve(rows, avail=10.0)
		self.assertEqual(len(sres), 1)
		sre = sres[0]
		# Anchored to the MWO's Sales Order, NOT the Stock Entry.
		self.assertEqual(sre.voucher_type, "Sales Order")
		self.assertEqual(sre.voucher_no, "SO-1")
		self.assertEqual(sre.voucher_detail_no, "SOI-1")
		# Reserved at the row's target warehouse, against the row's MWO + last operation.
		self.assertEqual(sre.warehouse, "WH-DEPT")
		self.assertEqual(sre.reserved_qty, 5.0)
		self.assertEqual(sre.manufacturing_work_order, "MWO-1")
		self.assertEqual(sre.manufacturing_operation, "MOP-LAST")
		self.assertEqual(sre.reservation_based_on, "Qty")
		sre.insert.assert_called_once()
		sre.submit.assert_called_once()

	def test_batch_item_builds_sb_entry_at_target(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "D-1",
				"t_warehouse": "WH-DEPT",
				"qty": 2.0,
				"batch_no": "B1",
				"manufacturing_operation": "MOP-LAST",
			}
		]
		_created, sres = self._reserve(
			rows, avail=10.0, has_batch_no=1, stock_uom="Carat"
		)
		sre = sres[0]
		self.assertEqual(sre.reservation_based_on, "Serial and Batch")
		sb_calls = [
			c for c in sre.append.call_args_list if c.args and c.args[0] == "sb_entries"
		]
		self.assertEqual(len(sb_calls), 1)
		sb_row = sb_calls[0].args[1]
		self.assertEqual(sb_row["warehouse"], "WH-DEPT")
		self.assertEqual(sb_row["batch_no"], "B1")
		self.assertEqual(sb_row["qty"], 2.0)

	def test_batch_uses_sbb_free_qty_not_erpnext_zero(self):
		"""Regression: under v16 ERPNext's batch-keyed availability reads 0, which used
		to silently skip every batched WIP row (no SRE re-created at the new operation,
		breaking Employee IR Process Loss). The SBB-aware free qty must drive reservation.
		"""
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 5.0,
				"batch_no": "B1",
				"manufacturing_operation": "MOP-LAST",
			}
		]
		# ERPNext availability = 0 (the v16 quirk) but SBB-aware free qty = 5.0.
		_created, sres = self._reserve(
			rows, avail=0.0, batch_free=5.0, has_batch_no=1, stock_uom="Gram"
		)
		self.assertEqual(len(sres), 1)
		self.assertEqual(sres[0].reserved_qty, 5.0)
		self.assertEqual(sres[0].reservation_based_on, "Serial and Batch")
		sres[0].submit.assert_called_once()

	def test_clamps_reserved_to_available(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 5.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		# Only 2.0 free at the target though 5.0 moved → reserve 2.0, never throw.
		_created, sres = self._reserve(rows, avail=2.0)
		self.assertEqual(sres[0].reserved_qty, 2.0)
		sres[0].submit.assert_called_once()

	def test_skips_when_nothing_free(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 5.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		created, sres = self._reserve(rows, avail=0.0)
		# Nothing free → no SRE built (no frappe.new_doc), no throw.
		self.assertEqual(sres, [])
		self.assertEqual(created, [])

	def test_voucher_qty_uses_floor_when_no_mr(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 3.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		# No MR data → voucher_qty = total_so_reserved(8.0) + reserved(3.0) = 11.0
		_created, sres = self._reserve(rows, avail=10.0, so_reserved=8.0, base_mr=None)
		self.assertEqual(sres[0].reserved_qty, 3.0)
		self.assertEqual(sres[0].voucher_qty, 11.0)

	def test_voucher_qty_uses_max_with_mr(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 3.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		# MR base 5010.0 dominates the floor 11.0 → voucher_qty = 5010.0
		_created, sres = self._reserve(
			rows, avail=10.0, so_reserved=8.0, base_mr=5010.0
		)
		self.assertEqual(sres[0].voucher_qty, 5010.0)

	def test_skips_row_without_sales_order(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 3.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		# MWO's MO has no Sales Order → skip (stock moved, but no malformed reservation).
		_created, sres = self._reserve(rows, sales_order=None)
		self.assertEqual(sres, [])

	def test_per_row_mwo_and_operation(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-D1",
				"qty": 1.0,
				"manufacturing_operation": "MOP-1",
			},
			{
				"custom_manufacturing_work_order": "MWO-2",
				"item_code": "M-2",
				"t_warehouse": "WH-D2",
				"qty": 1.0,
				"manufacturing_operation": "MOP-2",
			},
		]
		_created, sres = self._reserve(rows, avail=10.0)
		self.assertEqual(len(sres), 2)
		# Each SRE carries its OWN row's MWO/operation/target (not a shared header).
		self.assertEqual(sres[0].manufacturing_work_order, "MWO-1")
		self.assertEqual(sres[0].manufacturing_operation, "MOP-1")
		self.assertEqual(sres[0].warehouse, "WH-D1")
		self.assertEqual(sres[1].manufacturing_work_order, "MWO-2")
		self.assertEqual(sres[1].manufacturing_operation, "MOP-2")
		self.assertEqual(sres[1].warehouse, "WH-D2")

	def test_serial_tracked_qty_reservation(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "S-1",
				"t_warehouse": "WH-DEPT",
				"qty": 2.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		_created, sres = self._reserve(rows, avail=10.0, has_serial_no=1)
		sre = sres[0]
		self.assertEqual(sre.reservation_based_on, "Qty")
		self.assertEqual(sre.has_serial_no, 1)
		# No sb_entries appended for a non-batch (serial) item.
		sb_calls = [
			c for c in sre.append.call_args_list if c.args and c.args[0] == "sb_entries"
		]
		self.assertEqual(sb_calls, [])

	def test_two_rows_same_mwo_make_two_sres(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 1.0,
				"manufacturing_operation": "MOP-LAST",
			},
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-2",
				"t_warehouse": "WH-DEPT",
				"qty": 1.0,
				"manufacturing_operation": "MOP-LAST",
			},
		]
		_created, sres = self._reserve(rows, avail=10.0)
		self.assertEqual(len(sres), 2)
		self.assertTrue(all(s.manufacturing_work_order == "MWO-1" for s in sres))

	def test_available_exactly_equals_qty(self):
		rows = [
			{
				"custom_manufacturing_work_order": "MWO-1",
				"item_code": "M-1",
				"t_warehouse": "WH-DEPT",
				"qty": 5.0,
				"manufacturing_operation": "MOP-LAST",
			}
		]
		_created, sres = self._reserve(rows, avail=5.0)
		self.assertEqual(sres[0].reserved_qty, 5.0)


# ---------------------------------------------------------------------------
# TestHardDeleteCancelledSnapshots (hard-delete this run's cancelled source SREs)
# ---------------------------------------------------------------------------


class TestHardDeleteCancelledSnapshots(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.delete_doc")
	@patch(f"{_MOD}.frappe.db.exists", return_value=True)
	def test_deletes_each_snapshot_with_force(self, _mock_exists, mock_delete):
		snaps = [
			{"sre": FrappeDict({"name": "SRE-1"})},
			{"sre": FrappeDict({"name": "SRE-2"})},
		]
		_hard_delete_cancelled_snapshots(snaps)
		self.assertEqual(mock_delete.call_count, 2)
		mock_delete.assert_any_call(
			"Stock Reservation Entry", "SRE-1", force=1, ignore_permissions=True
		)
		mock_delete.assert_any_call(
			"Stock Reservation Entry", "SRE-2", force=1, ignore_permissions=True
		)

	@patch(f"{_MOD}.frappe.delete_doc")
	@patch(f"{_MOD}.frappe.db.exists", return_value=False)
	def test_skips_when_sre_absent(self, _mock_exists, mock_delete):
		_hard_delete_cancelled_snapshots([{"sre": FrappeDict({"name": "SRE-1"})}])
		mock_delete.assert_not_called()

	@patch(f"{_MOD}.frappe.delete_doc", side_effect=RuntimeError("locked"))
	@patch(f"{_MOD}.frappe.db.exists", return_value=True)
	def test_delete_error_propagates(self, _mock_exists, _mock_delete):
		# Atomic contract: a delete failure must bubble up so the bucket rolls back.
		with self.assertRaises(RuntimeError):
			_hard_delete_cancelled_snapshots([{"sre": FrappeDict({"name": "SRE-1"})}])

	def test_empty_or_none_is_noop(self):
		_hard_delete_cancelled_snapshots([])
		_hard_delete_cancelled_snapshots(None)


# ---------------------------------------------------------------------------
# TestTargetWarehouseFallback (resolve t_warehouse when last op carries none)
# ---------------------------------------------------------------------------


class TestTargetWarehouseFallback(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestMwoRealizedByArtifact(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestProcessMwoGroupArtifactSkip(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestMarkAllMwoMopLogsSyncedSelective(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestGetUnsyncedMopGroupsSelective(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


# ---------------------------------------------------------------------------
# TestEodSeRowPcs (pcs from MOP Log for D/G; metal keeps default)
# ---------------------------------------------------------------------------


class TestEodSeRowPcs(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_diamond_carries_log_pcs_metal_keeps_default(self):
		d_log = _log(
			item_code="D-1",
			batch_no="BD",
			qty_after_transaction_batch_based=0.5,
			pcs_after_transaction_batch_based=7,
			to_warehouse="WH-DEPT",
		)
		g_log = _log(
			item_code="G-1",
			batch_no="BG",
			qty_after_transaction_batch_based=0.2,
			pcs_after_transaction_batch_based=3,
			to_warehouse="WH-DEPT",
		)
		m_log = _log(
			item_code="M-1",
			batch_no="BM",
			qty_after_transaction_batch_based=2.0,
			pcs_after_transaction_batch_based=99,
			to_warehouse="WH-DEPT",
		)
		sre_map = {
			("D-1", "BD"): ["WH-SRE"],
			("G-1", "BG"): ["WH-SRE"],
			("M-1", "BM"): ["WH-SRE"],
		}
		rows, skipped = _build_eod_se_rows(
			"MWO-1", "MOP-A", [d_log, g_log, m_log], "WH-DEPT", sre_map
		)
		self.assertEqual(skipped, [])
		by_item = {r["item_code"]: r for r in rows}
		# Diamond/Gemstone carry the real stone count from the MOP Log balance.
		self.assertEqual(by_item["D-1"]["pcs"], 7)
		self.assertEqual(by_item["G-1"]["pcs"], 3)
		# Metal row leaves pcs unset → Stock Entry Detail default of "1" applies.
		self.assertNotIn("pcs", by_item["M-1"])


# ---------------------------------------------------------------------------
# TestEodBaseMrVoucherQty (MR total inflated by Manufacturing Setting tolerance)
# ---------------------------------------------------------------------------


class TestEodBaseMrVoucherQty(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.get_value", return_value=50000)
	@patch(f"{_MOD}.frappe.db.sql", return_value=[[10.0]])
	def test_inflates_mr_total_by_tolerance(self, _mock_sql, _mock_gv):
		# base 10 + 10 * (50000 / 100) = 5010
		self.assertAlmostEqual(_eod_base_mr_voucher_qty("MO-1", "MF-1"), 5010.0)

	@patch(f"{_MOD}.frappe.db.get_value", return_value=None)
	@patch(f"{_MOD}.frappe.db.sql", return_value=[[12.0]])
	def test_no_tolerance_returns_plain_mr_total(self, _mock_sql, _mock_gv):
		self.assertAlmostEqual(_eod_base_mr_voucher_qty("MO-1", "MF-1"), 12.0)

	def test_none_mo_returns_none(self):
		self.assertIsNone(_eod_base_mr_voucher_qty(None, "MF-1"))

	@patch(f"{_MOD}.frappe.db.sql", return_value=[[None]])
	def test_no_mr_rows_returns_none(self, _mock_sql):
		self.assertIsNone(_eod_base_mr_voucher_qty("MO-1", "MF-1"))


# ---------------------------------------------------------------------------
# TestEodConsolidation (many MWOs → ONE submitted Stock Entry per bucket)
# ---------------------------------------------------------------------------


class TestEodConsolidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}._stamp_last_eod_sync")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(f"{_MOD}._hard_delete_cancelled_snapshots")
	@patch(f"{_MOD}._reserve_sres_from_eod_se_rows")
	@patch(f"{_MOD}._snapshot_mwo_sres_for_relocation", return_value=[])
	@patch(f"{_MOD}.frappe.get_doc")
	@patch(f"{_MOD}.frappe.new_doc")
	def test_two_mwos_consolidated_into_one_submitted_se(
		self,
		mock_new_doc,
		mock_get_doc,
		_mock_snap,
		mock_reserve,
		mock_delete,
		mock_mark,
		_mock_stamp,
		_mock_savepoint,
		_mock_release,
		_mock_set_value,
	):
		se = FakeStockEntry("SE-CONS-1")
		mock_new_doc.return_value = se
		mock_get_doc.return_value = se

		main_mwos = [
			{
				"kind": "resolvable",
				"company": "Co",
				"manufacturer": "MF-1",
				"mwo": "MWO-1",
				"items": [
					{
						"item_code": "M-1",
						"qty": 1.0,
						"s_warehouse": "WH-SRE",
						"t_warehouse": "WH-D1",
					}
				],
				"t_warehouse": "WH-D1",
				"mop_data_list": [{"mop_name": "MOP-1"}],
				"last_mop_name": "MOP-1",
				"child_row_names": [],
			},
			{
				"kind": "resolvable",
				"company": "Co",
				"manufacturer": "MF-1",
				"mwo": "MWO-2",
				"items": [
					{
						"item_code": "M-2",
						"qty": 2.0,
						"s_warehouse": "WH-SRE",
						"t_warehouse": "WH-D2",
					}
				],
				"t_warehouse": "WH-D2",
				"mop_data_list": [{"mop_name": "MOP-2"}],
				"last_mop_name": "MOP-2",
				"child_row_names": [],
			},
		]
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}

		_commit_company_main_se(
			"Co",
			"MF-1",
			main_mwos,
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		)

		# Exactly ONE Stock Entry is created and submitted for both MWOs.
		mock_new_doc.assert_called_once()
		self.assertTrue(se.submitted)
		self.assertEqual(stats["submitted_ses"], ["SE-CONS-1"])
		self.assertEqual(stats["processed_mwos"], 2)
		self.assertEqual(stats["failed_mwos"], 0)
		self.assertEqual(failures, [])
		# Both MWOs' rows live on the single SE.
		self.assertEqual(len(se.items), 2)
		# The new reservation is built ONCE from the submitted SE's rows (union of MWOs).
		mock_reserve.assert_called_once()
		self.assertEqual(len(mock_reserve.call_args.args[1]), 2)
		# The cancelled source SREs are hard-deleted, once per MWO.
		self.assertEqual(mock_delete.call_count, 2)
		# Each MWO's MOP Logs are marked synced (once per MWO).
		self.assertEqual(mock_mark.call_count, 2)
		# Consolidated header carries no single MWO; rows carry their own.
		self.assertIsNone(se.manufacturing_work_order)
		self.assertEqual(se.manufacturer, "MF-1")

	@patch(f"{_MOD}._check_eod_source_batch_stock", return_value={})
	@patch(f"{_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(
		f"{_MOD}._preload_sre_warehouse_map", return_value={("M-1", "B1"): ["WH-SRE"]}
	)
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_plan_returns_resolvable_for_clean_mwo(self, _art, _sre, _item_val, _check):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				qty_after_transaction_batch_based=3.0,
				to_warehouse="WH-DEPT",
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures = []
		stats = {"processed_mwos": 0, "failed_mwos": 0, "submitted_ses": []}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertEqual(result["kind"], "resolvable")
		self.assertEqual(result["company"], "Test Co")
		self.assertEqual(len(result["items"]), 1)
		self.assertEqual(result["items"][0]["s_warehouse"], "WH-SRE")
		self.assertEqual(failures, [])

	def _single_resolvable_mwo(self):
		return [
			{
				"kind": "resolvable",
				"company": "Co",
				"manufacturer": "MF-1",
				"mwo": "MWO-1",
				"items": [
					{
						"item_code": "M-1",
						"qty": 1.0,
						"s_warehouse": "WH-SRE",
						"t_warehouse": "WH-D1",
						"custom_manufacturing_work_order": "MWO-1",
						"manufacturing_operation": "MOP-1",
					}
				],
				"t_warehouse": "WH-D1",
				"mop_data_list": [{"mop_name": "MOP-1"}],
				"last_mop_name": "MOP-1",
				"child_row_names": [],
			}
		]

	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}._stamp_last_eod_sync")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(f"{_MOD}._hard_delete_cancelled_snapshots")
	@patch(
		f"{_MOD}._reserve_sres_from_eod_se_rows",
		side_effect=RuntimeError("reserve failed"),
	)
	@patch(f"{_MOD}._snapshot_mwo_sres_for_relocation", return_value=[])
	@patch(f"{_MOD}.frappe.get_doc")
	@patch(f"{_MOD}.frappe.new_doc")
	def test_rollback_on_reserve_failure_preserves_cancelled_sres(
		self,
		mock_new_doc,
		mock_get_doc,
		_mock_snap,
		_mock_reserve,
		mock_delete,
		mock_mark,
		_mock_stamp,
		_mock_savepoint,
		_mock_release,
		mock_rollback,
		_mock_set_value,
	):
		se = FakeStockEntry("SE-DRAFT-RB")
		mock_new_doc.return_value = se
		mock_get_doc.return_value = se
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}
		_commit_company_main_se(
			"Co",
			"MF-1",
			self._single_resolvable_mwo(),
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		)
		# Phase 2 rolled back: reservation failed, so submit + SRE cancels are undone.
		mock_rollback.assert_any_call(save_point="eod_submit_phase")
		# On failure the cancelled SREs are NOT deleted and logs are NOT marked synced.
		mock_delete.assert_not_called()
		mock_mark.assert_not_called()
		# Draft SE survives for manual recovery; the MWO is held as failed.
		self.assertIn("SE-DRAFT-RB", stats["draft_ses"])
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertEqual(stats["submitted_ses"], [])

	@patch(f"{_MOD}.frappe.db.set_value")
	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}._stamp_last_eod_sync")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(
		f"{_MOD}._hard_delete_cancelled_snapshots",
		side_effect=RuntimeError("delete failed"),
	)
	@patch(f"{_MOD}._reserve_sres_from_eod_se_rows")
	@patch(f"{_MOD}._snapshot_mwo_sres_for_relocation", return_value=[])
	@patch(f"{_MOD}.frappe.get_doc")
	@patch(f"{_MOD}.frappe.new_doc")
	def test_rollback_on_delete_failure_preserves_cancelled_sres(
		self,
		mock_new_doc,
		mock_get_doc,
		_mock_snap,
		_mock_reserve,
		_mock_delete,
		mock_mark,
		_mock_stamp,
		_mock_savepoint,
		_mock_release,
		mock_rollback,
		_mock_set_value,
	):
		se = FakeStockEntry("SE-DRAFT-DB")
		mock_new_doc.return_value = se
		mock_get_doc.return_value = se
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}
		_commit_company_main_se(
			"Co",
			"MF-1",
			self._single_resolvable_mwo(),
			failures,
			stats,
			sync_log_name=None,
			selective=False,
		)
		# A delete failure also rolls the whole bucket back (cancelled SREs restored).
		mock_rollback.assert_any_call(save_point="eod_submit_phase")
		mock_mark.assert_not_called()
		self.assertIn("SE-DRAFT-DB", stats["draft_ses"])
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertEqual(stats["submitted_ses"], [])


# ---------------------------------------------------------------------------
# TestSyncLogItemSelectValues (guard: every Select value EOD writes is valid)
# ---------------------------------------------------------------------------


class TestSyncLogItemSelectValues(IntegrationTestCase):
	"""Regression for the held-row crash: ``sync_stage="Plan"`` / ``error_type="MWO
	Held"`` were not valid Select options, so _insert_sync_log_item raised and aborted
	the whole run. Insert a row for every (sync_stage, error_type, status) combo EOD
	writes and assert none raise a Select ValidationError."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_all_eod_select_combos_insert_without_error(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_insert_sync_log_item,
		)

		log = frappe.new_doc("MOP EOD Sync Log")
		log.status = "Queued"
		log.trigger_type = "Manual"
		log.posting_date = frappe.utils.nowdate()
		log.mop_settings = "MOP Settings"
		log.flags.ignore_permissions = True
		log.insert(ignore_permissions=True)

		combos = [
			("Resolve SRE Warehouse", "Missing SRE", "Failed"),
			("Resolve SRE Warehouse", "Missing Target Warehouse", "Failed"),
			("Build Stock Entry Row", "Validation Failed", "Failed"),
			("Save Draft Stock Entry", "", "Pending"),
			("Save Draft Stock Entry", "Stock Entry Save Failed", "Failed"),
			("Submit Stock Entry", "Stock Entry Submit Failed", "Draft Created"),
			("Completed", "", "Synced"),
		]
		for stage, etype, status in combos:
			_insert_sync_log_item(
				log.name,
				{
					"manufacturing_work_order": "MWO-X",
					"item_code": "M-1",
					"qty": 1.0,
					"status": status,
					"sync_stage": stage,
					"error_type": etype,
				},
			)

		count = frappe.db.count("MOP EOD Sync Log Item", {"parent": log.name})
		self.assertEqual(count, len(combos))


# ---------------------------------------------------------------------------
# TestValidateEodItemsForMwoReservation (batch/serial presence guard)
# ---------------------------------------------------------------------------


class TestValidateEodItemsForMwoReservation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.get_all", return_value=[])
	def test_empty_items_no_throw(self, _mock):
		_validate_eod_items_for_mwo_reservation([])

	@patch(f"{_MOD}.frappe.db.get_all", return_value=[])
	def test_item_not_found_throws(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_items_for_mwo_reservation([{"item_code": "X", "qty": 1.0}])

	@patch(f"{_MOD}.frappe.db.get_all")
	def test_batch_tracked_missing_batch_throws(self, mock_get_all):
		mock_get_all.return_value = [
			FrappeDict({"name": "D-1", "has_batch_no": 1, "has_serial_no": 0})
		]
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_items_for_mwo_reservation(
				[{"item_code": "D-1", "qty": 1.0, "batch_no": None}]
			)

	@patch(f"{_MOD}.frappe.db.get_all")
	def test_serialized_missing_serial_throws(self, mock_get_all):
		mock_get_all.return_value = [
			FrappeDict({"name": "S-1", "has_batch_no": 0, "has_serial_no": 1})
		]
		with self.assertRaises(frappe.ValidationError):
			_validate_eod_items_for_mwo_reservation(
				[{"item_code": "S-1", "qty": 1.0, "serial_no": None}]
			)

	@patch(f"{_MOD}.frappe.db.get_all")
	def test_batch_and_serial_present_no_throw(self, mock_get_all):
		mock_get_all.return_value = [
			FrappeDict({"name": "D-1", "has_batch_no": 1, "has_serial_no": 1})
		]
		_validate_eod_items_for_mwo_reservation(
			[{"item_code": "D-1", "qty": 1.0, "batch_no": "B1", "serial_no": "SN1"}]
		)

	@patch(f"{_MOD}.frappe.db.get_all")
	def test_zero_qty_row_skipped(self, mock_get_all):
		mock_get_all.return_value = [
			FrappeDict({"name": "D-1", "has_batch_no": 1, "has_serial_no": 0})
		]
		# qty<=0 row is skipped, so the missing batch_no does not throw.
		_validate_eod_items_for_mwo_reservation(
			[{"item_code": "D-1", "qty": 0.0, "batch_no": None}]
		)


# ---------------------------------------------------------------------------
# TestCheckEodSourceBatchStock (non-throwing physical batch-stock check)
# ---------------------------------------------------------------------------


class TestCheckEodSourceBatchStock(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=10.0)
	def test_sufficient_stock_returns_empty(self, _mock):
		items = [{"s_warehouse": "W", "item_code": "I", "batch_no": "B", "qty": 5.0}]
		self.assertEqual(_check_eod_source_batch_stock(items), {})

	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=1.0)
	def test_short_stock_reported(self, _mock):
		items = [{"s_warehouse": "W", "item_code": "I", "batch_no": "B", "qty": 5.0}]
		self.assertEqual(
			_check_eod_source_batch_stock(items), {("W", "I", "B"): (5.0, 1.0)}
		)

	@patch(
		"erpnext.stock.doctype.batch.batch.get_batch_qty",
		side_effect=Exception("ledger error"),
	)
	def test_get_batch_qty_exception_treated_as_zero(self, _mock):
		items = [{"s_warehouse": "W", "item_code": "I", "batch_no": "B", "qty": 5.0}]
		self.assertEqual(
			_check_eod_source_batch_stock(items), {("W", "I", "B"): (5.0, 0.0)}
		)

	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=5.0)
	def test_aggregates_rows_with_same_key(self, _mock):
		items = [
			{"s_warehouse": "W", "item_code": "I", "batch_no": "B", "qty": 3.0},
			{"s_warehouse": "W", "item_code": "I", "batch_no": "B", "qty": 4.0},
		]
		# needed 7.0 > physical 5.0
		self.assertEqual(
			_check_eod_source_batch_stock(items), {("W", "I", "B"): (7.0, 5.0)}
		)

	@patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=0.0)
	def test_rows_without_batch_or_qty_skipped(self, mock_gbq):
		items = [
			{"s_warehouse": "W", "item_code": "I", "batch_no": None, "qty": 5.0},
			{"s_warehouse": "W", "item_code": "I", "batch_no": "B", "qty": 0.0},
		]
		self.assertEqual(_check_eod_source_batch_stock(items), {})
		mock_gbq.assert_not_called()


# ---------------------------------------------------------------------------
# TestCommitCompanyIssuesSe (best-effort DRAFT issues SE for failed MWOs)
# ---------------------------------------------------------------------------


class TestCommitCompanyIssuesSe(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}._save_draft_eod_se")
	def test_empty_rows_early_return(self, mock_save):
		stats = {"draft_ses": []}
		_commit_company_issues_se("Co", "MF-1", [], stats)
		mock_save.assert_not_called()
		self.assertEqual(stats["draft_ses"], [])

	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}._save_draft_eod_se", return_value="SE-ISSUES-1")
	def test_rows_saved_as_unresolved_draft(self, mock_save, _sp, _rel):
		stats = {"draft_ses": []}
		_commit_company_issues_se(
			"Co",
			"MF-1",
			[{"item_code": "M-1", "qty": 1.0}],
			stats,
			sync_log_name="LOG-1",
		)
		mock_save.assert_called_once()
		self.assertEqual(
			mock_save.call_args.kwargs["eod_sync_source"], "MOP EOD Sync (Unresolved)"
		)
		self.assertEqual(stats["draft_ses"], ["SE-ISSUES-1"])

	@patch(f"{_MOD}.frappe.logger")
	@patch(f"{_MOD}.frappe.db.rollback")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}._save_draft_eod_se", side_effect=RuntimeError("save failed"))
	def test_save_failure_rolls_back_and_swallows(
		self, _save, _sp, mock_rollback, _logger
	):
		stats = {"draft_ses": []}
		# Best-effort: must NOT raise even when the draft save fails.
		_commit_company_issues_se(
			"Co", "MF-1", [{"item_code": "M-1", "qty": 1.0}], stats
		)
		mock_rollback.assert_any_call(save_point="eod_issues_phase")
		self.assertEqual(stats["draft_ses"], [])


# ---------------------------------------------------------------------------
# TestSaveDraftEodSe (header + audit fields, draft-only)
# ---------------------------------------------------------------------------


class TestSaveDraftEodSe(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.new_doc")
	def test_sets_header_and_audit_fields(self, mock_new_doc):
		se = FakeStockEntry("SE-DRAFT-1")
		mock_new_doc.return_value = se
		name = _save_draft_eod_se(
			"Co",
			None,
			None,
			[{"item_code": "M-1", "qty": 1.0}],
			header_manufacturer="MF-1",
			sync_log_name="LOG-1",
			eod_sync_source="MOP EOD Sync (Unresolved)",
		)
		self.assertEqual(name, "SE-DRAFT-1")
		self.assertEqual(se.stock_entry_type, "Material Transfer to Department")
		self.assertEqual(se.auto_created, 1)
		self.assertEqual(se.custom_is_eod_sync_stock_entry, 1)
		self.assertEqual(se.custom_eod_sync_source, "MOP EOD Sync (Unresolved)")
		self.assertEqual(se.custom_eod_sync_log, "LOG-1")
		self.assertEqual(se.manufacturer, "MF-1")
		self.assertEqual(len(se.items), 1)
		# Draft only — saved, never submitted.
		self.assertTrue(se.saved)
		self.assertFalse(se.submitted)

	@patch(f"{_MOD}.frappe.new_doc")
	def test_header_mop_name_and_mwo_applied(self, mock_new_doc):
		se = FakeStockEntry("SE-DRAFT-2")
		mock_new_doc.return_value = se
		_save_draft_eod_se(
			"Co",
			"MWO-1",
			"MO-1",
			[{"item_code": "M-1", "qty": 1.0}],
			header_mop_name="MOP-X",
		)
		self.assertEqual(se.manufacturing_operation, "MOP-X")
		self.assertEqual(se.manufacturing_work_order, "MWO-1")
		self.assertEqual(se.manufacturing_order, "MO-1")

	@patch(f"{_MOD}.frappe.new_doc")
	def test_no_sync_log_leaves_log_field_unset(self, mock_new_doc):
		se = FakeStockEntry("SE-DRAFT-3")
		mock_new_doc.return_value = se
		_save_draft_eod_se("Co", None, None, [{"item_code": "M-1", "qty": 1.0}])
		self.assertIsNone(getattr(se, "custom_eod_sync_log", None))


# ---------------------------------------------------------------------------
# TestMopManufacturerLabel / TestResolveEodManufacturerLabel / TestCollectMopNames
# ---------------------------------------------------------------------------


class TestMopManufacturerLabel(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_none_returns_none(self):
		self.assertIsNone(_mop_manufacturer_label(None))

	def test_dict_returns_manufacturer(self):
		self.assertEqual(_mop_manufacturer_label({"manufacturer": "MF-1"}), "MF-1")

	def test_object_returns_attribute(self):
		self.assertEqual(
			_mop_manufacturer_label(SimpleNamespace(manufacturer="MF-2")), "MF-2"
		)

	def test_missing_manufacturer_returns_none(self):
		self.assertIsNone(_mop_manufacturer_label({}))


class TestResolveEodManufacturerLabel(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}.frappe.db.get_value", return_value="MF-Q")
	def test_empty_list_falls_back_to_mwo(self, mock_gv):
		self.assertEqual(_resolve_eod_manufacturer_label([], "MWO-1"), "MF-Q")
		mock_gv.assert_called_once()

	def test_multiple_distinct_joined_sorted(self):
		mop_data_list = [
			{"mop_doc": _mop_doc(manufacturer="B")},
			{"mop_doc": _mop_doc(manufacturer="A")},
		]
		self.assertEqual(
			_resolve_eod_manufacturer_label(mop_data_list, "MWO-1"), "A, B"
		)

	def test_none_mop_doc_skipped(self):
		mop_data_list = [
			{"mop_doc": None},
			{"mop_doc": _mop_doc(manufacturer="A")},
		]
		self.assertEqual(_resolve_eod_manufacturer_label(mop_data_list, "MWO-1"), "A")

	@patch(f"{_MOD}.frappe.db.get_value", return_value="MF-Q")
	def test_no_manufacturers_falls_back_to_mwo(self, _mock):
		mop_data_list = [{"mop_doc": _mop_doc(manufacturer=None)}]
		self.assertEqual(
			_resolve_eod_manufacturer_label(mop_data_list, "MWO-1"), "MF-Q"
		)

	def test_empty_and_no_mwo_returns_none(self):
		self.assertIsNone(_resolve_eod_manufacturer_label([], None))


class TestCollectMopNames(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_empty_returns_empty_string(self):
		self.assertEqual(_collect_mop_names([]), "")

	def test_single(self):
		self.assertEqual(_collect_mop_names([{"mop_name": "MOP-A"}]), "MOP-A")

	def test_multiple_sorted(self):
		self.assertEqual(
			_collect_mop_names([{"mop_name": "MOP-B"}, {"mop_name": "MOP-A"}]),
			"MOP-A, MOP-B",
		)

	def test_entries_without_name_skipped(self):
		self.assertEqual(
			_collect_mop_names([{"mop_name": None}, {"mop_name": "MOP-A"}]), "MOP-A"
		)


# ---------------------------------------------------------------------------
# TestPlanMwoGroupBranches (no-op / failure classification branches)
# ---------------------------------------------------------------------------


class TestPlanMwoGroupBranches(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_no_last_operation_marks_synced(self, _art, mock_mark):
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": []}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertIsNone(result)
		self.assertEqual(stats["processed_mwos"], 1)
		mock_mark.assert_called_once()

	@patch(f"{_MOD}._resolve_department_warehouse", return_value=None)
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_no_t_warehouse_fails(self, _art, _dept):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				to_warehouse=None,
				qty_after_transaction_batch_based=1.0,
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertIsNone(result)
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertTrue(any(f.get("step") == "no_t_warehouse" for f in failures))

	@patch(f"{_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(
		f"{_MOD}._preload_sre_warehouse_map", return_value={("M-1", "B1"): ["WH-TO"]}
	)
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_same_warehouse_genuine_noop(self, _art, _sre, mock_mark, _val):
		# SRE warehouse == last op to_warehouse → row dropped → genuine no-op.
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				to_warehouse="WH-TO",
				qty_after_transaction_batch_based=1.0,
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertIsNone(result)
		self.assertEqual(stats["processed_mwos"], 1)
		mock_mark.assert_called_once()

	@patch(f"{_MOD}._eod_physical_batch_qty")
	@patch(f"{_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(
		f"{_MOD}._preload_sre_warehouse_map", return_value={("M-1", "B1"): ["WH-STALE"]}
	)
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_plan_noop_when_batch_already_at_target(
		self, _art, _sre, mock_mark, _val, mock_phys
	):
		# Regression for MOP-EOD-SYNC-2026-03647: the SRE-mapped source warehouse is stale
		# (0 physical) but the batch has already physically moved to the target department
		# warehouse. Every row resolves source == target -> genuine no-op -> MWO marked
		# synced with no failure (instead of a phantom batch_short holding the whole MWO).
		mock_phys.side_effect = lambda i, b, w: 5.0 if w == "WH-DEPT" else 0.0
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				to_warehouse="WH-DEPT",
				qty_after_transaction_batch_based=2.065,
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertIsNone(result)
		self.assertEqual(stats["processed_mwos"], 1)
		self.assertEqual(stats["failed_mwos"], 0)
		self.assertEqual(failures, [])
		mock_mark.assert_called_once()

	@patch(f"{_MOD}._check_eod_source_batch_stock", return_value={})
	@patch(f"{_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(f"{_MOD}._preload_sre_warehouse_map", return_value={})
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_missing_sre_fails_without_marking(
		self, _art, _sre, mock_mark, _val, _check
	):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				to_warehouse="WH-TO",
				qty_after_transaction_batch_based=1.0,
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertEqual(result["kind"], "failed")
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertTrue(any(f.get("step") == "no_sre_warehouse" for f in failures))
		mock_mark.assert_not_called()

	@patch(
		f"{_MOD}._check_eod_source_batch_stock",
		return_value={("WH-SRE", "M-1", "B1"): (5.0, 1.0)},
	)
	@patch(f"{_MOD}._validate_eod_items_for_mwo_reservation")
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(
		f"{_MOD}._preload_sre_warehouse_map", return_value={("M-1", "B1"): ["WH-SRE"]}
	)
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_batch_short_fails_with_issues_rows(
		self, _art, _sre, mock_mark, _val, _check
	):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				to_warehouse="WH-DEPT",
				qty_after_transaction_batch_based=5.0,
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertEqual(result["kind"], "failed")
		self.assertEqual(len(result["issues_rows"]), 1)
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertTrue(any(f.get("step") == "batch_short" for f in failures))
		mock_mark.assert_not_called()

	@patch(f"{_MOD}._check_eod_source_batch_stock", return_value={})
	@patch(
		f"{_MOD}._validate_eod_items_for_mwo_reservation",
		side_effect=frappe.ValidationError("bad batch data"),
	)
	@patch(f"{_MOD}._mark_all_mwo_mop_logs_synced")
	@patch(
		f"{_MOD}._preload_sre_warehouse_map", return_value={("M-1", "B1"): ["WH-SRE"]}
	)
	@patch(f"{_MOD}._mwo_realized_by_artifact", return_value=None)
	def test_validation_failure_fails_mwo(self, _art, _sre, mock_mark, _val, _check):
		logs = [
			_log(
				item_code="M-1",
				batch_no="B1",
				to_warehouse="WH-DEPT",
				qty_after_transaction_batch_based=5.0,
			)
		]
		mop_data_list = [{"mop_name": "MOP-A", "mop_doc": _mop_doc(), "logs": logs}]
		failures, stats = [], {"processed_mwos": 0, "failed_mwos": 0}
		result = _plan_mwo_group(
			("Test Co", "MWO-1"), mop_data_list, failures, stats, sync_log_name=None
		)
		self.assertEqual(result["kind"], "failed")
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertTrue(any(f.get("step") == "reservation_validate" for f in failures))


# ---------------------------------------------------------------------------
# TestReconcileReservationsForMwo (audit-first SRE reconciliation)
# ---------------------------------------------------------------------------


class TestReconcileReservationsForMwo(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	_BAL = (
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log."
		"get_current_mop_balance_rows"
	)

	def _sre(self, **o):
		base = {
			"name": "SRE-1",
			"item_code": "M-1",
			"warehouse": "WH-1",
			"reserved_qty": 5.0,
			"delivered_qty": 0.0,
			"manufacturing_operation": "MOP-A",
		}
		base.update(o)
		return FrappeDict(base)

	@patch(f"{_MOD}.frappe.logger")
	@patch(f"{_MOD}.frappe.get_doc")
	@patch(_BAL, return_value=[])
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_dry_run_logs_no_cancel(self, mock_get_all, _bal, mock_get_doc, _logger):
		mock_get_all.return_value = [self._sre()]
		_reconcile_reservations_for_mwo("MWO-1", dry_run=True)
		mock_get_doc.assert_not_called()

	@patch(f"{_MOD}.frappe.logger")
	@patch(f"{_MOD}.frappe.db.release_savepoint")
	@patch(f"{_MOD}.frappe.db.savepoint")
	@patch(f"{_MOD}.frappe.get_doc")
	@patch(_BAL, return_value=[])
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_action_cancels_zero_balance_sre(
		self, mock_get_all, _bal, mock_get_doc, _sp, _rel, _logger
	):
		mock_get_all.return_value = [self._sre()]
		doc = MagicMock()
		mock_get_doc.return_value = doc
		_reconcile_reservations_for_mwo("MWO-1", dry_run=False)
		mock_get_doc.assert_called_once_with("Stock Reservation Entry", "SRE-1")
		doc.cancel.assert_called_once()

	@patch(f"{_MOD}.frappe.get_doc")
	@patch(_BAL)
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_skips_fully_delivered_sre(self, mock_get_all, mock_bal, mock_get_doc):
		mock_get_all.return_value = [self._sre(reserved_qty=5.0, delivered_qty=5.0)]
		_reconcile_reservations_for_mwo("MWO-1")
		mock_bal.assert_not_called()
		mock_get_doc.assert_not_called()

	@patch(f"{_MOD}.frappe.get_doc")
	@patch(_BAL)
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_skips_sre_without_operation(self, mock_get_all, mock_bal, mock_get_doc):
		mock_get_all.return_value = [self._sre(manufacturing_operation=None)]
		_reconcile_reservations_for_mwo("MWO-1")
		mock_bal.assert_not_called()
		mock_get_doc.assert_not_called()

	@patch(f"{_MOD}.frappe.logger")
	@patch(f"{_MOD}.frappe.get_doc")
	@patch(_BAL)
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_no_cancel_when_balance_present(
		self, mock_get_all, mock_bal, mock_get_doc, _logger
	):
		mock_get_all.return_value = [self._sre()]
		mock_bal.return_value = [
			FrappeDict({"item_code": "M-1", "to_warehouse": "WH-1", "qty": 5.0})
		]
		_reconcile_reservations_for_mwo("MWO-1", dry_run=False)
		mock_get_doc.assert_not_called()


# ---------------------------------------------------------------------------
# TestFormatBatchShortDiagnostics (message assembly over the SRE qb helpers)
# ---------------------------------------------------------------------------


class TestFormatBatchShortDiagnostics(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOD}._collect_mop_names", return_value="MOP-A")
	@patch(f"{_MOD}._resolve_eod_manufacturer_label", return_value="MF-1")
	@patch(f"{_MOD}._list_open_sre_other_warehouses", return_value=[])
	@patch(f"{_MOD}._list_open_sre_for_batch")
	def test_lists_open_sre_here(self, mock_here, _other, _mfr, _mops):
		mock_here.return_value = [
			{"name": "SRE-1", "warehouse": "WH-1", "open_qty": 3.0}
		]
		msg = _format_batch_short_diagnostics(
			"M-1", "WH-1", "B1", 5.0, 5.0, "MWO-1", [{"mop_name": "MOP-A"}], "Co"
		)
		self.assertIn("Company: Co", msg)
		self.assertIn("Manufacturer: MF-1", msg)
		self.assertIn("Open Stock Reservation Entry SRE-1", msg)

	@patch(f"{_MOD}._collect_mop_names", return_value="MOP-A")
	@patch(f"{_MOD}._resolve_eod_manufacturer_label", return_value="MF-1")
	@patch(f"{_MOD}._list_open_sre_other_warehouses", return_value=[])
	@patch(f"{_MOD}._list_open_sre_for_batch")
	def test_stale_sre_hint_when_physical_zero(self, mock_here, _other, _mfr, _mops):
		mock_here.return_value = [
			{"name": "SRE-1", "warehouse": "WH-1", "open_qty": 3.0}
		]
		msg = _format_batch_short_diagnostics(
			"M-1", "WH-1", "B1", 5.0, 0.0, "MWO-1", [{"mop_name": "MOP-A"}], "Co"
		)
		self.assertIn("physical batch qty is 0", msg)

	@patch(f"{_MOD}._collect_mop_names", return_value="MOP-A")
	@patch(f"{_MOD}._resolve_eod_manufacturer_label", return_value="MF-1")
	@patch(f"{_MOD}._list_open_sre_other_warehouses")
	@patch(f"{_MOD}._list_open_sre_for_batch", return_value=[])
	def test_other_warehouse_fallback(self, _here, mock_other, _mfr, _mops):
		mock_other.return_value = [
			{"name": "SRE-2", "warehouse": "WH-2", "open_qty": 2.0}
		]
		msg = _format_batch_short_diagnostics(
			"M-1", "WH-1", "B1", 5.0, 5.0, "MWO-1", [{"mop_name": "MOP-A"}], "Co"
		)
		self.assertIn("other warehouse(s)", msg)
		self.assertIn("SRE-2", msg)

	@patch(f"{_MOD}._collect_mop_names", return_value="MOP-A")
	@patch(f"{_MOD}._resolve_eod_manufacturer_label", return_value=None)
	@patch(f"{_MOD}._list_open_sre_other_warehouses", return_value=[])
	@patch(f"{_MOD}._list_open_sre_for_batch", return_value=[])
	def test_manufacturer_not_set_line(self, _here, _other, _mfr, _mops):
		msg = _format_batch_short_diagnostics(
			"M-1", "WH-1", "B1", 5.0, 5.0, "MWO-1", [{"mop_name": "MOP-A"}], "Co"
		)
		self.assertIn("(not set on Operation / Work Order)", msg)


# ---------------------------------------------------------------------------
# Branch-gap fills for already-tested functions
# ---------------------------------------------------------------------------


class TestBuildEodSeRowsSerial(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_serial_no_included_in_row(self):
		logs = [
			_log(
				item_code="S-1",
				batch_no=None,
				serial_no="SN1",
				to_warehouse="WH-DEPT",
				qty_after_transaction_batch_based=1.0,
			)
		]
		sre_map = {("S-1", None): ["WH-SRE"]}
		rows, skipped = _build_eod_se_rows("MWO-1", "MOP-A", logs, "WH-DEPT", sre_map)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["serial_no"], "SN1")
		self.assertEqual(skipped, [])


class TestSnapshotBatchEdges(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _sre_row(self, **o):
		base = {
			"name": "SRE-1",
			"item_code": "D-1",
			"warehouse": "WH-SRE",
			"reserved_qty": 5.0,
			"delivered_qty": 0.0,
			"voucher_type": "Sales Order",
			"voucher_no": "SO-1",
			"voucher_detail_no": "row1",
			"voucher_qty": 5.0,
			"company": "Co",
			"stock_uom": "Carat",
			"reservation_based_on": "Serial and Batch",
			"manufacturing_work_order": "MWO-1",
			"manufacturing_operation": "MOP-A",
		}
		base.update(o)
		return FrappeDict(base)

	@patch(f"{_MOD}.frappe.get_all")
	@patch(f"{_MOD}.frappe.get_cached_value", return_value=(1, 0))
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_multiple_sb_entries_captured(self, mock_db_get_all, _cached, mock_get_all):
		mock_db_get_all.return_value = [self._sre_row()]
		mock_get_all.return_value = [
			FrappeDict({"batch_no": "B1", "qty": 3.0, "delivered_qty": 0.0}),
			FrappeDict({"batch_no": "B2", "qty": 2.0, "delivered_qty": 0.0}),
		]
		items = [
			{"item_code": "D-1", "batch_no": "B1", "s_warehouse": "WH-SRE", "qty": 5.0}
		]
		snaps = _snapshot_mwo_sres_for_relocation("MWO-1", items, "WH-DEPT")
		self.assertEqual(len(snaps), 1)
		self.assertEqual(
			snaps[0]["sb_entries"],
			[{"batch_no": "B1", "qty": 3.0}, {"batch_no": "B2", "qty": 2.0}],
		)

	@patch(f"{_MOD}.frappe.get_all")
	@patch(f"{_MOD}.frappe.get_cached_value", return_value=(1, 0))
	@patch(f"{_MOD}.frappe.db.get_all")
	def test_fully_delivered_batch_dropped(
		self, mock_db_get_all, _cached, mock_get_all
	):
		mock_db_get_all.return_value = [self._sre_row(delivered_qty=1.0)]
		mock_get_all.return_value = [
			FrappeDict({"batch_no": "B1", "qty": 5.0, "delivered_qty": 2.0}),
			FrappeDict({"batch_no": "B2", "qty": 2.0, "delivered_qty": 2.0}),
		]
		items = [
			{"item_code": "D-1", "batch_no": "B1", "s_warehouse": "WH-SRE", "qty": 5.0}
		]
		snaps = _snapshot_mwo_sres_for_relocation("MWO-1", items, "WH-DEPT")
		# SRE-level remaining = reserved 5 - delivered 1 = 4.
		self.assertEqual(snaps[0]["remaining"], 4.0)
		# B1: 5-2=3 kept; B2: 2-2=0 dropped.
		self.assertEqual(snaps[0]["sb_entries"], [{"batch_no": "B1", "qty": 3.0}])


class TestApplyMwoFilterRowsNoOperation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_no_operation_filter_includes_all_mops(self):
		logs = [
			_log(manufacturing_work_order="MWO-1", manufacturing_operation="MOP-A"),
			_log(manufacturing_work_order="MWO-1", manufacturing_operation="MOP-B"),
		]
		filter_rows = [
			FrappeDict(
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": None,
					"enabled": 1,
				}
			)
		]
		included, excluded = _apply_mwo_filter_rows(logs, filter_rows)
		self.assertEqual(len(included), 2)
		self.assertEqual(excluded, [])


class TestReserveBatchAtPhysicalWarehouse(IntegrationTestCase):
	"""Physical-warehouse-aware WIP re-reservation (the EOD orphan heal).

	Reserving at the warehouse where the batch PHYSICALLY sits (not the current-op
	department warehouse) is required for correctness: the later Process Loss SE consumes
	from ``sre.warehouse``, so a wrong warehouse would trade "no SRE" for negative stock.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(
		self,
		*,
		active_sre=False,
		so_anchor={
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"base_mr_voucher_qty": 100,
		},
		physical={"WH-X": 5.0},
		siblings=(),
		dept_wh="WH-DEPT",
		mop_logs=(),
		free_by_wh={"WH-X": 5.0, "WH-DEPT": 0.0},
		balance=5.0,
		needed=0.01,
		operation="MOP-CUR",
		item_flags=(1, 0, "Gram"),
		build_return="SRE-NEW",
	):
		"""Run _reserve_batch_at_physical_warehouse with every collaborator mocked.

		Returns (result, build_mock) where build_mock is the patched
		_build_and_submit_mwo_sre (inspect .called / .call_args).
		"""

		def _free(_item, wh, _batch):
			return free_by_wh.get(wh, 0.0)

		with patch(f"{_MOD}._active_sre_exists", return_value=active_sre), patch(
			f"{_MOD}._resolve_mwo_so_anchor", return_value=so_anchor
		), patch(
			f"{_MOD}._physical_batch_warehouses", return_value=dict(physical)
		), patch(
			f"{_MOD}._cancelled_and_sibling_sre_warehouses", return_value=list(siblings)
		), patch(f"{_MOD}._mop_log_to_warehouses", return_value=list(mop_logs)), patch(
			f"{_MOD}._resolve_department_warehouse", return_value=dept_wh
		), patch(f"{_MOD}.frappe.get_cached_doc", return_value=MagicMock()), patch(
			f"{_MOD}._free_batch_qty_to_reserve", side_effect=_free
		), patch(f"{_MOD}._mwo_batch_balance", return_value=balance), patch(
			f"{_MOD}.frappe.get_cached_value", return_value=item_flags
		), patch(
			f"{_MOD}._build_and_submit_mwo_sre", return_value=build_return
		) as build_mock:
			result = _reserve_batch_at_physical_warehouse(
				"MWO-1", "M-1", "B1", needed, operation, "Co"
			)
		return result, build_mock

	def test_picks_physical_warehouse_over_dept(self):
		result, build = self._run()
		self.assertEqual(result, ["SRE-NEW"])
		self.assertTrue(build.called)
		# Signature: (company, mwo, item_code, warehouse, batch_no, reserved_qty, available, ...)
		args = build.call_args.args
		self.assertEqual(
			args[3], "WH-X"
		)  # warehouse = the one with free physical stock
		self.assertEqual(
			args[5], 5.0
		)  # reserved_qty = min(max(balance, needed), best_free)

	def test_reserves_mwo_balance_not_whole_free_pool(self):
		# Batch shared by two orphaned MWOs: 10 free at WH-X but this MWO's balance is 6.
		result, build = self._run(
			physical={"WH-X": 10.0}, free_by_wh={"WH-X": 10.0}, balance=6.0
		)
		self.assertEqual(result, ["SRE-NEW"])
		self.assertEqual(build.call_args.args[3], "WH-X")
		self.assertEqual(build.call_args.args[5], 6.0)

	def test_no_warehouse_holds_free_qty_returns_none(self):
		result, build = self._run(physical={}, free_by_wh={"WH-DEPT": 0.0})
		self.assertIsNone(result)
		self.assertFalse(build.called)

	def test_best_free_below_needed_returns_none(self):
		# Best warehouse cannot even cover the loss floor -> fail loudly, never relocate.
		result, build = self._run(
			physical={"WH-X": 5.0}, free_by_wh={"WH-X": 5.0}, needed=6.0
		)
		self.assertIsNone(result)
		self.assertFalse(build.called)

	def test_no_sales_order_anchor_returns_none(self):
		result, build = self._run(so_anchor=None)
		self.assertIsNone(result)
		self.assertFalse(build.called)

	def test_idempotent_when_active_sre_exists(self):
		result, build = self._run(active_sre=True)
		self.assertIsNone(result)
		self.assertFalse(build.called)

	def test_non_batch_returns_none(self):
		# No batch_no -> nothing to heal (Process Loss is batch-only).
		with patch(f"{_MOD}._active_sre_exists", return_value=False):
			self.assertIsNone(
				_reserve_batch_at_physical_warehouse(
					"MWO-1", "M-1", None, 0.01, "MOP-CUR", "Co"
				)
			)


class TestEodPostConditionGuard(IntegrationTestCase):
	"""EOD Phase 2 must never commit a batched cancellation without a live replacement."""

	@classmethod
	def setUpClass(cls):
		pass

	def _snapshot(self):
		return [
			{
				"sre": SimpleNamespace(
					name="SRE-OLD", item_code="M-1", manufacturing_operation="MOP-A"
				),
				"remaining": 3.0,
				"has_batch_no": 1,
				"has_serial_no": 0,
				"sb_entries": [{"batch_no": "B1", "qty": 3.0}],
			}
		]

	def _main_mwos(self):
		return [
			{
				"mwo": "MWO-1",
				"items": [
					{
						"item_code": "M-1",
						"t_warehouse": "WH-DEPT",
						"custom_manufacturing_work_order": "MWO-1",
						"qty": 3.0,
						"batch_no": "B1",
						"manufacturing_operation": "MOP-A",
					}
				],
				"t_warehouse": "WH-DEPT",
				"child_row_names": ["CR-1"],
				"mop_data_list": [{"mop_name": "MOP-A", "mop_doc": _mop_doc()}],
			}
		]

	def _commit(self, heal_return):
		"""Run _commit_company_main_se Phase 2 with the re-reservation skipped so the
		batched snapshot is orphaned; heal_return drives the guard's outcome.

		Returns (failures, stats, marks, hard_deletes, rollbacks).
		"""
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}
		marks, hard_deletes, rollbacks = [], [], []

		with patch(f"{_MOD}.frappe.db.savepoint"), patch(
			f"{_MOD}.frappe.db.release_savepoint"
		), patch(
			f"{_MOD}._rollback_to_savepoint",
			side_effect=lambda sp: rollbacks.append(sp),
		), patch(f"{_MOD}._save_draft_eod_se", return_value="SE-DRAFT"), patch(
			f"{_MOD}._snapshot_mwo_sres_for_relocation", return_value=self._snapshot()
		), patch(f"{_MOD}._cancel_sre_snapshots"), patch(
			f"{_MOD}.frappe.get_doc", return_value=FakeStockEntry("SE-DRAFT")
		), patch(
			# Simulate the v16 batch skip: nothing re-reserved.
			f"{_MOD}._reserve_sres_from_eod_se_rows"
		), patch(f"{_MOD}._active_sre_exists", return_value=False), patch(
			f"{_MOD}._reserve_batch_at_physical_warehouse", return_value=heal_return
		), patch(
			f"{_MOD}._hard_delete_cancelled_snapshots",
			side_effect=lambda snaps: hard_deletes.append(snaps),
		), patch(
			f"{_MOD}._mark_all_mwo_mop_logs_synced",
			side_effect=lambda mwos, selective=False: marks.append(mwos),
		), patch(f"{_MOD}._stamp_last_eod_sync"), patch(f"{_MOD}._bulk_set_child_rows"):
			_commit_company_main_se("Co", "MF-1", self._main_mwos(), failures, stats)
		return failures, stats, marks, hard_deletes, rollbacks

	def test_heal_success_commits_bucket(self):
		failures, stats, marks, hard_deletes, rollbacks = self._commit(["SRE-HEAL"])
		self.assertEqual(failures, [])
		self.assertIn("SE-DRAFT", stats["submitted_ses"])
		self.assertEqual(marks, [["MWO-1"]])  # MOP Logs marked synced
		self.assertEqual(len(hard_deletes), 1)  # cancelled snapshots hard-deleted
		self.assertEqual(rollbacks, [])

	def test_heal_failure_rolls_back_and_does_not_mark_synced(self):
		failures, stats, marks, hard_deletes, rollbacks = self._commit(None)
		self.assertIn("eod_submit_phase", rollbacks)  # bucket rolled back
		self.assertEqual(len(failures), 1)
		self.assertEqual(marks, [])  # never marked synced
		self.assertEqual(hard_deletes, [])  # cancellation NOT made permanent
		self.assertEqual(stats["submitted_ses"], [])
