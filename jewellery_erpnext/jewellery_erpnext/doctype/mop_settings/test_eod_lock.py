# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from datetime import timedelta
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
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

_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock"


def _settings(running=0, lock_until=None):
	future = add_to_date(now_datetime(), hours=2)
	past = add_to_date(now_datetime(), hours=-1)
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


class TestIsEodSyncLocked(FrappeTestCase):
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


class TestSetEodSyncRunning(FrappeTestCase):
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


class TestSetEodSyncQueued(FrappeTestCase):
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


class TestReleaseEodSyncLock(FrappeTestCase):
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


class TestReleaseExpiredEodSyncLock(FrappeTestCase):
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


class TestValidateNotEodSyncLocked(FrappeTestCase):
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


class TestCheckAndEnqueueEodSync(FrappeTestCase):
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

	@patch(f"{_SCHED_MOD}.frappe.enqueue")
	@patch(f"{_SCHED_MOD}.set_eod_sync_queued")
	@patch(f"{_SCHED_MOD}.frappe.db.get_value")
	@patch(f"{_SCHED_MOD}.now_datetime")
	@patch(f"{_SCHED_MOD}.release_expired_eod_sync_lock")
	def test_enqueues_when_time_matches(
		self, _mock_rel, mock_now, mock_gv, mock_queued, mock_enq
	):
		from datetime import datetime

		mock_now.return_value = datetime(2026, 5, 31, 2, 0, 0)
		mock_gv.return_value = self._make_settings()

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler import (
			check_and_enqueue_eod_sync,
		)

		check_and_enqueue_eod_sync()

		mock_queued.assert_called_once()
		mock_enq.assert_called_once()

	@patch(f"{_SCHED_MOD}.frappe.enqueue")
	@patch(f"{_SCHED_MOD}.set_eod_sync_queued")
	@patch(f"{_SCHED_MOD}.frappe.db.get_value")
	@patch(f"{_SCHED_MOD}.now_datetime")
	@patch(f"{_SCHED_MOD}.release_expired_eod_sync_lock")
	def test_no_enqueue_when_time_does_not_match(
		self, _mock_rel, mock_now, mock_gv, mock_queued, mock_enq
	):
		from datetime import datetime

		mock_now.return_value = datetime(2026, 5, 31, 3, 15, 0)  # 03:15 ≠ 02:00
		mock_gv.return_value = self._make_settings()

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler import (
			check_and_enqueue_eod_sync,
		)

		check_and_enqueue_eod_sync()

		mock_queued.assert_not_called()
		mock_enq.assert_not_called()

	@patch(f"{_SCHED_MOD}.frappe.enqueue")
	@patch(f"{_SCHED_MOD}.set_eod_sync_queued")
	@patch(f"{_SCHED_MOD}.frappe.db.get_value")
	@patch(f"{_SCHED_MOD}.now_datetime")
	@patch(f"{_SCHED_MOD}.release_expired_eod_sync_lock")
	def test_no_enqueue_when_already_queued(
		self, _mock_rel, mock_now, mock_gv, mock_queued, mock_enq
	):
		from datetime import datetime

		mock_now.return_value = datetime(2026, 5, 31, 2, 0, 0)
		mock_gv.return_value = self._make_settings(eod_sync_status="Queued")

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler import (
			check_and_enqueue_eod_sync,
		)

		check_and_enqueue_eod_sync()

		mock_queued.assert_not_called()
		mock_enq.assert_not_called()

	@patch(f"{_SCHED_MOD}.frappe.enqueue")
	@patch(f"{_SCHED_MOD}.set_eod_sync_queued")
	@patch(f"{_SCHED_MOD}.frappe.db.get_value")
	@patch(f"{_SCHED_MOD}.now_datetime")
	@patch(f"{_SCHED_MOD}.release_expired_eod_sync_lock")
	def test_no_enqueue_when_already_ran_today(
		self, _mock_rel, mock_now, mock_gv, mock_queued, mock_enq
	):
		from datetime import datetime

		today = datetime(2026, 5, 31, 2, 0, 0)
		mock_now.return_value = today
		mock_gv.return_value = self._make_settings(
			eod_sync_last_completed_on=datetime(2026, 5, 31, 0, 0, 0)
		)

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler import (
			check_and_enqueue_eod_sync,
		)

		check_and_enqueue_eod_sync()

		mock_queued.assert_not_called()
		mock_enq.assert_not_called()

	@patch(f"{_SCHED_MOD}.frappe.enqueue")
	@patch(f"{_SCHED_MOD}.set_eod_sync_queued")
	@patch(f"{_SCHED_MOD}.frappe.db.get_value")
	@patch(f"{_SCHED_MOD}.now_datetime")
	@patch(f"{_SCHED_MOD}.release_expired_eod_sync_lock")
	def test_no_enqueue_when_running(
		self, _mock_rel, mock_now, mock_gv, mock_queued, mock_enq
	):
		from datetime import datetime

		mock_now.return_value = datetime(2026, 5, 31, 2, 0, 0)
		mock_gv.return_value = self._make_settings(eod_sync_running=1)

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.scheduler import (
			check_and_enqueue_eod_sync,
		)

		check_and_enqueue_eod_sync()

		mock_queued.assert_not_called()
		mock_enq.assert_not_called()


# ---------------------------------------------------------------------------
# Permission tests for MOPSettings controller
# ---------------------------------------------------------------------------


_SETTINGS_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_settings"


class TestMOPSettingsPermissions(FrappeTestCase):
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


class TestDraftSePreservedOnSubmitFailure(FrappeTestCase):
	"""Validates Phase 2 savepoint design: draft SE must survive a submit failure."""

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
		return_value={("M-1", "B1"): "WH-SRE"},
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
		from frappe.types.frappedict import _dict as FD

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

		from frappe.types.frappedict import _dict as FD

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_process_mwo_group,
		)

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


class TestConsolidatedErrorLog(FrappeTestCase):
	"""Only one Error Log is created for the full sync, not one per MWO."""

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
		from frappe.types.frappedict import _dict as FD

		mock_log_error.return_value = FD({"name": "ERR-CONSOLIDATED-001"})
		mock_get_groups.return_value = {
			("Co", "MWO-A"): [],
			("Co", "MWO-B"): [],
		}

		# Both MWOs fail in planning → no consolidated SE, two failures, one error log.
		def _inject_failures(group_key, mop_data_list, failures, stats, sync_log_name=None, selective=False):
			_, mwo = group_key
			failures.append(
				{
					"step": "no_sre_warehouse",
					"mwo": mwo,
					"error_message": f"Simulated failure for {mwo}",
				}
			)
			stats["failed_mwos"] += 1
			return {"kind": "failed", "company": "Co", "manufacturer": "MF-1", "issues_rows": []}

		mock_plan.side_effect = _inject_failures

		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			sync_mop_logs,
		)

		sync_mop_logs()

		# Exactly one Error Log must be created (the consolidated one)
		self.assertEqual(mock_log_error.call_count, 1)
		title_arg = (
			mock_log_error.call_args[1].get("title") or mock_log_error.call_args[0][0]
		)
		self.assertIn("MOP EOD Sync Failed", title_arg)
