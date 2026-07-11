"""Opt-in READ COMMITTED transaction isolation (non-retry deadlock reduction).

Frappe/MariaDB runs at the InnoDB default REPEATABLE READ, whose next-key / gap
locks are a primary source of 1213 deadlocks -- notably ERPNext's stock-ledger
range ``FOR UPDATE`` on ``(item_code, warehouse, posting_datetime > ?)`` (finding
F-004, ~37% of production deadlocks). READ COMMITTED never creates gap locks, so it
removes that whole class while PRESERVING every explicit ``... FOR UPDATE`` record
lock this app relies on for correctness (``lock_bins`` / ``preallocate_series`` /
``get_doc(for_update=True)`` / ``getseries``). It is safe here specifically because
concurrency is enforced by those explicit pessimistic locks, not by the
repeatable-read snapshot.

Frappe exposes no isolation setting for the MariaDB driver (it inherits the server
default), so we pin it per connection with ``SET SESSION`` from the request/job
entry hooks -- covering all web requests + RQ background jobs (~92% of the lock
corpus). ``SET SESSION TRANSACTION ISOLATION LEVEL`` is idempotent and legal with a
transaction open, and applies to subsequent transactions (proven pattern:
``frappe/database/migrate.py`` runs ``set session lock_wait_timeout``).

GATED, default OFF: fires only when site_config ``use_read_committed`` is truthy, so
it can be enabled / rolled back WITHOUT a code change and A/B-soaked. The toggle is
SYMMETRIC: with the flag off, the hook pins the session back to REPEATABLE READ, so
warm/pooled worker connections that ran at READ COMMITTED while the flag was on
revert on their very next request/job instead of staying sticky until a restart.

IMPORTANT interplay with ops: if ops pin ``transaction-isolation = READ-COMMITTED``
in ``my.cnf`` (for airtight coverage of bare ``bench execute`` / console / patches),
KEEP the site flag ON -- with the flag OFF this hook now actively overrides the
server default back to REPEATABLE READ for every request/job.

Trade-off to verify before enabling: READ COMMITTED gives non-repeatable / phantom
reads (a fresh read view per statement, not per transaction). Confirm on a copy
site that stock-ledger balance recompute and reservation-availability reads stay
correct under load before setting ``use_read_committed: 1`` in production.
"""

import frappe


def set_read_committed():
	"""``before_request`` / ``before_job`` hook: pin this connection's session
	isolation per site_config ``use_read_committed`` -- READ COMMITTED when enabled,
	REPEATABLE READ (the InnoDB default) when disabled, so toggling the flag reverts
	warm connections symmetrically.

	Best-effort and silent on failure -- an isolation hint must never break a request
	or a background job. Cost when off: one idempotent SET per request/job."""
	level = (
		"READ COMMITTED" if frappe.conf.get("use_read_committed") else "REPEATABLE READ"
	)
	try:
		frappe.db.sql(f"SET SESSION TRANSACTION ISOLATION LEVEL {level}")
	except Exception:
		frappe.logger("jewellery_erpnext").debug(
			"set_read_committed: SET SESSION isolation failed", exc_info=True
		)
