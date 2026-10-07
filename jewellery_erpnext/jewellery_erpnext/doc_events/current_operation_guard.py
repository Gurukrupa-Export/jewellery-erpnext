"""Work-order current-operation guard for Employee IR and Department IR.

THE RULE
--------
A row of an Employee IR (EIR) or Department IR (DIR) may act on a work order only through the
work order's CURRENT operation, i.e. ``Manufacturing Work Order.manufacturing_operation`` (the
pointer). The pointer must belong to a submitted work order, must have no successor (no MOP names
it as ``previous_mop``) and must be in the state the transaction needs. Every department visit
and every employee cycle mints a new Manufacturing Operation, so the MOP name already identifies
the workflow instance: a draft written for an earlier visit (A -> B -> A) or an earlier cycle in
the same department simply names a MOP that is no longer the pointer. No version field is needed.

The stage is the pointer's department + ``department_ir_status`` -- never ``MWO.department``,
which only Department IR Receive writes and which therefore lags while a transfer is in transit.

WHY IT EXISTS (incident 2026-10-05, kggk-prod)
---------------------------------------------
Employee Issue EMP-IR-Labh-2026-43405 stayed a draft for four days and was then submitted after
its two work orders had moved from Model Making to Final Polish. ``on_submit_issue_new`` wrote
WIP onto the two finished operations and cloned their old balances into unsynced MOP Logs, which
left each work order with two "current" operations. Nothing stopped it:

* the only duplicate check (``validate_duplication_and_gr_wt``) ran from ``before_validate``,
  which returns as soon as ``docstatus != 0`` -- Frappe sets docstatus to 1 before
  ``before_validate`` on every submit path, so it never ran at submit;
* the check was a plain SELECT, so under REPEATABLE READ two overlapping saves could not see
  each other (how the duplicate draft came to exist in the first place);
* Department IR never looked at Employee IR drafts at all.

WHAT IS ENFORCED, AND WHEN
--------------------------
For every save of a new document (or of a draft whose rows / relevant header fields changed), on
every submit path (desk, REST, ``frappe.client``, bulk, the Submission Queue worker) and on
cancel:

* each row's MOP is the pointer of a submitted work order, has no successor and satisfies the
  per-transaction predicate (see ``_row_problems``), mirroring the client filters in
  ``employee_ir.js`` / ``department_ir.js`` exactly;
* no OTHER Employee Issue draft holds the same work order (decision of 2026-10-06: an outstanding
  Employee Issue draft blocks every new EIR and DIR on its work order until it is submitted,
  Discarded or Deleted; the document itself is excluded by name, drafts of other work orders
  never block, and the draft's age is irrelevant);
* cancels may only undo a transition whose effect is still the current state; an Employee Issue
  cancel is also refused while another submitted Employee Issue lists the same operation (legacy
  double issue: only the reviewed repair cancels those), and deletes only the open time log its
  own submit opened (:func:`issue_own_open_time_logs`).

LOCKING (lock_order.py RULE D / RULE E)
---------------------------------------
Every attempt starts with :func:`begin_attempt` -- the first statement of the controller's
``check_if_latest`` (an existing document) or ``before_insert`` (a new one): a save or submit during
the EOD sync or an open stock-reconciliation window is refused there by plain reads, before any
lock below (the ``before_save`` / ``before_submit`` doc_events repeat it, authoritatively).

Decisions come only from locking ("current") reads, because a row lock does not refresh a
REPEATABLE READ snapshot. The document's row MOPs are locked first, then its work orders, each set
sorted, one primary-key ``SELECT ... FOR UPDATE`` per row, taken once, all within the attempt's one
lock budget (:func:`_attempt_wait`):

* a NEW document takes the block in ``before_insert`` -- before ``set_new_name`` locks the shared
  naming-series row, so no request waits on a busy work order while holding that row;
* a submit, or a save of a draft whose rows / predicate fields changed, takes it in the
  controller's ``check_if_latest`` (:func:`lock_before_own_rows`) -- before Frappe loads the
  document's own rows FOR UPDATE -- and decides in ``before_validate``, before validate / QC
  creation / any MOP, MOP Log, time-log or stock side effect;
* an Employee Receive submit -- a saved draft's (``check_if_latest``) and a REST
  insert-and-submit (``before_insert``) alike -- first takes its Tree / Series / Bin pre-locks
  (``employee_ir.take_receive_prelocks``) and then the block, i.e. the Stock Entry order
  Tree -> Series -> Bin -> MOP -> MWO, before Frappe names the document or locks its rows. A
  Receive whose pre-locks were not taken early (warn mode, or ``on_submit_receive`` called
  directly) is checked inside ``on_submit_receive`` right after them
  (``check_after_receive_prelocks``);
* cancels lock their own rows (``check_if_latest``) and trees first, then the block
  (``guard_cancel``);
* a Department IR SAVE also locks each row operation's PREVIOUS operation in the operation phase:
  ``update_previous_mop_data`` back-fills that operation's received weights later in the save, and
  a Manufacturing Operation write after the work-order lock would invert RULE E.

The plain reads used beside the locked rows -- every operation of the work orders, and the
Employee Issue drafts holding them -- come from the transaction's snapshot, which can be older than
the wait for the block. They only NOMINATE: on the locking paths every hit that would refuse is
re-read with a record-only, non-waiting locking read first, and dropped when that read disproves it:

* a later operation (successor) of the current one is re-read by primary key (``LOCK IN SHARE
  MODE NOWAIT``); one an Employee Receive or Department Issue cancel removed meanwhile -- kggk_prod
  deletes the operation it minted, kggk_uat keeps it marked Revert -- no longer counts;
* an Employee Issue draft's row and parent are re-read by primary key (``LOCK IN SHARE MODE SKIP
  LOCKED``); a draft submitted, discarded or moved to another work order meanwhile no longer
  counts. A document another transaction is writing right now (typically that draft's own submit,
  queued behind this block) keeps the snapshot's verdict: it is still a draft until that
  transaction commits. This confirmation can only ever let a transaction go on; the terminal
  re-check below still sees every draft before the commit.

Successors and drafts COMMITTED after the snapshot are never missed: every writer that mints a
successor holds the predecessor's block and moves the pointer, which the block reads fresh, and a
new draft is caught by the terminal re-check below.

The snapshot also feeds the side effects that follow a decision: MOP Log balance clones, weight
recomputes, time logs and tree lookups are plain reads. So a submit or cancel whose row operations
changed between its snapshot and its locking read -- typically it waited for another transaction
writing them -- is refused as busy right after a decision that found nothing wrong
(:func:`_refuse_if_snapshot_older`); its retry reads afresh.

What a cancel writes after the block -- its MOP Log rows, the cancelled Department IR / Stock
Entry rows naming an operation it removes, the casting tree's draft stamps -- is written by
primary key after a plain read (``mop_log.cancel_voucher_mop_logs``,
``lock_order.update_by_primary_key``): none of those filters is indexed on production, and a
filtered UPDATE would lock the whole table until the cancel commits.

Residual deadlock edges remain by design; each resolves as an InnoDB 1213 (one side rolls back
and is retried, nothing is half-written):

* an Employee Receive's metal-injection / loss Stock Entries lock Bins resolved only then (each
  Stock Entry's ``prelock_bins``), after the block; an Employee Receive cancel cancels its Process
  Loss / injection Stock Entries (``prelock_bins_on_cancel``) after the block too;
* Refining Entry (and the work-order split) writes the work order before its operations;
* the terminal draft re-check is the last statement of ``on_update`` / ``on_submit``, but Frappe
  still runs ``save_version``, ``on_change`` and the Server Scripts after it, with the re-check's
  share / gap locks held for those milliseconds;
* the Receive pre-locks (Tree / Series / Bin) are plain ``FOR UPDATE`` and wait the server
  default, outside the block's budget;
* a casting Employee Issue cancel cancels its tree's Stock Entries (Bins) after the block;
* a REST ``PUT`` / ``PATCH`` of a saved Employee / Department IR loads it ``for_update`` (its own
  rows) before the pre-locks and the block, so a concurrent new document of the same work order
  can deadlock with it. The desk and the Submission Queue do not load documents that way.

Draft existence is the one fact a primary-key lock cannot provide. It is checked twice: a plain
read right after the block (fast feedback, confirmed as above), and a NOWAIT ``LOCK IN SHARE MODE``
re-check as the last statement of ``on_update`` (saves) / ``on_submit`` (submits). The re-check
never waits; a hit rolls the whole transaction back. Frappe still writes the Version row and runs
``on_change`` hooks after it in the same transaction, so its share / gap locks are held for those
too; none of them waits on Employee IR rows, so that stays milliseconds. Its first statement is
COVERED by the work-order index, so it only conflicts with rows being inserted, deleted or moved;
rows another transaction holds (an Employee IR re-saved without reference changes, a discard, a
cancel: Frappe's ``check_if_latest`` loads a document's rows FOR UPDATE) are skipped and judged by
this transaction's snapshot, and only a row that may be another Issue draft's makes the re-check
report the work order busy -- a retry, never a wrong decision (see :func:`_terminal_drafts`).

KILL SWITCH
-----------
``site_config.json`` ``"current_operation_guard"``:

* anything but ``"warn"`` (the default): enforce, as described above;
* ``"warn"``: saves and submits get the PRE-GUARD locking behaviour. No Manufacturing Operation /
  Work Order lock and no NOWAIT re-check is taken, and an Employee Receive takes its Tree / Series
  / Bin pre-locks inside ``on_submit_receive`` as it did before the guard. The rules are still
  evaluated (plain reads) and every violation -- including the manual-reopen block on a
  Manufacturing Operation save -- becomes an Error Log plus an orange message instead of an
  error. Use it only to unblock an unforeseen legitimate flow, or to relieve a lock problem, while
  a code fix is prepared: in warn mode the incident can recur.

Not affected by the switch: the cancel guards (they always lock and always enforce), the EOD
hold of a work order with a stale open operation, and the Receive voucher resolver's refusal of an
operation issued by two submitted Issues.

``site_config.json`` ``"current_operation_lock_wait"``: how many seconds an interactive save /
submit / cancel waits for busy work orders IN TOTAL (default 15; ``0`` = do not wait): one budget
per document attempt, shared by every row of the block, not 15 s per row. Background jobs (the
Submission Queue worker) keep the server's ``innodb_lock_wait_timeout`` per statement. It changes
nothing for the non-waiting reads or for deadlocks, which are reported as busy at once.
"""

import math
import time

import frappe
from frappe import _
from frappe.utils import cint, cstr, escape_html, get_datetime

from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_manufacturing_operations,
	lock_work_orders,
)

OPEN_STATUSES = ("Not Started", "On Hold", "WIP", "QC Pending", "QC Completed")
CLOSED_STATUSES = ("Finished", "Revert")
RECEIVABLE_STATUSES = ("On Hold", "WIP", "QC Completed")
ISSUE_BLOCKED_TRANSIT = ("In-Transit", "Revert")
EIR_CANCELLABLE_ISSUE_STATUSES = ("WIP", "QC Pending", "QC Completed", "On Hold")
REVERT = "Revert"


def is_reverted(status, department_ir_status=None):
	"""True for an operation kept only as history.

	kggk_uat does not delete the operation a cancelled Department Issue / Employee Receive minted:
	it marks it ``department_ir_status = "Revert"`` (status Not Started) and moves the work order
	back to the source. Every picker already skips those rows, and the audit
	(``mop_lineage_audit._co_reverted``) treats them the same way: never a live operation and
	never a successor.
	"""
	return cstr(status) == REVERT or cstr(department_ir_status) == REVERT


def is_open_operation(status, department_ir_status=None):
	"""True for an operation that is still live work: an open status and not reverted."""
	return cstr(status) in OPEN_STATUSES and not is_reverted(
		status, department_ir_status
	)


EIR_ISSUE = "eir_issue"
EIR_RECEIVE = "eir_receive"
DIR_ISSUE = "dir_issue"
DIR_RECEIVE = "dir_receive"

# Header fields whose change re-runs the save-time check on an existing draft.
PREDICATE_HEADER_FIELDS = {
	"Employee IR": (
		"type",
		"company",
		"department",
		"operation",
		"employee",
		"subcontracting",
		"subcontractor",
	),
	"Department IR": (
		"type",
		"company",
		"current_department",
		"next_department",
		"receive_against",
	),
}

CHILD_TABLE_FIELD = {
	"Employee IR": "employee_ir_operations",
	"Department IR": "department_ir_operation",
}

EIR_OPERATION_TABLE = "tabEmployee IR Operation"
EIR_OPERATION_MWO_COLUMN = "manufacturing_work_order"

# Columns of every operation of a work order (the plain "family" read and its locking re-read).
FAMILY_FIELDS = (
	"name",
	"manufacturing_work_order",
	"previous_mop",
	"status",
	"department",
	"department_ir_status",
	"operation",
	"creation",
)

DEFAULT_LOCK_WAIT_SECONDS = 15
REVIEWED_REPAIR_FLAG = "current_operation_reviewed_repair"
# doc.flags key: what this save / submit attempt decided under the lock block.
GUARDED_FLAG = "current_operation_guarded"
# doc.flags key set by employee_ir.take_receive_prelocks: this submit attempt already holds its
# Tree / Series / Bin pre-locks, so the block may be taken now.
RECEIVE_PRELOCKS_FLAG = "receive_prelocks_taken"
# doc.flags key: the monotonic-clock deadline of this attempt's lock budget (see _attempt_wait).
LOCK_DEADLINE_FLAG = "current_operation_lock_deadline"

# An Employee Issue opens its time log moments after its submit (``issue_submitted_on``): the
# opener of an open time log is the submitted Issue closest to its ``from_time`` inside
# [-5 s, +10 min]. Same numbers as ``mop_lineage_audit._CO_TIME_LOG_EARLY`` /
# ``_CO_TIME_LOG_WINDOW`` (a test pins them together), so the cancel and the repair manifests
# attribute every row the same way.
TIME_LOG_OPENER_EARLY_SECONDS = 5
TIME_LOG_OPENER_WINDOW_SECONDS = 600

# Messages: names listed per sentence, and problem lines per error, before "(+N more)".
NAME_LIST_CAP = 10
MAX_MESSAGE_LINES = 25


class CurrentOperationError(frappe.ValidationError):
	"""A transaction would act on something other than the work order's current operation."""


class StaleOperationError(CurrentOperationError):
	"""The row's operation is no longer the work order's current operation."""


class OutstandingDraftError(CurrentOperationError):
	"""Another Employee Issue draft already holds the work order."""


class AmbiguousOperationError(CurrentOperationError):
	"""The work order has no single valid current operation (legacy inconsistent data)."""


class HistoryRewriteError(CurrentOperationError):
	"""A cancel or edit would reopen an operation the work order has already moved past."""


class WorkOrderBusyError(CurrentOperationError):
	"""Another transaction holds the work order right now; retrying shortly will work."""


# Severity order used when several problems are raised at once.
_SEVERITY = (
	AmbiguousOperationError,
	HistoryRewriteError,
	StaleOperationError,
	OutstandingDraftError,
	CurrentOperationError,
)


# ----------------------------------------------------------------------------------------------
# document helpers
# ----------------------------------------------------------------------------------------------


def profile(doc):
	"""``eir_issue`` / ``eir_receive`` / ``dir_issue`` / ``dir_receive`` (``None`` otherwise)."""
	if doc.doctype == "Employee IR":
		return EIR_ISSUE if doc.get("type") == "Issue" else EIR_RECEIVE
	if doc.doctype == "Department IR":
		return DIR_ISSUE if doc.get("type") == "Issue" else DIR_RECEIVE
	return None


def _rows(doc):
	return doc.get(CHILD_TABLE_FIELD[doc.doctype]) or []


def refs(doc):
	"""``[(idx, mop, mwo)]`` for every row of the document."""
	return [
		(
			row.get("idx"),
			cstr(row.get("manufacturing_operation")).strip(),
			cstr(row.get("manufacturing_work_order")).strip(),
		)
		for row in _rows(doc)
	]


def refs_changed(doc):
	"""True for a new document, or when rows or predicate header fields changed since load."""
	if doc.is_new():
		return True
	before = doc.get_doc_before_save()
	if not before:
		return True
	for field in PREDICATE_HEADER_FIELDS.get(doc.doctype, ()):
		if cstr(doc.get(field)) != cstr(before.get(field)):
			return True
	now = sorted((mop, mwo) for _idx, mop, mwo in refs(doc))
	then = sorted((mop, mwo) for _idx, mop, mwo in refs(before))
	return now != then


def _old_ref_mwos(doc):
	"""Work orders the saved version of the document referenced (rows may have moved)."""
	if doc.is_new():
		return set()
	before = doc.get_doc_before_save()
	if not before:
		return set()
	return {mwo for _idx, _mop, mwo in refs(before) if mwo}


def _same(a, b):
	return cstr(a).strip().casefold() == cstr(b).strip().casefold()


def _b(value):
	return frappe.bold(escape_html(cstr(value))) if cstr(value) else _("(blank)")


def _name_list(names, cap=NAME_LIST_CAP):
	"""``A, B, C (+N more)``: at most ``cap`` names, bold, in the given order."""
	names = [n for n in names if n]
	text = ", ".join(_b(n) for n in names[:cap])
	if len(names) > cap:
		text += " " + _("(+{0} more)").format(len(names) - cap)
	return text


def _mode():
	return cstr(frappe.conf.get("current_operation_guard") or "enforce").strip().lower()


def enforcing():
	"""False only in the kill switch's warn mode (see KILL SWITCH in the module docstring)."""
	return _mode() != "warn"


def _lock_wait():
	"""Seconds an interactive attempt waits for its lock block in total (its budget, see
	:func:`_attempt_wait`); ``None`` (the server default, per statement) for background jobs.

	``current_operation_lock_wait`` in site_config: unset / blank -> 15; ``0`` -> do not wait
	(NOWAIT); a positive integer -> that many seconds. Anything else (negative, not a number)
	falls back to 15 rather than failing every save.
	"""
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


def _clock():
	"""Monotonic seconds (test seam for the lock budget)."""
	return time.monotonic()


def _attempt_wait(doc=None):
	"""The ``wait`` this attempt's guarded locking reads use (``lock_order`` RULE E).

	ONE budget per document attempt (``_lock_wait()`` seconds), not per row: the operation phase
	and the work-order phase of the block -- and any later guarded lock of the same attempt --
	share one deadline, started by the attempt's first guarded locking statement. Each statement
	waits ``max(1, ceil(remaining))`` seconds (MariaDB's WAIT takes whole seconds); once the
	deadline has passed, NOWAIT -- a free row is still taken, a held one fails at once and the
	caller reports the row it could not get as busy. A lock this transaction already holds never
	waits, so a re-entrant second pass costs nothing.

	The deadline lives on ``doc.flags`` and :func:`begin_attempt` clears it, so a bulk action that
	saves several documents in one request gives each its own budget. Background jobs keep the
	server's ``innodb_lock_wait_timeout`` per statement (``None``); ``0`` means NOWAIT throughout.
	Without ``doc`` the budget covers this one call.
	"""
	budget = _lock_wait()
	if budget is None or budget <= 0:
		return budget
	flags = doc.flags if doc is not None else frappe._dict()

	def wait():
		now = _clock()
		deadline = flags.get(LOCK_DEADLINE_FLAG)
		if deadline is None:
			deadline = flags[LOCK_DEADLINE_FLAG] = now + budget
		remaining = deadline - now
		if remaining <= 0:
			return 0
		return max(1, math.ceil(remaining))

	return wait


def begin_attempt(doc, stored_docstatus=None):
	"""First statement of every save / submit / cancel attempt of an Employee IR / Department IR:
	the controller's ``check_if_latest`` for an existing document (``stored_docstatus`` = its
	stored docstatus, a plain read) and ``before_insert`` for a new one. Before any lock:

	* the previous attempt's lock budget is forgotten (:func:`_attempt_wait`);
	* a save or submit during the EOD sync or an open stock-reconciliation window is refused
	  (:func:`refuse_if_movement_blocked`).
	"""
	doc.flags.pop(LOCK_DEADLINE_FLAG, None)
	refuse_if_movement_blocked(doc, stored_docstatus)


def refuse_if_movement_blocked(doc, stored_docstatus=None):
	"""Refuse a save or submit while stock movement is frozen -- before any lock is taken.

	The same two validators the ``before_save`` / ``before_submit`` doc_events run
	(``hooks.py``), which stay the authoritative check: the EOD sync lock
	(``eod_lock.validate_not_eod_sync_locked``) and an open stock-reconciliation window
	(``stock_recon_window.validate_stock_movement_allowed``). Those hooks only run after
	``check_if_latest`` / ``before_insert``, where a submit takes its Tree / Series / Bin pre-locks
	and the MOP / MWO block: a doomed submit (typically a queued one, whose worker waits the server
	default per row) would first wait on -- and hold -- rows the EOD run is writing, and could end
	as "busy, retry" instead of the EOD message. Both are plain reads with their own bypass flags
	(``in_eod_mop_sync``, ``in_stock_recon_window_sync``); like the doc_events they do not run
	under ``flags.ignore_validate``.

	Only for what the doc_events refuse at save / submit: a new document, or a stored DRAFT being
	saved or submitted. A discard, an update after submit and a cancel are left alone (a cancel's
	``before_cancel`` doc_event runs before ``on_cancel`` takes any guard or tree lock). A
	whitelisted method called on a saved draft from the form (``run_doc_method``, which calls
	``check_if_latest``) is refused too while the window is on: nothing can be saved then anyway.
	"""
	if doc.flags.get("ignore_validate"):
		return
	if not doc.is_new():
		if getattr(doc, "_action", None) == "discard":
			return
		if (
			stored_docstatus is None
			or cint(stored_docstatus) != 0
			or cint(doc.docstatus) not in (0, 1)
		):
			return
	from jewellery_erpnext.jewellery_erpnext import stock_recon_window
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import eod_lock

	eod_lock.validate_not_eod_sync_locked(doc)
	stock_recon_window.validate_stock_movement_allowed(doc)


def _reviewed_repair(doc):
	allowed = frappe.flags.get(REVIEWED_REPAIR_FLAG) or set()
	return (doc.doctype, doc.name) in allowed


def _after_locks(doc, phase):
	"""Test seam: called right after the lock block is held. Does nothing in production."""


# ----------------------------------------------------------------------------------------------
# error reporting
# ----------------------------------------------------------------------------------------------


def _raise(problems, doc, always=False):
	"""Raise one aggregated, single-line error for ``[(exc_class, message)]``.

	The Submission Queue banner shows only the last line of a traceback, so rows are joined
	with ``<br>`` rather than newlines. In warn mode the problems are logged and shown as an
	orange message instead -- unless ``always`` (cancel guards), which ignores the kill switch.
	"""
	if not problems:
		return
	exc = next(
		(cls for cls in _SEVERITY if any(p[0] is cls for p in problems)),
		CurrentOperationError,
	)
	seen = set()
	messages = []
	for _cls, message in problems:
		if message not in seen:
			seen.add(message)
			messages.append(message)
	if len(messages) > MAX_MESSAGE_LINES:
		hidden = len(messages) - MAX_MESSAGE_LINES
		messages = messages[:MAX_MESSAGE_LINES] + [
			_("... and {0} more problem(s).").format(hidden)
		]
	text = "<br>".join(messages)

	if always or _mode() != "warn":
		frappe.throw(text, exc=exc, title=_("Work Order Current Operation"))

	frappe.log_error(
		title=_("Current operation guard (warn mode): {0} {1}").format(
			doc.doctype, doc.name or ""
		),
		message=text,
		reference_doctype=doc.doctype,
		reference_name=doc.name if not doc.is_new() else None,
	)
	frappe.msgprint(text, indicator="orange", title=_("Work Order Current Operation"))


def _busy(entity, names, *, plural=None, detail=None):
	"""Raise ``WorkOrderBusyError`` naming the busy record.

	``names`` is the record that could not be locked or -- when one statement covered several
	rows and the busy one is unknown -- every candidate (capped, under ``plural``).
	"""
	if isinstance(names, str):
		names = [names]
	names = sorted({cstr(n) for n in names or () if n})
	retry = _(
		"is being updated by another transaction (for example a queued Employee IR "
		"submission). Please try again in a minute."
	)
	if len(names) == 1:
		subject = f"{entity} {_b(names[0])}" + (f" {detail}" if detail else "")
		message = f"{subject} {retry}"
	else:
		message = _("One of {0} ({1}) {2}").format(
			plural or entity, _name_list(names), retry
		)
	frappe.throw(message, exc=WorkOrderBusyError, title=_("Work Order Busy"))


def _locked_row(exc):
	"""The row ``lock_order._lock_rows_by_name`` was waiting for when ``exc`` was raised."""
	return getattr(exc, "lock_row_name", None)


def _work_order_hint(mop):
	"""``(work order X)`` for a busy operation's message; a plain read, message detail only."""
	if not mop:
		return None
	try:
		mwo = frappe.db.get_value(
			"Manufacturing Operation", mop, "manufacturing_work_order"
		)
	except Exception:
		return None
	return _("(work order {0})").format(_b(mwo)) if mwo else None


# ----------------------------------------------------------------------------------------------
# reads
# ----------------------------------------------------------------------------------------------


def _lock_block(mops, mwos, doc=None):
	"""Lock row MOPs, then work orders (RULE E). Returns ``(mop_rows, mwo_rows)``.

	The work-order set is the given MWOs plus the work orders of the locked MOPs. Both phases
	wait within the attempt's one budget (:func:`_attempt_wait`; ``doc`` carries it). A busy row
	is reported by name (``lock_order`` records which one the failed statement was waiting for).
	"""
	wait = _attempt_wait(doc)
	try:
		mop_rows = lock_manufacturing_operations(mops, wait=wait)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		busy = _locked_row(exc)
		_busy(
			_("Manufacturing Operation"),
			busy or mops,
			plural=_("these Manufacturing Operations"),
			detail=_work_order_hint(busy),
		)

	all_mwos = set(m for m in mwos if m)
	all_mwos.update(
		r.manufacturing_work_order
		for r in mop_rows.values()
		if r.manufacturing_work_order
	)
	try:
		mwo_rows = lock_work_orders(all_mwos, wait=wait)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		_busy(
			_("Work order"),
			_locked_row(exc) or all_mwos,
			plural=_("these work orders"),
		)
	return mop_rows, mwo_rows


def _refuse_if_snapshot_older(mop_rows, names):
	"""``WorkOrderBusyError`` when an operation of ``names`` changed between this transaction's
	snapshot and its locking read.

	The block refreshes nothing: under REPEATABLE READ the snapshot is fixed by the transaction's
	first plain read, which on every path comes before the block (``check_if_latest`` reads the
	stored document; the Submission Queue worker's transaction starts right after it commits the
	job id). A submit or cancel that WAITED for an operation another transaction was writing --
	a Stock Entry's MOP Log bridge rewrites its buckets, an Issue / Receive moves it -- decides
	from the fresh locked row, but then clones MOP Log balances, recomputes weights and opens or
	closes time logs from the old snapshot, silently dropping what that transaction wrote. Every
	such writer updates the operation's ``modified``; so the locked rows' ``modified`` is compared
	with a plain read of the same rows, and any difference is refused as busy: the retry starts a
	fresh snapshot. Rows this transaction wrote itself read back as written (no false hit); a
	transaction without a snapshot yet takes it here, after the locks. Called only after a
	locking decision found nothing to refuse (submit and cancel; a draft save clones nothing).
	"""
	locked = {}
	for key in {n for n in names or () if n}:
		row = mop_rows.get(key)
		# keyed by the locked row's own spelling: the requested key may differ in case
		if row is not None and row.get("name"):
			locked[row.name] = row
	if not locked:
		return
	seen = {
		r[0]: r[1]
		for r in frappe.db.sql(
			"SELECT name, modified FROM `tabManufacturing Operation` WHERE name IN %(names)s",
			{"names": sorted(locked)},
		)
	}
	changed = sorted(
		name for name, row in locked.items() if seen.get(name) != row.get("modified")
	)
	if not changed:
		return
	retry = _("Please try again: the retry reads it afresh.")
	if len(changed) == 1:
		hint = _work_order_hint(changed[0])
		message = _(
			"Manufacturing Operation {0} was changed by another transaction while this one "
			"waited for it. {1}"
		).format(_b(changed[0]) + (f" {hint}" if hint else ""), retry)
	else:
		message = _(
			"Manufacturing Operations {0} were changed by another transaction while this one "
			"waited for them. {1}"
		).format(_name_list(changed), retry)
	frappe.throw(message, exc=WorkOrderBusyError, title=_("Work Order Busy"))


def _plain_rows(mops, mwos):
	"""Same shape as ``_lock_block`` but with plain reads (preflight / advisory checks)."""
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		MANUFACTURING_OPERATION_LOCK_FIELDS,
		WORK_ORDER_LOCK_FIELDS,
	)

	mop_rows = {}
	names = sorted(m for m in set(mops) if m)
	if names:
		for row in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", names]},
			fields=list(MANUFACTURING_OPERATION_LOCK_FIELDS),
			limit_page_length=0,
		):
			mop_rows[row.name] = row
	all_mwos = set(m for m in mwos if m)
	all_mwos.update(
		r.manufacturing_work_order
		for r in mop_rows.values()
		if r.manufacturing_work_order
	)
	mwo_rows = {}
	if all_mwos:
		for row in frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", sorted(all_mwos)]},
			fields=list(WORK_ORDER_LOCK_FIELDS),
			limit_page_length=0,
		):
			mwo_rows[row.name] = row
	return mop_rows, mwo_rows


def _family(mwos):
	"""Plain read of every MOP of the given work orders, keyed by MWO (existing index).

	Used for the successor check, open siblings and message details. The snapshot can be older
	than the lock block, so on the locking paths its successor hits are confirmed by
	:func:`_confirm_successors` before they count. A successor this snapshot MISSES needs no
	such care: every writer that mints one holds the predecessor's lock block and moves the
	pointer in the same transaction, so the fresh (locked) pointer already reflects it.
	"""
	family = {}
	mwos = sorted(m for m in set(mwos) if m)
	if not mwos:
		return family
	for row in frappe.get_all(
		"Manufacturing Operation",
		filters={"manufacturing_work_order": ["in", mwos]},
		fields=list(FAMILY_FIELDS),
		limit_page_length=0,
	):
		family.setdefault(row.manufacturing_work_order, []).append(row)
	return family


def _successors(family_rows, mops):
	"""Operations naming one of ``mops`` as their ``previous_mop`` -- reverted history excluded."""
	return [
		r
		for r in family_rows or []
		if r.previous_mop in mops and not is_reverted(r.status, r.department_ir_status)
	]


def _has_successor(family_rows, mop):
	return bool(_successors(family_rows, {mop}))


def _confirm_successors(family, mops):
	"""``family`` with every successor of ``mops`` re-read by a current read.

	The plain family read may show an operation that a cancel removed after this transaction's
	snapshot was taken (an Employee Receive / Department Issue cancel moves the pointer back and
	deletes the operation it minted -- kggk_uat keeps it, marked Revert): that "phantom" would
	report a false AmbiguousOperationError / HistoryRewriteError. Candidates are re-read by
	primary key with a record-only ``LOCK IN SHARE MODE NOWAIT`` (RULE E: never wait while holding
	the block); a row that is gone is dropped, the others are replaced by their fresh version (a
	Revert one then no longer counts as a successor). Only called on the locking paths, after the
	block is held.
	"""
	mops = {m for m in mops if m}
	candidates = sorted(
		{r.name for rows in family.values() for r in _successors(rows, mops)}
	)
	if not candidates:
		return family
	try:
		fresh = {
			r.name: r
			for r in frappe.db.sql(
				f"""
				SELECT {", ".join(f"`{f}`" for f in FAMILY_FIELDS)}
				FROM `tabManufacturing Operation`
				WHERE name IN %(names)s
				LOCK IN SHARE MODE NOWAIT
				""",
				{"names": candidates},
				as_dict=True,
			)
		}
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		_busy(
			_("Manufacturing Operation"),
			candidates,
			plural=_("these Manufacturing Operations"),
		)
	wanted = set(candidates)
	confirmed = {}
	for mwo, rows in family.items():
		kept = []
		for row in rows:
			if row.name in wanted:
				row = fresh.get(row.name)
				if row is None:
					continue  # deleted after this transaction's snapshot
			kept.append(row)
		confirmed[mwo] = kept
	return confirmed


def _previous_operations(mops):
	"""The previous operation of each row operation, for a Department IR save's lock block.

	A plain read is exact here: ``previous_mop`` is set when an operation is created and never
	changes, and a document can only name operations committed before its request started.
	"""
	names = sorted({m for m in mops if m})
	if not names:
		return []
	previous = {
		r.previous_mop
		for r in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", names]},
			fields=["previous_mop"],
			limit_page_length=0,
		)
		if r.previous_mop
	}
	return sorted(previous - set(names))


def _draft_index_name():
	"""Name of the single-column index on ``manufacturing_work_order`` (``search_index`` in the
	Employee IR Operation JSON), or ``None``. Frappe 16 names it ``<field>_index``, older versions
	used the bare column name, so resolve it by column rather than by a hard-coded name.
	"""
	try:
		index = frappe.db.get_column_index(
			EIR_OPERATION_TABLE, EIR_OPERATION_MWO_COLUMN
		)
	except Exception:
		return None
	return index.get("Key_name") if index else None


_logged_missing_index = False


def outstanding_issue_drafts(mwos, *, exclude=None, locking=False):
	"""Employee Issue drafts (other than ``exclude``) holding any of ``mwos``.

	Returns ``[{"draft", "mwo", "idx", "owner", "creation", "row_name"}]`` (``row_name`` is the
	Employee IR Operation row).

	``locking=True`` is the terminal re-check (:func:`_terminal_drafts`): locking reads that
	see the latest committed rows even under an old REPEATABLE READ snapshot and never wait.
	Without the index the locking variant would lock the whole table, so it falls back to a
	plain read (logged once per process) instead.

	The plain variant reads the snapshot: callers holding the lock block confirm its hits with
	:func:`_confirm_drafts` before refusing anything.
	"""
	global _logged_missing_index

	mwos = sorted(m for m in set(mwos or []) if m)
	if not mwos:
		return []
	exclude = exclude or ""

	index_name = _draft_index_name() if locking else None
	if locking and not index_name:
		if not _logged_missing_index:
			_logged_missing_index = True
			frappe.log_error(
				title=_("Current operation guard: Employee IR Operation index missing"),
				message=_(
					"Index `manufacturing_work_order` on `tabEmployee IR Operation` is missing; the "
					"terminal draft re-check fell back to a plain read. Run bench migrate."
				),
			)
		locking = False

	if not locking:
		rows = frappe.db.sql(
			"""
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
			""",
			{"mwos": mwos, "exclude": exclude},
			as_dict=True,
		)
		return rows

	try:
		return _terminal_drafts(mwos, exclude, index_name)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		# 1020 under snapshot isolation (mapped to a deadlock) on any of its locking reads
		_busy(_("Work order"), mwos, plural=_("this document's work orders"))


def _terminal_drafts(mwos, exclude, index_name):
	"""The terminal re-check: every Employee Issue draft (other than ``exclude``) holding one of
	``mwos`` as COMMITTED -- including drafts committed after this transaction's snapshot -- and
	``WorkOrderBusyError`` when that cannot be told without waiting. Never waits (RULE E).

	1. ``SELECT name ... FORCE INDEX (manufacturing_work_order) ... LOCK IN SHARE MODE NOWAIT``:
	   the names of every row on these work orders. The read is COVERED by the index, so it locks
	   index entries only: it fails (busy) only while another transaction inserts, deletes or moves
	   a row of these work orders, and from here on nobody can until this transaction ends.
	2. Those rows by primary key, ``SKIP LOCKED``. A row another transaction is writing right now
	   is skipped (Frappe's ``check_if_latest`` loads a document's rows FOR UPDATE: a re-save
	   without reference changes, a discard, a cancel). A skipped row counts only if this
	   transaction's snapshot cannot rule it out: unknown there, or an Issue draft's row there
	   (docstatus only ever grows, so a row that was not a draft row then is not one now) ->
	   busy, a retry. Rows of a Receive being written never refuse the transaction holding the
	   block.
	3. The parents of the remaining draft rows by primary key, ``SKIP LOCKED``; a parent being
	   written right now keeps its snapshot verdict (an Issue draft then is a draft until that
	   write commits), and one the snapshot does not know is busy.
	"""
	try:
		names = [
			r[0]
			for r in frappe.db.sql(
				f"""
				SELECT name FROM `{EIR_OPERATION_TABLE}` FORCE INDEX (`{index_name}`)
				WHERE manufacturing_work_order IN %(mwos)s
				LOCK IN SHARE MODE NOWAIT
				""",
				{"mwos": mwos},
			)
		]
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		# One statement over every work order: which row was busy is unknown.
		_busy(_("Work order"), mwos, plural=_("this document's work orders"))
	if not names:
		return []

	rows = {
		r.name: r
		for r in frappe.db.sql(
			f"""
			SELECT name, parent, parenttype, parentfield, manufacturing_work_order, idx, docstatus
			FROM `{EIR_OPERATION_TABLE}`
			WHERE name IN %(names)s
			LOCK IN SHARE MODE SKIP LOCKED
			""",
			{"names": sorted(names)},
			as_dict=True,
		)
	}
	skipped = sorted(set(names) - set(rows))
	if skipped:
		then = {
			r.name: r
			for r in frappe.db.sql(
				f"""
				SELECT o.name, o.parent, o.parenttype, o.parentfield, o.docstatus,
					e.type, e.docstatus AS parent_docstatus
				FROM `{EIR_OPERATION_TABLE}` o
				LEFT JOIN `tabEmployee IR` e ON e.name = o.parent
				WHERE o.name IN %(names)s
				""",
				{"names": skipped},
				as_dict=True,
			)
		}
		if any(_may_be_a_draft_row(then.get(n), exclude) for n in skipped):
			_busy(_("Work order"), mwos, plural=_("this document's work orders"))

	candidates = [r for r in rows.values() if _is_draft_row(r, exclude)]
	parents = sorted({r.parent for r in candidates})
	if not parents:
		return []
	heads = {
		h.name: h
		for h in frappe.db.sql(
			"""
			SELECT name, type, docstatus, owner, creation
			FROM `tabEmployee IR`
			WHERE name IN %(parents)s
			LOCK IN SHARE MODE SKIP LOCKED
			""",
			{"parents": parents},
			as_dict=True,
		)
	}
	locked = [p for p in parents if p not in heads]
	if locked:
		then = {
			h.name: h
			for h in frappe.db.sql(
				"""
				SELECT name, type, docstatus, owner, creation
				FROM `tabEmployee IR`
				WHERE name IN %(parents)s
				""",
				{"parents": locked},
				as_dict=True,
			)
		}
		if any(p not in then for p in locked):
			_busy(_("Work order"), mwos, plural=_("this document's work orders"))
		heads.update(then)

	drafts = []
	for child in sorted(candidates, key=lambda r: (r.parent, r.idx or 0)):
		head = heads.get(child.parent)
		if not head or head.type != "Issue" or int(head.docstatus or 0) != 0:
			continue
		drafts.append(
			frappe._dict(
				draft=child.parent,
				mwo=child.manufacturing_work_order,
				idx=child.idx,
				owner=head.owner,
				creation=head.creation,
				row_name=child.name,
			)
		)
	return drafts


def _is_draft_row(row, exclude):
	"""A draft row of another Employee IR's operations table."""
	return bool(
		row
		and row.parenttype == "Employee IR"
		and row.parentfield == "employee_ir_operations"
		and int(row.docstatus or 0) == 0
		and row.parent != exclude
	)


def _may_be_a_draft_row(then, exclude):
	"""Can a row another transaction is writing right now be another Issue draft's row?
	``then``: the row and its parent as this transaction's snapshot shows them (``None`` =
	unknown there). Docstatus never goes back to 0, so a row that was not a draft row then is
	not one now."""
	if then is None:
		return True
	return _is_draft_row(then, exclude) and (
		then.type in (None, "Issue") and int(then.parent_docstatus or 0) == 0
	)


def _confirm_drafts(drafts, mwos):
	"""The plain-read draft hits a current read does not disprove (locking paths only).

	The plain read in ``_check`` sees the transaction's snapshot, which can predate the wait for
	the lock block: a draft submitted, discarded, deleted or moved to another work order in the
	meantime would refuse a valid transaction ("already on Employee Issue X (Draft...)" for an X
	that is already submitted). Each hit is re-read with record-only locking reads that never wait
	(RULE E): its Employee IR Operation row and its parent, by primary key, ``LOCK IN SHARE MODE
	SKIP LOCKED``. A row or parent another transaction is writing right now is skipped:

	* row read: no longer docstatus 0, or no longer on one of ``mwos`` -> dropped;
	* parent read: no longer a draft Issue -> dropped;
	* row skipped (or gone) but parent read: nobody is writing that document now -- every writer
	  of its rows locks the parent first (save / submit / discard: ``check_if_latest`` takes
	  parent and rows FOR UPDATE; delete: the parent, NOWAIT) -- so the row is gone (the draft's
	  rows were replaced or removed) -> dropped;
	* row and parent both skipped: the document is being written right now (typically that
	  draft's own submit, queued behind this block, which locked its rows in ``check_if_latest``)
	  or was deleted: the snapshot's verdict stands -- it is still a draft until that commits.

	Dropping can only err towards letting the transaction go on, and ``final_draft_check``
	re-reads every draft row of these work orders with NOWAIT locks before the commit, so a draft
	is never missed; keeping errs towards a refusal the user can retry. Primary-key reads of
	existing rows: record locks only.
	"""
	mwos = {m for m in mwos if m}
	rows = sorted({d.get("row_name") for d in drafts if d.get("row_name")})
	if not rows:
		return list(drafts)
	try:
		return _confirmed_drafts(drafts, mwos, rows)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		# SKIP LOCKED never waits; this is 1020 under snapshot isolation (mapped to a deadlock)
		_busy(
			_("Work order"),
			sorted({d.mwo for d in drafts}),
			plural=_("this document's work orders"),
		)


def _confirmed_drafts(drafts, mwos, rows):
	children = {
		c.name: c
		for c in frappe.db.sql(
			f"""
			SELECT name, parent, parenttype, manufacturing_work_order, idx, docstatus
			FROM `{EIR_OPERATION_TABLE}`
			WHERE name IN %(rows)s
			LOCK IN SHARE MODE SKIP LOCKED
			""",
			{"rows": rows},
			as_dict=True,
		)
	}
	heads = {
		h.name: h
		for h in frappe.db.sql(
			"""
			SELECT name, type, docstatus, owner, creation
			FROM `tabEmployee IR`
			WHERE name IN %(parents)s
			LOCK IN SHARE MODE SKIP LOCKED
			""",
			{"parents": sorted({d.draft for d in drafts if d.get("row_name")})},
			as_dict=True,
		)
	}
	confirmed = []
	for d in drafts:
		if not d.get("row_name"):
			confirmed.append(d)
			continue
		child, head = children.get(d.row_name), heads.get(d.draft)
		if head is not None and (head.type != "Issue" or int(head.docstatus or 0) != 0):
			continue  # submitted, discarded or no longer an Issue
		if child is None:
			if head is not None:
				continue  # its row is gone: the draft no longer holds this work order
			confirmed.append(
				d
			)  # being written right now: still a draft until it commits
			continue
		if (
			child.parenttype != "Employee IR"
			or int(child.docstatus or 0) != 0
			or child.manufacturing_work_order not in mwos
		):
			continue  # submitted, discarded, or moved to another work order
		confirmed.append(
			frappe._dict(
				draft=child.parent,
				mwo=child.manufacturing_work_order,
				idx=child.idx,
				owner=head.owner if head is not None else d.get("owner"),
				creation=head.creation if head is not None else d.get("creation"),
				row_name=child.name,
			)
		)
	return confirmed


# ----------------------------------------------------------------------------------------------
# rule evaluation
# ----------------------------------------------------------------------------------------------


def _pointer_text(mwo_row, mop_row_map, family_rows):
	"""Human description of the work order's current operation for messages."""
	pointer = mwo_row.manufacturing_operation if mwo_row else None
	if not pointer:
		return _("no current operation")
	info = mop_row_map.get(pointer) or next(
		(r for r in family_rows or [] if r.name == pointer), None
	)
	if not info:
		return _b(pointer)
	return _("{0} in {1} ({2})").format(
		_b(pointer), _b(info.department), cstr(info.status) or _("no status")
	)


def _row_problems(doc, prof, phase, mop_rows, mwo_rows, family):
	"""``[(exc_class, message)]`` for every row that breaks the rule. Pure: reads nothing."""
	problems = []
	seen_mop = {}
	seen_mwo = {}

	for idx, mop, row_mwo in refs(doc):
		if not mop:
			if phase == "save":
				continue
			problems.append(
				(
					CurrentOperationError,
					_("Row {0}: Manufacturing Operation is required.").format(idx),
				)
			)
			continue

		if mop in seen_mop:
			problems.append(
				(
					CurrentOperationError,
					_(
						"Row {0}: operation {1} is already on row {2} of this document."
					).format(idx, _b(mop), seen_mop[mop]),
				)
			)
			continue
		seen_mop[mop] = idx

		m = mop_rows.get(mop)
		if not m:
			problems.append(
				(
					CurrentOperationError,
					_("Row {0}: Manufacturing Operation {1} does not exist.").format(
						idx, _b(mop)
					),
				)
			)
			continue

		mwo = row_mwo or m.manufacturing_work_order
		if (
			row_mwo
			and m.manufacturing_work_order
			and not _same(row_mwo, m.manufacturing_work_order)
		):
			problems.append(
				(
					CurrentOperationError,
					_(
						"Row {0}: operation {1} belongs to work order {2}, not {3}."
					).format(idx, _b(mop), _b(m.manufacturing_work_order), _b(row_mwo)),
				)
			)
			continue

		if mwo in seen_mwo:
			problems.append(
				(
					CurrentOperationError,
					_(
						"Row {0}: work order {1} is already on row {2} of this document."
					).format(idx, _b(mwo), seen_mwo[mwo]),
				)
			)
			continue
		seen_mwo[mwo] = idx

		w = mwo_rows.get(mwo)
		if not w or int(w.docstatus or 0) != 1:
			problems.append(
				(
					CurrentOperationError,
					_("Row {0}: work order {1} is not submitted.").format(idx, _b(mwo)),
				)
			)
			continue

		fam = family.get(mwo) or []
		pointer = w.manufacturing_operation

		if not pointer:
			problems.append(
				(
					AmbiguousOperationError,
					_(
						"Row {0}: work order {1} has no current operation; ask a System Manager to "
						"run the current-operation audit."
					).format(idx, _b(mwo)),
				)
			)
			continue

		# The pointer comes from the LOCKED work-order row, so it is fresh even when the plain
		# family read is older; a pointer missing from that read only affects the message.
		if pointer != mop:
			problems.append(
				(
					StaleOperationError,
					_(
						"Row {0}: {1} is not the current operation of work order {2}; the work "
						"order is now at {3}. Discard this draft and create a new one from the "
						"current operation."
					).format(idx, _b(mop), _b(mwo), _pointer_text(w, mop_rows, fam)),
				)
			)
			continue

		if _has_successor(fam, mop):
			problems.append(
				(
					AmbiguousOperationError,
					_(
						"Row {0}: operation {1} of work order {2} already has a later operation; "
						"ask a System Manager to run the current-operation audit."
					).format(idx, _b(mop), _b(mwo)),
				)
			)
			continue

		if doc.get("company") and m.company and not _same(doc.company, m.company):
			problems.append(
				(
					CurrentOperationError,
					_("Row {0}: operation {1} belongs to company {2}, not {3}.").format(
						idx, _b(mop), _b(m.company), _b(doc.company)
					),
				)
			)
			continue

		problem = _state_problem(doc, prof, idx, mop, mwo, m)
		if problem:
			problems.append((CurrentOperationError, problem))

	return problems


def _state_problem(doc, prof, idx, mop, mwo, m):
	"""Per-transaction predicate on the (locked) current operation, mirroring the client."""
	status = cstr(m.status)
	transit = cstr(m.department_ir_status)
	where = _("{0} is {1} in {2}").format(
		_b(mop), status or _("no status"), _b(m.department)
	)

	if prof == EIR_ISSUE:
		holder_taken = (
			m.employee if doc.get("subcontracting") == "Yes" else m.subcontractor
		)
		if (
			status != "Not Started"
			or not _same(m.department, doc.get("department"))
			or transit in ISSUE_BLOCKED_TRANSIT
			or m.operation
			or holder_taken
		):
			return _(
				"Row {0}: {1}; an Employee Issue needs work order {2}'s current operation Not "
				"Started, unassigned and received in {3}."
			).format(idx, where, _b(mwo), _b(doc.get("department")))
		return None

	if prof == EIR_RECEIVE:
		problems = []
		if status not in RECEIVABLE_STATUSES:
			problems.append(_("status {0}").format(status or _("blank")))
		if not _same(m.department, doc.get("department")):
			problems.append(_("department {0}").format(_b(m.department)))
		if not _same(m.operation, doc.get("operation")):
			problems.append(_("operation {0}").format(_b(m.operation)))
		if doc.get("employee") and not _same(m.employee, doc.employee):
			problems.append(_("employee {0}").format(_b(m.employee)))
		if (
			doc.get("subcontracting") == "Yes"
			and doc.get("subcontractor")
			and not _same(m.subcontractor, doc.subcontractor)
		):
			problems.append(_("subcontractor {0}").format(_b(m.subcontractor)))
		if problems:
			return _(
				"Row {0}: {1} of work order {2} does not match this Receive ({3})."
			).format(idx, _b(mop), _b(mwo), ", ".join(problems))
		return None

	if prof == DIR_ISSUE:
		if _same(doc.get("current_department"), doc.get("next_department")):
			return _("Current and next department cannot be the same.")
		if (
			status != "Not Started"
			or not _same(m.department, doc.get("current_department"))
			or transit in ISSUE_BLOCKED_TRANSIT
			or m.employee
			or m.subcontractor
		):
			return _(
				"Row {0}: {1}; a Department Issue needs work order {2}'s current operation Not "
				"Started, not with an employee or subcontractor, and received in {3}."
			).format(idx, where, _b(mwo), _b(doc.get("current_department")))
		return None

	if prof == DIR_RECEIVE:
		if transit != "In-Transit":
			return _(
				"Row {0}: {1} of work order {2} is no longer in transit from {3} to {4} ({5})."
			).format(
				idx,
				_b(mop),
				_b(mwo),
				_b(doc.get("receive_against")),
				_b(doc.get("current_department")),
				transit or _("not in transit"),
			)
		# In transit, but not as this Receive needs it: name what does not match.
		mismatches = []
		if status != "Not Started":
			mismatches.append(_("status {0}").format(status or _("blank")))
		if cstr(m.department_issue_id) != cstr(doc.get("receive_against")):
			mismatches.append(
				_("sent by {0}").format(
					_b(m.department_issue_id)
					if m.department_issue_id
					else _("no Department Issue")
				)
			)
		if not _same(m.department, doc.get("current_department")):
			mismatches.append(_("going to {0}").format(_b(m.department)))
		if not mismatches:
			return None
		text = _(
			"Row {0}: {1} of work order {2} is in transit but does not match this Receive ({3}); "
			"a Department Receive needs it Not Started, sent by {4} to {5}."
		).format(
			idx,
			_b(mop),
			_b(mwo),
			", ".join(mismatches),
			_b(doc.get("receive_against")),
			_b(doc.get("current_department")),
		)
		if status in CLOSED_STATUSES:
			text += " " + _(
				"It was closed while still in transit: ask a System Manager to run the "
				"current-operation audit, or remove this row to receive the others."
			)
		return text

	return None


def _draft_problems(drafts):
	"""One OutstandingDraftError line per DRAFT (not per work order), its work orders capped."""
	grouped = {}
	for d in drafts:
		entry = grouped.setdefault(d.draft, {"first": d, "mwos": []})
		if d.mwo not in entry["mwos"]:
			entry["mwos"].append(d.mwo)
	problems = []
	for draft, entry in grouped.items():
		d, mwos = entry["first"], entry["mwos"]
		if len(mwos) == 1:
			held = _("Work order {0} is").format(_b(mwos[0]))
			target = _("this work order")
		else:
			held = _("Work orders {0} are").format(_name_list(mwos))
			target = _("these work orders")
		problems.append(
			(
				OutstandingDraftError,
				_(
					"{0} already on Employee Issue {1} (Draft, created {2} by {3}). Submit {1} if "
					"the work is still needed, or Discard it, before creating another Employee or "
					"Department transaction for {4}."
				).format(
					held,
					_b(draft),
					frappe.format(d.creation, {"fieldtype": "Datetime"}),
					_b(d.owner),
					target,
				),
			)
		)
	return problems


def _sibling_warning(doc, mwo_rows, family):
	"""Non-blocking warning when a work order still carries a stale open operation."""
	notes = []
	for mwo, w in mwo_rows.items():
		pointer = w.manufacturing_operation
		stale = [
			r.name
			for r in family.get(mwo) or []
			if r.name != pointer and is_open_operation(r.status, r.department_ir_status)
		]
		if stale:
			notes.append(
				_(
					"Work order {0} also has open operation(s) {1} that are not current."
				).format(_b(mwo), ", ".join(_b(s) for s in stale))
			)
	if notes:
		frappe.msgprint(
			"<br>".join(notes)
			+ "<br>"
			+ _(
				"They will be repaired by the current-operation audit; this transaction proceeds."
			),
			indicator="orange",
			title=_("Stale Operation On Work Order"),
		)


def _block_refs(references, phase, old_mwos):
	"""``(row operations, work orders)`` a check of ``phase`` is about; a draft save also covers
	the work orders its saved version referenced (rows may have moved away from them)."""
	mops = [mop for _idx, mop, _mwo in references if mop]
	mwos = {mwo for _idx, _mop, mwo in references if mwo}
	if phase == "save":
		mwos |= set(old_mwos)
	return mops, mwos


def _block_operations(prof, phase, mops):
	"""The operations of the lock block's first phase.

	A Department IR SAVE adds its row operations' previous operations:
	``validate_and_update_gross_wt_from_mop`` -> ``update_previous_mop_data`` writes them later
	in the save, so they join this one sorted operation set rather than being locked after the
	work orders (lock_order RULE E).
	"""
	if phase == "save" and prof in (DIR_ISSUE, DIR_RECEIVE):
		return list(mops) + _previous_operations(mops)
	return list(mops)


def lock_before_own_rows(doc):
	"""Existing Employee IR / Department IR: take the lock block BEFORE Frappe's
	``check_if_latest`` locks the document's own rows (the controllers call this first in their
	``check_if_latest``). Locks only; the decision is still ``_check``'s, in ``before_validate``,
	which re-takes the same locks (re-entrant, no wait).

	``check_if_latest`` loads the saved document ``for_update`` -- the parent row AND its child
	rows, with the gap after them on the ``parent`` index -- before the first hook runs. A save or
	submit that only then waited for the block, while a NEW document of the same work order held
	that block and inserted its rows into that gap, deadlocked (race suite: "dir receive submit"
	waiting on the operation lock vs a Department IR insert). Block first, then the document's
	rows -- the order a new document has (lock_order RULE D).

	Only for transitions that take the block later anyway: a submit, and a draft save whose rows
	or predicate header fields changed (compared with the stored version by a plain read; a
	concurrent change is refused by ``check_if_latest`` itself). A Department IR draft re-saved
	without such changes locks only its rows' previous operations (it still writes them). Not for
	cancels (their tree locks must precede the block), discards, updates after submit, new
	documents (they take it in ``before_insert``), an Employee Receive without its pre-locks, or
	warn mode.
	"""
	prof = profile(doc)
	if not prof or doc.is_new() or not enforcing():
		return
	if getattr(doc, "_action", None) == "discard":  # Document.discard sets it first
		return
	header = PREDICATE_HEADER_FIELDS.get(doc.doctype, ())
	stored = frappe.db.get_value(
		doc.doctype, doc.name, ["docstatus", *header], as_dict=True
	)
	if not stored or int(stored.docstatus or 0) != 0:
		return
	docstatus = int(doc.docstatus or 0)
	stored_refs = [
		(
			cstr(r.manufacturing_operation).strip(),
			cstr(r.manufacturing_work_order).strip(),
		)
		for r in frappe.get_all(
			_child_doctype(doc.doctype),
			filters={
				"parent": doc.name,
				"parenttype": doc.doctype,
				"parentfield": CHILD_TABLE_FIELD[doc.doctype],
			},
			fields=["manufacturing_operation", "manufacturing_work_order"],
			limit_page_length=0,
		)
	]
	references = refs(doc)
	if docstatus == 1:
		phase, old_mwos = "submit", set()
		if not _blocks_now(doc, prof, phase):
			return
	elif docstatus == 0:
		changed = any(
			cstr(doc.get(f)) != cstr(stored.get(f)) for f in header
		) or sorted((mop, mwo) for _idx, mop, mwo in references) != sorted(stored_refs)
		if not changed:
			if prof in (DIR_ISSUE, DIR_RECEIVE):
				# No check, but update_previous_mop_data still back-fills the previous
				# operations on this save: take those row locks before the document's own rows
				# too, or a new Department IR holding them in its block (and inserting rows into
				# this document's gap) closes a cycle.
				_lock_previous_operations(
					_previous_operations(
						[mop for _idx, mop, _mwo in references if mop]
					),
					doc=doc,
				)
			return
		phase, old_mwos = "save", {mwo for _mop, mwo in stored_refs if mwo}
	else:
		return
	mops, mwos = _block_refs(references, phase, old_mwos)
	if mops or mwos:
		_lock_block(_block_operations(prof, phase, mops), mwos, doc=doc)


def _lock_previous_operations(names, doc=None):
	"""Operation row locks only (no work order): a Department IR draft re-saved without
	reference changes still writes its rows' previous operations."""
	if not names:
		return
	try:
		lock_manufacturing_operations(names, wait=_attempt_wait(doc))
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		busy = _locked_row(exc)
		_busy(
			_("Manufacturing Operation"),
			busy or names,
			plural=_("these Manufacturing Operations"),
			detail=_work_order_hint(busy),
		)


def _child_doctype(doctype):
	return {
		"Employee IR": "Employee IR Operation",
		"Department IR": "Department IR Operation",
	}[doctype]


def _check(doc, phase, locking):
	"""Lock (or read), evaluate the rule set and the draft rule; remember what was guarded.

	In warn mode a locking check runs as a plain one: the kill switch restores the pre-guard
	locking behaviour (see KILL SWITCH).
	"""
	prof = profile(doc)
	if not prof:
		return
	if locking and not enforcing():
		locking = False
	references = refs(doc)
	old_mwos = _old_ref_mwos(doc) if phase == "save" else set()
	mops, mwos = _block_refs(references, phase, old_mwos)
	if not mops and not mwos:
		return

	if locking:
		mop_rows, mwo_rows = _lock_block(
			_block_operations(prof, phase, mops), mwos, doc=doc
		)
		_after_locks(doc, phase)
	else:
		mop_rows, mwo_rows = _plain_rows(mops, mwos)

	family = _family(mwo_rows.keys())
	if locking:
		family = _confirm_successors(
			family, {w.manufacturing_operation for w in mwo_rows.values()}
		)
	problems = _row_problems(doc, prof, phase, mop_rows, mwo_rows, family)

	row_mwos = {
		(mwo or (mop_rows.get(mop) or {}).get("manufacturing_work_order"))
		for _idx, mop, mwo in references
	}
	row_mwos.discard(None)
	row_mwos.discard("")
	exclude = None if doc.is_new() else doc.name
	drafts = outstanding_issue_drafts(row_mwos, exclude=exclude)
	if locking and drafts:
		drafts = _confirm_drafts(drafts, row_mwos)
	problems += _draft_problems(drafts)

	if locking:
		doc.flags[GUARDED_FLAG] = {
			"phase": phase,
			"docstatus": int(doc.docstatus or 0),
			"mwos": sorted(row_mwos),
		}

	_raise(problems, doc)
	if locking and phase == "submit":
		# A valid submit's side effects (MOP Log clones, weight recomputes, time logs) read the
		# row operations with plain reads: refuse when the snapshot is older than the locked rows.
		_refuse_if_snapshot_older(mop_rows, mops)
	if not problems:
		_sibling_warning(doc, mwo_rows, family)


# ----------------------------------------------------------------------------------------------
# hook entry points (called from the Employee IR / Department IR controllers)
# ----------------------------------------------------------------------------------------------


def _blocks_now(doc, prof, phase):
	"""May this check take the lock block right now?

	Everything may, except an Employee Receive submit whose Tree / Series / Bin pre-locks are not
	held yet (``employee_ir.take_receive_prelocks`` sets RECEIVE_PRELOCKS_FLAG): taking MOP / MWO
	before them would invert the Stock Entry order. That one gets an advisory plain check here and
	the authoritative one in ``on_submit_receive`` (``check_after_receive_prelocks``).
	"""
	if prof == EIR_RECEIVE and phase == "submit":
		return bool(doc.flags.get(RECEIVE_PRELOCKS_FLAG))
	return True


def on_before_insert(doc, method=None):
	"""New document: take the lock block BEFORE ``set_new_name`` locks the naming series."""
	prof = profile(doc)
	if not prof:
		return
	doc.flags.pop(GUARDED_FLAG, None)  # a fresh attempt decides afresh
	phase = "save" if int(doc.docstatus or 0) == 0 else "submit"
	_check(doc, phase, locking=_blocks_now(doc, prof, phase))


def on_before_validate(doc, method=None):
	"""Existing drafts (changed refs) and every submit path, before any side effect."""
	prof = profile(doc)
	if not prof:
		return
	if doc.flags.in_insert:
		return  # handled in before_insert
	doc.flags.pop(GUARDED_FLAG, None)  # a fresh attempt decides afresh
	docstatus = int(doc.docstatus or 0)
	if docstatus == 0:
		if refs_changed(doc):
			_check(doc, "save", locking=True)
		return
	if docstatus == 1:
		_check(doc, "submit", locking=_blocks_now(doc, prof, "submit"))


def check_after_receive_prelocks(doc):
	"""Authoritative Employee Receive check, after Tree / Series / Bin pre-locks.

	Nothing to do when this submit attempt already decided under the block (taken right after
	the pre-locks -- in ``check_if_latest`` or ``before_insert`` -- and decided in
	``before_validate`` / ``before_insert``): the rows cannot have changed since, and
	``final_draft_check`` still runs at the end.
	"""
	guarded = doc.flags.get(GUARDED_FLAG)
	if (
		guarded
		and guarded.get("phase") == "submit"
		and guarded.get("docstatus") == int(doc.docstatus or 0)
	):
		return
	_check(doc, "submit", locking=True)


def final_draft_check(doc, method=None):
	"""Terminal NOWAIT re-check for Employee Issue drafts (last statement of on_update/on_submit)."""
	if not enforcing():
		return
	guarded = doc.flags.get(GUARDED_FLAG)
	if not guarded or int(doc.docstatus or 0) != guarded.get("docstatus"):
		return
	drafts = outstanding_issue_drafts(
		guarded.get("mwos"), exclude=doc.name, locking=True
	)
	_raise(_draft_problems(drafts), doc)


def preflight(doc):
	"""Lock-free submit check run at enqueue time (Submission Queue), for immediate feedback: the
	EOD / reconciliation-window refusal (a queued submit is always of a saved draft), then the
	current-operation rules. The worker repeats both before any lock."""
	if not profile(doc):
		return
	refuse_if_movement_blocked(doc, 0)
	_check(doc, "submit", locking=False)


def on_discard(doc, method=None):
	"""Frappe 16 ``discard()`` only db_sets the parent; mark the child rows discarded too.

	Without this, every reader that filters child rows on ``docstatus = 0`` (this guard's draft
	re-check, ``stock_entry.validate_ir``) keeps treating the discarded draft as outstanding.
	"""
	for df in frappe.get_meta(doc.doctype).get_table_fields():
		frappe.db.sql(
			f"""
			UPDATE `tab{df.options}`
			SET docstatus = 2
			WHERE parent = %s AND parenttype = %s AND parentfield = %s
			""",
			(doc.name, doc.doctype, df.fieldname),
		)


# ----------------------------------------------------------------------------------------------
# cancel guard
# ----------------------------------------------------------------------------------------------


def _minted_operations(doc, prof, references):
	"""MOPs this document created, keyed by row MOP (plain read on the MOP mwo index)."""
	minted = {}
	if prof == DIR_ISSUE:
		for _idx, mop, mwo in references:
			if not mwo:
				continue
			name = frappe.db.get_value(
				"Manufacturing Operation",
				{"department_issue_id": doc.name, "manufacturing_work_order": mwo},
				"name",
			)
			if name:
				minted[mop] = name
	elif prof == EIR_RECEIVE:
		for _idx, mop, mwo in references:
			if not mop:
				continue
			name = frappe.db.get_value(
				"Manufacturing Operation",
				{
					"employee_ir": doc.name,
					"previous_mop": mop,
					"manufacturing_work_order": mwo,
				},
				"name",
			)
			if name:
				minted[mop] = name
	return minted


def other_submitted_issues(mops, exclude=None):
	"""``{mop: [frappe._dict(employee_ir, issue_submitted_on)]}``: the submitted Employee Issues
	other than ``exclude`` that list each of ``mops`` (plain read through the
	``manufacturing_operation`` index), oldest submit first.

	One operation is issued by at most one submitted Issue -- a second Issue needs it Not Started
	and unassigned -- so a hit is legacy data (an Issue submitted by the pre-guard code while the
	operation was already issued). It cannot appear after this read: an Issue submit of these
	operations waits on the block, and is refused once it gets it.
	"""
	names = sorted({m for m in mops or () if m})
	if not names:
		return {}
	out = {}
	for r in frappe.db.sql(
		"""
		SELECT DISTINCT o.manufacturing_operation AS mop, e.name AS employee_ir,
			e.issue_submitted_on
		FROM `tabEmployee IR Operation` o
		INNER JOIN `tabEmployee IR` e ON e.name = o.parent
		WHERE o.parenttype = 'Employee IR'
			AND o.manufacturing_operation IN %(mops)s
			AND e.type = 'Issue'
			AND e.docstatus = 1
			AND e.name != %(exclude)s
		ORDER BY e.issue_submitted_on, e.name
		""",
		{"mops": names, "exclude": exclude or ""},
		as_dict=True,
	):
		out.setdefault(r.mop, []).append(
			frappe._dict(
				employee_ir=r.employee_ir, issue_submitted_on=r.issue_submitted_on
			)
		)
	return out


def _confirm_submitted_issues(by_mop):
	"""Keep only the hits of ``other_submitted_issues`` a record-only locking read still finds
	submitted.

	That read is a plain one: a cancel that waited on the block can still see, in its REPEATABLE
	READ snapshot, an Issue the reviewed repair has cancelled meanwhile -- and would send the user
	to a repair that already ran. The hits' parents are re-read by primary key, ``LOCK IN SHARE
	MODE NOWAIT`` (latest committed state; a conflict is busy, retry).
	"""
	names = sorted(
		{i.employee_ir for issues in (by_mop or {}).values() for i in issues}
	)
	if not names:
		return {}
	try:
		live = set(
			frappe.db.sql(
				"""
				SELECT name FROM `tabEmployee IR`
				WHERE name IN %(names)s AND docstatus = 1
				LOCK IN SHARE MODE NOWAIT
				""",
				{"names": names},
				pluck=True,
			)
		)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		_busy("Employee IR", names)
	out = {}
	for mop, issues in by_mop.items():
		kept = [i for i in issues if i.employee_ir in live]
		if kept:
			out[mop] = kept
	return out


def _seconds_after(later, earlier):
	"""``later - earlier`` in seconds, rounded to the millisecond; ``None`` if either is empty."""
	if not later or not earlier:
		return None
	return round((get_datetime(later) - get_datetime(earlier)).total_seconds(), 3)


def time_log_opener(from_time, issues):
	"""The Issue of ``issues`` (each with ``issue_submitted_on``; oldest first) whose own submit
	opened a time log starting at ``from_time``: the closest submit inside [-5 s, +10 min] of it,
	or ``None``. "Closest" matters: two Issues of one operation can be submitted minutes apart, so
	a plain window would hand both time logs to the first. The audit's
	``mop_lineage_audit._co_time_log_opener`` uses the same rule (a test pins the two together)."""
	best = None
	for issue in issues or []:
		gap = _seconds_after(from_time, issue.get("issue_submitted_on"))
		if (
			gap is None
			or gap < -TIME_LOG_OPENER_EARLY_SECONDS
			or gap > TIME_LOG_OPENER_WINDOW_SECONDS
		):
			continue
		if best is None or abs(gap) < best[0]:
			best = (abs(gap), issue)
	return best[1] if best else None


def issue_own_open_time_logs(eir, open_rows, other_issues=None):
	"""Names of the rows of ``open_rows`` -- the OPEN time logs (``name``, ``from_time``) of an
	Employee Issue's holder on ONE of its operations -- that this Issue's own submit opened: the
	rows its cancel deletes. ``other_issues``: the other submitted Issues listing that operation
	(:func:`other_submitted_issues`).

	* No other submitted Issue lists the operation -- every ordinary cancel, since
	  :func:`guard_cancel` refuses one whose operation another submitted Issue also lists: the
	  rows opened from this Issue's submit on (5 s early tolerated), i.e. its own and a Resume
	  Job's after a Pause. An older open row belongs to an earlier holding (a pre-guard Issue
	  cancel left its timer running) and is kept, as it was before this Issue.
	* Another submitted Issue lists it too -- a legacy double issue; only the reviewed repair
	  cancels then: every row goes to the Issue whose submit is closest to its ``from_time``
	  (:func:`time_log_opener`, this Issue included -- it is already docstatus 2 in the database
	  when ``on_cancel`` runs, so it is added from memory), and of the rows that come to this Issue
	  only the one closest to its submit is deleted. The other Issue's running timer stays, even
	  when the two submits were minutes apart.
	* No ``issue_submitted_on``: the holder's only open row on the operation when no other
	  submitted Issue lists it; otherwise nothing (the rows cannot be told apart).
	"""
	rows = list(open_rows or [])
	if not rows:
		return []
	submitted = eir.get("issue_submitted_on")
	if not other_issues:
		if not submitted:
			return [rows[0].name] if len(rows) == 1 else []
		own = []
		for r in rows:
			gap = _seconds_after(r.get("from_time"), submitted)
			if gap is not None and gap >= -TIME_LOG_OPENER_EARLY_SECONDS:
				own.append(r.name)
		return sorted(own)
	if not submitted:
		return []
	me = frappe._dict(employee_ir=eir.name, issue_submitted_on=submitted)
	issues = sorted(
		(i for i in [*other_issues, me] if i.get("issue_submitted_on")),
		key=lambda i: (get_datetime(i.issue_submitted_on), cstr(i.employee_ir)),
	)
	mine = [
		r
		for r in rows
		if (time_log_opener(r.get("from_time"), issues) or {}).get("employee_ir")
		== eir.name
	]
	if not mine:
		return []
	closest = min(
		mine, key=lambda r: (abs(_seconds_after(r.from_time, submitted)), r.name)
	)
	return [closest.name]


def guard_cancel(doc):
	"""A cancel may only undo a transition whose effect is still the current state.

	Locks the row MOPs and the MOPs this document created, then the work orders (RULE E).
	Bypassed only for documents named in ``frappe.flags.current_operation_reviewed_repair`` by
	the reviewed repair script. Always enforced (the kill switch does not apply).

	An Employee Issue cancel is also refused while another submitted Employee Issue lists one of
	its operations (a legacy double issue): the cancel would put the operation back to Not Started
	and unassigned while that Issue still holds it. Only the reviewed repair cancels those.
	"""
	prof = profile(doc)
	if not prof:
		return
	references = refs(doc)
	minted = _minted_operations(doc, prof, references)
	mops = [mop for _idx, mop, _mwo in references if mop] + list(minted.values())
	mwos = {mwo for _idx, _mop, mwo in references if mwo}
	mop_rows, mwo_rows = _lock_block(mops, mwos, doc=doc)
	_after_locks(doc, "cancel")
	if _reviewed_repair(doc):
		return

	family = _confirm_successors(
		_family(mwo_rows.keys()),
		{w.manufacturing_operation for w in mwo_rows.values()},
	)
	double_issued = (
		_confirm_submitted_issues(
			other_submitted_issues([mop for _idx, mop, _mwo in references], doc.name)
		)
		if prof == EIR_ISSUE
		else {}
	)
	problems = []
	for _idx, mop, row_mwo in references:
		m = mop_rows.get(mop)
		mwo = row_mwo or (m.manufacturing_work_order if m else None)
		w = mwo_rows.get(mwo)
		if not m or not w:
			continue
		if double_issued.get(mop):
			problems.append(
				(
					AmbiguousOperationError,
					_double_issue_refusal(doc, mop, mwo, double_issued[mop]),
				)
			)
			continue
		fam = family.get(mwo) or []
		pointer = w.manufacturing_operation

		# ``effect``: the operation this document's effect lives on (``e``: its locked row) --
		# the operation it minted (Department Issue, Employee Receive) or the row operation.
		if prof in (DIR_ISSUE, EIR_RECEIVE):
			effect = minted.get(mop)
			e = mop_rows.get(effect) if effect else None
		else:
			effect, e = mop, m

		ok = False
		if prof == DIR_ISSUE:
			ok = bool(
				e
				and pointer == effect
				and cstr(e.department_ir_status) == "In-Transit"
				and cstr(e.status) == "Not Started"
				and not e.department_receive_id
				and not _has_successor(fam, effect)
			)
		elif prof == DIR_RECEIVE:
			ok = bool(
				pointer == mop
				and cstr(m.department_receive_id) == doc.name
				and cstr(m.department_ir_status) == "Received"
				and cstr(m.status) == "Not Started"
				and not m.operation
				and not _has_successor(fam, mop)
			)
		elif prof == EIR_ISSUE:
			holder_ok = (
				_same(m.subcontractor, doc.get("subcontractor"))
				if doc.get("subcontracting") == "Yes"
				else _same(m.employee, doc.get("employee"))
			)
			ok = bool(
				pointer == mop
				and cstr(m.status) in EIR_CANCELLABLE_ISSUE_STATUSES
				and _same(m.operation, doc.get("operation"))
				and holder_ok
				and not _has_successor(fam, mop)
			)
		elif prof == EIR_RECEIVE:
			ok = bool(
				e
				and pointer == effect
				and cstr(e.status) == "Not Started"
				and not e.operation
				and cstr(e.department_ir_status) not in ISSUE_BLOCKED_TRANSIT
				and not _has_successor(fam, effect)
			)

		if not ok:
			problems.append(
				(
					HistoryRewriteError,
					_cancel_refusal(doc, prof, mop, mwo, w, effect, e, fam, mop_rows),
				)
			)

	# Cancel guards always enforce, even in warn mode.
	_raise(problems, doc, always=True)
	# The reversal reads the operations with plain reads (MOP Log flips and recomputes, time
	# logs): refuse when the snapshot is older than the locked rows.
	_refuse_if_snapshot_older(mop_rows, mops)


def _double_issue_refusal(doc, mop, mwo, others):
	return _(
		"Cannot cancel {0} {1}: operation {2} of work order {3} was also issued by submitted "
		"Employee IR {4}; cancelling would put it back to Not Started while that Issue still "
		"holds it. Ask a System Manager to run the current-operation audit and repair it with a "
		"reviewed manifest."
	).format(
		_(doc.doctype),
		_b(doc.name),
		_b(mop),
		_b(mwo),
		_name_list([o.employee_ir for o in others]),
	)


def _submitted_after_the_work_moved_on(doc, fam, mop):
	"""True when this Employee Issue was submitted after ``mop`` already had a later operation:
	a stale draft submitted late, which reopened a finished operation (the 2026-10-05 incident).
	Reversing the transactions since would undo legitimate work."""
	submitted = doc.get("issue_submitted_on")
	if not submitted:
		return False
	return any(
		s.get("creation") and get_datetime(s.creation) < get_datetime(submitted)
		for s in _successors(fam, {mop})
	)


def _cancel_refusal(doc, prof, mop, mwo, w, effect, e, fam, mop_rows):
	"""The refusal of one row: why its effect is no longer current, and what the cancel would
	undo -- always the ROW operation, the one every reversal rewrites (a Department Issue / an
	Employee Receive cancel reopens its source; a Department Receive cancel sends the received
	operation back in transit; an Employee Issue cancel takes the operation back)."""
	pointer = w.manufacturing_operation
	if prof == DIR_RECEIVE:
		undo = _("cancelling now would send {0} back in transit").format(_b(mop))
		if e and cstr(e.status) in EIR_CANCELLABLE_ISSUE_STATUSES:
			undo += " " + _("while it is {0}").format(cstr(e.status))
	else:
		undo = _("cancelling now would reopen {0}").format(_b(mop))

	if (
		prof == EIR_ISSUE
		and e
		and pointer != effect
		and _submitted_after_the_work_moved_on(doc, fam, mop)
	):
		# Not a reversible history: the Issue itself came after the later work. The usual
		# "reverse the later transactions first" advice would destroy legitimate work.
		return _(
			"Cannot cancel {0} {1}: it was submitted after work order {2} had already moved on "
			"from {3} (it is now at {4}), and its submit reopened {3}. Do not reverse the "
			"transactions made since: ask a System Manager to run the current-operation audit and "
			"repair it with a reviewed manifest."
		).format(
			_(doc.doctype),
			_b(doc.name),
			_b(mwo),
			_b(mop),
			_pointer_text(w, mop_rows, fam),
		)

	if not e:
		reason = _(
			"the operation it created on work order {0} no longer exists"
		).format(_b(mwo))
	elif pointer != effect:
		reason = _("work order {0} has moved on to {1}").format(
			_b(mwo), _pointer_text(w, mop_rows, fam)
		)
	elif _has_successor(fam, effect):
		reason = _(
			"operation {0} of work order {1} already has a later operation (ask a System "
			"Manager to run the current-operation audit)"
		).format(_b(effect), _b(mwo))
	else:
		reason = _effect_state(prof, doc, mwo, e)
	return _(
		"Cannot cancel {0} {1}: {2}; {3}. Reverse the later transactions first, or move the "
		"work order with a new transfer."
	).format(_(doc.doctype), _b(doc.name), reason, undo)


def _effect_state(prof, doc, mwo, e):
	"""What happened to ``e`` -- still the current operation -- since this document's effect."""
	if prof == DIR_ISSUE and e.department_receive_id:
		return _(
			"its transfer {0} of work order {1} was already received by {2}"
		).format(_b(e.name), _b(mwo), _b(e.department_receive_id))
	if (
		prof == DIR_RECEIVE
		and e.department_receive_id
		and cstr(e.department_receive_id) != cstr(doc.name)
	):
		return _("{0} of work order {1} was received by {2}").format(
			_b(e.name), _b(mwo), _b(e.department_receive_id)
		)
	if prof != DIR_ISSUE and cstr(e.department_ir_status) == "In-Transit":
		text = _("{0} of work order {1} is in transit").format(_b(e.name), _b(mwo))
		if e.department_issue_id:
			text += " " + _("({0})").format(_b(e.department_issue_id))
		return text
	holder = (
		e.subcontractor
		if (cint(e.for_subcontracting) and e.subcontractor)
		else e.employee
	)
	if cstr(e.status) in EIR_CANCELLABLE_ISSUE_STATUSES and (holder or e.operation):
		text = _("{0} of work order {1} is {2}").format(
			_b(e.name), _b(mwo), cstr(e.status)
		)
		if holder:
			text += " " + _("with {0}").format(_b(holder))
		if e.operation:
			text += " " + _("for {0}").format(_b(e.operation))
		issue = _holding_issue(e.name)
		if issue:
			text += " " + _("(Employee IR {0})").format(_b(issue))
		return text
	return _("{0} of work order {1} is {2} in {3}").format(
		_b(e.name), _b(mwo), cstr(e.status) or _("no status"), _b(e.department)
	)


def _holding_issue(mop):
	"""The latest submitted Employee Issue of ``mop`` (plain read through the new
	``manufacturing_operation`` index; message detail only, never a decision)."""
	rows = frappe.db.sql(
		"""
		SELECT e.name
		FROM `tabEmployee IR Operation` o
		INNER JOIN `tabEmployee IR` e ON e.name = o.parent
		WHERE o.manufacturing_operation = %s
			AND o.parenttype = 'Employee IR'
			AND e.docstatus = 1
			AND e.type = 'Issue'
		ORDER BY e.creation DESC, e.name DESC
		LIMIT 1
		""",
		(mop,),
	)
	return rows[0][0] if rows else None


# ----------------------------------------------------------------------------------------------
# Manufacturing Operation desk / API edits
# ----------------------------------------------------------------------------------------------


def validate_manual_reopen(mop_doc):
	"""Refuse reopening (Finished/Revert -> open) an operation that is not the current one.

	Internal writers use ``set_value`` / ``bulk_update`` / ``flags.ignore_validation`` and never
	reach this check; it only stops desk or API saves from recreating the conflicting state.
	"""
	if mop_doc.is_new() or not mop_doc.get("manufacturing_work_order"):
		return
	before = mop_doc.get_doc_before_save()
	if not before:
		return
	if (
		cstr(before.status) not in CLOSED_STATUSES
		or cstr(mop_doc.status) not in OPEN_STATUSES
	):
		return
	pointer = frappe.db.get_value(
		"Manufacturing Work Order",
		mop_doc.manufacturing_work_order,
		"manufacturing_operation",
	)
	if pointer == mop_doc.name:
		return
	_raise(
		[
			(
				HistoryRewriteError,
				_(
					"Cannot reopen {0}: it is not the current operation of work order {1} (current: "
					"{2}). Reopening it would give the work order two open operations."
				).format(
					_b(mop_doc.name), _b(mop_doc.manufacturing_work_order), _b(pointer)
				),
			)
		],
		mop_doc,
	)
