"""Bounded, jittered retry for residual InnoDB lock errors (1205 / 1213).

This is the LAST-RESORT safety net from the lock-contention remediation — NOT a
substitute for the root-cause fixes (naming, lock ordering, transaction-scope
reduction, queue serialization). It exists only to absorb the rare deadlock that
InnoDB can always produce even with perfect lock ordering.

Hard rules:

* Retry ONLY ``QueryDeadlockError`` (1213) and ``QueryTimeoutError`` (1205). Never
  retry real business errors (negative stock, "reserved for other transactions",
  validation) — those are not transient and must surface to the operator.
* The wrapped callable MUST be idempotent / safely re-runnable. Each retry first
  ``frappe.db.rollback()``s, so any partial work from the failed attempt is undone
  before the next one.
* Bounded attempts with exponential backoff + jitter, so a thundering herd doesn't
  re-collide in lockstep.

If retries fire frequently in production, the root cause is NOT yet fixed — go back
to the contention map, don't raise ``max_attempts``.
"""

import functools
import random
import time

import frappe
from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

#: Only these are retried. Both map to "try restarting transaction" in MariaDB.
RETRYABLE_LOCK_ERRORS = (QueryDeadlockError, QueryTimeoutError)


def run_with_retry(fn, *args, max_attempts=3, base_delay=0.2, max_delay=2.0, **kwargs):
	"""Run ``fn(*args, **kwargs)``, retrying only on 1205/1213 up to ``max_attempts``.

	Between attempts the transaction is rolled back and the worker sleeps for an
	exponentially increasing, jittered interval. Re-raises the original error once
	attempts are exhausted (or immediately for any non-retryable error).
	"""
	last_error = None
	for attempt in range(1, max_attempts + 1):
		try:
			return fn(*args, **kwargs)
		except RETRYABLE_LOCK_ERRORS as exc:
			last_error = exc
			if attempt >= max_attempts:
				raise
			frappe.db.rollback()
			delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
			# Full jitter: sleep in [0, delay] so colliding workers desynchronise.
			time.sleep(delay * random.random())
			frappe.logger("bounded_retry").warning(
				f"Retrying after {type(exc).__name__} "
				f"(attempt {attempt + 1}/{max_attempts}): {exc}"
			)
	# Unreachable, but keep linters happy.
	if last_error:
		raise last_error


def retry_on_lock_error(max_attempts=3, base_delay=0.2, max_delay=2.0):
	"""Decorator form of :func:`run_with_retry`.

	Usage::

	    @retry_on_lock_error()
	    def _materialize_transfer_se(mr_name):
	        ...  # idempotent body
	"""

	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			return run_with_retry(
				fn,
				*args,
				max_attempts=max_attempts,
				base_delay=base_delay,
				max_delay=max_delay,
				**kwargs,
			)

		return wrapper

	return decorator
