# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Row locking and draft reads for process control on branches without the full current-operation guard.

On kggk_uat, ``doc_events/current_operation_guard.py`` provides the MOP-then-MWO lock block that
Employee IR and Department IR take, the snapshot check and the Employee IR draft reads, and
``process_control`` uses those. This branch's guard has only the status helpers, so this module
provides the same five names with the same contracts, built on ``lock_order`` (which this branch
has):

* :func:`_lock_block` locks the operations, then their work orders, sorted (``lock_order`` RULE E),
  within one wait budget per attempt, and reports a busy row as :class:`WorkOrderBusyError`.
* :func:`_refuse_if_snapshot_older` refuses (retry) when an operation changed while this
  transaction waited for it.
* :func:`outstanding_issue_drafts` / :func:`_confirm_drafts`: open Employee IR Issue drafts on
  work orders, from a plain read or (``locking=True``) a current read.

Difference to kggk_uat: Employee IR and Department IR on this branch do not take the lock block
themselves. A Process Transfer still never overwrites their work: their row updates wait for its
locks (and it refuses a job they have already changed), but they do not refuse up front while a
transfer holds the job.
"""

import math
import time

import frappe
from frappe import _
from frappe.utils import cstr, escape_html

from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_manufacturing_operations,
	lock_work_orders,
)

LOCK_DEADLINE_FLAG = "process_control_lock_deadline"
DEFAULT_LOCK_WAIT_SECONDS = 15


class WorkOrderBusyError(frappe.ValidationError):
	"""A row this document needs is being changed by another transaction; try again."""


def _b(value) -> str:
	return frappe.bold(escape_html(cstr(value)))


def _lock_wait():
	"""Seconds an interactive attempt waits for its locks in total (site_config
	``current_operation_lock_wait``, default 15; 0 = do not wait). Background jobs: the server
	default per statement (None)."""
	if getattr(frappe.local, "job", None):
		return None
	value = frappe.conf.get("current_operation_lock_wait")
	if value is None or cstr(value).strip() == "":
		return DEFAULT_LOCK_WAIT_SECONDS
	try:
		wait = int(value)
	except (TypeError, ValueError):
		return DEFAULT_LOCK_WAIT_SECONDS
	return wait if wait >= 0 else DEFAULT_LOCK_WAIT_SECONDS


def _attempt_wait(doc):
	budget = _lock_wait()
	if budget is None or budget <= 0:
		return budget
	flags = doc.flags if doc is not None else frappe._dict()

	def wait():
		now = time.monotonic()
		deadline = flags.get(LOCK_DEADLINE_FLAG)
		if deadline is None:
			deadline = flags[LOCK_DEADLINE_FLAG] = now + budget
		remaining = deadline - now
		return 0 if remaining <= 0 else max(1, math.ceil(remaining))

	return wait


def _busy(entity, names):
	names = sorted({cstr(n) for n in names or () if n})
	frappe.throw(
		_(
			"{0} {1} is being updated by another transaction. Please try again in a minute."
		).format(entity, ", ".join(_b(n) for n in names[:5])),
		exc=WorkOrderBusyError,
		title=_("Work Order Busy"),
	)


def _lock_block(mops, mwos, doc=None):
	"""Lock the operations, then the work orders (theirs plus ``mwos``). Returns
	``(mop_rows, mwo_rows)`` from the locking reads."""
	wait = _attempt_wait(doc)
	mops = [m for m in mops if m]
	try:
		mop_rows = lock_manufacturing_operations(mops, wait=wait)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		busy = getattr(exc, "lock_row_name", None)
		_busy(_("Manufacturing Operation"), [busy] if busy else mops)
	all_mwos = {m for m in mwos if m} | {
		r.manufacturing_work_order
		for r in mop_rows.values()
		if r.manufacturing_work_order
	}
	try:
		mwo_rows = lock_work_orders(all_mwos, wait=wait)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		busy = getattr(exc, "lock_row_name", None)
		_busy(_("Work order"), [busy] if busy else all_mwos)
	return mop_rows, mwo_rows


def _refuse_if_snapshot_older(mop_rows, names):
	"""Refuse (retry) when an operation's ``modified`` differs between the locking read and this
	transaction's snapshot: something committed while this transaction waited."""
	locked = {
		r.name: r
		for key, r in (mop_rows or {}).items()
		if key in set(names or ()) and r.get("name")
	}
	if not locked:
		return
	seen = dict(
		frappe.db.sql(
			"SELECT name, modified FROM `tabManufacturing Operation` WHERE name IN %(names)s",
			{"names": sorted(locked)},
		)
	)
	changed = sorted(n for n, r in locked.items() if seen.get(n) != r.get("modified"))
	if changed:
		frappe.throw(
			_(
				"Manufacturing Operation {0} was changed by another transaction while this one waited "
				"for it. Please try again: the retry reads it afresh."
			).format(", ".join(_b(n) for n in changed)),
			exc=WorkOrderBusyError,
			title=_("Work Order Busy"),
		)


_DRAFTS_SQL = """
	SELECT o.parent AS draft, o.manufacturing_work_order AS mwo, o.idx,
		e.owner, e.creation, o.name AS row_name
	FROM `tabEmployee IR Operation` o
	INNER JOIN `tabEmployee IR` e ON e.name = o.parent
	WHERE o.manufacturing_work_order IN %(mwos)s
		AND o.parenttype = 'Employee IR'
		AND e.docstatus = 0
		AND e.type = 'Issue'
		AND e.name != %(exclude)s
	ORDER BY o.parent, o.idx
"""


def outstanding_issue_drafts(mwos, *, exclude=None, locking=False):
	"""Employee IR Issue drafts holding any of ``mwos``: ``[{draft, mwo, idx, owner, creation,
	row_name}]``. ``locking=True`` reads the latest committed rows (``LOCK IN SHARE MODE``) instead
	of this transaction's snapshot."""
	mwos = sorted({m for m in mwos or () if m})
	if not mwos:
		return []
	query = _DRAFTS_SQL + (" LOCK IN SHARE MODE" if locking else "")
	try:
		return frappe.db.sql(
			query, {"mwos": mwos, "exclude": exclude or ""}, as_dict=True
		)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		_busy(_("Work order"), mwos)


def _confirm_drafts(drafts, mwos):
	"""Snapshot hits re-read as current: only drafts that still exist now are kept."""
	current = {
		(d["draft"], d["mwo"]) for d in outstanding_issue_drafts(mwos, locking=True)
	}
	return [d for d in drafts if (d["draft"], d["mwo"]) in current]
