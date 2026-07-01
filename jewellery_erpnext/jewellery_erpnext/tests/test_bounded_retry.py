# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the bounded, lock-error-only retry helper (bounded_retry)."""

from unittest.mock import MagicMock, patch

from frappe.exceptions import QueryDeadlockError, QueryTimeoutError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import bounded_retry
from jewellery_erpnext.jewellery_erpnext.bounded_retry import (
	RETRYABLE_LOCK_ERRORS,
	retry_on_lock_error,
	run_with_retry,
)


# Patch sleep + rollback for every test so retries are instant and don't touch the
# real test transaction.
@patch.object(bounded_retry.time, "sleep", MagicMock())
@patch.object(bounded_retry.frappe.db, "rollback", MagicMock())
class TestRunWithRetry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_returns_on_first_success(self):
		fn = MagicMock(return_value="ok")
		self.assertEqual(run_with_retry(fn, "a", k=1), "ok")
		fn.assert_called_once_with("a", k=1)

	def test_retries_deadlock_then_succeeds(self):
		fn = MagicMock(side_effect=[QueryDeadlockError("x"), "ok"])
		self.assertEqual(run_with_retry(fn, max_attempts=3), "ok")
		self.assertEqual(fn.call_count, 2)

	def test_retries_timeout_then_succeeds(self):
		fn = MagicMock(
			side_effect=[QueryTimeoutError("x"), QueryTimeoutError("x"), "ok"]
		)
		self.assertEqual(run_with_retry(fn, max_attempts=3), "ok")
		self.assertEqual(fn.call_count, 3)

	def test_raises_after_exhausting_attempts(self):
		fn = MagicMock(side_effect=QueryDeadlockError("x"))
		with self.assertRaises(QueryDeadlockError):
			run_with_retry(fn, max_attempts=3)
		self.assertEqual(fn.call_count, 3)

	def test_non_lock_error_is_not_retried(self):
		fn = MagicMock(side_effect=ValueError("real bug"))
		with self.assertRaises(ValueError):
			run_with_retry(fn, max_attempts=3)
		fn.assert_called_once()

	def test_rolls_back_between_attempts(self):
		fn = MagicMock(side_effect=[QueryDeadlockError("x"), "ok"])
		# Fresh local patch so the assertion count isn't polluted by the shared
		# class-level rollback mock used by the other tests.
		with patch.object(bounded_retry.frappe.db, "rollback") as mock_rb:
			run_with_retry(fn, max_attempts=2)
			mock_rb.assert_called_once()

	def tearDown(self):
		return super().tearDown()


@patch.object(bounded_retry.time, "sleep", MagicMock())
@patch.object(bounded_retry.frappe.db, "rollback", MagicMock())
class TestRetryOnLockErrorDecorator(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_decorator_retries_then_succeeds(self):
		calls = {"n": 0}

		@retry_on_lock_error(max_attempts=3)
		def f(x):
			calls["n"] += 1
			if calls["n"] < 2:
				raise QueryDeadlockError("x")
			return x * 2

		self.assertEqual(f(5), 10)
		self.assertEqual(calls["n"], 2)

	def test_decorator_passes_non_lock_error_through(self):
		@retry_on_lock_error(max_attempts=3)
		def f():
			raise KeyError("nope")

		with self.assertRaises(KeyError):
			f()

	def tearDown(self):
		return super().tearDown()


class TestRetryableSet(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_only_1205_1213_are_retryable(self):
		self.assertEqual(
			set(RETRYABLE_LOCK_ERRORS), {QueryDeadlockError, QueryTimeoutError}
		)

	def tearDown(self):
		return super().tearDown()
