# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Real multi-connection races for the work-order current-operation guard.

Every test runs the REAL Employee IR / Department IR controllers in two or more SPAWNED processes,
each on its own database connection, against a COMMITTED fixture world
(``tests/_current_operation_fixtures.py``). ``multiprocessing`` Events pin the interleaving under
test, so each scenario is deterministic rather than whatever the scheduler happens to produce.
``tests/test_current_operation_guard.py`` covers the rules in one transaction; this suite covers
what only separate transactions can show: lock waits, REPEATABLE READ snapshots, commit order.

OPT-IN: ``CURRENT_OPERATION_RACE_TESTS=1``
------------------------------------------
Skipped unless that variable is ``1``, because unlike the other suites of this app it

* COMMITS: a race only exists between committed transactions. Each test writes its own world
  (prefix ``TCOR<run>-<token>``), commits it and purges it in ``tearDown``; a second purge must
  find nothing and a residue scan must find no row naming the prefix. ``tearDownModule`` then
  scans every text column of the database for the run's prefix;
* SPAWNS processes (``multiprocessing`` "spawn"): each runs ``frappe.init`` / ``frappe.connect``
  with the code under test first on ``sys.path``, i.e. several site connections at once;
* holds row locks for seconds and runs a ~60 s stress phase.

Run it on a DISPOSABLE site only, with the code under test on ``PYTHONPATH``::

    CURRENT_OPERATION_RACE_TESTS=1 PYTHONPATH=<worktree> bench --site <disposable site> \\
        run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_current_operation_race

``CURRENT_OPERATION_RACE_STRESS_SECONDS`` changes the stress duration (default 60) and
``CURRENT_OPERATION_RACE_SEED`` replays a stress run (its seed is printed in the run log).

HOW A RACE IS PINNED
--------------------
The first transaction parks in ``current_operation_guard._after_locks`` -- the test seam that runs
right after the MOP -> MWO lock block is held -- until the test releases it. The second
transaction is started and the test PROVES it is blocked on a row lock before releasing the first:
from ``information_schema.INNODB_LOCK_WAITS`` when the site's database user may read it (that
needs the PROCESS privilege), otherwise from the waiter's own ``information_schema.PROCESSLIST``
row (a database user always sees its own connections): the expected locking statement on the
expected row, running for a while, while the holder's connection sits idle in its transaction,
corroborated by the server's ``Innodb_row_lock_current_waits``. Only then is the holder released.

INSIDE EACH PROCESS
-------------------
RQ is replaced by a recorder (``frappe.enqueue`` and the queue itself: the bench workers run the
installed code, not the code under test), realtime events and ``frappe.log_error`` are recorded
instead of sent / written, e-mail is muted, the guard is forced to ``enforce`` and every commit made
INSIDE an action is recorded (an IR cascade must not commit half-way). The world's Department
Operations switch every optional behaviour off, as in the integration suite.

WHAT A "NO SIDE EFFECT" ASSERTION COMPARES
------------------------------------------
Each world has a twin work order built with the same history. After a race, the winner's
transaction is replayed ALONE on the twin by a reference process, and the name-free "shape" of
both work orders (pointer, every operation's state and lineage, time logs, MOP Log rows, the
documents' docstatus) must be identical: the loser left nothing behind and the winner did exactly
what it does alone.

THE STRESS TESTS
----------------
Workers run production transaction boundaries (a draft commits, then its submit commits, or a
REST insert with docstatus 1) on 1-2 work orders per document, against Stock-Entry-like
transactions (operation ``FOR UPDATE``, then the work order). Every failed action records the
locking / writing statements its transaction had run and the one it was waiting on, so a deadlock
is classified without ``SHOW ENGINE INNODB STATUS`` (which also needs PROCESS).

``TestFormerDefects`` replays the interleavings in which this harness (and the review that followed)
found the guard wrong -- a refusal decided from a stale snapshot, a holder's re-check tripping over
rows written before the block, a naming-row deadlock, a phantom successor, a kill switch that
still locked -- and asserts the fixed behaviour.
"""

import contextlib
import multiprocessing
import os
import pickle
import queue
import random
import re
import sys
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.core.doctype.submission_queue.submission_queue import queue_submission
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr, flt, random_string

import jewellery_erpnext
from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	CustomSubmissionQueue,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	current_operation_guard as guard,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	_current_operation_fixtures as fx,
)

RACE_ENV = "CURRENT_OPERATION_RACE_TESTS"
STRESS_SECONDS_ENV = "CURRENT_OPERATION_RACE_STRESS_SECONDS"
RACES_ENABLED = os.environ.get(RACE_ENV) == "1"
SKIP_REASON = (
	f"set {RACE_ENV}=1 to run: this suite spawns processes, holds row locks and COMMITS "
	"fixture worlds (purged afterwards) -- disposable sites only"
)

# The directory the code under test was imported from (the worktree under PYTHONPATH).
CODE_ROOT = str(Path(jewellery_erpnext.__file__).resolve().parents[1])
# Every world of one module run is "<RUN_BASE>-<token>", so one LIKE finds all of them.
RUN_BASE = f"TCOR{random_string(4).lower()}"

# Seconds any process waits for a coordination event before giving up.
EVENT_TIMEOUT = 90
# current_operation_lock_wait of the waiter in the timeout scenarios, and how much longer than
# that a timed-out waiter may take.
SHORT_LOCK_WAIT = 3
TIMEOUT_SLACK = 2.5
STRESS_SECONDS = flt(os.environ.get(STRESS_SECONDS_ENV) or 60)

GUARD_ENTRY_POINTS = (
	"on_before_insert",
	"on_before_validate",
	"check_after_receive_prelocks",
	"final_draft_check",
	"guard_cancel",
	"preflight",
	"validate_manual_reopen",
)
GUARD_ERRORS = {
	cls.__name__
	for cls in (
		guard.CurrentOperationError,
		guard.StaleOperationError,
		guard.OutstandingDraftError,
		guard.AmbiguousOperationError,
		guard.HistoryRewriteError,
		guard.WorkOrderBusyError,
	)
}
ER_LOCK_WAIT_TIMEOUT = 1205
ER_LOCK_DEADLOCK = 1213
ER_SPECIFIC_ACCESS_DENIED = 1227
BLOCKED_TRANSIT = ("In-Transit", "Revert")


# ==============================================================================================
# shared helpers
# ==============================================================================================


def _plain(text):
	"""Message text without HTML tags."""
	return re.sub(r"<[^>]+>", "", cstr(text)).strip()


# Frappe 16 talks to MariaDB through mysqlclient (MySQLdb) or PyMySQL.
_DRIVER_MODULES = ("MySQLdb", "_mysql", "pymysql")


def _db_errors(exc):
	"""MariaDB error numbers anywhere in ``exc``'s cause / context chain, the chain's class
	names, and the frames of the statement that failed in the database.

	The guard turns a lock timeout or deadlock into ``WorkOrderBusyError`` raised inside the
	``except``, so the original ``QueryTimeoutError`` / ``QueryDeadlockError`` (and the driver
	error under it) -- and with it the frames of the waiting statement -- is only reachable
	through ``__context__``.
	"""
	codes, chain, db_frames, seen, stack = [], [], [], set(), [exc]
	while stack:
		e = stack.pop()
		if e is None or id(e) in seen:
			continue
		seen.add(id(e))
		chain.append(type(e).__name__)
		if (
			type(e).__module__.startswith(_DRIVER_MODULES)
			and e.args
			and isinstance(e.args[0], int)
		):
			codes.append(e.args[0])
		if not db_frames and type(e).__name__ in (
			"QueryTimeoutError",
			"QueryDeadlockError",
		):
			db_frames = [f.name for f in traceback.extract_tb(e.__traceback__)]
		stack.extend(
			a for a in (getattr(e, "args", None) or ()) if isinstance(a, BaseException)
		)
		stack.extend((e.__cause__, e.__context__))
	return sorted(set(codes)), chain, db_frames


def _describe(exc):
	codes, chain, db_frames = _db_errors(exc)
	return {
		"type": type(exc).__name__,
		"guard": isinstance(exc, guard.CurrentOperationError),
		"validation": isinstance(exc, frappe.ValidationError),
		"message": _plain(exc)[:600],
		"db_codes": codes,
		"chain": chain,
		"frames": [f.name for f in traceback.extract_tb(exc.__traceback__)],
		"db_frames": db_frames,
		"traceback": "".join(traceback.format_exception(exc))[-2500:],
	}


# Statements that take or wait for row locks: what a lock error is about.
_LOCKING_STATEMENT = re.compile(
	r"FOR UPDATE|LOCK IN SHARE MODE|^\s*(INSERT|UPDATE|DELETE)\b",
	re.IGNORECASE | re.MULTILINE,
)
# The guard block's own rows (not their child tables, e.g. `tabManufacturing Operation Time Log`).
_GUARD_ROWS = re.compile(r"`tabManufacturing (Operation|Work Order)`")


def _squash(text):
	return re.sub(r"\s+", " ", cstr(text)).strip()[:600]


def _on_guard_rows(error):
	"""Was the failed statement a lock on the guard block's own rows?"""
	return bool(_GUARD_ROWS.search(error["waited"])) or bool(
		{"lock_manufacturing_operations", "lock_work_orders"} & set(error["db_frames"])
	)


def _executed_statement():
	"""The statement the driver executed last, values included (set once it succeeded)."""
	with contextlib.suppress(Exception):
		executed = frappe.db._cursor._executed
		if isinstance(executed, bytes):
			executed = executed.decode(errors="replace")
		return cstr(executed)
	return ""


def _classify(error):
	"""One label per failed action: DEADLOCK always wins, then the guard, then the rest."""
	if ER_LOCK_DEADLOCK in error["db_codes"] or "QueryDeadlockError" in error["chain"]:
		return "DEADLOCK"
	if error["guard"]:
		suffix = " (lock timeout)" if ER_LOCK_WAIT_TIMEOUT in error["db_codes"] else ""
		return f"guard:{error['type']}{suffix}"
	if ER_LOCK_WAIT_TIMEOUT in error["db_codes"]:
		return "LOCK_WAIT_TIMEOUT"
	if error["validation"]:
		return f"validation:{error['type']}"
	return f"OTHER:{error['type']}"


# Frames worth naming when reporting where a refusal was raised.
_WHERE_FRAMES = (
	"final_draft_check",
	"outstanding_issue_drafts",
	"lock_manufacturing_operations",
	"lock_work_orders",
	"_lock_block",
	"_check",
	"guard_cancel",
	"check_if_latest",
)


def _where(error):
	"""The innermost interesting frame of a refusal (``frappe.throw`` internals skipped)."""
	return next(
		(f for f in reversed(error["frames"]) if f in _WHERE_FRAMES),
		error["frames"][-1],
	)


def _plain_exists(doctype, name):
	"""Consistent (snapshot) read: does this transaction's REPEATABLE READ view see the row?"""
	return bool(frappe.db.sql(f"SELECT 1 FROM `tab{doctype}` WHERE name = %s", name))


def _noop(*args, **kwargs):
	return None


class _RecordingQueue:
	"""Stands in for an RQ queue: the bench workers run the installed code, not this branch."""

	def __init__(self, sink):
		self.sink = sink

	def enqueue_call(self, *args, **kwargs):
		self.sink.append(cstr((kwargs.get("kwargs") or {}).get("method")))
		return SimpleNamespace(id=kwargs.get("job_id"))


# ==============================================================================================
# child side -- every spawned process runs _actor_main
# ==============================================================================================


class _CoordinationTimeout(Exception):
	"""A process waited too long for a coordination event: the TEST is stuck, not the guard."""


def _actor_main(spec, events, outbox):
	"""Entry point of a spawned process: connect, instrument, play one role, report, disconnect.

	"spawn" hands the child the parent's ``sys.path``, so this module -- and through it the guard
	-- was imported from the same root as in the parent (the worktree under ``PYTHONPATH``). Keep
	that root first for anything imported later; ``hello`` reports where the code came from and
	the parent asserts it.
	"""
	try:
		if sys.path[:1] != [spec["code_root"]]:
			sys.path.insert(0, spec["code_root"])
		frappe.init(site=spec["site"], sites_path=spec["sites_path"])
		frappe.connect()
		actor = _Actor(spec, events, outbox)
		actor.hello()
		globals()[spec["fn"]](actor, **spec["kwargs"])
		actor.done()
	except BaseException as exc:
		outbox.put(
			{
				"role": spec["role"],
				"kind": "crash",
				"t": time.time(),
				"error": _describe(exc),
			}
		)
	finally:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		with contextlib.suppress(Exception):
			frappe.destroy()


class _Actor:
	"""What a role function gets: coordination, instrumentation and recorded attempts."""

	def __init__(self, spec, events, outbox):
		self.role = spec["role"]
		self.events = events
		self.outbox = outbox
		self.facts = {}
		self.attempts = []
		self.enqueued = []
		self.logged = []
		self._inside = None
		self._inside_commits = 0
		self._trace = []
		self._commit = frappe.db.commit
		self._rollback = frappe.db.rollback
		self._instrument(spec)

	def _instrument(self, spec):
		import frappe.utils.background_jobs as background_jobs

		frappe.flags.mute_emails = True
		frappe.local.conf.update(
			{
				"mute_emails": 1,
				"current_operation_guard": "enforce",
				**(spec.get("conf") or {}),
			}
		)

		def record_enqueue(method, *args, **kwargs):
			self.enqueued.append(cstr(getattr(method, "__qualname__", method)))

		def record_log_error(*args, **kwargs):
			title = kwargs.get("title") or (args[0] if args else "")
			message = kwargs.get("message") or (args[1] if len(args) > 1 else "")
			self.logged.append(_plain(f"{title}: {message}")[:300])
			return frappe._dict(name=None)

		def commit(*args, **kwargs):
			if self._inside:
				self._inside_commits += 1
			return self._commit(*args, **kwargs)

		real_sql = frappe.db.sql

		def sql(query, *args, **kwargs):
			"""Remember the locking / writing statements of the current action, so a lock error
			can say what the transaction held and what it was waiting for."""
			if not self._inside:
				return real_sql(query, *args, **kwargs)
			text = cstr(query)
			relevant = _LOCKING_STATEMENT.search(text)
			try:
				result = real_sql(query, *args, **kwargs)
			except Exception:
				if relevant:
					values = args[0] if args else kwargs.get("values")
					self._trace.append(f"WAITED: {_squash(text)} {cstr(values)[:120]}")
				raise
			if relevant:
				self._trace.append(_squash(_executed_statement() or text))
			return result

		frappe.enqueue = record_enqueue
		background_jobs.enqueue = record_enqueue
		background_jobs.get_queue = lambda *a, **k: _RecordingQueue(self.enqueued)
		frappe.publish_realtime = _noop
		# The EOD lock and the reconciliation window are MOP Settings state that other suites
		# on a shared site toggle: off for every actor (their refusal has its own tests).
		from jewellery_erpnext.jewellery_erpnext import stock_recon_window
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import eod_lock

		eod_lock.is_eod_sync_locked = lambda: False
		stock_recon_window._enabled = lambda: False
		frappe.log_error = record_log_error
		frappe.db.commit = commit
		frappe.db.sql = sql
		if spec.get("guard") == "off":
			for name in GUARD_ENTRY_POINTS:
				setattr(guard, name, _noop)

	# -- coordination ------------------------------------------------------------------------

	def send(self, kind, **data):
		self.outbox.put({"role": self.role, "kind": kind, "t": time.time(), **data})

	def set(self, name):
		self.events[name].set()

	def wait(self, name, timeout=EVENT_TIMEOUT):
		if not self.events[name].wait(timeout):
			raise _CoordinationTimeout(f"{self.role} waited {timeout}s for {name!r}")

	def hello(self):
		self.send(
			"hello",
			connection_id=frappe.db.sql("SELECT CONNECTION_ID()")[0][0],
			pid=os.getpid(),
			code=jewellery_erpnext.__file__,
			guard=guard.__file__,
		)

	def done(self):
		self.send(
			"done",
			facts=self.facts,
			attempts=self.attempts,
			enqueued=self.enqueued,
			logged=self.logged,
		)

	# -- parking inside the guard ------------------------------------------------------------

	def _park(self, locked, release, doc, phase):
		self.facts["parked"] = {
			"doctype": doc.doctype,
			"name": doc.name,
			"phase": phase,
			"t": time.time(),
		}
		self.set(locked)
		self.wait(release)
		self.facts["released_t"] = time.time()

	def park_after_locks(self, locked, release):
		"""Hold the guard's lock block: park in the ``_after_locks`` seam (first call only)."""
		original, state = guard._after_locks, {"parked": False}

		def _after_locks(doc, phase):
			original(doc, phase)
			if not state["parked"]:
				state["parked"] = True
				self._park(locked, release, doc, phase)

		guard._after_locks = _after_locks

	def park_in(self, entry_point, locked, release):
		"""Pause right after a guard entry point (used by the control run, where the entry points
		are no-ops and ``_after_locks`` is never reached)."""
		original, state = getattr(guard, entry_point), {"parked": False}

		def parked(doc, *args, **kwargs):
			result = original(doc, *args, **kwargs)
			if not state["parked"]:
				state["parked"] = True
				self._park(locked, release, doc, entry_point)
			return result

		setattr(guard, entry_point, parked)

	def observe_after_locks(self, fn):
		"""Call ``fn(doc, phase)`` each time this process holds the guard's lock block."""
		original = guard._after_locks

		def _after_locks(doc, phase):
			original(doc, phase)
			fn(doc, phase)

		guard._after_locks = _after_locks

	# -- actions -----------------------------------------------------------------------------

	def attempt(self, label, fn, *, commit=True, before_commit=None, keep=True):
		"""Run ``fn``; commit on success (after ``before_commit``), roll back on failure."""
		rec = {"label": label, "role": self.role, "t0": time.time()}
		self._inside, self._inside_commits, self._trace = label, 0, []
		try:
			value = fn()
			rec["t_return"] = time.time()
			if before_commit:
				before_commit()
		except _CoordinationTimeout:
			raise
		except Exception as exc:
			rec["t_return"] = rec.get("t_return") or time.time()
			rec.update(ok=False, error=_describe(exc))
			# what this transaction held, and the statement it was refused or waiting on
			waited = [t for t in self._trace if t.startswith("WAITED: ")]
			rec["error"]["waited"] = waited[-1] if waited else ""
			rec["error"]["held"] = [t for t in self._trace if t not in waited][-25:]
			with contextlib.suppress(Exception):
				self._rollback()
		else:
			rec.update(ok=True, value=cstr(value)[:200])
			if commit:
				self._commit()  # the real commit, not the counting wrapper
				rec["t_commit"] = time.time()
		finally:
			rec["commits_inside"] = self._inside_commits
			self._inside = None
		rec["elapsed"] = rec["t_return"] - rec["t0"]
		if keep:
			self.attempts.append(rec)
		return rec


# -- documents, built inside the child -------------------------------------------------------


def _build(world, action):
	"""An unsaved Employee IR / Department IR for ``action`` (fixture builders, explicit name)."""
	kind = action["kind"]
	if kind in ("eir_issue", "eir_receive"):
		doc = fx.employee_ir(
			world,
			"Issue" if kind == "eir_issue" else "Receive",
			action["rows"],
			department=action.get("department", "MM"),
			employee=action.get("employee", "MM1"),
		)
	elif kind == "dir_issue":
		doc = fx.department_ir(
			world, "Issue", action["rows"], current=action["current"], to=action["to"]
		)
	elif kind == "dir_receive":
		doc = fx.department_ir(
			world,
			"Receive",
			action["rows"],
			current=action["current"],
			previous=action["previous"],
			receive_against=action["receive_against"],
		)
	else:
		raise ValueError(f"unknown document kind {kind!r}")
	doc.flags.fixture_name = action.get("name")
	if action.get("docstatus") == 1:
		# REST insert-and-submit. A fresh in-memory Issue row carries None in rpt_wt_issue,
		# which the Issue's bulk update cannot write into its NOT NULL column.
		doc.docstatus = 1
		for row in doc.get("employee_ir_operations") or []:
			row.rpt_wt_issue = 0
	return doc


def _replay_queue_job(queue_name, job_id, handed):
	"""The Submission Queue job exactly as an RQ worker runs it -- ``execute_job`` ->
	``execute_action`` -> ``background_submission`` -- but in this process (the bench workers run
	the installed code). ``handed`` is the pickled document RQ would hand the worker."""
	from frappe.utils.background_jobs import execute_job

	with patch(
		"frappe.core.doctype.submission_queue.submission_queue.get_current_job",
		return_value=SimpleNamespace(id=job_id),
	):
		execute_job(
			site=frappe.local.site,
			method="frappe.model.document.execute_action",
			event=None,
			job_name="frappe.model.document.execute_action",
			kwargs={
				"__doctype": "Submission Queue",
				"__name": queue_name,
				"__action": "background_submission",
				"to_be_queued_doc": handed,
				"action_for_queuing": "Submit",
			},
			user="Administrator",
			is_async=False,
		)
	return frappe.db.get_value("Submission Queue", queue_name, "status")


def _prepare(actor, world, action):
	"""Build or load the document NOW -- which opens this transaction's REPEATABLE READ
	snapshot -- and return the zero-argument callable that performs the action."""
	op = action["op"]
	if op == "submit":
		doc = frappe.get_doc(action["doctype"], action["name"])
		return lambda: doc.submit().name
	if op == "insert":
		doc = _build(world, action)
		if action.get("real_name"):
			# through the document's real naming series (its tabSeries row), not set_name
			return lambda: doc.insert().name
		return lambda: fx.insert_ir(doc).name
	if op == "cancel":
		doc = frappe.get_doc(action["doctype"], action["name"])

		def cancel():
			doc.cancel()
			return doc.name

		return cancel
	if op == "move_rows":
		# a draft's rows replaced by ``rows`` ((mop, mwo) pairs), then saved
		doc = frappe.get_doc(action["doctype"], action["name"])
		table = (
			"employee_ir_operations"
			if doc.doctype == "Employee IR"
			else "department_ir_operation"
		)
		doc.set(table, [])
		for mop, mwo in action["rows"]:
			doc.append(
				table, {"manufacturing_operation": mop, "manufacturing_work_order": mwo}
			)
		return lambda: doc.save().name
	if op == "queue_job":
		# The queue row's own notification (realtime / Notification Log) is recorded instead.
		notified = actor.facts.setdefault("notified", [])
		CustomSubmissionQueue.notify = lambda self, status, act: notified.append(status)
		handed = pickle.loads(action["handed"])
		return lambda: _replay_queue_job(action["queue"], action["job_id"], handed)
	raise ValueError(f"unknown action {op!r}")


# -- roles -----------------------------------------------------------------------------------


def _role_racer(actor, *, world, action, me, order, other, park_in=None):
	"""One side of a two-transaction race.

	Both sides build / load their document first (``<me>_ready``), so both snapshots predate
	both commits. The ``first`` side then starts and parks inside the guard's lock block
	(``<me>_parked``) -- or, with ``park_in``, right after that guard entry point returns --
	until ``<me>_release``; the ``second`` side starts once the first is parked
	(``<me>_started``) and so runs into the first one's locks.
	"""
	run = _prepare(actor, world, action)
	actor.set(f"{me}_ready")
	if order == "first":
		if park_in:
			actor.park_in(park_in, f"{me}_parked", f"{me}_release")
		else:
			actor.park_after_locks(f"{me}_parked", f"{me}_release")
		actor.wait(f"{other}_ready")
	else:
		actor.wait(f"{other}_parked")
		actor.set(f"{me}_started")
	actor.attempt(action["label"], run)
	actor.set(f"{me}_done")


def _role_later(actor, *, world, action, me, after):
	"""Once every event in ``after`` is set, run ``action`` in a fresh transaction (a retry)."""
	for name in after:
		actor.wait(name)
	frappe.db.rollback()
	actor.attempt(action["label"], _prepare(actor, world, action))
	actor.set(f"{me}_done")


def _role_reference(actor, *, world, actions):
	"""The winner's transaction(s), replayed ALONE on the twin work order after the race."""
	actor.wait("REF_go")
	frappe.db.rollback()
	for action in actions:
		if not actor.attempt(action["label"], _prepare(actor, world, action))["ok"]:
			break
	actor.set("REF_done")


def _role_ten_one_first(actor, *, world, mwo, name, control):
	"""Process A of the 2026-10-01 race: insert an Employee Issue draft for ``mwo`` and stay
	inside ``before_insert`` -- holding the guard's lock block, or (control) merely paused
	there -- until released."""
	if control:
		actor.park_in("on_before_insert", "A_parked", "A_release")
	else:
		actor.park_after_locks("A_parked", "A_release")
	doc = _build(world, {"kind": "eir_issue", "rows": [mwo], "name": name})
	actor.wait("B_snapshot")
	actor.attempt("insert issue draft", lambda: fx.insert_ir(doc).name)
	actor.set("A_done")


def _role_ten_one_second(actor, *, world, mwo, name, other):
	"""Process B of the 2026-10-01 race (draft 43408 vs 43405): open the REPEATABLE READ snapshot
	BEFORE A writes anything, then insert another Employee Issue draft for the same work order
	while A is inside its insert. Records what B's snapshot can see of A's draft."""
	actor.facts["snapshot_sees_other"] = _plain_exists("Employee IR", other)
	actor.set("B_snapshot")
	actor.wait("A_parked")
	doc = _build(
		world, {"kind": "eir_issue", "rows": [mwo], "name": name, "employee": "MM2"}
	)
	actor.observe_after_locks(
		lambda d, phase: actor.facts.setdefault(
			"decision_sees_other", _plain_exists("Employee IR", other)
		)
	)

	def before_commit():
		actor.set("B_returned")
		actor.wait("A_done")
		actor.facts["commit_sees_other"] = _plain_exists("Employee IR", other)

	actor.set("B_inserting")
	if not actor.attempt(
		"insert issue draft",
		lambda: fx.insert_ir(doc).name,
		before_commit=before_commit,
	)["ok"]:
		actor.set("B_returned")


def _role_rewrite_operation(actor, *, mop, waiter):
	"""What a Stock Entry's MOP Log bridge does to an operation -- ``MOPLog.validate`` locks it and
	rewrites its weight buckets (``modified`` with them) -- started once ``waiter`` opened its
	snapshot, parked while holding the row, then COMMITTED."""
	actor.wait(f"{waiter}_ready")

	def rewrite():
		frappe.db.sql(
			"SELECT name FROM `tabManufacturing Operation` WHERE name = %s FOR UPDATE",
			mop,
		)
		other = flt(frappe.db.get_value("Manufacturing Operation", mop, "other_wt"))
		frappe.db.set_value("Manufacturing Operation", mop, "other_wt", other + 0.25)
		actor.set("H_parked")
		actor.wait("H_release")
		return mop

	actor.attempt("rewrite operation", rewrite)
	actor.set("H_done")


def _role_hold_work_order(actor, *, mwo, waiter):
	"""A Stock-Entry-like transaction parked while it holds the work-order row (the lock
	``stamp_snc_requirement`` takes), then rolled back."""
	actor.wait(f"{waiter}_ready")
	frappe.db.sql(
		"SELECT name FROM `tabManufacturing Work Order` WHERE name = %s FOR UPDATE", mwo
	)
	actor.set("H_parked")
	actor.wait("H_release")
	frappe.db.rollback()
	actor.set("H_done")


# -- stress roles ----------------------------------------------------------------------------


def _stress_state(mwos):
	"""Current pointer state of every work order, read in a FRESH transaction."""
	frappe.db.rollback()
	rows = frappe.db.sql(
		"""
		SELECT w.name AS mwo, w.manufacturing_operation AS pointer, o.status, o.department,
			o.department_ir_status, o.employee, o.operation, o.department_issue_id,
			o.previous_mop
		FROM `tabManufacturing Work Order` w
		LEFT JOIN `tabManufacturing Operation` o ON o.name = w.manufacturing_operation
		WHERE w.name IN %(mwos)s
		ORDER BY w.name
		""",
		{"mwos": mwos},
		as_dict=True,
	)
	held = set(
		frappe.db.sql_list(
			"""
			SELECT o.manufacturing_work_order
			FROM `tabEmployee IR Operation` o
			INNER JOIN `tabEmployee IR` e ON e.name = o.parent
			WHERE o.manufacturing_work_order IN %(mwos)s AND e.docstatus = 0 AND e.type = 'Issue'
			""",
			{"mwos": mwos},
		)
	)
	for row in rows:
		row.held = row.mwo in held
	return rows


class _Stress:
	"""Per-worker bookkeeping: outcome counts per action and the failures that matter."""

	def __init__(self, actor, world, seed):
		self.actor = actor
		self.world = world
		self.rng = random.Random(seed)
		self.seq = 0
		self.counts = {}
		self.deadlocks = []
		self.timeouts = []
		self.others = []
		self.durations = []
		self.department_key = {v: k for k, v in world.departments.items()}
		self.employee_key = {v: k for k, v in world.employees.items()}

	def name(self, kind):
		self.seq += 1
		return f"{self.world.prefix}-{self.actor.role}-{kind}-{self.seq:04d}"

	def pick(self, rows):
		"""One or two of ``rows``, in random order (multi-row documents lock in sorted order)."""
		return self.rng.sample(rows, min(len(rows), self.rng.choice((1, 2))))

	def run(self, label, fn):
		rec = self.actor.attempt(label, fn, keep=False)
		self.durations.append(round(rec["elapsed"], 3))
		outcome = "ok" if rec["ok"] else _classify(rec["error"])
		key = f"{label} -> {outcome}"
		self.counts[key] = self.counts.get(key, 0) + 1
		if not rec["ok"]:
			error = rec["error"]
			brief = {
				"role": self.actor.role,
				"label": label,
				"type": error["type"],
				"message": error["message"][:300],
				"waited": error["waited"],
				"guard_rows": _on_guard_rows(error),
				"held": error["held"][-12:],
				"frames": error["frames"][-10:],
				"db_frames": error["db_frames"][-8:],
				"elapsed": round(rec["elapsed"], 3),
			}
			if outcome == "DEADLOCK":
				self.deadlocks.append(brief)
			elif ER_LOCK_WAIT_TIMEOUT in error["db_codes"] and len(self.timeouts) < 10:
				self.timeouts.append(brief)
			elif outcome.startswith("OTHER") and len(self.others) < 20:
				self.others.append({**brief, "traceback": error["traceback"]})
		return rec

	def submit_or_discard(self, label, doctype, doc):
		"""Production transaction boundaries: the draft commits, then the submit commits (as
		desk + the Submission Queue do); a refused submit discards its draft, so no draft keeps
		blocking its work orders -- or, for a Department Receive, its Issue -- for the rest of
		the run."""
		if not self.run(f"{label} draft", lambda: fx.insert_ir(doc).name)["ok"]:
			return
		time.sleep(
			self.rng.uniform(0, 0.03)
		)  # the committed draft is visible to everyone
		if not self.run(
			f"{label} submit", lambda: fx.submit_ir(doc.name, doctype).name
		)["ok"]:
			self.discard(doctype, doc.name)

	def discard(self, doctype, name):
		for _ in range(20):
			frappe.db.rollback()
			if frappe.db.get_value(doctype, name, "docstatus") != 0:
				return
			if self.run(
				"discard draft", lambda: frappe.get_doc(doctype, name).discard()
			)["ok"]:
				return
			time.sleep(self.rng.uniform(0.05, 0.2))
		self.actor.facts.setdefault("undiscarded", []).append(name)

	def report(self):
		durations = sorted(self.durations) or [0]
		self.actor.facts["stress"] = {
			"counts": self.counts,
			"deadlocks": self.deadlocks,
			"timeouts": self.timeouts,
			"others": self.others,
			"actions": len(self.durations),
			"p50": durations[len(durations) // 2],
			"max": durations[-1],
		}


def _stress_wait_for_go(actor):
	actor.set(f"{actor.role}_ready")
	actor.wait("go")
	return time.time()


def _role_stress_employee(actor, *, world, mwos, seconds, seed, employees):
	"""Employee Issues and Receives on one or two work orders per document: either a draft that
	commits and is then submitted (discarded when refused), or a REST insert with docstatus 1."""
	stress = _Stress(actor, world, seed)
	end = _stress_wait_for_go(actor) + seconds
	while time.time() < end:
		state = _stress_state(mwos)
		receivable = [
			s
			for s in state
			if s.status == "WIP"
			and s.employee
			and s.department_ir_status not in BLOCKED_TRANSIT
		]
		issuable = [
			s
			for s in state
			if s.status == "Not Started"
			and not s.employee
			and not s.operation
			and not s.held
			and s.department_ir_status not in BLOCKED_TRANSIT
			and stress.department_key.get(s.department) in employees
		]
		rest = stress.rng.random() < 0.4
		if receivable and (not issuable or stress.rng.random() < 0.5):
			groups = {}
			for s in receivable:
				groups.setdefault((s.department, s.employee, s.operation), []).append(s)
			(department, employee, _op), rows = stress.rng.choice(
				sorted(groups.items())
			)
			action = {
				"kind": "eir_receive",
				"rows": [(s.pointer, s.mwo) for s in stress.pick(rows)],
				"department": stress.department_key[department],
				"employee": stress.employee_key[employee],
				"name": stress.name("EIRR"),
				"docstatus": 1 if rest else 0,
			}
			label = "eir receive"
		elif issuable:
			by_department = {}
			for s in issuable:
				by_department.setdefault(
					stress.department_key[s.department], []
				).append(s)
			department, rows = stress.rng.choice(sorted(by_department.items()))
			action = {
				"kind": "eir_issue",
				"rows": [(s.pointer, s.mwo) for s in stress.pick(rows)],
				"department": department,
				"employee": stress.rng.choice(employees[department]),
				"name": stress.name("EIRI"),
				"docstatus": 1 if rest else 0,
			}
			label = "eir issue"
		else:
			time.sleep(stress.rng.uniform(0.01, 0.05))
			continue
		doc = _build(world, action)
		if rest:
			stress.run(f"{label} rest", lambda doc=doc: fx.insert_ir(doc).name)
		else:
			stress.submit_or_discard(label, "Employee IR", doc)
		time.sleep(stress.rng.uniform(0, 0.02))
	stress.report()


def _role_stress_department(actor, *, world, mwos, seconds, seed, routes):
	"""Department Issues (one or two work orders) and the matching Department Receives, each a
	draft that commits and is then submitted (discarded when refused)."""
	stress = _Stress(actor, world, seed)
	end = _stress_wait_for_go(actor) + seconds
	while time.time() < end:
		state = _stress_state(mwos)
		transit = [
			s
			for s in state
			if s.department_ir_status == "In-Transit" and s.status == "Not Started"
		]
		movable = [
			s
			for s in state
			if s.status == "Not Started"
			and not s.employee
			and not s.operation
			and not s.held
			and s.department_ir_status not in BLOCKED_TRANSIT
			and stress.department_key.get(s.department) in routes
		]
		if transit and (not movable or stress.rng.random() < 0.5):
			issue = stress.rng.choice(sorted({s.department_issue_id for s in transit}))
			source, target = frappe.db.get_value(
				"Department IR", issue, ["current_department", "next_department"]
			)
			action = {
				"kind": "dir_receive",
				# a Department Receive takes every work order of its Issue
				"rows": [s.mwo for s in transit if s.department_issue_id == issue],
				"current": stress.department_key[target],
				"previous": stress.department_key[source],
				"receive_against": issue,
				"name": stress.name("DIRR"),
			}
			label = "dir receive"
		elif movable:
			by_department = {}
			for s in movable:
				by_department.setdefault(
					stress.department_key[s.department], []
				).append(s)
			department, rows = stress.rng.choice(sorted(by_department.items()))
			action = {
				"kind": "dir_issue",
				"rows": [s.mwo for s in stress.pick(rows)],
				"current": department,
				"to": stress.rng.choice(routes[department]),
				"name": stress.name("DIRI"),
			}
			label = "dir issue"
		else:
			time.sleep(stress.rng.uniform(0.01, 0.05))
			continue
		stress.submit_or_discard(label, "Department IR", _build(world, action))
		time.sleep(stress.rng.uniform(0, 0.02))
	stress.report()


def _role_stress_stock(actor, *, world, mwos, seconds, seed, previous_share):
	"""Stock-Entry-like transactions: lock an operation ``FOR UPDATE`` (``MOPLog.validate``),
	then write its work order (``stamp_snc_requirement``) -- the MOP -> MWO order the guard
	follows. The operation is the pointer as read just before (it may be stale by the time it
	is locked, like a Stock Entry drafted earlier); ``previous_share`` of them deliberately take
	the pointer's predecessor instead (a Material Transfer drafted before the last move)."""
	stress = _Stress(actor, world, seed)
	end = _stress_wait_for_go(actor) + seconds
	while time.time() < end:
		state = {s.mwo: s for s in _stress_state(mwos)}
		mwo = stress.rng.choice(sorted(state))
		row = state[mwo]
		use_previous = bool(row.previous_mop) and stress.rng.random() < previous_share
		target = row.previous_mop if use_previous else row.pointer
		label = "stock entry on previous" if use_previous else "stock entry on pointer"

		def run(target=target, mwo=mwo):
			frappe.db.sql(
				"SELECT name FROM `tabManufacturing Operation` WHERE name = %s FOR UPDATE",
				target,
			)
			time.sleep(stress.rng.uniform(0, 0.02))
			frappe.db.sql(
				"UPDATE `tabManufacturing Work Order` SET snc_requirement = %s WHERE name = %s",
				("Not Need", mwo),
			)
			time.sleep(stress.rng.uniform(0, 0.01))

		stress.run(label, run)
		time.sleep(stress.rng.uniform(0, 0.02))
	stress.report()


# ==============================================================================================
# parent side
# ==============================================================================================


_LOCK_TABLES_READABLE = (
	None  # None = not probed yet; False = the site user lacks PROCESS
)


def _innodb_lock_wait(connection_id):
	"""The InnoDB lock wait of ``connection_id`` (``None`` if none, or if unreadable)."""
	global _LOCK_TABLES_READABLE
	if _LOCK_TABLES_READABLE is False:
		return None
	try:
		rows = frappe.db.sql(
			"""
			SELECT r.trx_mysql_thread_id AS waiting, b.trx_mysql_thread_id AS blocking,
				r.trx_state AS state, LEFT(r.trx_query, 300) AS query
			FROM information_schema.INNODB_LOCK_WAITS w
			INNER JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id
			INNER JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id
			WHERE r.trx_mysql_thread_id = %s
			""",
			connection_id,
			as_dict=True,
		)
	except Exception as exc:
		if getattr(exc, "args", None) and exc.args[0] == ER_SPECIFIC_ACCESS_DENIED:
			_LOCK_TABLES_READABLE = False
			return None
		raise
	_LOCK_TABLES_READABLE = True
	return rows[0] if rows else None


def _processlist(connection_id):
	rows = frappe.db.sql(
		"""
		SELECT ID, COMMAND, STATE, INFO, TIME_MS
		FROM information_schema.PROCESSLIST WHERE ID = %s
		""",
		connection_id,
		as_dict=True,
	)
	return rows[0] if rows else None


def _row_lock_current_waits():
	rows = frappe.db.sql("SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_current_waits'")
	return int(rows[0][1]) if rows else 0


class _Race:
	"""Spawned actors and the named Events that pin their interleaving (parent side)."""

	def __init__(self, events):
		self.mp = multiprocessing.get_context("spawn")
		self.events = {name: self.mp.Event() for name in events}
		self.outbox = self.mp.Queue()
		self.procs = {}
		self.hellos = {}
		self.reports = {}
		self.crashes = {}

	def start(self, role, fn, *, guard_mode="on", conf=None, **kwargs):
		spec = {
			"role": role,
			"fn": fn,
			"kwargs": kwargs,
			"site": frappe.local.site,
			"sites_path": os.path.abspath(frappe.local.sites_path),
			"code_root": CODE_ROOT,
			"conf": conf or {},
			"guard": guard_mode,
		}
		proc = self.mp.Process(
			target=_actor_main,
			args=(spec, self.events, self.outbox),
			name=f"cog-race-{role}",
			daemon=True,
		)
		proc.start()
		self.procs[role] = proc

	# -- message pump ------------------------------------------------------------------------

	def _pump(self, block_for=0.0):
		try:
			msg = (
				self.outbox.get(timeout=block_for)
				if block_for
				else self.outbox.get_nowait()
			)
		except queue.Empty:
			return False
		target = {
			"hello": self.hellos,
			"done": self.reports,
			"crash": self.crashes,
		}.get(msg["kind"])
		if target is not None:
			target[msg["role"]] = msg
		return True

	def _drain(self):
		while self._pump():
			pass

	def _check(self):
		self._drain()
		if self.crashes:
			details = {r: c["error"]["traceback"] for r, c in self.crashes.items()}
			raise AssertionError(f"race actor crashed: {details}")
		for role, proc in self.procs.items():
			if proc.exitcode not in (None, 0) and role not in self.reports:
				raise AssertionError(
					f"race actor {role} died with exit code {proc.exitcode}"
				)

	def await_event(self, name, timeout=EVENT_TIMEOUT):
		deadline = time.monotonic() + timeout
		while not self.events[name].wait(0.05):
			self._check()
			if time.monotonic() > deadline:
				raise AssertionError(f"timed out waiting for {name!r}")

	def is_set(self, name):
		return self.events[name].is_set()

	def set(self, name):
		self.events[name].set()

	def hello(self, role, timeout=EVENT_TIMEOUT):
		deadline = time.monotonic() + timeout
		while role not in self.hellos:
			self._pump(0.05)
			self._check()
			if time.monotonic() > deadline:
				raise AssertionError(f"race actor {role} never connected")
		return self.hellos[role]

	def prove_lock_wait(self, waiter, *needles, holder=None, min_ms=250, timeout=20):
		"""Evidence that ``waiter``'s connection is blocked on a row lock right now.

		``INNODB_LOCK_WAITS`` (waiter and blocker thread ids) when readable; otherwise the
		waiter's PROCESSLIST row must run a statement containing every needle for at least
		``min_ms`` while the holder's connection is idle in its transaction and the server
		reports at least one current row-lock wait.
		"""
		connection = self.hello(waiter)["connection_id"]
		holder_connection = self.hello(holder)["connection_id"] if holder else None
		deadline = time.monotonic() + timeout
		last = None
		while time.monotonic() < deadline:
			self._check()
			wait = _innodb_lock_wait(connection)
			if wait:
				if holder_connection is not None and wait.blocking != holder_connection:
					raise AssertionError(
						f"{waiter} waits on {wait.blocking}, not {holder}"
					)
				return {"source": "INNODB_LOCK_WAITS", **wait}
			row = last = _processlist(connection)
			info = cstr((row or {}).get("INFO")).upper()
			if (
				_LOCK_TABLES_READABLE is False
				and row
				and row.COMMAND == "Query"
				and flt(row.TIME_MS) >= min_ms
				and all(n.upper() in info for n in needles)
			):
				holder_row = (
					_processlist(holder_connection) if holder_connection else None
				)
				waits = _row_lock_current_waits()
				if waits >= 1 and (holder_row is None or holder_row.COMMAND == "Sleep"):
					return {
						"source": "PROCESSLIST",
						"waiter_state": row.STATE,
						"waiting_ms": flt(row.TIME_MS),
						"statement": "..." + cstr(row.INFO)[-160:],
						"holder_command": holder_row.COMMAND if holder_row else None,
						"row_lock_current_waits": waits,
					}
			time.sleep(0.05)
		raise AssertionError(
			f"{waiter} was never seen in a lock wait; last row: {last}"
		)

	def finish(self, timeout=EVENT_TIMEOUT * 2):
		"""Every actor's final report (raises if one crashed, died or overran ``timeout``)."""
		deadline = time.monotonic() + timeout
		exited = {}
		while set(self.procs) - set(self.reports):
			self._pump(0.1)
			self._check()
			for role, proc in self.procs.items():
				if role in self.reports or proc.exitcode is None:
					continue
				# its last message may still be in the pipe: allow it a moment
				exited.setdefault(role, time.monotonic())
				if time.monotonic() - exited[role] > 5:
					raise AssertionError(f"race actor {role} exited without a report")
			if time.monotonic() > deadline:
				missing = sorted(set(self.procs) - set(self.reports))
				raise AssertionError(f"race actors {missing} did not finish")
		for proc in self.procs.values():
			proc.join(15)
		return self.reports

	def close(self):
		"""Stop whatever still runs. A killed actor's connection drops and the server rolls its
		transaction back, so nothing it held survives into the purge."""
		for role, proc in self.procs.items():
			if proc.is_alive() and role in self.reports:
				proc.join(10)
		for proc in self.procs.values():
			if proc.is_alive():
				proc.terminate()
				proc.join(10)
		with contextlib.suppress(Exception):
			self.outbox.close()


def _shape(world, mwo, roles):
	"""Everything the guard protects on ``mwo``, without names: the pointer, every operation's
	state and lineage (``op<i>`` in creation order), its time logs, every MOP Log row, and the
	docstatus of the documents in ``roles`` (``{document name: role}``)."""
	department = {v: k for k, v in world.departments.items()}
	employee = {v: k for k, v in world.employees.items()}
	operation = {v: k for k, v in world.operations.items()}
	item = {v: k for k, v in world.item.items()}
	ops = fx.operations(mwo)
	index = {r.name: f"op{i}" for i, r in enumerate(ops)}

	def ref(name):
		if not name:
			return ""
		return roles.get(name) or index.get(name) or "?"

	operations = [
		{
			"status": r.status,
			"department": department.get(r.department, r.department),
			"department_ir_status": cstr(r.department_ir_status),
			"operation": operation.get(r.operation, cstr(r.operation)),
			"employee": employee.get(r.employee, cstr(r.employee)),
			"previous": ref(r.previous_mop),
			"department_issue_id": ref(r.department_issue_id),
			"department_receive_id": ref(r.department_receive_id),
			"employee_ir": ref(r.employee_ir),
			"time_logs": [
				(
					bool(t.from_time),
					bool(t.to_time),
					employee.get(t.employee, cstr(t.employee)),
				)
				for t in fx.time_logs(r.name)
			],
		}
		for r in ops
	]
	mop_logs = sorted(
		(
			ref(r.manufacturing_operation),
			item.get(r.item_code, r.item_code),
			r.voucher_type,
			ref(r.voucher_no),
			flt(r.qty_after_transaction_batch_based, 3),
			int(r.is_cancelled or 0),
			int(r.is_synced or 0),
			int(r.flow_index or 0),
		)
		for r in fx.mop_logs(manufacturing_work_order=mwo)
	)
	head = frappe.db.get_value(
		"Manufacturing Work Order",
		mwo,
		["manufacturing_operation", "department"],
		as_dict=True,
	)
	documents = {}
	for name, role in roles.items():
		for doctype in ("Employee IR", "Department IR"):
			status = frappe.db.get_value(doctype, name, "docstatus")
			if status is not None:
				documents[role] = (doctype, int(status))
				break
		else:
			documents[role] = "absent"
	return {
		"pointer": ref(head.manufacturing_operation),
		"department": department.get(head.department, head.department),
		"operations": operations,
		"mop_logs": mop_logs,
		"documents": documents,
	}


# Columns that can carry a world's prefix but that fixtures.purge does not delete by prefix.
_RESIDUE_COLUMNS = (
	("tabBin", ("item_code", "warehouse")),
	("tabStock Ledger Entry", ("item_code", "warehouse", "voucher_no")),
	("tabStock Entry", ("manufacturing_work_order", "employee_ir", "department_ir")),
	("tabStock Entry Detail", ("item_code", "s_warehouse", "t_warehouse")),
	("tabStock Reservation Entry", ("item_code", "warehouse")),
	(
		"tabMOP Log",
		("item_code", "manufacturing_work_order", "voucher_no", "to_warehouse"),
	),
	("tabManufacturing Operation", ("name", "manufacturing_work_order")),
	("tabEmployee IR Operation", ("parent", "manufacturing_work_order")),
	("tabDepartment IR Operation", ("parent", "manufacturing_work_order")),
	("tabError Log", ("reference_name", "method", "error")),
	("tabNotification Log", ("document_name", "subject")),
	("tabSubmission Queue", ("ref_docname", "exception")),
	("tabVersion", ("docname", "data")),
	("tabComment", ("reference_name", "subject", "content")),
	("tabDeleted Document", ("deleted_name", "data")),
	("tabSeries", ("name",)),
)


def _count_like(table, columns, needle):
	if not frappe.db.table_exists(table.removeprefix("tab")):
		return 0
	columns = [c for c in columns if frappe.db.has_column(table.removeprefix("tab"), c)]
	if not columns:
		return 0
	where = " OR ".join(f"`{c}` LIKE %(needle)s" for c in columns)
	return frappe.db.sql(
		f"SET STATEMENT max_statement_time=60 FOR SELECT COUNT(*) FROM `{table}` WHERE {where}",
		{"needle": f"%{needle}%"},
	)[0][0]


def _residue(prefix, operations):
	"""Rows that still name the world after its purge: prefix columns, plus the child rows,
	MOP Logs, Versions and Comments of the operations the controllers minted (named
	``MOP-...``, so they never carry the prefix themselves)."""
	left = {}
	for table, columns in _RESIDUE_COLUMNS:
		n = _count_like(table, columns, prefix)
		if n:
			left[table] = n
	if operations:
		checks = [
			(f"tab{df.options}", "parent")
			for df in frappe.get_meta("Manufacturing Operation").get_table_fields()
		] + [
			("tabMOP Log", "manufacturing_operation"),
			("tabVersion", "docname"),
			("tabComment", "reference_name"),
			("tabManufacturing Operation", "name"),
		]
		for table, column in checks:
			n = frappe.db.sql(
				f"SELECT COUNT(*) FROM `{table}` WHERE `{column}` IN %(names)s",
				{"names": operations},
			)[0][0]
			if n:
				left[f"{table}.{column}"] = left.get(f"{table}.{column}", 0) + n
	return left


def _scan_database(needle):
	"""``{table: rows}`` for every table with a text column containing ``needle``."""
	found = {}
	columns = frappe.db.sql(
		"""
		SELECT TABLE_NAME, GROUP_CONCAT(COLUMN_NAME) AS cols
		FROM information_schema.COLUMNS
		WHERE TABLE_SCHEMA = DATABASE()
			AND DATA_TYPE IN ('varchar', 'char', 'text', 'tinytext', 'mediumtext', 'longtext')
		GROUP BY TABLE_NAME
		""",
		as_dict=True,
	)
	for row in columns:
		where = " OR ".join(f"`{c}` LIKE %(needle)s" for c in row.cols.split(","))
		n = frappe.db.sql(
			f"SET STATEMENT max_statement_time=60 FOR "
			f"SELECT COUNT(*) FROM `{row.TABLE_NAME}` WHERE {where}",
			{"needle": f"%{needle}%"},
		)[0][0]
		if n:
			found[row.TABLE_NAME] = n
	return found


# Every world this module run created (purged again in tearDownModule, whatever happened).
_WORLDS = []


@contextlib.contextmanager
def _session_lock_wait(seconds):
	"""Bound this connection's row-lock waits (``innodb_lock_wait_timeout``) inside the block."""
	previous = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
	frappe.db.sql(f"SET SESSION innodb_lock_wait_timeout = {int(seconds)}")
	try:
		yield
	finally:
		frappe.db.sql(f"SET SESSION innodb_lock_wait_timeout = {int(previous)}")


def _purge(prefix, attempts=6):
	"""``fixtures.purge`` committed, retried after a rollback on a lock error.

	``purge`` deletes with ``LIKE '%<prefix>%'`` predicates: it scans whole tables under REPEATABLE
	READ and next-key-locks every row it examines, so a concurrent writer on the same site (another
	suite) can deadlock it or make it wait. It is idempotent, so retrying is safe.
	"""
	for attempt in range(attempts):
		try:
			with _session_lock_wait(10):
				deleted = fx.purge(prefix)
			frappe.db.commit()
			return deleted
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			frappe.db.rollback()
			if attempt == attempts - 1:
				raise
			time.sleep(0.5 * (attempt + 1))


def tearDownModule():
	"""Nothing of this module run may survive anywhere in the database: purge every world of the
	run once more (a test whose own purge failed has already reported it), then scan every text
	column of every table for the run's prefix."""
	if not RACES_ENABLED or not _WORLDS:
		return
	frappe.db.rollback()
	for prefix in _WORLDS:
		_purge(prefix)
	found = _scan_database(RUN_BASE)
	frappe.db.rollback()
	if found:
		raise AssertionError(f"rows naming {RUN_BASE} survived the race suite: {found}")


@contextlib.contextmanager
def _no_enqueue():
	"""Parent-side controller calls: record RQ enqueues instead of reaching the bench workers."""
	sink = []
	with patch(
		"frappe.utils.background_jobs.get_queue", lambda *a, **k: _RecordingQueue(sink)
	):
		yield sink
	if sink:
		raise AssertionError(f"a fixture cascade enqueued jobs: {sink}")


@unittest.skipUnless(RACES_ENABLED, SKIP_REASON)
class _RaceCase(IntegrationTestCase):
	"""One committed world per test; purged afterwards and verified clean."""

	def setUp(self):
		super().setUp()
		frappe.db.rollback()
		self.started = time.monotonic()
		self.race = None
		self.prefix = fx.new_prefix(RUN_BASE)
		_WORLDS.append(self.prefix)
		self.world = fx.make_world(self.prefix)
		self.commit()

	def tearDown(self):
		try:
			if self.race:
				self.race.close()
		finally:
			self._purge_and_verify()
			print(f"[race] {self.id()}: {time.monotonic() - self.started:.1f}s")
			super().tearDown()

	def _purge_and_verify(self):
		frappe.db.rollback()
		operations = frappe.db.sql_list(
			"SELECT name FROM `tabManufacturing Operation` WHERE manufacturing_work_order LIKE %s",
			f"{self.prefix}%",
		)
		frappe.db.rollback()
		_purge(self.prefix)
		fx.clear_caches(self.world)
		again = _purge(self.prefix)
		left = _residue(self.prefix, operations)
		frappe.db.rollback()
		self.assertEqual(again, {}, "a second purge still found rows")
		self.assertEqual(left, {}, "rows naming the world survived its purge")

	# -- helpers -----------------------------------------------------------------------------

	def commit(self):
		frappe.db.commit()

	def fresh(self):
		"""End the parent's transaction so its next read sees what the actors committed."""
		frappe.db.rollback()
		frappe.db.value_cache.clear()

	def doc_name(self, kind):
		return f"{self.prefix}-{kind}"

	def new_race(self, *events):
		self.race = _Race(events)
		return self.race

	def assert_code_under_test(self, reports, expected_enqueues=None):
		"""``expected_enqueues``: ``{role: [method, ...]}`` an actor is expected to have asked
		for (recorded, never sent -- e.g. ``frappe.delete_doc``'s dynamic-link cleanup, which
		Frappe enqueues outside its test runner)."""
		for role, hello in self.race.hellos.items():
			self.assertTrue(
				hello["code"].startswith(CODE_ROOT)
				and hello["guard"].startswith(CODE_ROOT),
				f"actor {role} imported {hello['code']}, not the code under test in {CODE_ROOT}",
			)
		for role, report in reports.items():
			self.assertEqual(
				report["enqueued"],
				(expected_enqueues or {}).get(role, []),
				f"actor {role} enqueued RQ jobs",
			)

	def attempt(self, reports, role, index=0):
		return reports[role]["attempts"][index]

	def assert_refused(self, rec, *types):
		self.assertFalse(rec["ok"], f"{rec['label']} was not refused")
		self.assertIn(rec["error"]["type"], types, rec["error"]["traceback"])
		return rec["error"]

	def assert_ok(self, rec):
		self.assertTrue(rec["ok"], rec.get("error", {}).get("traceback"))
		self.assertNotIn(
			rec["label"], self.race_commits_inside, "the action committed half-way"
		)

	@property
	def race_commits_inside(self):
		labels = []
		for report in (self.race.reports if self.race else {}).values():
			labels.extend(report.get("commits_inside") or [])
		return labels

	def issue_drafts(self, mwo):
		return frappe.db.sql_list(
			"""
			SELECT DISTINCT e.name FROM `tabEmployee IR Operation` o
			INNER JOIN `tabEmployee IR` e ON e.name = o.parent
			WHERE o.manufacturing_work_order = %s AND e.docstatus = 0 AND e.type = 'Issue'
			ORDER BY e.name
			""",
			mwo,
		)

	def docstatus(self, doctype, name):
		return frappe.db.get_value(doctype, name, "docstatus")

	def summary(self, **values):
		"""One line per test in the run log: who won, how, and how long the waiter waited."""
		print(f"[race] {self.id()}: {values}")


# ==============================================================================================
# 1. the 2026-10-01 race: two Employee Issue drafts for one work order
# ==============================================================================================


class TestTenOneRace(_RaceCase):
	"""B opens its REPEATABLE READ snapshot, A inserts an Employee Issue draft for work order X and
	holds the guard's block, B inserts another Issue draft for X. On 2026-10-01 both drafts
	committed (43405 and 43408): B's plain duplicate check could not see A's draft."""

	def _race(self, control):
		mwo, mop = fx.make_work_order(self.world, "MM")
		self.commit()
		before = fx.snapshot(mwo)
		first, second = self.doc_name("EIR-A"), self.doc_name("EIR-B")
		race = self.new_race(
			"A_parked", "A_release", "A_done", "B_snapshot", "B_inserting", "B_returned"
		)
		mode = "off" if control else "on"
		race.start(
			"A",
			"_role_ten_one_first",
			guard_mode=mode,
			world=self.world,
			mwo=mwo,
			name=first,
			control=control,
		)
		race.start(
			"B",
			"_role_ten_one_second",
			guard_mode=mode,
			world=self.world,
			mwo=mwo,
			name=second,
			other=first,
		)
		race.await_event("A_parked")
		evidence = None
		if control:
			race.await_event("B_returned")
			# B's insert completed while A was still paused inside its own insert.
			self.assertFalse(race.is_set("A_done"))
		else:
			race.await_event("B_inserting")
			evidence = race.prove_lock_wait(
				"B", "FOR UPDATE", "tabManufacturing Operation", mop, holder="A"
			)
		race.set("A_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()
		return mwo, before, first, second, reports, evidence

	def test_the_guard_leaves_exactly_one_draft(self):
		mwo, before, first, second, reports, evidence = self._race(control=False)
		a, b = self.attempt(reports, "A"), self.attempt(reports, "B")

		self.assert_ok(a)
		error = self.assert_refused(b, "OutstandingDraftError", "WorkOrderBusyError")
		facts = reports["B"]["facts"]
		self.assertFalse(facts["snapshot_sees_other"])
		# The 10-01 condition: when B decided, its snapshot could not see A's committed draft,
		# so the plain early check passed and only the terminal locking re-check could catch it.
		self.assertIs(facts["decision_sees_other"], False)
		if error["type"] == "OutstandingDraftError":
			self.assertIn("final_draft_check", error["frames"])
			self.assertIn(first, error["message"])

		self.assertEqual(self.issue_drafts(mwo), [first])
		self.assertIsNone(self.docstatus("Employee IR", second))
		self.assertEqual(fx.snapshot(mwo), before)
		self.summary(
			loser=error["type"],
			raised_in=_where(error),
			lock_wait=evidence,
			b_waited_s=round(b["t_return"] - b["t0"], 2),
		)

	def test_control_without_the_guard_both_drafts_commit(self):
		"""The same schedule with every guard entry point patched to a no-op in both processes:
		both drafts commit, exactly as on 2026-10-01 -- so the test above is sensitive."""
		mwo, before, first, second, reports, _ = self._race(control=True)

		self.assert_ok(self.attempt(reports, "A"))
		self.assert_ok(self.attempt(reports, "B"))
		self.assertFalse(reports["B"]["facts"]["commit_sees_other"])
		self.assertEqual(self.issue_drafts(mwo), sorted([first, second]))
		self.assertEqual(fx.snapshot(mwo), before)
		self.summary(drafts=self.issue_drafts(mwo))


# ==============================================================================================
# two transactions racing for one work order: the common driver
# ==============================================================================================


class _TwinRace(_RaceCase):
	"""A race on work order X, with its twin Y given the same history for the reference run."""

	def twins(self, history):
		"""Two work orders with identical histories; ``history(mwo, tag)`` returns ``{name: role}``."""
		pairs = []
		for tag in ("X", "Y"):
			mwo, mop = fx.make_work_order(self.world, "MM")
			with _no_enqueue():
				roles = history(mwo, mop, tag)
			pairs.append(SimpleNamespace(mwo=mwo, mop=mop, roles=roles, tag=tag))
		self.commit()
		return pairs

	def run_race(self, x, y, *, first, second, actions, reference, needles, conf=None):
		"""``actions[role]`` races on X; ``reference`` (actions on Y) replays the expected winner.

		Returns the reports and the lock-wait evidence. Asserts that the twin shapes match.
		"""
		race = self.new_race(
			*(
				f"{r}_{e}"
				for r in (first, second)
				for e in ("ready", "parked", "release", "started", "done")
			),
			"REF_go",
			"REF_done",
		)
		race.start(
			first,
			"_role_racer",
			world=self.world,
			action=actions[first],
			me=first,
			order="first",
			other=second,
		)
		race.start(
			second,
			"_role_racer",
			conf=conf,
			world=self.world,
			action=actions[second],
			me=second,
			order="second",
			other=first,
		)
		race.start("REF", "_role_reference", world=self.world, actions=reference)
		race.await_event(f"{first}_parked")
		race.await_event(f"{second}_started")
		evidence = race.prove_lock_wait(second, *needles, holder=first)
		race.set(f"{first}_release")
		race.await_event(f"{first}_done")
		race.await_event(f"{second}_done")
		race.set("REF_go")
		race.await_event("REF_done")
		reports = race.finish()
		self.assert_code_under_test(reports)
		for rec in reports["REF"]["attempts"]:
			self.assert_ok(rec)
		self.fresh()
		self.assertEqual(
			_shape(self.world, x.mwo, x.roles),
			_shape(self.world, y.mwo, y.roles),
			"the race left a different state than the winner alone",
		)
		return reports, evidence


# ==============================================================================================
# 2. Employee Issue submit vs Department Issue submit, both acquisition orders
# ==============================================================================================


class TestIssueSubmitVersusDepartmentSubmit(_TwinRace):
	"""Exactly one of the two commits and the loser leaves no MOP / MOP Log / MWO change.

	``draft``: the Employee Issue is a saved draft (the production path: draft, then a queued
	submit). Its draft blocks the Department Issue, so the Employee Issue wins in BOTH orders.
	``rest``: the Employee Issue is inserted with docstatus 1 (REST insert-and-submit), so
	whoever takes the lock block first wins.
	"""

	def _history(self, eir_mode):
		def history(mwo, mop, tag):
			transfer = fx.insert_ir(
				fx.department_ir(self.world, "Issue", [mwo], current="MM", to="PP")
			)
			roles = {transfer.name: "DIR"}
			name = self.doc_name(f"EIR-{tag}")
			if eir_mode == "draft":
				# After the Department Issue draft: Department IR drafts never block.
				draft = fx.employee_ir(self.world, "Issue", [mwo], employee="MM1")
				draft.flags.fixture_name = name
				fx.insert_ir(draft)
			roles[name] = "EIR"
			return roles

		return history

	def _actions(self, pair, eir_mode):
		eir = next(n for n, r in pair.roles.items() if r == "EIR")
		dir_ = next(n for n, r in pair.roles.items() if r == "DIR")
		if eir_mode == "draft":
			e = {
				"op": "submit",
				"doctype": "Employee IR",
				"name": eir,
				"label": "submit EIR",
			}
		else:
			e = {
				"op": "insert",
				"kind": "eir_issue",
				"rows": [pair.mwo],
				"name": eir,
				"docstatus": 1,
				"label": "insert+submit EIR",
			}
		d = {
			"op": "submit",
			"doctype": "Department IR",
			"name": dir_,
			"label": "submit DIR",
		}
		return {"E": e, "D": d}

	def _run(self, eir_mode, first):
		second = "D" if first == "E" else "E"
		x, y = self.twins(self._history(eir_mode))
		winner = "E" if eir_mode == "draft" else first
		reports, evidence = self.run_race(
			x,
			y,
			first=first,
			second=second,
			actions=self._actions(x, eir_mode),
			reference=[self._actions(y, eir_mode)[winner]],
			needles=("FOR UPDATE", "tabManufacturing Operation", x.mop),
		)
		loser = "D" if winner == "E" else "E"
		won, lost = self.attempt(reports, winner), self.attempt(reports, loser)
		self.assert_ok(won)
		error = self.assert_refused(lost, *GUARD_ERRORS)
		eir = next(n for n, r in x.roles.items() if r == "EIR")
		dir_ = next(n for n, r in x.roles.items() if r == "DIR")
		committed = {
			"E": self.docstatus("Employee IR", eir) == 1,
			"D": self.docstatus("Department IR", dir_) == 1,
		}
		self.assertEqual(committed, {winner: True, loser: False})
		self.assertEqual(fx.mop_logs(voucher_no=eir if loser == "E" else dir_), [])
		self.summary(
			mode=eir_mode,
			first=first,
			winner=winner,
			loser=error["type"],
			raised_in=_where(error),
			lock_wait=evidence,
		)
		return error

	def test_saved_eir_draft_takes_the_block_first(self):
		error = self._run("draft", first="E")
		# D's snapshot predates E's commit, so its plain read still shows E as a draft; the
		# confirmation sees E submitted and drops it. D is refused on the fresh operation
		# (WIP now), before any side effect.
		self.assertEqual(error["type"], "CurrentOperationError")
		self.assertNotIn("final_draft_check", error["frames"])

	def test_department_issue_takes_the_block_first_and_is_refused_by_the_draft(self):
		error = self._run("draft", first="D")
		self.assertEqual(error["type"], "OutstandingDraftError")
		self.assertNotIn("final_draft_check", error["frames"])

	def test_rest_eir_issue_takes_the_block_first_and_wins(self):
		error = self._run("rest", first="E")
		self.assertEqual(error["type"], "CurrentOperationError")

	def test_department_issue_takes_the_block_first_and_wins(self):
		error = self._run("rest", first="D")
		self.assertEqual(error["type"], "StaleOperationError")


# ==============================================================================================
# 3. Employee Issue DRAFT insert vs Department Issue submit, both orders
# ==============================================================================================


class TestIssueDraftVersusDepartmentSubmit(_TwinRace):
	"""Never both committed: a new Employee Issue draft and a Department Issue submit of the same
	work order. When the draft wins, the Department Issue -- whose snapshot predates the draft --
	passes the plain early check, runs its side effects and is refused by the terminal NOWAIT
	re-check, which rolls all of it back."""

	def _run(self, first):
		second = "D" if first == "E" else "E"

		def history(mwo, mop, tag):
			transfer = fx.insert_ir(
				fx.department_ir(self.world, "Issue", [mwo], current="MM", to="PP")
			)
			return {transfer.name: "DIR", self.doc_name(f"EIR-{tag}"): "EIR"}

		x, y = self.twins(history)

		def actions(pair):
			eir = next(n for n, r in pair.roles.items() if r == "EIR")
			dir_ = next(n for n, r in pair.roles.items() if r == "DIR")
			return {
				"E": {
					"op": "insert",
					"kind": "eir_issue",
					"rows": [pair.mwo],
					"name": eir,
					"label": "insert EIR draft",
				},
				"D": {
					"op": "submit",
					"doctype": "Department IR",
					"name": dir_,
					"label": "submit DIR",
				},
			}

		reports, evidence = self.run_race(
			x,
			y,
			first=first,
			second=second,
			actions=actions(x),
			reference=[actions(y)[first]],
			needles=("FOR UPDATE", "tabManufacturing Operation", x.mop),
		)
		won, lost = self.attempt(reports, first), self.attempt(reports, second)
		self.assert_ok(won)
		error = self.assert_refused(lost, *GUARD_ERRORS)
		eir = next(n for n, r in x.roles.items() if r == "EIR")
		dir_ = next(n for n, r in x.roles.items() if r == "DIR")
		self.assertFalse(
			self.docstatus("Employee IR", eir) == 0
			and self.docstatus("Department IR", dir_) == 1,
			"the draft and the transfer both committed",
		)
		self.summary(
			first=first,
			loser=error["type"],
			raised_in=_where(error),
			lock_wait=evidence,
		)
		return error, x, dir_

	def test_the_draft_takes_the_block_first(self):
		error, x, dir_ = self._run(first="E")
		self.assertEqual(error["type"], "OutstandingDraftError")
		# Refused by the terminal re-check AFTER the transfer ran (and was rolled back).
		self.assertIn("final_draft_check", error["frames"])
		self.assertEqual(self.docstatus("Department IR", dir_), 0)
		self.assertEqual(fx.mop_logs(voucher_no=dir_), [])
		self.assertEqual(fx.pointer(x.mwo), x.mop)

	def test_the_department_issue_takes_the_block_first(self):
		error, x, _dir = self._run(first="D")
		self.assertEqual(error["type"], "StaleOperationError")
		eir = next(n for n, r in x.roles.items() if r == "EIR")
		self.assertIsNone(self.docstatus("Employee IR", eir))


# ==============================================================================================
# 4. two submits of one Employee Issue, and a retried queue job
# ==============================================================================================


class TestDoubleSubmit(_TwinRace):
	def test_one_transition_and_side_effects_written_once(self):
		"""The queued submit (Submission Queue job replayed in-process) holds the block; a desk
		submit of the same draft, loaded before the job committed, waits for that block -- taken
		before Frappe's check_if_latest locks the document's rows -- and is then refused as stale
		by check_if_latest; then the SAME job runs again (a retry) and is refused too. One
		transition: MOP Log rows and the time log are written exactly once."""

		def history(mwo, mop, tag):
			draft = fx.employee_ir(self.world, "Issue", [mwo], employee="MM1")
			draft.flags.fixture_name = self.doc_name(f"EIR-{tag}")
			fx.insert_ir(draft)
			return {draft.name: "EIR"}

		x, y = self.twins(history)
		eir = next(iter(x.roles))
		with patch.object(CustomSubmissionQueue, "queue_action") as queue_action:
			queue_submission(fx.reload(eir, "Employee IR"), "Submit")
		self.commit()
		job = queue_action.call_args.kwargs
		handed = pickle.dumps(job["to_be_queued_doc"])
		queued = frappe.db.get_value("Submission Queue", {"ref_docname": eir}, "name")

		job_action = {
			"op": "queue_job",
			"queue": queued,
			"job_id": job["job_id"],
			"handed": handed,
			"label": "queue job",
		}
		race = self.new_race(
			*(
				f"{r}_{e}"
				for r in ("J", "S")
				for e in ("ready", "parked", "release", "started", "done")
			),
			"R_done",
			"REF_go",
			"REF_done",
		)
		race.start(
			"J",
			"_role_racer",
			world=self.world,
			action=job_action,
			me="J",
			order="first",
			other="S",
		)
		race.start(
			"S",
			"_role_racer",
			world=self.world,
			action={
				"op": "submit",
				"doctype": "Employee IR",
				"name": eir,
				"label": "desk submit",
			},
			me="S",
			order="second",
			other="J",
		)
		race.start(
			"R",
			"_role_later",
			world=self.world,
			action={**job_action, "label": "retried queue job"},
			me="R",
			after=("J_done", "S_done"),
		)
		race.start(
			"REF",
			"_role_reference",
			world=self.world,
			actions=[
				{
					"op": "submit",
					"doctype": "Employee IR",
					"name": next(iter(y.roles)),
					"label": "submit",
				}
			],
		)
		race.await_event("J_parked")
		race.await_event("S_started")
		evidence = race.prove_lock_wait(
			"S", "FOR UPDATE", "tabManufacturing Operation", x.mop, holder="J"
		)
		race.set("J_release")
		race.await_event("R_done")
		race.set("REF_go")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		first, desk, retry = (
			self.attempt(reports, "J"),
			self.attempt(reports, "S"),
			self.attempt(reports, "R"),
		)
		self.assertTrue(first["ok"], first.get("error"))
		self.assertEqual(first["value"], "Finished")
		error = self.assert_refused(desk, "TimestampMismatchError")
		self.assertIn("check_if_latest", error["frames"])
		# The retried job swallows its failure into the queue row, as the worker does.
		self.assertTrue(retry["ok"], retry.get("error"))
		self.assertEqual(retry["value"], "Failed")
		row = frappe.db.get_value(
			"Submission Queue", queued, ["status", "exception"], as_dict=True
		)
		self.assertEqual(row.status, "Failed")
		self.assertIn(
			"TimestampMismatchError", (row.exception or "").strip().splitlines()[-1]
		)
		self.assertEqual(reports["J"]["facts"]["notified"], ["Finished"])
		self.assertEqual(reports["R"]["facts"]["notified"], ["Failed"])

		self.assertEqual(self.docstatus("Employee IR", eir), 1)
		self.assertEqual(len(fx.mop_logs(voucher_no=eir)), 2)
		self.assertEqual([t.to_time for t in fx.time_logs(x.mop)], [None])
		self.assertEqual(
			_shape(self.world, x.mwo, x.roles), _shape(self.world, y.mwo, y.roles)
		)
		self.summary(
			desk=error["type"],
			retry=row.status,
			lock_wait=evidence,
			desk_waited_s=round(desk["t_return"] - desk["t0"], 2),
		)


# ==============================================================================================
# 5. lock-wait timeout
# ==============================================================================================


class TestLockWaitTimeout(_TwinRace):
	"""The holder keeps the block; the waiter (``current_operation_lock_wait`` = 3) gets
	``WorkOrderBusyError`` after about that long and writes nothing."""

	def test_waiting_for_the_operation_lock_times_out_as_busy(self):
		def history(mwo, mop, tag):
			return {
				self.doc_name(f"EIR-{tag}"): "EIR",
				self.doc_name(f"DIR-{tag}"): "DIR",
			}

		x, y = self.twins(history)

		def holder(pair):
			return {
				"op": "insert",
				"kind": "eir_issue",
				"rows": [pair.mwo],
				"name": next(n for n, r in pair.roles.items() if r == "EIR"),
				"label": "insert EIR draft",
			}

		waiter = {
			"op": "insert",
			"kind": "dir_issue",
			"rows": [x.mwo],
			"current": "MM",
			"to": "PP",
			"name": next(n for n, r in x.roles.items() if r == "DIR"),
			"label": "insert DIR",
		}
		race = self.new_race(
			*(
				f"{r}_{e}"
				for r in ("H", "W")
				for e in ("ready", "parked", "release", "started", "done")
			),
			"REF_go",
			"REF_done",
		)
		race.start(
			"H",
			"_role_racer",
			world=self.world,
			action=holder(x),
			me="H",
			order="first",
			other="W",
		)
		race.start(
			"W",
			"_role_racer",
			conf={"current_operation_lock_wait": SHORT_LOCK_WAIT},
			world=self.world,
			action=waiter,
			me="W",
			order="second",
			other="H",
		)
		race.start("REF", "_role_reference", world=self.world, actions=[holder(y)])
		race.await_event("H_parked")
		race.await_event("W_started")
		evidence = race.prove_lock_wait(
			"W", "FOR UPDATE WAIT", "tabManufacturing Operation", x.mop, holder="H"
		)
		race.await_event("W_done")  # timed out while H still holds the block
		race.set("H_release")
		race.await_event("H_done")
		race.set("REF_go")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		held, waited = self.attempt(reports, "H"), self.attempt(reports, "W")
		self.assert_ok(held)
		error = self.assert_refused(waited, "WorkOrderBusyError")
		self.assertIn(ER_LOCK_WAIT_TIMEOUT, error["db_codes"])
		# the statement that timed out is the guard's operation lock, and the message says so
		self.assertIn("lock_manufacturing_operations", error["db_frames"])
		self.assertTrue(
			error["message"].startswith("Manufacturing Operation"), error["message"]
		)
		self.assertIn(x.mop, error["message"])
		self.assertGreaterEqual(waited["elapsed"], SHORT_LOCK_WAIT - 0.1)
		self.assertLess(waited["elapsed"], SHORT_LOCK_WAIT + TIMEOUT_SLACK)
		self.assertIsNone(self.docstatus("Department IR", waiter["name"]))
		self.assertEqual(
			_shape(self.world, x.mwo, x.roles), _shape(self.world, y.mwo, y.roles)
		)
		self.summary(waited_s=round(waited["elapsed"], 2), lock_wait=evidence)

	def test_waiting_for_the_work_order_lock_times_out_as_busy(self):
		"""The holder is a Stock-Entry-like transaction on the WORK ORDER row only: the waiter
		gets the operation lock, then times out on the work order."""
		mwo, mop = fx.make_work_order(self.world, "MM")
		with _no_enqueue():
			transfer = fx.insert_ir(
				fx.department_ir(self.world, "Issue", [mwo], current="MM", to="PP")
			)
		self.commit()
		before = fx.snapshot(mwo)
		race = self.new_race(
			"W_ready",
			"W_parked",
			"W_release",
			"W_started",
			"W_done",
			"H_ready",
			"H_parked",
			"H_release",
			"H_done",
		)
		race.start("H", "_role_hold_work_order", mwo=mwo, waiter="W")
		race.start(
			"W",
			"_role_racer",
			conf={"current_operation_lock_wait": SHORT_LOCK_WAIT},
			world=self.world,
			action={
				"op": "submit",
				"doctype": "Department IR",
				"name": transfer.name,
				"label": "submit DIR",
			},
			me="W",
			order="second",
			other="H",
		)
		race.await_event("H_parked")
		race.await_event("W_started")
		evidence = race.prove_lock_wait(
			"W", "FOR UPDATE WAIT", "tabManufacturing Work Order", mwo, holder="H"
		)
		race.await_event("W_done")
		race.set("H_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		waited = self.attempt(reports, "W")
		error = self.assert_refused(waited, "WorkOrderBusyError")
		self.assertIn(ER_LOCK_WAIT_TIMEOUT, error["db_codes"])
		# the operation lock was granted; the work-order lock is the one that timed out
		self.assertIn("lock_work_orders", error["db_frames"])
		self.assertTrue(error["message"].startswith("Work order"), error["message"])
		self.assertIn(mwo, error["message"])
		self.assertGreaterEqual(waited["elapsed"], SHORT_LOCK_WAIT - 0.1)
		self.assertLess(waited["elapsed"], SHORT_LOCK_WAIT + TIMEOUT_SLACK)
		self.assertEqual(self.docstatus("Department IR", transfer.name), 0)
		self.assertEqual(fx.mop_logs(voucher_no=transfer.name), [])
		self.assertEqual(fx.snapshot(mwo), before)
		self.summary(waited_s=round(waited["elapsed"], 2), lock_wait=evidence)


# ==============================================================================================
# 6. lock-order stress
# ==============================================================================================


class TestLockOrderStress(_RaceCase):
	"""Employee IR, Department IR and Stock-Entry-like workers on one pool of sibling work
	orders for ``STRESS_SECONDS``: no deadlock (1213) involving the guard's rows, and every work
	order ends with exactly one open operation -- its pointer -- and no MOP Log row of a document
	that did not commit.

	A deadlock "involves the guard block" when its victim was waiting on a Manufacturing
	Operation / Manufacturing Work Order row lock: those rows are the block, and only the block
	and Stock Entries take them. Deadlocks on other rows (Frappe re-writing a MOP's time-log child
	rows with DELETE + INSERT under gap locks) are reported in the run log but not asserted. Each
	failure carries the statements its transaction had locked and the one it waited on.
	"""

	MWOS = 6
	EMPLOYEES = {"MM": ("MM1", "MM2"), "PP": ("PP1",), "FP": ("FP1",)}
	ROUTES = {"MM": ("PP",), "PP": ("MM", "FP"), "FP": ("PP",)}
	SUBMITS = (
		"eir issue rest",
		"eir issue submit",
		"eir receive rest",
		"eir receive submit",
		"dir issue submit",
		"dir receive submit",
	)

	def _stress(self, workers):
		mwos = [fx.make_work_order(self.world, "MM")[0] for _ in range(self.MWOS)]
		self.commit()
		race = self.new_race("go", *(f"{role}_ready" for role in workers))
		seed = int(
			os.environ.get("CURRENT_OPERATION_RACE_SEED") or random.randrange(1 << 30)
		)
		for i, (role, (fn, extra)) in enumerate(workers.items()):
			race.start(
				role,
				fn,
				world=self.world,
				mwos=mwos,
				seconds=STRESS_SECONDS,
				seed=seed + i,
				**extra,
			)
		for role in workers:
			race.await_event(f"{role}_ready")
		race.set("go")
		reports = race.finish(timeout=STRESS_SECONDS + EVENT_TIMEOUT * 2)
		self.assert_code_under_test(reports)
		self.fresh()

		result = SimpleNamespace(
			mwos=mwos,
			seed=seed,
			counts={},
			deadlocks=[],
			timeouts=[],
			others=[],
			actions=0,
		)
		for role, report in reports.items():
			stats = report["facts"]["stress"]
			result.actions += stats["actions"]
			result.deadlocks += stats["deadlocks"]
			result.timeouts += stats["timeouts"]
			result.others += stats["others"]
			for key, n in stats["counts"].items():
				result.counts[key] = result.counts.get(key, 0) + n
			self.assertFalse(report["facts"].get("undiscarded"), f"{role} left drafts")
		result.submitted = sum(
			n
			for key, n in result.counts.items()
			if key.endswith("-> ok") and key.split(" -> ")[0] in self.SUBMITS
		)
		result.guard_deadlocks = [d for d in result.deadlocks if d["guard_rows"]]
		self.summary(
			seed=seed,
			seconds=STRESS_SECONDS,
			workers=sorted(workers),
			actions=result.actions,
			submitted=result.submitted,
			counts=dict(sorted(result.counts.items())),
			deadlocks=result.deadlocks,
			lock_timeouts=result.timeouts,
		)
		self.assert_consistent(result)
		return result

	def assert_no_guard_deadlock(self, result):
		if result.guard_deadlocks:
			first = [
				{k: d[k] for k in ("role", "label", "waited", "frames")}
				for d in result.guard_deadlocks[:3]
			]
			self.fail(
				f"{len(result.guard_deadlocks)} deadlock(s) on the guard's rows (1213) in "
				f"{result.actions} actions (seed {result.seed}); first: {first}"
			)

	def assert_consistent(self, result):
		self.assertEqual(result.others, [], "unexpected errors")
		self.assertGreaterEqual(
			result.submitted, 20, "the stress did too little work to mean anything"
		)
		for mwo in result.mwos:
			ops = fx.operations(mwo)
			pointer = fx.pointer(mwo)
			open_ops = [
				r.name
				for r in ops
				if guard.is_open_operation(r.status, r.department_ir_status)
			]
			self.assertEqual(open_ops, [pointer], f"{mwo}: open operations != pointer")
			self.assertFalse(
				[r.name for r in ops if r.previous_mop == pointer],
				f"{mwo}: the pointer has a successor",
			)
			self.assertEqual(
				self.issue_drafts(mwo), [], f"{mwo}: an Issue draft was left"
			)
		vouchers = frappe.db.sql(
			"""
			SELECT DISTINCT voucher_type, voucher_no FROM `tabMOP Log`
			WHERE manufacturing_work_order IN %(mwos)s AND is_cancelled = 0
				AND voucher_type IN ('Employee IR', 'Department IR')
			""",
			{"mwos": result.mwos},
		)
		for voucher_type, voucher_no in vouchers:
			self.assertEqual(
				self.docstatus(voucher_type, voucher_no),
				1,
				f"MOP Log rows of {voucher_type} {voucher_no}, which is not submitted",
			)

	def test_employee_ir_and_stock_entries(self):
		"""Employee Issues / Receives (drafts then submits, and REST inserts) against
		Stock-Entry-like transactions on the pointer or its predecessor."""
		result = self._stress(
			{
				"E1": ("_role_stress_employee", {"employees": self.EMPLOYEES}),
				"E2": ("_role_stress_employee", {"employees": self.EMPLOYEES}),
				"S1": ("_role_stress_stock", {"previous_share": 0.3}),
				"S2": ("_role_stress_stock", {"previous_share": 0.3}),
			}
		)
		self.assert_no_guard_deadlock(result)

	def test_employee_ir_department_ir_and_stock_entries(self):
		"""The full mix: Employee IR, Department IR and Stock-Entry-like workers.

		Failed before the fix (31 deadlocks in 60 s): a Department IR SAVE took the block (row
		MOP, then MWO) and then ``validate_and_update_gross_wt_from_mop`` ->
		``update_previous_mop_data`` wrote ``received_net_wt`` / ``received_gross_wt`` on the row
		operation's PREVIOUS operation -- a Manufacturing Operation lock taken after the work-order
		lock (lock_order RULE E). Any transaction holding that previous operation and then wanting
		the work order -- a Stock Entry posted against it (``MOPLog.validate`` then
		``stamp_snc_requirement``), or a stale Employee IR / Department IR naming it, whose block
		locks it before the work order -- closed the cycle. A Department IR save now locks the
		previous operations in the block's operation phase.
		"""
		result = self._stress(
			{
				"E1": ("_role_stress_employee", {"employees": self.EMPLOYEES}),
				"E2": ("_role_stress_employee", {"employees": self.EMPLOYEES}),
				"D1": ("_role_stress_department", {"routes": self.ROUTES}),
				"D2": ("_role_stress_department", {"routes": self.ROUTES}),
				"S1": ("_role_stress_stock", {"previous_share": 0.3}),
				"S2": ("_role_stress_stock", {"previous_share": 0.3}),
			}
		)
		self.assert_no_guard_deadlock(result)


# ==============================================================================================
# 7. defects this harness and the review exposed, now fixed
# ==============================================================================================


class TestFormerDefects(_RaceCase):
	"""Interleavings in which the guard used to decide wrong. Each test replays one with real
	transactions and asserts the fixed behaviour; the docstrings say what happened before."""

	def _events(self, *roles):
		return [
			f"{r}_{e}"
			for r in roles
			for e in ("ready", "parked", "release", "started", "done")
		]

	def test_a_receive_that_waited_on_its_issue_submit_is_accepted(self):
		"""BEFORE: ``_check`` decided "another Employee Issue draft holds this work order" from a
		PLAIN read taken after waiting for the lock block. The Receive's REPEATABLE READ snapshot
		predated the commit of the Issue's SUBMIT, so a valid Receive -- by the time it held the
		locks the operation was WIP with this employee -- was refused with OutstandingDraftError
		naming an Issue that was already submitted. NOW: the hit is confirmed by a current read of
		the draft's row (submitted meanwhile -> dropped) and the Receive is accepted."""
		mwo, mop = fx.make_work_order(self.world, "MM")
		draft = fx.employee_ir(self.world, "Issue", [mwo], employee="MM1")
		draft.flags.fixture_name = self.doc_name("EIR-I")
		with _no_enqueue():
			fx.insert_ir(draft)
		self.commit()
		receive = {
			"op": "insert",
			"kind": "eir_receive",
			"rows": [mwo],
			"employee": "MM1",
			"name": self.doc_name("EIR-R"),
			"label": "insert receive draft",
		}
		race = self.new_race(*self._events("J", "R"))
		race.start(
			"J",
			"_role_racer",
			world=self.world,
			action={
				"op": "submit",
				"doctype": "Employee IR",
				"name": draft.name,
				"label": "submit issue",
			},
			me="J",
			order="first",
			other="R",
		)
		race.start(
			"R",
			"_role_racer",
			world=self.world,
			action=receive,
			me="R",
			order="second",
			other="J",
		)
		race.await_event("J_parked")
		race.await_event("R_started")
		evidence = race.prove_lock_wait(
			"R", "FOR UPDATE", "tabManufacturing Operation", mop, holder="J"
		)
		race.set("J_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		self.assert_ok(self.attempt(reports, "J"))
		self.assertEqual(self.docstatus("Employee IR", draft.name), 1)
		self.assertEqual(
			(fx.operation(mop).status, fx.operation(mop).employee),
			("WIP", self.world.employees.MM1),
		)
		self.assert_ok(self.attempt(reports, "R"))
		self.assertEqual(self.docstatus("Employee IR", receive["name"]), 0)
		self.summary(receive="accepted", lock_wait=evidence)

	def test_a_submit_that_waited_on_a_rewrite_of_its_operation_is_refused_as_busy(
		self,
	):
		"""BEFORE (review finding): the lock block refreshes nothing. A submit whose REPEATABLE
		READ snapshot was opened before another transaction rewrote its operation (a Stock
		Entry's MOP Log bridge rewrites the buckets) waited for that operation, decided from the
		fresh locked row -- and then cloned MOP Log balances and recomputed weights from its OLD
		snapshot, silently dropping what the other transaction wrote. NOW: the operation's
		``modified`` under the lock differs from the snapshot's, so the submit is refused as busy
		before any side effect, and its retry (a fresh snapshot) goes through."""
		mwo, mop = fx.make_work_order(self.world, "MM")
		draft = fx.employee_ir(self.world, "Issue", [mwo], employee="MM1")
		draft.flags.fixture_name = self.doc_name("EIR-I")
		with _no_enqueue():
			fx.insert_ir(draft)
		self.commit()
		submit = {
			"op": "submit",
			"doctype": "Employee IR",
			"name": draft.name,
			"label": "submit issue",
		}
		race = self.new_race(
			"W_ready",
			"W_started",
			"W_done",
			"H_parked",
			"H_release",
			"H_done",
			"R_done",
		)
		race.start("H", "_role_rewrite_operation", mop=mop, waiter="W")
		race.start(
			"W",
			"_role_racer",
			world=self.world,
			action=submit,
			me="W",
			order="second",
			other="H",
		)
		race.start(
			"R",
			"_role_later",
			world=self.world,
			action={**submit, "label": "retry the submit"},
			me="R",
			after=["W_done", "H_done"],
		)
		race.await_event("W_started")
		evidence = race.prove_lock_wait(
			"W", "FOR UPDATE", "tabManufacturing Operation", mop, holder="H"
		)
		race.set("H_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		self.assert_ok(self.attempt(reports, "H"))
		error = self.assert_refused(self.attempt(reports, "W"), "WorkOrderBusyError")
		self.assertIn(
			"changed by another transaction while this one waited", error["message"]
		)
		self.assertIn("_refuse_if_snapshot_older", error["frames"])
		self.assert_ok(self.attempt(reports, "R"))
		self.assertEqual(self.docstatus("Employee IR", draft.name), 1)
		self.assertEqual(
			(fx.operation(mop).status, fx.operation(mop).employee),
			("WIP", self.world.employees.MM1),
		)
		self.summary(waiter="busy", retry="submitted", lock_wait=evidence)

	def test_a_rest_receive_takes_its_block_before_writing_its_rows(self):
		"""BEFORE: an Employee Receive inserted with docstatus 1 wrote its Employee IR Operation
		rows BEFORE taking the lock block (so did a Receive draft's submit, in update_children).
		A transaction already HOLDING the block for that work order then hit those uncommitted
		rows in its terminal NOWAIT re-check and was refused WorkOrderBusyError -- the transaction
		that took the block FIRST lost. NOW: a Receive submit takes its Tree / Series / Bin
		pre-locks and the block before naming itself or writing rows; it simply waits."""
		mwo, mop = fx.make_work_order(self.world, "MM")
		with _no_enqueue():
			fx.issue(self.world, mwo, employee="MM1")
		self.commit()
		race = self.new_race(*self._events("H", "W"))
		race.start(
			"H",
			"_role_racer",
			world=self.world,
			action={
				"op": "insert",
				"kind": "eir_receive",
				"rows": [mwo],
				"employee": "MM1",
				"name": self.doc_name("EIR-H"),
				"label": "insert receive draft",
			},
			me="H",
			order="first",
			other="W",
		)
		race.start(
			"W",
			"_role_racer",
			world=self.world,
			action={
				"op": "insert",
				"kind": "eir_receive",
				"rows": [mwo],
				"employee": "MM1",
				"name": self.doc_name("EIR-W"),
				"docstatus": 1,
				"label": "REST receive",
			},
			me="W",
			order="second",
			other="H",
		)
		race.await_event("H_parked")
		race.await_event("W_started")
		evidence = race.prove_lock_wait(
			"W", "FOR UPDATE", "tabManufacturing Operation", mop, holder="H"
		)
		race.set("H_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		self.assert_ok(self.attempt(reports, "H"))
		self.assertEqual(self.docstatus("Employee IR", self.doc_name("EIR-H")), 0)
		self.assert_ok(self.attempt(reports, "W"))
		self.assertEqual(self.docstatus("Employee IR", self.doc_name("EIR-W")), 1)
		self.assertEqual(fx.operation(mop).status, "Finished")
		self.assertNotEqual(fx.pointer(mwo), mop)
		self.summary(holder="saved", rest_receive="received", lock_wait=evidence)

	def test_a_rest_receive_and_a_new_receive_draft_never_deadlock_on_the_naming_row(
		self,
	):
		"""BEFORE (review finding, reproduced as 1213): a REST insert-and-submit of a Receive only
		did a plain check in before_insert, NAMED itself (locking the Employee IR naming-series
		row) and only then waited for the work order's operation in on_submit_receive, while a
		new Receive draft of the same operation held that operation (its before_insert block) and
		waited for the naming row: a deadlock, its victim the VALID draft. NOW the REST Receive
		takes its pre-locks and the block before naming; the draft waits on the OPERATION and,
		once the Receive committed, is refused as stale -- both through the REAL naming series.

		A is parked right after it was named (the guard's before_validate entry point, after
		``set_new_name`` on both the old and the new code), the moment the old code held the naming
		row but not yet the operation: there the draft's wait was on ``tabSeries``."""
		mwo, mop = fx.make_work_order(self.world, "MM")
		with _no_enqueue():
			fx.issue(self.world, mwo, employee="MM1")
		self.commit()
		race = self.new_race(*self._events("A", "B"))
		race.start(
			"A",
			"_role_racer",
			world=self.world,
			action={
				"op": "insert",
				"kind": "eir_receive",
				"rows": [mwo],
				"employee": "MM1",
				"docstatus": 1,
				"real_name": True,
				"label": "REST receive, named by its series",
			},
			me="A",
			order="first",
			other="B",
			park_in="on_before_validate",
		)
		race.start(
			"B",
			"_role_racer",
			world=self.world,
			action={
				"op": "insert",
				"kind": "eir_receive",
				"rows": [mwo],
				"employee": "MM1",
				"real_name": True,
				"label": "receive draft, named by its series",
			},
			me="B",
			order="second",
			other="A",
		)
		race.await_event("A_parked")
		race.await_event("B_started")
		evidence = race.prove_lock_wait(
			"B", "FOR UPDATE", "tabManufacturing Operation", mop, holder="A"
		)
		race.set("A_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		a, b = self.attempt(reports, "A"), self.attempt(reports, "B")
		self.assert_ok(a)
		self.assertIn(f"EMP-IR-{self.world.manufacturer}-", a["value"])
		error = self.assert_refused(b, "StaleOperationError")
		self.assertNotIn(ER_LOCK_DEADLOCK, error["db_codes"])
		self.assertEqual(fx.operation(mop).status, "Finished")
		self.summary(draft=error["type"], lock_wait=evidence)

	def test_a_draft_moved_off_the_work_order_does_not_block_the_transfer_waiting_for_it(
		self,
	):
		"""BEFORE: an Employee Issue draft D on work order X was edited onto work order Y while a
		new Department Issue of X waited for X's block (D's edit locks X as its old reference).
		The transfer's snapshot still showed D on X, so it was refused with OutstandingDraftError.
		NOW: D's old row is confirmed by a current read -- gone -- and the transfer is accepted.

		(The waiter is a Department Issue on purpose: a second EMPLOYEE Issue of X would still be
		refused here by the legacy ``validate_duplication_and_gr_wt``, a plain-read UX check that
		sees the same stale snapshot -- outside the guard, and fail-safe.)"""
		x_mwo, x_mop = fx.make_work_order(self.world, "MM")
		y_mwo, y_mop = fx.make_work_order(self.world, "MM")
		draft = fx.employee_ir(self.world, "Issue", [x_mwo], employee="MM1")
		draft.flags.fixture_name = self.doc_name("EIR-D")
		with _no_enqueue():
			fx.insert_ir(draft)
		self.commit()
		race = self.new_race(*self._events("D", "N"))
		race.start(
			"D",
			"_role_racer",
			world=self.world,
			action={
				"op": "move_rows",
				"doctype": "Employee IR",
				"name": draft.name,
				"rows": [(y_mop, y_mwo)],
				"label": "move the draft to Y",
			},
			me="D",
			order="first",
			other="N",
		)
		race.start(
			"N",
			"_role_racer",
			world=self.world,
			action={
				"op": "insert",
				"kind": "dir_issue",
				"rows": [x_mwo],
				"current": "MM",
				"to": "PP",
				"name": self.doc_name("DIR-N"),
				"label": "new transfer draft of X",
			},
			me="N",
			order="second",
			other="D",
		)
		race.await_event("D_parked")
		race.await_event("N_started")
		evidence = race.prove_lock_wait(
			"N", "FOR UPDATE", "tabManufacturing Work Order", x_mwo, holder="D"
		)
		race.set("D_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		self.assert_ok(self.attempt(reports, "D"))
		self.assert_ok(self.attempt(reports, "N"))
		self.assertEqual(self.docstatus("Department IR", self.doc_name("DIR-N")), 0)
		self.assertEqual(self.issue_drafts(x_mwo), [])
		self.assertEqual(self.issue_drafts(y_mwo), [draft.name])
		self.summary(new_transfer="accepted", lock_wait=evidence)

	def test_a_transfer_cancel_in_flight_leaves_no_phantom_later_operation(self):
		"""BEFORE: a Department Issue cancel deletes the operation the Issue minted and moves the
		work order back to the source. An Employee Issue of the source that waited for that cancel
		still saw the deleted operation in its snapshot -- a "later operation" -- and was refused
		AmbiguousOperationError ("ask a System Manager to run the audit"). NOW: the successor is
		confirmed by a current read -- gone -- and the Issue is accepted.

		(A new Department Issue of the source would still be refused here by the legacy
		``department_ir_utils.validate_duplicate``, a plain-read UX check that sees the cancelled
		transfer as live in the same stale snapshot -- outside the guard, and fail-safe.)"""
		mwo, source = fx.make_work_order(self.world, "MM")
		with _no_enqueue():
			dir_issue, _ = fx.transfer(
				self.world, mwo, current="MM", to="PP", receive=False
			)
		minted = fx.pointer(mwo)
		self.commit()
		race = self.new_race(*self._events("C", "T"))
		race.start(
			"C",
			"_role_racer",
			world=self.world,
			action={
				"op": "cancel",
				"doctype": "Department IR",
				"name": dir_issue.name,
				"label": "cancel the transfer",
			},
			me="C",
			order="first",
			other="T",
		)
		race.start(
			"T",
			"_role_racer",
			world=self.world,
			action={
				"op": "insert",
				"kind": "eir_issue",
				"rows": [(source, mwo)],
				"employee": "MM1",
				"name": self.doc_name("EIR-T"),
				"label": "issue the source",
			},
			me="T",
			order="second",
			other="C",
		)
		race.await_event("C_parked")
		race.await_event("T_started")
		evidence = race.prove_lock_wait(
			"T", "FOR UPDATE", "tabManufacturing Operation", source, holder="C"
		)
		race.set("C_release")
		reports = race.finish()
		self.assert_code_under_test(
			reports,
			expected_enqueues={"C": ["frappe.model.delete_doc.delete_dynamic_links"]},
		)
		self.fresh()

		self.assert_ok(self.attempt(reports, "C"))
		self.assertFalse(frappe.db.exists("Manufacturing Operation", minted))
		self.assertEqual(fx.pointer(mwo), source)
		self.assert_ok(self.attempt(reports, "T"))
		self.assertEqual(self.issue_drafts(mwo), [self.doc_name("EIR-T")])
		self.summary(issue="accepted", lock_wait=evidence)

	def test_warn_mode_takes_no_lock_while_a_stock_entry_holds_the_work_order(self):
		"""BEFORE: the kill switch only downgraded rule messages -- every lock, wait and busy error
		stayed, so the documented emergency lever could not relieve a lock problem. NOW: in warn
		mode a save takes no Manufacturing Operation / Work Order lock: a Department Issue goes
		through at once while a Stock-Entry-like transaction holds the work-order row."""
		mwo, _mop = fx.make_work_order(self.world, "MM")
		self.commit()
		race = self.new_race(
			*self._events("W"), "H_ready", "H_parked", "H_release", "H_done"
		)
		race.start("H", "_role_hold_work_order", mwo=mwo, waiter="W")
		race.start(
			"W",
			"_role_racer",
			conf={"current_operation_guard": "warn"},
			world=self.world,
			action={
				"op": "insert",
				"kind": "dir_issue",
				"rows": [mwo],
				"current": "MM",
				"to": "PP",
				"name": self.doc_name("DIR-W"),
				"label": "insert DIR (warn mode)",
			},
			me="W",
			order="second",
			other="H",
		)
		race.await_event("H_parked")
		race.await_event("W_done")  # while H still holds the work order
		race.set("H_release")
		reports = race.finish()
		self.assert_code_under_test(reports)
		self.fresh()

		waited = self.attempt(reports, "W")
		self.assert_ok(waited)
		self.assertLess(waited["elapsed"], SHORT_LOCK_WAIT)
		self.assertEqual(self.docstatus("Department IR", self.doc_name("DIR-W")), 0)
		self.summary(warn_insert_s=round(waited["elapsed"], 2))
