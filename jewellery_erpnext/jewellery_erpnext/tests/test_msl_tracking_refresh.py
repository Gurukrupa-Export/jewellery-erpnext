# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the after-commit MSL tracking refresh (doc_events/warehouse_tracking.py).

Employee IR used to recompute ``Warehouse.custom_msl_tracking`` inside its locked submit
transaction and swallow every exception, including 1205/1213. It now registers an
after-commit callback that marks the warehouse dirty and enqueues a deduplicated short
job (``msl::<warehouse>``); the job re-runs while the warehouse keeps getting dirtied.

Pure mock tests: no Redis, no worker and no writes. The two-connection Warehouse-lock
test (plan T-08) needs real commits and is not part of this module.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
import redis
from frappe.tests import IntegrationTestCase
from rq.job import JobStatus

from jewellery_erpnext.jewellery_erpnext import bounded_retry
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	warehouse_tracking as wt,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import employee_ir

WH = "Assembly MSL WH - 6 - GEPL"


class _FakeCache:
	"""Dict-backed stand-in for ``frappe.cache`` that records the call order."""

	def __init__(self, calls=None):
		self.store = {}
		self.calls = calls if calls is not None else []

	def set_value(self, key, val, expires_in_sec=None, **kwargs):
		self.calls.append(("set", key, expires_in_sec))
		self.store[key] = val

	def get_value(self, key, **kwargs):
		self.calls.append(("get", key))
		return self.store.get(key)

	def delete_value(self, key, **kwargs):
		self.calls.append(("delete", key))
		self.store.pop(key, None)


class _FakeWH:
	def __init__(self):
		self.rows = []
		self.flags = SimpleNamespace()
		self.saved = False

	def set(self, field, val):
		self.rows = list(val)

	def append(self, field, row):
		self.rows.append(row)

	def save(self, ignore_permissions=False):
		self.saved = True


class _IsolatedCallbacks(IntegrationTestCase):
	def setUp(self):
		# Each test starts with no pending after-commit callbacks; anything pending
		# outside the test is restored afterwards.
		pending = list(frappe.db.after_commit._functions)
		frappe.db.after_commit.reset()

		def _restore():
			frappe.db.after_commit.reset()
			for fn in pending:
				frappe.db.after_commit.add(fn)

		self.addCleanup(_restore)

	def _pending(self):
		return list(frappe.db.after_commit._functions)


class TestQueueMslTrackingRefresh(_IsolatedCallbacks):
	def test_registers_one_callback_and_no_io(self):
		"""Inside the business transaction: one callback, no Redis, no recompute."""
		with patch("frappe.enqueue") as enqueue, patch.object(
			frappe.cache, "set_value"
		) as set_value, patch.object(wt, "recalculate_msl_tracking") as recalc:
			wt.queue_msl_tracking_refresh(WH)

		pending = self._pending()
		self.assertEqual(len(pending), 1)
		self.assertIs(pending[0].func, wt._enqueue_msl_refresh)
		self.assertEqual(pending[0].args, (WH,))
		enqueue.assert_not_called()
		set_value.assert_not_called()
		recalc.assert_not_called()

	def test_no_warehouse_registers_nothing(self):
		for value in (None, ""):
			with self.subTest(warehouse=value):
				wt.queue_msl_tracking_refresh(value)
				self.assertEqual(self._pending(), [])

	def test_rollback_discards_the_scheduled_refresh(self):
		"""A failed submit is rolled back; frappe's rollback clears after_commit, so a
		refresh is never scheduled for a movement that did not happen."""
		with patch("frappe.enqueue") as enqueue:
			wt.queue_msl_tracking_refresh(WH)
			self.assertEqual(len(self._pending()), 1)
			frappe.db.rollback()
			self.assertEqual(self._pending(), [])
		enqueue.assert_not_called()


class TestEnqueueCallback(_IsolatedCallbacks):
	def test_callback_marks_dirty_and_enqueues_deduplicated_short_job(self):
		cache = _FakeCache()
		with patch.object(frappe, "cache", cache), patch("frappe.enqueue") as enqueue:
			wt._enqueue_msl_refresh(WH)

		enqueue.assert_called_once_with(
			wt.MSL_REFRESH_JOB,
			queue="short",
			timeout=300,
			job_id=f"msl::{WH}",
			deduplicate=True,
			warehouse=WH,
		)
		key = wt.MSL_REFRESH_DIRTY_KEY.format(WH)
		self.assertEqual(cache.store, {key: 1})
		# The flag is set before the dedup check, so a STARTED job sees it.
		self.assertEqual(cache.calls, [("set", key, wt.MSL_REFRESH_DIRTY_TTL)])

	def test_job_path_resolves_and_is_not_whitelisted(self):
		"""execute_job imports the method by its dotted path; it saves with
		ignore_permissions, so it must not be callable over HTTP."""
		self.assertIs(frappe.get_attr(wt.MSL_REFRESH_JOB), wt.refresh_msl_tracking_job)
		self.assertNotIn(wt.refresh_msl_tracking_job, frappe.whitelisted)

	def test_callback_never_raises(self):
		"""It runs inside frappe.db.commit(): raising would fail the request/job after
		the business commit and skip the callbacks queued behind it."""
		failures = (
			("enqueue", redis.exceptions.ConnectionError("queue down")),
			("enqueue", frappe.ValidationError("queue full")),
			("set_value", RuntimeError("cache broken")),
		)
		for target, exc in failures:
			with self.subTest(target=target, error=type(exc).__name__):
				cache = _FakeCache()
				if target == "set_value":
					cache.set_value = MagicMock(side_effect=exc)
				with patch.object(frappe, "cache", cache), patch(
					"frappe.enqueue", side_effect=exc if target == "enqueue" else None
				), patch("frappe.log_error") as log_error:
					wt._enqueue_msl_refresh(WH)  # must not raise

				log_error.assert_called_once()
				self.assertEqual(
					log_error.call_args.kwargs.get("title"),
					"MSL tracking refresh could not be queued",
				)

	def test_runs_from_commit_after_earlier_callbacks_and_before_later_ones(self):
		"""frappe runs after-commit callbacks in order; a failing enqueue must not stop
		the callbacks registered after it."""
		order = []

		def _failing_enqueue(*args, **kwargs):
			order.append("enqueue")
			raise redis.exceptions.ConnectionError("queue down")

		frappe.db.after_commit.add(lambda: order.append("before"))
		wt.queue_msl_tracking_refresh(WH)
		frappe.db.after_commit.add(lambda: order.append("after"))
		with patch.object(frappe, "cache", _FakeCache()), patch(
			"frappe.enqueue", side_effect=_failing_enqueue
		), patch("frappe.log_error"):
			frappe.db.after_commit.run()

		self.assertEqual(order, ["before", "enqueue", "after"])


class TestFrappeDeduplication(IntegrationTestCase):
	"""Our job id through frappe's real enqueue (Redis replaced by mocks)."""

	_BJ = "frappe.utils.background_jobs"

	def _enqueue(self, existing_status):
		existing = None
		if existing_status:
			existing = MagicMock()
			existing.get_status.return_value = existing_status
		q = MagicMock()
		with patch(f"{self._BJ}.get_job", return_value=existing) as get_job, patch(
			f"{self._BJ}.get_queue", return_value=q
		), patch(f"{self._BJ}._check_queue_size"), patch.object(
			frappe, "cache", _FakeCache()
		), patch("frappe.log_error") as log_error:
			wt._enqueue_msl_refresh(WH)
		log_error.assert_not_called()
		get_job.assert_called_once_with(f"msl::{WH}")
		return existing, q

	def test_queued_or_started_job_is_not_queued_again(self):
		for status in (JobStatus.QUEUED, JobStatus.STARTED):
			with self.subTest(status=status):
				existing, q = self._enqueue(status)
				q.enqueue_call.assert_not_called()
				existing.delete.assert_not_called()

	def test_finished_job_is_replaced_by_a_new_one(self):
		from frappe.utils.background_jobs import create_job_id

		for status in (None, JobStatus.FINISHED, JobStatus.FAILED):
			with self.subTest(status=status):
				existing, q = self._enqueue(status)
				if existing:
					existing.delete.assert_called_once()
				q.enqueue_call.assert_called_once()
				kwargs = q.enqueue_call.call_args.kwargs
				job_id = kwargs["job_id"]
				self.assertEqual(job_id, create_job_id(f"msl::{WH}"))
				self.assertNotIn(":", job_id)  # RQ rejects ':' in job ids
				self.assertEqual(kwargs["timeout"], 300)
				self.assertEqual(kwargs["kwargs"]["method"], wt.MSL_REFRESH_JOB)
				self.assertEqual(kwargs["kwargs"]["kwargs"], {"warehouse": WH})
				self.assertEqual(kwargs["kwargs"]["user"], frappe.session.user)


class TestRefreshJob(IntegrationTestCase):
	def _run(self, recalc_side_effect=None, calls=None):
		calls = calls if calls is not None else []
		cache = _FakeCache(calls)
		key = wt.MSL_REFRESH_DIRTY_KEY.format(WH)
		cache.store[key] = 1

		def _recalc(warehouse):
			calls.append(("recalc", warehouse))
			if recalc_side_effect:
				return recalc_side_effect(cache, key)
			return 0

		with patch.object(frappe, "cache", cache), patch(
			"frappe.db.commit", side_effect=lambda *a, **k: calls.append(("commit",))
		), patch.object(wt, "recalculate_msl_tracking", side_effect=_recalc) as recalc:
			wt.refresh_msl_tracking_job(WH)
		return recalc, calls, cache, key

	def test_single_pass_clears_flag_then_reads_a_fresh_snapshot(self):
		"""Clear the flag, start a new transaction, recompute, commit, re-check."""
		recalc, calls, cache, key = self._run()
		self.assertEqual(recalc.call_count, 1)
		self.assertEqual(
			calls,
			[("delete", key), ("commit",), ("recalc", WH), ("commit",), ("get", key)],
		)
		self.assertNotIn(key, cache.store)

	def test_reruns_when_dirtied_mid_pass(self):
		"""A movement that commits while the job is recomputing is picked up by a second
		pass that starts after it."""
		state = {"n": 0}

		def _dirty_once(cache, key):
			state["n"] += 1
			if state["n"] == 1:
				cache.store[key] = 1

		recalc, calls, _cache, key = self._run(_dirty_once)
		self.assertEqual(recalc.call_count, 2)
		one_pass = [
			("delete", key),
			("commit",),
			("recalc", WH),
			("commit",),
			("get", key),
		]
		self.assertEqual(calls, one_pass * 2)

	def test_pass_count_is_capped(self):
		def _always_dirty(cache, key):
			cache.store[key] = 1

		recalc, _calls, _cache, _key = self._run(_always_dirty)
		self.assertEqual(recalc.call_count, wt.MSL_REFRESH_MAX_PASSES)

	def test_lock_errors_propagate_after_bounded_retry(self):
		"""The job swallows nothing: a persistent 1205/1213 is retried by
		bounded_retry (rolled back between attempts) and then reaches execute_job,
		which logs it and marks the RQ job failed."""
		for exc_type in bounded_retry.RETRYABLE_LOCK_ERRORS:
			with self.subTest(error=exc_type.__name__):

				def _raise(cache, key, exc_type=exc_type):
					raise exc_type("lock")

				with patch("frappe.db.rollback") as rollback, patch.object(
					bounded_retry.time, "sleep"
				), patch.object(bounded_retry.frappe, "logger"):
					with self.assertRaises(exc_type):
						self._run(_raise)
				self.assertEqual(rollback.call_count, 2)  # 3 attempts

	def test_other_errors_propagate_without_retry(self):
		def _raise(cache, key):
			raise frappe.TimestampMismatchError("changed meanwhile")

		with patch("frappe.db.rollback") as rollback:
			with self.assertRaises(frappe.TimestampMismatchError):
				self._run(_raise)
		rollback.assert_not_called()


class TestRecomputeUnchanged(IntegrationTestCase):
	"""The job writes exactly what the inline recompute wrote: same function, full
	recompute from the ledger, same rows and flags."""

	MONTH = [
		{
			"item_code": "M-G",
			"issue_qty": 4.0,
			"receive_qty": 3.0,
			"loss_qty": 1.0,
			"pending_qty": 0.0,
		},
		{
			"item_code": "M-S",
			"issue_qty": 2.5,
			"receive_qty": 0.0,
			"loss_qty": 0.0,
			"pending_qty": 2.5,
		},
	]
	CUMULATIVE = [
		{
			"item_code": "M-G",
			"issue_qty": 10.0,
			"receive_qty": 3.0,
			"loss_qty": 1.0,
			"pending_qty": 6.0,
		},
		{
			"item_code": "M-S",
			"issue_qty": 2.5,
			"receive_qty": 0.0,
			"loss_qty": 0.0,
			"pending_qty": 2.5,
		},
		{  # fully settled before this month: dropped by both paths
			"item_code": "M-Z",
			"issue_qty": 1.0,
			"receive_qty": 1.0,
			"loss_qty": 0.0,
			"pending_qty": 0.0,
		},
	]

	def setUp(self):
		# flt() reads the rounding method from System Settings; load it before
		# frappe.get_doc is patched (without Redis it would go through get_doc).
		frappe.get_system_settings("rounding_method")

	def _with_ledger(self, fn):
		docs = []
		real_get_doc = frappe.get_doc

		def _tracking(filters, *args, **kwargs):
			return self.MONTH if filters.get("from_date") else self.CUMULATIVE

		def _get_doc(doctype, name=None, *args, **kwargs):
			if doctype != "Warehouse":
				return real_get_doc(doctype, name, *args, **kwargs)
			self.assertEqual(name, WH)
			docs.append(_FakeWH())
			return docs[-1]

		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(employee="EMP", warehouse_type="Raw Material"),
		), patch("frappe.get_precision", return_value=3), patch.object(
			wt, "today", return_value="2026-11-17"
		), patch.object(
			wt, "get_warehouse_item_tracking", side_effect=_tracking
		), patch.object(
			# kggk_uat builds both views from one ledger scan (_msl_tracking_views), the same
			# two views get_warehouse_item_tracking returns.
			wt,
			"_msl_tracking_views",
			side_effect=lambda warehouse, month_start: (self.MONTH, self.CUMULATIVE),
		), patch("frappe.get_doc", side_effect=_get_doc), patch(
			"frappe.db.commit"
		), patch.object(frappe, "cache", _FakeCache()):
			result = fn()
		self.assertEqual(len(docs), 1)
		return result, docs[0]

	def test_job_rows_equal_the_inline_recompute(self):
		n_inline, inline = self._with_ledger(lambda: wt.recalculate_msl_tracking(WH))
		_none, job = self._with_ledger(lambda: wt.refresh_msl_tracking_job(WH))

		self.assertEqual(n_inline, 2)
		self.assertEqual(job.rows, inline.rows)
		self.assertEqual(vars(job.flags), vars(inline.flags))
		self.assertTrue(job.saved and inline.saved)
		self.assertEqual(
			job.rows,
			[
				{
					"item_code": "M-G",
					"opening_qty": 6.0,
					"issue_qty": 4.0,
					"receive_qty": 3.0,
					"loss_qty": 1.0,
					"pending_qty": 6.0,
				},
				{
					"item_code": "M-S",
					"opening_qty": 0.0,
					"issue_qty": 2.5,
					"receive_qty": 0.0,
					"loss_qty": 0.0,
					"pending_qty": 2.5,
				},
			],
		)
		self.assertTrue(job.flags.ignore_msl_guards and job.flags.ignore_version)


class TestEmployeeIRCallSites(IntegrationTestCase):
	"""Tripwire: every Issue/Receive submit and cancel path ends in the scheduler, and
	Employee IR never calls the recompute inline again."""

	def test_issue_and_receive_paths_use_the_scheduler(self):
		cls = employee_ir.EmployeeIR
		self.assertIn(
			"on_submit_issue_new(cancel=True)", inspect.getsource(cls.on_cancel)
		)
		self.assertIn(
			"on_submit_receive(cancel=True)", inspect.getsource(cls.on_cancel)
		)
		for method in (cls.on_submit_issue_new, cls.on_submit_receive):
			with self.subTest(method=method.__name__):
				self.assertIn("self._refresh_msl_tracking()", inspect.getsource(method))
		self.assertIn(
			"queue_msl_tracking_refresh", inspect.getsource(cls._refresh_msl_tracking)
		)

	def test_employee_ir_never_recomputes_inline(self):
		self.assertNotIn("recalculate_msl_tracking(", inspect.getsource(employee_ir))
