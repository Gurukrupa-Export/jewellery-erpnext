import random
import time

import frappe
from frappe.exceptions import QueryDeadlockError, QueryTimeoutError


def submit_with_retry(doc, max_attempts=3, base_delay=0.2):
	"""Submit ``doc``, retrying ONLY on InnoDB 1205 lock-wait-timeout.

	Caller MUST guarantee the submit operation is idempotent upstream
	(link-field guard, parent docstatus check, etc.) before invoking
	this helper.

	1205 (QueryTimeoutError) path:
	  Under default ``innodb_rollback_on_timeout=OFF``, MariaDB rolls back
	  only the failed statement; the surrounding transaction stays alive.
	  We rollback to a savepoint, reload the doc, apply exponential
	  backoff with jitter, and retry inside this helper.

	1213 (QueryDeadlockError) path:
	  InnoDB rolls back the entire transaction victim, so no savepoint
	  inside that transaction survives. Continuing to execute statements
	  in the same transaction is unsafe. We log context and re-raise so
	  the OUTER unit-of-work (background worker / HTTP request / bulk
	  action loop) can restart cleanly on a fresh transaction. Idempotency
	  guards upstream ensure the restart cannot duplicate side effects.

	``doc.flags.throw_batch_error`` short-circuits the 1205 retry too so
	bulk-workflow row-level diagnostics surface verbatim.
	"""
	if not doc or not getattr(doc, "name", None):
		doc.submit()
		return doc

	sp = (
		("sub_" + (doc.doctype or "") + "_" + (doc.name or ""))
		.replace(" ", "_")
		.replace("-", "_")[:60]
	)
	last_exc = None
	for attempt in range(1, max_attempts + 1):
		frappe.db.savepoint(sp)
		try:
			doc.submit()
			return doc
		except QueryDeadlockError:
			# 1213: whole transaction was rolled back by InnoDB. Do NOT
			# attempt savepoint-rollback (savepoint is gone) and do NOT
			# continue in the same transaction. Log context and re-raise
			# so the outer boundary restarts on a fresh transaction.
			try:
				frappe.logger("jewellery_erpnext.safe_submit").error(
					f"submit_with_retry: {doc.doctype} {doc.name} attempt {attempt} "
					f"hit QueryDeadlockError (1213); re-raising for outer-tx restart"
				)
			except Exception:
				pass
			raise
		except QueryTimeoutError as exc:
			last_exc = exc
			frappe.db.rollback(save_point=sp)
			if getattr(doc.flags, "throw_batch_error", False):
				raise
			if attempt >= max_attempts:
				raise
			delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
			try:
				frappe.logger("jewellery_erpnext.safe_submit").warning(
					f"submit_with_retry: {doc.doctype} {doc.name} attempt {attempt} "
					f"hit QueryTimeoutError (1205); retrying after {delay:.2f}s"
				)
			except Exception:
				pass
			time.sleep(delay)
			doc = frappe.get_doc(doc.doctype, doc.name)
	if last_exc is not None:
		raise last_exc
	return doc
