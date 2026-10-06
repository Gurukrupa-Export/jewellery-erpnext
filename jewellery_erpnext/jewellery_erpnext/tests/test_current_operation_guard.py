# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The work-order current-operation guard, through the REAL Employee IR / Department IR lifecycle.

Every test builds its own seedless world (``tests/_current_operation_fixtures.py``), drives real
documents through ``insert`` / ``submit`` / ``cancel`` / ``discard`` and asserts real database
state -- operation status / operation / employee, time logs, MOP Log rows per voucher, the work
order's pointer, the document's docstatus -- together with the typed error raised by
``doc_events/current_operation_guard.py``. The incident of 2026-10-05 (Employee Issue
EMP-IR-...-43405 submitted four days late, reopening two finished operations) is
``TestIncident.test_stale_issue_draft_cannot_reopen_a_finished_operation``.

ISOLATION
---------
Each test rolls the WHOLE transaction back in ``tearDown`` rather than relying on
IntegrationTestCase's once-per-class rollback: the guard's terminal draft re-check takes
``LOCK IN SHARE MODE`` gap locks on ``tabEmployee IR Operation``, and other suites share this
disposable site, so no lock is held longer than one test. ``frappe.db.commit`` is stubbed and the
suite fails if any cascade tries to commit; RQ enqueues are recorded instead of reaching the bench
workers (which run the unpatched code) and also fail the test. ``tearDownClass`` purges anything
that still carries the run's prefix, i.e. anything that was committed by mistake.

WHAT IS STUBBED
---------------
Nothing in the Employee IR / Department IR cascades, except where a test docstring says so. The
world's Department Operations switch every optional behaviour off (no casting tree, QC,
raw-material injection, finding repack, mould or receive delay) and receives are at full weight,
so the heavy Receive pieces (``inject_extra_metal_for_eir_receive``, ``create_loss_stock_entries``,
``create_finding_repack_for_row``) run for real and simply book nothing. Patched: the deliberate
failure injections in ``TestAtomicity`` (``create_tree_on_issue`` / ``batch_add_time_logs``
raise); ``cancel_loss_stock_entries`` in the Employee Receive cancels and
``EmployeeIR.create_subcontracting_order`` in the subcontracting tests (outside the guard; see
their docstrings); the commit stub and the RQ queue recorder; and, on the queued path, the
desk's ``queue_in_background`` / scheduler / telemetry switches, ``queue_action`` (recorded
instead of reaching RQ) and the worker's ``get_current_job`` / ``commit`` / ``rollback`` /
``notify`` when its job is replayed in-process.

LEGACY DRAFTS
-------------
Under the guard an Employee Issue draft blocks its work order, so a draft that is STALE can no
longer be produced by the real controllers -- that is the point. The incident tests reach that
state the way production did: the draft is inserted by the real controller and then hidden
(``fixtures.hidden``) while the history is written through the real controllers -- the 10-01
REPEATABLE READ race that let 43405 and 43408 coexist, followed by code that never looked for
drafts. Drafts left over from before the deploy are written as stored with
``fixtures.raw_employee_ir``. The race itself, with real concurrent transactions, belongs to the
concurrency suite.
"""

import json
import pickle
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import frappe.client
from frappe.core.doctype.submission_queue.submission_queue import queue_submission
from frappe.desk.form.save import savedocs
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime

from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	CustomSubmissionQueue,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import current_operation_guard
from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
	EIR_OPERATION_MWO_COLUMN,
	EIR_OPERATION_TABLE,
	REVIEWED_REPAIR_FLAG,
	AmbiguousOperationError,
	CurrentOperationError,
	HistoryRewriteError,
	OutstandingDraftError,
	StaleOperationError,
	is_open_operation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	resolve_employee_ir_issue_voucher_for_receive,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_MOP_LOG_GATHER_FIELDS,
	_group_logs_by_company_and_mwo,
	_open_non_pointer_operations,
	_plan_mwo_group,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx

EMPLOYEE_IR_MODULE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"
)
EOD_LOCK_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock"
RECON_WINDOW_MODULE = "jewellery_erpnext.jewellery_erpnext.stock_recon_window"
WARN_LOG_TITLE = "Current operation guard (warn mode)"
EOD_MESSAGE = "EOD sync is in progress"
# A locking statement of any kind (row locks, share locks, the non-waiting variants).
LOCKING_SQL = ("FOR UPDATE", "LOCK IN SHARE MODE", "NOWAIT", "SKIP LOCKED")


class _RecordingQueue:
	"""Stands in for an RQ queue: the bench workers run the unpatched code."""

	count = 0

	def __init__(self, sink):
		self.sink = sink

	def enqueue_call(self, *args, **kwargs):
		self.sink.append((kwargs.get("kwargs") or {}).get("method"))
		return SimpleNamespace(id=kwargs.get("job_id"))


class _GuardHarness(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.run_prefix = fx.new_prefix()
		cls.world_count = 0

	@classmethod
	def tearDownClass(cls):
		# Every test rolls back; anything still carrying this run's prefix was committed. The
		# purge scans whole tables, so end its transaction at once either way (no lingering
		# next-key locks for whoever else is testing on this site).
		frappe.db.rollback()
		leaked = fx.purge(cls.run_prefix)
		if leaked:
			frappe.db.commit()
		else:
			frappe.db.rollback()
		super().tearDownClass()
		if leaked:
			raise AssertionError(
				f"{cls.__name__} committed rows (purged now): {leaked}"
			)

	def setUp(self):
		super().setUp()
		type(self).world_count += 1
		self.commits = []
		self.enqueued = []
		for patcher in (
			patch.object(frappe.db, "commit", self._record_commit),
			patch(
				"frappe.utils.background_jobs.get_queue",
				lambda *a, **k: _RecordingQueue(self.enqueued),
			),
			# The EOD lock and the reconciliation window live in MOP Settings, which other
			# suites on this shared site toggle (test_mop_settings): pin both off. The tests of
			# the refusal itself switch them on explicitly.
			patch(f"{EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=False),
			patch(f"{RECON_WINDOW_MODULE}._enabled", return_value=False),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.world = fx.make_world(f"{self.run_prefix}-{self.world_count:02d}")
		frappe.local.message_log = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.value_cache.clear()
		fx.clear_caches(self.world)
		frappe.flags.pop("current_operation_reviewed_repair", None)
		frappe.local.message_log = []
		super().tearDown()
		self.assertEqual(self.commits, [], "a cascade committed mid-transaction")
		self.assertEqual(
			self.enqueued, [], "a cascade enqueued a job for the old-code workers"
		)

	def _record_commit(self, *args, **kwargs):
		self.commits.append(args)

	# -- helpers --------------------------------------------------------------------------

	def assertGuard(self, exc_class, fn, *args, **kwargs):
		"""Run ``fn``; assert it raised exactly ``exc_class``; return the message."""
		with self.assertRaises(CurrentOperationError) as ctx:
			fn(*args, **kwargs)
		self.assertIs(type(ctx.exception), exc_class, msg=str(ctx.exception))
		return str(ctx.exception)

	def assertOnlyPointerOpen(self, mwo):
		self.assertEqual(fx.open_operations(mwo), [fx.pointer(mwo)])

	def docstatus(self, doctype, name):
		return frappe.db.get_value(doctype, name, "docstatus")

	@contextmanager
	def production_eir_naming(self):
		"""Name Employee IRs by the DocType's own naming-series rule inside the block, as
		production does (kg-gk: ``EMP-IR-.manufacturer.-.YYYY.-.#####``, naming_rule Expression).

		CI installs the ``git_action_v16`` fixtures, whose Property Setters switch Employee IR to
		``autoname = hash`` (naming_rule Random): a hash name never touches ``tabSeries``, so a
		test of WHERE the naming row is locked (lock_order RULE D) would fail -- or pass without
		testing anything -- there. The rule stored on the DocType row itself is untouched by
		Property Setters. Patches the cached Meta, which ``set_new_name`` reads; nothing inside
		the block may clear the Employee IR cache."""
		standard = frappe.db.get_value("DocType", "Employee IR", "autoname")
		self.assertIn("#", standard or "", "Employee IR has no naming-series rule")
		meta = frappe.get_meta("Employee IR")
		with (
			patch.object(meta, "autoname", standard),
			patch.object(meta, "naming_rule", "Expression"),
		):
			yield

	def messages(self):
		out = []
		for entry in frappe.local.message_log or []:
			if isinstance(entry, str):
				entry = json.loads(entry)
			out.append(f"{entry.get('title') or ''} {entry.get('message') or ''}")
		return out

	def legacy_chain(self, department="MM"):
		"""A work order in the incident's end state, built directly: an old operation ``stale``
		reopened to WIP (operation + employee set, it has a successor), then a finished
		successor, then the pointer ``current`` (Not Started, seeded balance)."""
		w = self.world
		mwo, stale = fx.make_work_order(w, department, operation_status="WIP")
		frappe.db.set_value(
			"Manufacturing Operation",
			stale,
			{"operation": w.operations[department], "employee": w.employees["MM1"]},
			update_modified=False,
		)
		middle = fx.make_operation(
			w, mwo, department, status="Finished", previous_mop=stale
		)
		current = fx.make_operation(w, mwo, department, previous_mop=middle)
		fx.seed_balance(w, current, mwo)
		fx.set_pointer(mwo, current)
		return mwo, stale, current


# ==============================================================================================
# the normal lifecycle
# ==============================================================================================


class TestNormalLifecycle(_GuardHarness):
	def test_one_open_operation_equals_the_pointer_at_every_step(self):
		w = self.world
		mwo, first = fx.make_work_order(w, "MM")
		self.assertOnlyPointerOpen(mwo)

		issue = fx.issue(w, mwo, department="MM", employee="MM1")
		op = fx.operation(first)
		self.assertEqual(
			(op.status, op.operation, op.employee),
			("WIP", w.operations.MM, w.employees.MM1),
		)
		self.assertEqual(fx.pointer(mwo), first)
		self.assertOnlyPointerOpen(mwo)
		self.assertEqual(len(fx.mop_logs(voucher_no=issue.name)), 2)
		self.assertEqual([t.to_time for t in fx.time_logs(first)], [None])

		receive = fx.receive(w, mwo, department="MM", employee="MM1")
		second = fx.pointer(mwo)
		self.assertNotEqual(second, first)
		self.assertEqual(fx.operation(first).status, "Finished")
		self.assertEqual(fx.operation(second).previous_mop, first)
		self.assertIsNotNone(fx.time_logs(first)[0].to_time)
		self.assertOnlyPointerOpen(mwo)

		dir_issue = fx.submit_ir(
			fx.insert_ir(fx.department_ir(w, "Issue", [mwo], current="MM", to="PP"))
		)
		third = fx.pointer(mwo)
		self.assertEqual(fx.operation(second).status, "Finished")
		self.assertEqual(
			(fx.operation(third).department, fx.operation(third).department_ir_status),
			(w.departments.PP, "In-Transit"),
		)
		self.assertOnlyPointerOpen(mwo)

		dir_receive = fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					w,
					"Receive",
					[mwo],
					current="PP",
					previous="MM",
					receive_against=dir_issue.name,
				)
			)
		)
		self.assertEqual(fx.pointer(mwo), third)
		self.assertEqual(fx.operation(third).department_ir_status, "Received")
		self.assertEqual(
			frappe.db.get_value("Manufacturing Work Order", mwo, "department"),
			w.departments.PP,
		)
		self.assertOnlyPointerOpen(mwo)

		# A second department, then onward to Final Polish: still one open operation.
		fx.work_cycle(w, mwo, department="PP", employee="PP1")
		self.assertOnlyPointerOpen(mwo)
		fx.transfer(w, mwo, current="PP", to="FP")
		self.assertOnlyPointerOpen(mwo)
		self.assertEqual(fx.operation(fx.pointer(mwo)).department, w.departments.FP)
		for doc in (issue, receive, dir_issue, dir_receive):
			self.assertEqual(self.docstatus(doc.doctype, doc.name), 1)


# ==============================================================================================
# an outstanding Employee Issue draft blocks its work order
# ==============================================================================================


class TestOutstandingDraftBlocks(_GuardHarness):
	def test_a_second_employee_issue_is_rejected(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		before = fx.snapshot(mwo)

		second = fx.employee_ir(w, "Issue", [mwo], employee="MM2")
		message = self.assertGuard(OutstandingDraftError, fx.insert_ir, second)

		self.assertIn(draft.name, message)
		self.assertIn(mwo, message)
		self.assertFalse(frappe.db.exists("Employee IR", second.flags.fixture_name))
		self.assertEqual(fx.snapshot(mwo), before)

	def test_an_employee_receive_is_rejected_too(self):
		"""10-01 18:39 under the guard: Receive 43514 while draft 43405 was still open. The draft
		and the submitted Issue 43408 coexist only through the 10-01 race (see LEGACY DRAFTS)."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM2"))
		with fx.hidden("Employee IR", draft.name):
			fx.issue(w, mwo, employee="MM1")
		self.assertEqual(fx.operation(mop).status, "WIP")
		before = fx.snapshot(mwo)

		receive = fx.employee_ir(w, "Receive", [mwo], employee="MM1")
		message = self.assertGuard(OutstandingDraftError, fx.insert_ir, receive)

		self.assertIn(draft.name, message)
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertFalse(frappe.db.exists("Employee IR", receive.flags.fixture_name))

	def test_a_department_issue_save_is_rejected_and_the_work_order_does_not_move(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo]))
		before = fx.snapshot(mwo)

		transfer = fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
		message = self.assertGuard(OutstandingDraftError, fx.insert_ir, transfer)

		self.assertIn(draft.name, message)
		self.assertFalse(frappe.db.exists("Department IR", transfer.flags.fixture_name))
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(fx.pointer(mwo), mop)

	def test_a_department_issue_saved_before_the_draft_is_rejected_at_submit(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		transfer = fx.insert_ir(
			fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
		)
		# Department IR drafts never block, so the Employee Issue draft can still be created.
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo]))
		before = fx.snapshot(mwo)

		message = self.assertGuard(OutstandingDraftError, fx.submit_ir, transfer)

		self.assertIn(draft.name, message)
		self.assertEqual(self.docstatus("Department IR", transfer.name), 0)
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(fx.pointer(mwo), mop)

	def test_a_department_issue_draft_edited_onto_a_held_work_order_is_rejected(self):
		w = self.world
		free, _ = fx.make_work_order(w, "MM")
		held, held_mop = fx.make_work_order(w, "MM")
		transfer = fx.insert_ir(
			fx.department_ir(w, "Issue", [free], current="MM", to="PP")
		)
		fx.insert_ir(fx.employee_ir(w, "Issue", [held]))

		transfer = fx.reload(transfer)
		transfer.append(
			"department_ir_operation",
			{"manufacturing_operation": held_mop, "manufacturing_work_order": held},
		)
		message = self.assertGuard(OutstandingDraftError, transfer.save)

		self.assertIn(held, message)
		self.assertNotIn(free, message)
		rows = frappe.get_all(
			"Department IR Operation",
			filters={"parent": transfer.name},
			pluck="manufacturing_work_order",
		)
		self.assertEqual(rows, [free])

	def test_drafts_on_other_work_orders_never_block(self):
		w = self.world
		held, _ = fx.make_work_order(w, "MM")
		other, other_mop = fx.make_work_order(w, "MM")
		third, _ = fx.make_work_order(w, "MM")
		fx.insert_ir(fx.employee_ir(w, "Issue", [held]))

		fx.issue(w, other, employee="MM2")
		dir_issue, _ = fx.transfer(w, third, current="MM", to="PP", receive=False)

		self.assertEqual(fx.operation(other_mop).status, "WIP")
		self.assertEqual(self.docstatus("Department IR", dir_issue.name), 1)

	def test_department_ir_drafts_never_block(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		fx.insert_ir(fx.department_ir(w, "Issue", [mwo], current="MM", to="PP"))

		issue = fx.issue(w, mwo, employee="MM1")

		self.assertEqual(self.docstatus("Employee IR", issue.name), 1)
		self.assertEqual(fx.operation(mop).status, "WIP")


# ==============================================================================================
# the draft itself, Discard and Delete
# ==============================================================================================


class TestDraftSelfAndRelease(_GuardHarness):
	def test_the_draft_itself_can_be_edited_and_submitted_at_any_age(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		frappe.db.set_value(
			"Employee IR",
			draft.name,
			"creation",
			fx.days_ago(30),
			update_modified=False,
		)

		draft = fx.reload(draft)
		# A predicate field: the save re-runs the guard.
		draft.employee = w.employees.MM2
		draft.save()
		fx.submit_ir(draft)

		op = fx.operation(mop)
		self.assertEqual((op.status, op.employee), ("WIP", w.employees.MM2))
		self.assertEqual(fx.pointer(mwo), mop)
		self.assertEqual(len(fx.mop_logs(voucher_no=draft.name)), 2)
		self.assertEqual([t.employee for t in fx.time_logs(mop)], [w.employees.MM2])
		self.assertEqual(self.docstatus("Employee IR", draft.name), 1)

	def test_discard_unblocks_immediately_and_discards_the_rows(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		self.assertGuard(
			OutstandingDraftError,
			fx.insert_ir,
			fx.employee_ir(w, "Issue", [mwo], employee="MM2"),
		)

		fx.reload(draft).discard()

		self.assertEqual(self.docstatus("Employee IR", draft.name), 2)
		self.assertEqual(
			frappe.get_all(
				"Employee IR Operation",
				filters={"parent": draft.name},
				pluck="docstatus",
			),
			[2],
		)
		issue = fx.issue(w, mwo, employee="MM2")
		self.assertEqual(self.docstatus("Employee IR", issue.name), 1)
		self.assertEqual(fx.operation(mop).employee, w.employees.MM2)

	def test_department_ir_discard_discards_its_rows(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		transfer = fx.insert_ir(
			fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
		)

		fx.reload(transfer).discard()

		self.assertEqual(self.docstatus("Department IR", transfer.name), 2)
		self.assertEqual(
			frappe.get_all(
				"Department IR Operation",
				filters={"parent": transfer.name},
				pluck="docstatus",
			),
			[2],
		)
		# The discarded transfer no longer counts as a duplicate either.
		dir_issue, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		self.assertEqual(self.docstatus("Department IR", dir_issue.name), 1)

	def test_delete_unblocks_immediately(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))

		frappe.delete_doc("Employee IR", draft.name)

		self.assertFalse(frappe.db.exists("Employee IR", draft.name))
		issue = fx.issue(w, mwo, employee="MM2")
		self.assertEqual(fx.operation(mop).employee, w.employees.MM2)
		self.assertEqual(self.docstatus("Employee IR", issue.name), 1)


# ==============================================================================================
# stale drafts: the incident and its variants
# ==============================================================================================


class TestIncident(_GuardHarness):
	def test_stale_issue_draft_cannot_reopen_a_finished_operation(self):
		"""EMP-IR-...-43405 step by step, with two work orders as in production.

		10-01 15:30   draft D: Employee Issue of both work orders' current operations m1 / m2
		10-01 15:32   Issue 43408 of m1 / m2 to another employee, submitted
		10-01 18:39   Receive 43514: m1 / m2 Finished, successors minted, the pointers move
		10-02..10-05  Department IRs Model Making -> Pre Polish -> Diamond Setting -> Final Polish;
		              there work order 1's current operation is issued (WIP), work order 2's is not
		10-05 16:03   D is submitted

		Production reopened m1 / m2 (WIP), opened a time log on each and cloned their pre-receive
		balances into unsynced MOP Logs. Now the submit raises StaleOperationError naming both work
		orders and where they are, and writes nothing.
		"""
		w = self.world
		(mwo1, m1), (mwo2, m2) = (
			fx.make_work_order(w, "MM"),
			fx.make_work_order(w, "MM"),
		)
		mwos = [mwo1, mwo2]
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", mwos, employee="MM1"))

		# Under the guard the open draft holds both work orders, so the incident cannot even
		# start: the duplicate Issue (43408) is refused.
		self.assertGuard(
			OutstandingDraftError,
			fx.insert_ir,
			fx.employee_ir(w, "Issue", mwos, employee="MM2"),
		)

		# Production's history, through the real controllers, while D is invisible to them (the
		# 10-01 snapshot race, then code that never looked for drafts; see LEGACY STATES in the
		# fixture module).
		with fx.hidden("Employee IR", draft.name):
			fx.work_cycle(w, mwos, department="MM", employee="MM2")
			fx.transfer(w, mwos, current="MM", to="PP")
			fx.transfer(w, mwos, current="PP", to="DS")
			fx.transfer(w, mwos, current="DS", to="FP")
			fx.issue(w, mwo1, department="FP", employee="FP1")
		current1, current2 = fx.pointer(mwo1), fx.pointer(mwo2)
		self.assertEqual(
			[fx.operation(m).status for m in (m1, m2, current1, current2)],
			["Finished", "Finished", "WIP", "Not Started"],
		)
		self.assertEqual(self.docstatus("Employee IR", draft.name), 0)
		before = fx.snapshot(*mwos)

		message = self.assertGuard(StaleOperationError, fx.submit_ir, draft)

		for expected in (m1, m2, mwo1, mwo2, current1, current2, w.departments.FP):
			self.assertIn(expected, message)
		# Statuses, operations, employees, time logs, MOP Logs and pointers: all untouched.
		self.assertEqual(fx.snapshot(*mwos), before)
		self.assertEqual(fx.mop_logs(voucher_no=draft.name), [])
		self.assertEqual(self.docstatus("Employee IR", draft.name), 0)
		for mwo in mwos:
			self.assertOnlyPointerOpen(mwo)

	def test_a_draft_left_open_from_before_the_deploy_is_refused(self):
		"""The same end state reached the other way: the history happened first and the draft is a
		leftover of the old code, written as stored (created 92 s before the Issue that took M)."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, department="MM", employee="MM1")
		fx.receive(w, mwo, department="MM", employee="MM1")
		fx.transfer(w, mwo, current="MM", to="PP")
		fx.work_cycle(w, mwo, department="PP", employee="PP1")
		fx.transfer(w, mwo, current="PP", to="FP")
		current = fx.pointer(mwo)
		draft = fx.raw_employee_ir(
			w,
			"Issue",
			[(stale, mwo)],
			department="MM",
			employee="MM1",
			creation=add_to_date(get_datetime(issue.creation), seconds=-92),
		)
		before = fx.snapshot(mwo)
		self.assertEqual(fx.operation(stale).status, "Finished")

		message = self.assertGuard(
			StaleOperationError, fx.submit_ir, draft, "Employee IR"
		)

		for expected in (stale, mwo, current, w.departments.FP):
			self.assertIn(expected, message)
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(fx.operation(stale).status, "Finished")
		self.assertEqual(fx.mop_logs(voucher_no=draft), [])
		self.assertEqual(fx.pointer(mwo), current)
		self.assertEqual(self.docstatus("Employee IR", draft), 0)
		self.assertOnlyPointerOpen(mwo)

	def test_a_young_draft_that_is_stale_is_rejected(self):
		"""Age is irrelevant: one minute old (31995-style), the draft still names an operation the
		work order has finished in the same department."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		draft = fx.raw_employee_ir(
			w,
			"Issue",
			[(stale, mwo)],
			employee="MM2",
			creation=add_to_date(get_datetime(), minutes=-1),
		)
		before = fx.snapshot(mwo)

		self.assertGuard(StaleOperationError, fx.submit_ir, draft, "Employee IR")

		self.assertEqual(fx.snapshot(mwo), before)

	def test_an_old_draft_that_is_still_current_is_accepted(self):
		"""36341-style: thirty days old, but nothing has moved -- it is still the current work."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		frappe.db.set_value(
			"Employee IR",
			draft.name,
			"creation",
			fx.days_ago(30),
			update_modified=False,
		)

		fx.submit_ir(draft)

		self.assertEqual(fx.operation(mop).status, "WIP")
		self.assertEqual(len(fx.mop_logs(voucher_no=draft.name)), 2)

	def test_a_draft_from_an_earlier_visit_cannot_submit_after_a_return(self):
		"""A -> B -> A: back in Model Making, but on a NEW operation; the department matching is
		not enough, the draft's operation is not the pointer."""
		w = self.world
		mwo, first_visit = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		fx.transfer(w, mwo, current="MM", to="PP")
		fx.work_cycle(w, mwo, department="PP", employee="PP1")
		fx.transfer(w, mwo, current="PP", to="MM")
		returned = fx.pointer(mwo)
		self.assertEqual(fx.operation(returned).department, w.departments.MM)
		draft = fx.raw_employee_ir(
			w, "Issue", [(first_visit, mwo)], employee="MM1", creation=fx.days_ago(3)
		)
		before = fx.snapshot(mwo)

		message = self.assertGuard(
			StaleOperationError, fx.submit_ir, draft, "Employee IR"
		)

		self.assertIn(returned, message)
		self.assertEqual(fx.snapshot(mwo), before)
		# The stale draft still holds the work order until it is discarded -- which is exactly
		# what the message tells the operator to do. After that the current operation is free.
		self.assertGuard(
			OutstandingDraftError,
			fx.insert_ir,
			fx.employee_ir(w, "Issue", [mwo], employee="MM2"),
		)
		fx.reload(draft, "Employee IR").discard()
		fx.issue(w, mwo, employee="MM2")
		self.assertEqual(fx.operation(returned).status, "WIP")

	def test_a_draft_from_an_earlier_cycle_in_the_same_department_is_rejected(self):
		w = self.world
		mwo, first_cycle = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		second_cycle = fx.pointer(mwo)
		draft = fx.raw_employee_ir(
			w, "Issue", [(first_cycle, mwo)], employee="MM1", creation=fx.days_ago(2)
		)
		before = fx.snapshot(mwo)

		message = self.assertGuard(
			StaleOperationError, fx.submit_ir, draft, "Employee IR"
		)

		self.assertIn(second_cycle, message)
		self.assertEqual(fx.snapshot(mwo), before)

	def test_a_stale_department_issue_draft_cannot_submit(self):
		"""The Department IR analogue. A transfer drafted on the current operation; the work order
		is then worked on (Department IR drafts never block). Submitting the old transfer would
		mint a second successor from the finished operation and leave the real one orphaned."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		transfer = fx.insert_ir(
			fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
		)
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		before = fx.snapshot(mwo)

		message = self.assertGuard(StaleOperationError, fx.submit_ir, transfer)

		self.assertIn(stale, message)
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(self.docstatus("Department IR", transfer.name), 0)

	def test_a_second_department_issue_of_the_same_work_order_is_refused_at_submit(
		self,
	):
		"""Two transfer drafts of one operation, saved concurrently: the legacy save-time
		duplicate check is a plain read, so under REPEATABLE READ neither save sees the other
		(emulated with ``fixtures.hidden``). Once the first is submitted, the second names a
		finished operation and would mint a twin (family B, at document level)."""
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		first = fx.insert_ir(fx.department_ir(w, "Issue", [mwo], current="MM", to="PP"))
		with fx.hidden("Department IR", first.name):
			second = fx.insert_ir(
				fx.department_ir(w, "Issue", [mwo], current="MM", to="FP")
			)
		fx.submit_ir(first)
		before = fx.snapshot(mwo)

		message = self.assertGuard(StaleOperationError, fx.submit_ir, second)

		self.assertIn(source, message)
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(
			len([r for r in fx.operations(mwo) if r.previous_mop == source]), 1
		)

	def test_a_work_order_in_transit_cannot_be_moved_or_issued_again(self):
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		in_transit = fx.pointer(mwo)
		before = fx.snapshot(mwo)

		# The source operation again (a second transfer of the same goods): stale.
		self.assertGuard(
			StaleOperationError,
			fx.insert_ir,
			fx.department_ir(w, "Issue", [(source, mwo)], current="MM", to="FP"),
		)
		# The in-transit operation: current, but it has not been received yet.
		self.assertGuard(
			CurrentOperationError,
			fx.insert_ir,
			fx.department_ir(w, "Issue", [(in_transit, mwo)], current="PP", to="FP"),
		)
		self.assertGuard(
			CurrentOperationError,
			fx.insert_ir,
			fx.employee_ir(
				w, "Issue", [(in_transit, mwo)], department="PP", employee="PP1"
			),
		)
		self.assertEqual(fx.snapshot(mwo), before)

	def test_a_new_draft_naming_a_finished_operation_is_rejected_at_save(self):
		w = self.world
		mwo, finished = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		before = fx.snapshot(mwo)

		draft = fx.employee_ir(w, "Issue", [(finished, mwo)], employee="MM2")
		self.assertGuard(StaleOperationError, fx.insert_ir, draft)

		self.assertFalse(frappe.db.exists("Employee IR", draft.flags.fixture_name))
		self.assertEqual(fx.snapshot(mwo), before)


# ==============================================================================================
# legacy conflicted work orders (two open operations)
# ==============================================================================================


class TestLegacyConflict(_GuardHarness):
	def test_transactions_naming_the_stale_operation_are_rejected(self):
		w = self.world
		mwo, stale, _current = self.legacy_chain()
		before = fx.snapshot(mwo)

		for doc in (
			fx.employee_ir(w, "Issue", [(stale, mwo)], employee="MM2"),
			fx.employee_ir(w, "Receive", [(stale, mwo)], employee="MM1"),
			fx.department_ir(w, "Issue", [(stale, mwo)], current="MM", to="PP"),
		):
			with self.subTest(doctype=doc.doctype, type=doc.type):
				self.assertGuard(StaleOperationError, fx.insert_ir, doc)
		self.assertEqual(fx.snapshot(mwo), before)

	def test_transactions_on_the_pointer_proceed_with_a_warning(self):
		w = self.world
		mwo, stale, current = self.legacy_chain()

		issue = fx.issue(w, mwo, employee="MM2")

		self.assertEqual(self.docstatus("Employee IR", issue.name), 1)
		self.assertEqual(fx.operation(current).status, "WIP")
		# Untouched: the audit repairs it.
		self.assertEqual(fx.operation(stale).status, "WIP")
		warnings = [m for m in self.messages() if "Stale Operation On Work Order" in m]
		self.assertTrue(warnings, self.messages())
		self.assertIn(stale, warnings[0])

	def test_after_the_incident_work_goes_on_at_the_pointer_and_the_stale_operation_is_refused(
		self,
	):
		"""The real footprint (stale draft submitted in warn mode, as in TestEODHold), in both
		production shapes: work order 1 has two WIP operations (EA26688: pointer issued in Final
		Polish), work order 2 a WIP stale one next to a Not Started pointer (EA26685). Until the
		repair, transactions on the pointers proceed with a warning naming the stale sibling;
		anything naming the stale operations is refused."""
		w = self.world
		(mwo1, m1), (mwo2, m2) = (
			fx.make_work_order(w, "MM"),
			fx.make_work_order(w, "MM"),
		)
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo1, mwo2], employee="MM1"))
		with fx.hidden("Employee IR", draft.name):
			fx.work_cycle(w, [mwo1, mwo2], department="MM", employee="MM2")
			fx.transfer(w, [mwo1, mwo2], current="MM", to="FP")
			fx.issue(w, mwo1, department="FP", employee="FP1")
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch("frappe.log_error"),
		):
			fx.submit_ir(draft)
		pointer1, pointer2 = fx.pointer(mwo1), fx.pointer(mwo2)
		self.assertEqual(
			[fx.operation(m).status for m in (m1, pointer1, m2, pointer2)],
			["WIP", "WIP", "WIP", "Not Started"],
		)
		before = fx.snapshot(mwo1, mwo2)

		for doc in (
			fx.employee_ir(w, "Receive", [(m1, mwo1)], employee="MM1"),
			fx.employee_ir(w, "Issue", [(m2, mwo2)], employee="MM2"),
			fx.department_ir(w, "Issue", [(m2, mwo2)], current="MM", to="PP"),
		):
			with self.subTest(doctype=doc.doctype, type=doc.type):
				self.assertGuard(StaleOperationError, fx.insert_ir, doc)
		self.assertEqual(fx.snapshot(mwo1, mwo2), before)

		frappe.local.message_log = []
		fx.receive(w, mwo1, department="FP", employee="FP1")
		fx.issue(w, mwo2, department="FP", employee="FP1")

		self.assertEqual(fx.operation(pointer1).status, "Finished")
		self.assertEqual(fx.operation(pointer2).status, "WIP")
		self.assertEqual(
			(fx.operation(m1).status, fx.operation(m2).status), ("WIP", "WIP")
		)
		warnings = [m for m in self.messages() if "Stale Operation On Work Order" in m]
		self.assertTrue(any(m1 in m for m in warnings), warnings)
		self.assertTrue(any(m2 in m for m in warnings), warnings)

	def test_a_work_order_without_a_pointer_is_ambiguous(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		fx.set_pointer(mwo, None)

		message = self.assertGuard(
			AmbiguousOperationError,
			fx.insert_ir,
			fx.employee_ir(w, "Issue", [(mop, mwo)]),
		)
		self.assertIn(mwo, message)

	def test_a_pointer_that_already_has_a_successor_is_ambiguous(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		fx.make_operation(w, mwo, "MM", previous_mop=mop)

		self.assertGuard(
			AmbiguousOperationError,
			fx.insert_ir,
			fx.employee_ir(w, "Issue", [(mop, mwo)]),
		)


# ==============================================================================================
# multi-row documents and rows that move between work orders
# ==============================================================================================


class TestMultiRow(_GuardHarness):
	def _three_work_orders_one_stale(self):
		w = self.world
		first, first_mop = fx.make_work_order(w, "MM")
		second, second_mop = fx.make_work_order(w, "MM")
		third, third_old = fx.make_work_order(w, "MM")
		fx.work_cycle(w, third, department="MM", employee="MM2")
		return [(first_mop, first), (second_mop, second), (third_old, third)]

	def test_one_stale_row_fails_the_whole_submit_naming_only_its_work_order(self):
		w = self.world
		rows = self._three_work_orders_one_stale()
		mwos = [mwo for _mop, mwo in rows]
		draft = fx.raw_employee_ir(
			w, "Issue", rows, employee="MM1", creation=fx.days_ago(4)
		)
		before = fx.snapshot(*mwos)

		message = self.assertGuard(
			StaleOperationError, fx.submit_ir, draft, "Employee IR"
		)

		self.assertIn(mwos[2], message)
		self.assertNotIn(mwos[0], message)
		self.assertNotIn(mwos[1], message)
		self.assertEqual(fx.snapshot(*mwos), before)
		self.assertEqual(fx.mop_logs(voucher_no=draft), [])
		for mop, _mwo in rows[:2]:
			self.assertEqual(fx.operation(mop).status, "Not Started")

	def test_a_new_document_with_one_stale_row_is_never_created(self):
		w = self.world
		rows = self._three_work_orders_one_stale()
		doc = fx.employee_ir(w, "Issue", rows, employee="MM1")

		self.assertGuard(StaleOperationError, fx.insert_ir, doc)

		self.assertFalse(frappe.db.exists("Employee IR", doc.flags.fixture_name))

	def test_a_row_moved_to_another_work_order_is_checked_there_and_releases_the_old_one(
		self,
	):
		w = self.world
		x, x_mop = fx.make_work_order(w, "MM")
		y, y_mop = fx.make_work_order(w, "MM")
		z, z_mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [x], employee="MM1"))
		other = fx.insert_ir(fx.employee_ir(w, "Issue", [y], employee="MM2"))

		moved = fx.reload(draft)
		moved.employee_ir_operations[0].manufacturing_operation = y_mop
		moved.employee_ir_operations[0].manufacturing_work_order = y
		message = self.assertGuard(OutstandingDraftError, moved.save)
		self.assertIn(other.name, message)

		moved = fx.reload(draft)
		moved.employee_ir_operations[0].manufacturing_operation = z_mop
		moved.employee_ir_operations[0].manufacturing_work_order = z
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *args, **kwargs):
			statements.append((str(query), args[0] if args else kwargs.get("values")))
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", recording_sql):
			moved.save()

		# Both the work order the row LEFT (being released) and the one it joined are locked.
		locked = {
			values[0]
			for query, values in statements
			if "FOR UPDATE" in query
			and "`tabManufacturing Work Order`" in query
			and isinstance(values, list | tuple)
		}
		self.assertEqual(locked, {x, z})
		fx.issue(w, x, employee="MM2")  # released: x is free again
		self.assertEqual(fx.operation(x_mop).status, "WIP")


# ==============================================================================================
# REST, frappe.client and the Submission Queue
# ==============================================================================================


class TestOtherSubmitPaths(_GuardHarness):
	def _payload(self, mwo, mop, **extra):
		w = self.world
		return {
			"doctype": "Employee IR",
			"type": "Issue",
			"company": w.company,
			"manufacturer": w.manufacturer,
			"department": w.departments.MM,
			"operation": w.operations.MM,
			"employee": w.employees.MM2,
			"subcontracting": "No",
			"custom_transfer_type": "",
			"employee_ir_operations": [
				{
					"manufacturing_operation": mop,
					"manufacturing_work_order": mwo,
					"rpt_wt_issue": 0,
				}
			],
			**extra,
		}

	def test_rest_insert_with_docstatus_1_is_guarded_before_naming(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		before = fx.snapshot(mwo)

		with self.production_eir_naming():
			self.assertGuard(
				OutstandingDraftError,
				frappe.client.insert,
				self._payload(mwo, mop, docstatus=1),
			)

		self.assertEqual(fx.snapshot(mwo), before)
		# RULE D: refused in before_insert, i.e. before set_new_name touched the naming series.
		self.assertEqual(
			frappe.db.sql(
				"SELECT name FROM `tabSeries` WHERE name LIKE %s", f"%{w.prefix}%"
			),
			(),
		)

	def test_rest_insert_with_docstatus_1_of_a_stale_operation_is_guarded(self):
		w = self.world
		mwo, finished = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		before = fx.snapshot(mwo)

		self.assertGuard(
			StaleOperationError,
			frappe.client.insert,
			self._payload(mwo, finished, docstatus=1),
		)
		self.assertEqual(fx.snapshot(mwo), before)

	def test_rest_insert_and_submit_of_valid_documents_still_works(self):
		"""No false positive on the REST insert-and-submit path (Receive is checked after its
		pre-locks), and naming happens after the guard: the series is the manufacturer's."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")

		with self.production_eir_naming():
			issue = frappe.client.insert(
				self._payload(mwo, mop, employee=w.employees.MM1, docstatus=1)
			)
		self.assertEqual(issue["docstatus"], 1)
		self.assertTrue(
			issue["name"].startswith(f"EMP-IR-{w.manufacturer}-"), issue["name"]
		)
		self.assertEqual(fx.operation(mop).status, "WIP")

		gross = frappe.db.get_value("Manufacturing Operation", mop, "gross_wt")
		with self.production_eir_naming():
			receive = frappe.client.insert(
				self._payload(
					mwo,
					mop,
					type="Receive",
					employee=w.employees.MM1,
					docstatus=1,
					employee_ir_operations=[
						{
							"manufacturing_operation": mop,
							"manufacturing_work_order": mwo,
							"received_gross_wt": gross,
						}
					],
				)
			)
		self.assertEqual(receive["docstatus"], 1)
		self.assertTrue(
			receive["name"].startswith(f"EMP-IR-{w.manufacturer}-"), receive["name"]
		)
		self.assertEqual(fx.operation(mop).status, "Finished")
		self.assertNotEqual(fx.pointer(mwo), mop)
		self.assertOnlyPointerOpen(mwo)

	def test_bulk_submit_refuses_the_stale_draft_and_submits_the_rest(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events.bulk_update import (
			custom_submit_cancel_or_update_docs,
		)

		w = self.world
		good, good_mop = fx.make_work_order(w, "MM")
		moved, stale = fx.make_work_order(w, "MM")
		fx.work_cycle(w, moved, department="MM", employee="MM2")
		valid = fx.insert_ir(fx.employee_ir(w, "Issue", [good], employee="MM1"))
		stale_draft = fx.raw_employee_ir(w, "Issue", [(stale, moved)], employee="MM1")
		before = fx.snapshot(moved)

		# _bulk_action commits after each document and rolls back after a failure. Emulate both
		# inside this test's transaction with a moving savepoint.
		real_rollback = frappe.db.rollback
		frappe.db.savepoint("cog_bulk")
		with (
			patch.object(
				frappe.db, "commit", lambda *a, **k: frappe.db.savepoint("cog_bulk")
			),
			patch.object(
				frappe.db,
				"rollback",
				lambda *a, **k: real_rollback(save_point="cog_bulk"),
			),
			patch("frappe.log_error") as log_error,
		):
			failed = custom_submit_cancel_or_update_docs(
				"Employee IR", [valid.name, stale_draft], "submit"
			)

		self.assertEqual(failed, [stale_draft])
		log_error.assert_called_once()
		self.assertEqual(self.docstatus("Employee IR", valid.name), 1)
		self.assertEqual(fx.operation(good_mop).status, "WIP")
		self.assertEqual(self.docstatus("Employee IR", stale_draft), 0)
		self.assertEqual(fx.snapshot(moved), before)

	def test_frappe_client_submit_is_guarded(self):
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		draft = fx.raw_employee_ir(w, "Issue", [(stale, mwo)], employee="MM2")
		before = fx.snapshot(mwo)

		self.assertGuard(
			StaleOperationError,
			frappe.client.submit,
			fx.reload(draft, "Employee IR").as_dict(),
		)

		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(self.docstatus("Employee IR", draft), 0)

	def _desk_submit(self, doc):
		"""Desk Submit (``frappe.desk.form.save.savedocs``) as production runs it: the Property
		Setter ``queue_in_background = 1`` and a running scheduler send it to the Submission
		Queue. ``queue_action`` is recorded instead of reaching RQ (the bench workers run the
		unpatched code); returns that mock."""
		meta = frappe.get_meta(doc.doctype)
		with (
			patch.object(meta, "queue_in_background", 1),
			patch("frappe.desk.form.save.is_scheduler_inactive", return_value=False),
			patch("frappe.desk.form.save.capture_doc"),
			patch.object(CustomSubmissionQueue, "queue_action") as queue_action,
		):
			savedocs(json.dumps(doc.as_dict(), default=str), "Submit")
		return queue_action

	def _queue_rows(self, name):
		return frappe.get_all(
			"Submission Queue",
			filters={"ref_docname": name},
			fields=["name", "status", "exception"],
		)

	def test_a_queued_stale_submit_is_refused_before_a_queue_row_exists(self):
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		draft = fx.raw_employee_ir(w, "Issue", [(stale, mwo)], employee="MM2")
		before = fx.snapshot(mwo)

		queue_action = None
		with self.assertRaises(StaleOperationError):
			queue_action = self._desk_submit(fx.reload(draft, "Employee IR"))

		# savedocs raised inside CustomSubmissionQueue.insert: nothing was queued.
		self.assertIsNone(queue_action)
		self.assertEqual(self._queue_rows(draft), [])
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(self.docstatus("Employee IR", draft), 0)

	def test_the_preflight_itself_refuses_a_stale_document(self):
		"""``CustomSubmissionQueue.insert`` called directly (``queue_submission``)."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		draft = fx.raw_employee_ir(w, "Issue", [(stale, mwo)], employee="MM2")

		with patch.object(CustomSubmissionQueue, "queue_action") as queue_action:
			self.assertGuard(
				StaleOperationError,
				queue_submission,
				fx.reload(draft, "Employee IR"),
				"Submit",
			)

		queue_action.assert_not_called()
		self.assertEqual(self._queue_rows(draft), [])

	def test_a_queued_valid_submit_is_queued(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))

		queue_action = self._desk_submit(fx.reload(draft))

		queue_action.assert_called_once()
		self.assertEqual(
			queue_action.call_args.kwargs["job_id"],
			f"submit::Employee IR::{draft.name}",
		)
		self.assertEqual(
			[row.status for row in self._queue_rows(draft.name)], ["Queued"]
		)
		self.assertEqual(fx.operation(mop).status, "Not Started")  # nothing ran yet

	def test_the_queue_worker_re_checks_a_draft_that_went_stale_after_it_was_queued(
		self,
	):
		"""The production path of 43405 (Submission Queue fokdm8618c): the worker submits the
		document it was handed. The draft is valid when queued; before the worker runs, the
		work order moves on (emulated with ``fixtures.hidden``, as in TestIncident). The worker
		is replayed in-process -- the bench workers run the unpatched code -- with the job's
		pickled document; its ``commit`` / ``rollback`` are emulated with a savepoint."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		queue_action = self._desk_submit(fx.reload(draft))
		job = queue_action.call_args.kwargs
		# What RQ hands the worker: a pickled copy of the document as it was queued.
		handed = pickle.loads(pickle.dumps(job["to_be_queued_doc"]))
		(queued,) = self._queue_rows(draft.name)

		with fx.hidden("Employee IR", draft.name):
			fx.work_cycle(w, mwo, department="MM", employee="MM2")
		self.assertEqual(fx.operation(mop).status, "Finished")
		before = fx.snapshot(mwo)

		real_rollback = frappe.db.rollback
		frappe.db.savepoint("cog_worker")
		with (
			patch.object(
				frappe.db, "commit", lambda *a, **k: frappe.db.savepoint("cog_worker")
			),
			patch.object(
				frappe.db,
				"rollback",
				lambda *a, **k: real_rollback(save_point="cog_worker"),
			),
			patch(
				"frappe.core.doctype.submission_queue.submission_queue.get_current_job",
				return_value=SimpleNamespace(id=job["job_id"]),
			),
			patch.object(CustomSubmissionQueue, "notify") as notify,
		):
			frappe.get_doc("Submission Queue", queued.name).background_submission(
				to_be_queued_doc=handed,
				action_for_queuing=job["action_for_queuing"],
			)

		(row,) = self._queue_rows(draft.name)
		self.assertEqual(row.status, "Failed")
		# Only the last traceback line: frames "with context" may carry connection settings.
		last_line = (row.exception or "").strip().splitlines()[-1:]
		self.assertTrue(
			last_line and "StaleOperationError" in last_line[0],
			"the worker failed for another reason",
		)
		notify.assert_called_once_with("Failed", job["action_for_queuing"])
		self.assertEqual(self.docstatus("Employee IR", draft.name), 0)
		self.assertEqual(fx.mop_logs(voucher_no=draft.name), [])
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertOnlyPointerOpen(mwo)


# ==============================================================================================
# atomicity: a later failure undoes everything
# ==============================================================================================


class TestAtomicity(_GuardHarness):
	"""The guard passes, the Issue starts writing, a later step fails: the request's rollback
	(emulated with a savepoint) must leave nothing behind -- the guard itself never commits."""

	def _submit_with_failure(self, target):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		before = fx.snapshot(mwo)
		frappe.db.savepoint("cog_atomicity")

		with patch(
			f"{EMPLOYEE_IR_MODULE}.{target}", side_effect=RuntimeError("injected")
		):
			with self.assertRaises(RuntimeError):
				fx.submit_ir(draft)

		# The Issue really did start writing before the injected failure...
		self.assertEqual(fx.operation(mop).status, "WIP")
		self.assertTrue(fx.mop_logs(voucher_no=draft.name))
		frappe.db.rollback(save_point="cog_atomicity")
		# ...and the rollback leaves no trace of it.
		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(self.docstatus("Employee IR", draft.name), 0)

	def test_failure_in_create_tree_on_issue_rolls_everything_back(self):
		self._submit_with_failure("create_tree_on_issue")

	def test_failure_in_batch_add_time_logs_rolls_everything_back(self):
		self._submit_with_failure("batch_add_time_logs")


# ==============================================================================================
# cancel guards
# ==============================================================================================


class TestCancelGuards(_GuardHarness):
	def _cancel_refused(self, doc, *mwos):
		before = fx.snapshot(*mwos)
		frappe.db.savepoint("cog_cancel")
		message = self.assertGuard(HistoryRewriteError, fx.reload(doc).cancel)
		self.assertEqual(fx.snapshot(*mwos), before)
		frappe.db.rollback(save_point="cog_cancel")
		self.assertEqual(self.docstatus(doc.doctype, doc.name), 1)
		return message

	def test_an_employee_issue_cannot_be_cancelled_after_its_receive(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue, _receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")

		message = self._cancel_refused(issue, mwo)

		self.assertIn(mop, message)
		self.assertEqual(fx.operation(mop).status, "Finished")

	def test_an_employee_issue_can_be_cancelled_before_its_receive(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, employee="MM1")

		fx.reload(issue).cancel()

		op = fx.operation(mop)
		self.assertEqual(
			(op.status, op.operation, op.employee), ("Not Started", None, None)
		)
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=issue.name)}, {1}
		)
		self.assertOnlyPointerOpen(mwo)

	def test_an_employee_receive_can_be_cancelled_while_its_successor_is_untouched(
		self,
	):
		"""``cancel_loss_stock_entries`` is stubbed: this receive booked no loss, and its SRE
		restore query reads ``Stock Reservation Entry.custom_replaced_sre_snapshot``, which
		patch ``add_sre_replaced_snapshot_field`` never provisioned on the disposable site (SQL
		1054). The cancel guard runs before it."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		_issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		minted = fx.pointer(mwo)

		with patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries") as cancel_loss:
			fx.reload(receive).cancel()

		cancel_loss.assert_called_once()

		# kggk_uat keeps the minted operation as Revert history instead of deleting it.
		op = fx.operation(minted)
		self.assertEqual(
			(op.department_ir_status, op.status, op.previous_mop),
			("Revert", "Not Started", mop),
		)
		self.assertEqual(fx.pointer(mwo), mop)
		self.assertEqual(fx.operation(mop).status, "WIP")  # back with the employee
		self.assertOnlyPointerOpen(mwo)

	def test_an_employee_receive_cannot_be_cancelled_once_the_work_order_moved_on(self):
		"""The cancel would retire the operation the Receive minted (kggk_uat: mark it Revert)
		and reopen its SOURCE: the message names the source, and where the work order went."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		_issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		minted = fx.pointer(mwo)
		fx.transfer(w, mwo, current="MM", to="PP", receive=False)

		message = self._cancel_refused(receive, mwo)

		self.assertIn(f"has moved on to <strong>{fx.pointer(mwo)}</strong>", message)
		self.assertIn(f"would reopen <strong>{mop}</strong>", message)
		self.assertNotIn(f"would reopen <strong>{minted}</strong>", message)

	def test_a_department_receive_cannot_be_cancelled_after_a_later_issue(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		_dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		received = fx.pointer(mwo)
		fx.transfer(w, mwo, current="PP", to="FP", receive=False)

		message = self._cancel_refused(dir_receive, mwo)

		self.assertIn(f"has moved on to <strong>{fx.pointer(mwo)}</strong>", message)
		self.assertIn(
			f"would send <strong>{received}</strong> back in transit", message
		)

	def test_a_department_issue_cannot_be_cancelled_after_its_receive(self):
		"""The work order did not move: the transfer was received. The cancel would retire the
		transfer's operation (kggk_uat: mark it Revert) and reopen the SOURCE."""
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		transfer_op = fx.pointer(mwo)

		message = self._cancel_refused(dir_issue, mwo)

		self.assertIn(
			f"its transfer <strong>{transfer_op}</strong> of work order <strong>{mwo}</strong> "
			f"was already received by <strong>{dir_receive.name}</strong>",
			message,
		)
		self.assertIn(f"would reopen <strong>{source}</strong>", message)
		self.assertNotIn("has moved on", message)
		self.assertEqual(fx.operation(source).status, "Finished")

	def test_a_department_receive_cannot_be_cancelled_once_an_employee_took_the_work(
		self,
	):
		"""Family A (the 10-01 Department Receive cancels): the receiving department had already
		issued the work order to an employee. The message names that Employee IR -- the document
		to reverse first -- and does not claim the work order "moved on" to the operation the
		cancel itself names."""
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		_dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		received = fx.pointer(mwo)
		employee_issue = fx.issue(w, mwo, department="PP", employee="PP1")

		message = self._cancel_refused(dir_receive, mwo)

		self.assertIn(employee_issue.name, message)
		self.assertIn(w.employees.PP1, message)
		self.assertIn(
			f"would send <strong>{received}</strong> back in transit while it is WIP",
			message,
		)
		self.assertNotIn("would reopen", message)
		self.assertNotIn("has moved on", message)
		op = fx.operation(received)
		self.assertEqual(
			(op.status, op.department_ir_status, op.employee),
			("WIP", "Received", w.employees.PP1),
		)

	def test_a_department_issue_cancel_restores_not_started_and_reverts_the_minted_operation(
		self,
	):
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		dir_issue, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		minted = fx.pointer(mwo)
		self.assertNotEqual(minted, source)

		fx.reload(dir_issue).cancel()

		self.assertEqual(fx.operation(source).status, "Not Started")
		# kggk_uat keeps the transfer's operation as Revert history instead of deleting it.
		op = fx.operation(minted)
		self.assertEqual(
			(op.department_ir_status, op.status, op.previous_mop),
			("Revert", "Not Started", source),
		)
		self.assertEqual(fx.pointer(mwo), source)
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=dir_issue.name)}, {1}
		)
		self.assertOnlyPointerOpen(mwo)
		# Not Started is exactly what every picker accepts: the transfer can simply be redone.
		fx.transfer(w, mwo, current="MM", to="PP")
		self.assertOnlyPointerOpen(mwo)

	def test_a_department_receive_cancel_restores_the_work_order_department(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		_dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		received = fx.pointer(mwo)
		self.assertEqual(
			frappe.db.get_value("Manufacturing Work Order", mwo, "department"),
			w.departments.PP,
		)

		fx.reload(dir_receive).cancel()

		self.assertEqual(
			frappe.db.get_value("Manufacturing Work Order", mwo, "department"),
			w.departments.MM,
		)
		op = fx.operation(received)
		self.assertEqual(
			(op.status, op.department_ir_status, op.department_receive_id),
			("Not Started", "In-Transit", None),
		)
		self.assertEqual(fx.pointer(mwo), received)

	def test_cancels_in_reverse_order_unwind_the_whole_history(self):
		"""LIFO unwinding is always allowed. ``cancel_loss_stock_entries`` is stubbed for the
		Employee Receive cancel, as in the test above."""
		w = self.world
		mwo, first = fx.make_work_order(w, "MM")
		issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		received = fx.pointer(mwo)
		dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		transferred = fx.pointer(mwo)

		fx.reload(dir_receive).cancel()
		self.assertOnlyPointerOpen(mwo)
		fx.reload(dir_issue).cancel()
		self.assertOnlyPointerOpen(mwo)
		with patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"):
			fx.reload(receive).cancel()
		self.assertOnlyPointerOpen(mwo)
		fx.reload(issue).cancel()

		self.assertEqual(fx.pointer(mwo), first)
		# kggk_uat: the two minted operations stay behind as Revert history.
		self.assertEqual(fx.open_operations(mwo), [first])
		self.assertEqual(fx.reverted_operations(mwo), [received, transferred])
		self.assertEqual(
			[r.name for r in fx.operations(mwo)], [first, received, transferred]
		)
		op = fx.operation(first)
		self.assertEqual(
			(op.status, op.operation, op.employee), ("Not Started", None, None)
		)
		for doc in (issue, receive, dir_issue, dir_receive):
			self.assertEqual(self.docstatus(doc.doctype, doc.name), 2)

	def test_cancel_guards_ignore_the_kill_switch(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		issue, _receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")

		with patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}):
			self._cancel_refused(issue, mwo)


# ==============================================================================================
# kggk_uat keeps what a cancel minted, marked Revert: history, never a later operation
# ==============================================================================================


class TestRevertedHistory(_GuardHarness):
	"""kggk_uat's Department Issue and Employee Receive cancels do NOT delete the operation they
	minted: they mark it ``department_ir_status = "Revert"`` (status Not Started, after
	``ensure_operation_not_in_use``) and move the work order back to the source -- so the reverted
	row still names the source as its ``previous_mop``. The guard must read it as history (as every
	picker, ``validate_no_reverted_operations`` and the audit do), or every cancel-and-redo flow is
	refused as "already has a later operation".

	These tests run the REAL kggk_uat cancels. ``_reverted`` records the operations a block's
	cancels marked Revert, and fails the test if any of them deleted a Manufacturing Operation
	instead (the kggk_prod behaviour)."""

	def _reverted_now(self):
		return frappe.get_all(
			"Manufacturing Operation",
			filters={
				"department_ir_status": "Revert",
				"manufacturing_work_order": ["like", f"{self.world.prefix}-%"],
			},
			order_by="creation asc, name asc",
			pluck="name",
		)

	@contextmanager
	def _reverted(self):
		real_delete = frappe.delete_doc
		deleted = []

		def delete_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Manufacturing Operation":
				deleted.append(name)
			return real_delete(doctype, name, *args, **kwargs)

		before = set(self._reverted_now())
		reverted = []
		with patch.object(frappe, "delete_doc", delete_doc):
			yield reverted
		self.assertEqual(
			deleted, [], "a kggk_uat cancel deleted the operation it minted"
		)
		reverted.extend(name for name in self._reverted_now() if name not in before)

	def _live(self, mwo):
		"""Open operations that are live work -- Revert rows are not."""
		return [
			r.name
			for r in fx.operations(mwo)
			if is_open_operation(r.status, r.department_ir_status)
		]

	def assertOnlyPointerLive(self, mwo):
		self.assertEqual(self._live(mwo), [fx.pointer(mwo)])

	def test_a_transfer_can_be_redone_after_its_issue_was_cancelled(self):
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		dir_issue, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		in_transit = fx.pointer(mwo)
		with self._reverted() as reverted:
			fx.reload(dir_issue).cancel()
		self.assertEqual(reverted, [in_transit])
		op = fx.operation(in_transit)
		self.assertEqual(
			(op.previous_mop, op.department_ir_status, op.status),
			(source, "Revert", "Not Started"),
		)
		self.assertEqual(
			(fx.pointer(mwo), fx.operation(source).status), (source, "Not Started")
		)

		redo, redo_receive = fx.transfer(w, mwo, current="MM", to="PP")

		self.assertEqual(self.docstatus("Department IR", redo.name), 1)
		self.assertEqual(self.docstatus("Department IR", redo_receive.name), 1)
		received = fx.pointer(mwo)
		self.assertNotIn(received, (source, in_transit))
		op = fx.operation(received)
		self.assertEqual(
			(op.previous_mop, op.department, op.department_ir_status),
			(source, w.departments.PP, "Received"),
		)
		# The source now names two later operations: the Revert history and the live redo.
		self.assertEqual(fx.reverted_operations(mwo), [in_transit])
		self.assertOnlyPointerLive(mwo)
		self.assertOnlyPointerOpen(mwo)
		# ... and the work goes on in the receiving department.
		fx.work_cycle(w, mwo, department="PP", employee="PP1")
		self.assertOnlyPointerLive(mwo)

	def test_the_restored_operation_can_be_issued_to_an_employee(self):
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		dir_issue, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		with self._reverted():
			fx.reload(dir_issue).cancel()

		issue = fx.issue(w, mwo, department="MM", employee="MM1")

		self.assertEqual(self.docstatus("Employee IR", issue.name), 1)
		self.assertEqual(fx.operation(source).status, "WIP")
		self.assertOnlyPointerLive(mwo)

	def test_work_can_be_received_again_after_its_receive_was_cancelled(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		_issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		minted = fx.pointer(mwo)
		with (
			self._reverted() as reverted,
			patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"),
		):
			fx.reload(receive).cancel()
		self.assertEqual(reverted, [minted])
		self.assertEqual(fx.operation(minted).previous_mop, mop)
		self.assertEqual((fx.pointer(mwo), fx.operation(mop).status), (mop, "WIP"))

		again = fx.receive(w, mwo, department="MM", employee="MM1")

		self.assertEqual(self.docstatus("Employee IR", again.name), 1)
		self.assertEqual(fx.operation(mop).status, "Finished")
		successor = fx.pointer(mwo)
		self.assertNotIn(successor, (mop, minted))
		self.assertEqual(fx.operation(successor).previous_mop, mop)
		self.assertEqual(fx.operation(minted).department_ir_status, "Revert")
		self.assertOnlyPointerLive(mwo)
		self.assertOnlyPointerOpen(mwo)

	def test_an_issue_can_be_cancelled_after_its_receive_was(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		with (
			self._reverted(),
			patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"),
		):
			fx.reload(receive).cancel()
			fx.reload(issue).cancel()

		op = fx.operation(mop)
		self.assertEqual(
			(op.status, op.operation, op.employee), ("Not Started", None, None)
		)
		self.assertEqual(self.docstatus("Employee IR", issue.name), 2)
		self.assertOnlyPointerLive(mwo)

	def test_cancels_in_reverse_order_unwind_the_whole_history(self):
		"""The LIFO unwind of TestCancelGuards on uat semantics: two Revert rows are left (the
		Receive-minted and the transfer's operation), each naming the operation it came from, and
		every cancel is allowed."""
		w = self.world
		mwo, first = fx.make_work_order(w, "MM")
		issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		received = fx.pointer(mwo)
		dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		transferred = fx.pointer(mwo)

		with (
			self._reverted() as reverted,
			patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"),
		):
			for doc in (dir_receive, dir_issue, receive, issue):
				fx.reload(doc).cancel()
				self.assertOnlyPointerLive(mwo)

		self.assertEqual(fx.pointer(mwo), first)
		self.assertEqual(reverted, [received, transferred])
		self.assertEqual(
			[fx.operation(name).previous_mop for name in reverted], [first, received]
		)
		op = fx.operation(first)
		self.assertEqual(
			(op.status, op.operation, op.employee), ("Not Started", None, None)
		)
		for doc in (issue, receive, dir_issue, dir_receive):
			self.assertEqual(self.docstatus(doc.doctype, doc.name), 2)

	def test_a_live_later_operation_still_makes_the_work_order_ambiguous(self):
		"""Only Revert history is ignored: a live successor of the pointer still refuses new work
		and the cancel of the transaction it came after."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, department="MM", employee="MM1")
		fx.make_operation(w, mwo, "MM", previous_mop=mop)  # live, legacy

		self.assertGuard(
			AmbiguousOperationError,
			fx.insert_ir,
			fx.employee_ir(w, "Receive", [mwo], employee="MM1"),
		)
		before = fx.snapshot(mwo)
		message = self.assertGuard(HistoryRewriteError, fx.reload(issue).cancel)
		self.assertIn("already has a later operation", message)
		self.assertEqual(fx.snapshot(mwo), before)

	def test_cancel_and_redo_at_every_step_then_unwind_everything(self):
		"""A transfer issued, cancelled and issued again; that one received and unwound too; the
		Employee Receive cancelled and received again; then everything unwound. Every step is
		allowed, the work order always has exactly one live operation, and the Revert rows pile up
		as history without ever counting as a later operation."""
		w = self.world
		mwo, first = fx.make_work_order(w, "MM")
		issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		received = fx.pointer(mwo)

		with self._reverted() as transfers:
			wrong, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
			wrong_op = fx.pointer(mwo)
			fx.reload(wrong).cancel()
			self.assertOnlyPointerLive(mwo)
			redo, redo_receive = fx.transfer(w, mwo, current="MM", to="PP")
			redo_op = fx.pointer(mwo)
			self.assertOnlyPointerLive(mwo)
			fx.reload(redo_receive).cancel()
			self.assertOnlyPointerLive(mwo)
			fx.reload(redo).cancel()
			self.assertOnlyPointerLive(mwo)
		self.assertEqual(transfers, [wrong_op, redo_op])
		self.assertEqual(
			[fx.operation(name).previous_mop for name in transfers], [received] * 2
		)
		self.assertEqual(
			(fx.pointer(mwo), fx.operation(received).status), (received, "Not Started")
		)

		with (
			self._reverted() as receives,
			patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"),
		):
			fx.reload(receive).cancel()
			self.assertOnlyPointerLive(mwo)
			again = fx.receive(w, mwo, department="MM", employee="MM1")
			again_op = fx.pointer(mwo)
			self.assertOnlyPointerLive(mwo)
			fx.reload(again).cancel()
			self.assertOnlyPointerLive(mwo)
			fx.reload(issue).cancel()
		self.assertEqual(receives, [received, again_op])

		self.assertEqual(fx.pointer(mwo), first)
		self.assertEqual(fx.open_operations(mwo), [first])
		self.assertEqual(
			sorted(fx.reverted_operations(mwo)),
			sorted([received, wrong_op, redo_op, again_op]),
		)
		op = fx.operation(first)
		self.assertEqual(
			(op.status, op.operation, op.employee), ("Not Started", None, None)
		)
		for doc in (issue, receive, wrong, redo, redo_receive, again):
			self.assertEqual(self.docstatus(doc.doctype, doc.name), 2)
		# ... and the work order starts over from its first operation.
		fx.work_cycle(w, mwo, department="MM", employee="MM2")
		self.assertOnlyPointerLive(mwo)

	def test_a_document_naming_a_reverted_operation_is_refused(self):
		"""A Revert row is never the current operation: a new Employee or Department IR naming one
		is refused by the guard (stale), before kggk_uat's own ``validate_no_reverted_operations``
		-- which still refuses it when the kill switch turns the guard's refusals into warnings."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		_issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		minted = fx.pointer(mwo)
		moved, source = fx.make_work_order(w, "MM")
		dir_issue, _ = fx.transfer(w, moved, current="MM", to="PP", receive=False)
		in_transit = fx.pointer(moved)
		with (
			self._reverted() as reverted,
			patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"),
		):
			fx.reload(receive).cancel()
			fx.reload(dir_issue).cancel()
		self.assertEqual(sorted(reverted), sorted([minted, in_transit]))
		before = fx.snapshot(mwo, moved)

		def documents():
			# An Employee Issue of the operation the cancelled Receive minted, and a new transfer
			# onward of the one the cancelled Department Issue minted (a Receive against that
			# cancelled Issue cannot even be created: Frappe refuses the cancelled link first).
			return (
				fx.employee_ir(w, "Issue", [(minted, mwo)], employee="MM2"),
				fx.department_ir(
					w, "Issue", [(in_transit, moved)], current="PP", to="FP"
				),
			)

		for doc in documents():
			with self.subTest(doctype=doc.doctype):
				message = self.assertGuard(StaleOperationError, fx.insert_ir, doc)
				self.assertIn("is not the current operation", message)

		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch("frappe.log_error"),
		):
			for doc in documents():
				with self.subTest(doctype=doc.doctype, mode="warn"):
					with self.assertRaisesRegex(
						frappe.ValidationError, "reverted by a cancelled IR"
					):
						fx.insert_ir(doc)
		self.assertEqual(fx.snapshot(mwo, moved), before)
		self.assertEqual((fx.pointer(mwo), fx.pointer(moved)), (mop, source))


# ==============================================================================================
# an Employee Issue cancel restores the operation's true pre-issue state
# ==============================================================================================


class TestIssueCancelRestoresPreIssueState(_GuardHarness):
	"""Before the Issue the operation was Not Started with no holder, no running timer and no
	open time log. The cancel used to leave ``started_time`` and the Issue's open time log behind
	(a Not Started operation with a running timer); it removes both now."""

	def _timer(self, mop):
		return frappe.db.get_value("Manufacturing Operation", mop, "started_time")

	def _add_time_log(
		self, mop, *, employee=None, to_time=None, minutes_ago=30, from_time=None
	):
		row = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation Time Log",
				"parent": mop,
				"parenttype": "Manufacturing Operation",
				"parentfield": "time_logs",
				"idx": 90 + len(fx.time_logs(mop)),
				"from_time": from_time
				or add_to_date(get_datetime(), minutes=-minutes_ago),
				"to_time": to_time,
				"employee": employee,
			}
		)
		row.db_insert()
		return row.name

	def _backdate_issue(self, issue, minutes):
		"""Move an Issue's submit -- and the time log it opened -- ``minutes`` into the past."""
		was = get_datetime(
			frappe.db.get_value("Employee IR", issue.name, "issue_submitted_on")
		)
		submitted = add_to_date(get_datetime(), minutes=-minutes)
		frappe.db.set_value(
			"Employee IR",
			issue.name,
			"issue_submitted_on",
			submitted,
			update_modified=False,
		)
		for mop in {r.manufacturing_operation for r in issue.employee_ir_operations}:
			for log in fx.time_logs(mop):
				if not log.to_time and get_datetime(log.from_time) >= add_to_date(
					was, seconds=-1
				):
					frappe.db.set_value(
						"Manufacturing Operation Time Log",
						log.name,
						"from_time",
						add_to_date(submitted, seconds=0.3),
						update_modified=False,
					)
		return submitted

	def _double_issue(self, minutes_apart=9):
		"""Legacy: the work order's operation issued to MM1, then -- by the pre-guard code -- a
		second submitted Issue of the SAME operation to MM1 ``minutes_apart`` minutes later, each
		with its own running timer. Returns ``(mwo, mop, first, second, first_log, second_log)``."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		first = fx.issue(w, mwo, employee="MM1")
		self._backdate_issue(first, minutes_apart)
		(first_log,) = [t.name for t in fx.time_logs(mop) if not t.to_time]
		second = fx.raw_employee_ir(
			w, "Issue", [(mop, mwo)], employee="MM1", docstatus=1
		)
		second_log = self._add_time_log(
			mop,
			employee=w.employees.MM1,
			from_time=add_to_date(
				frappe.db.get_value("Employee IR", second, "issue_submitted_on"),
				seconds=0.3,
			),
		)
		return mwo, mop, fx.reload(first), second, first_log, second_log

	def test_a_double_issued_operation_cannot_be_cancelled_normally(self):
		"""Cancelling either Issue would put the operation back to Not Started and unassigned
		while the other still holds it; only the reviewed repair may cancel one of them."""
		mwo, mop, first, second, first_log, second_log = self._double_issue()
		before = fx.snapshot(mwo)
		logs = fx.time_logs(mop)
		for name in (first.name, second):
			with self.subTest(issue=name):
				frappe.db.savepoint("cog_double_issue")
				message = self.assertGuard(
					AmbiguousOperationError, fx.reload(name, "Employee IR").cancel
				)
				self.assertIn("current-operation audit", message)
				self.assertEqual(fx.snapshot(mwo), before)
				self.assertEqual(fx.time_logs(mop), logs)
				frappe.db.rollback(save_point="cog_double_issue")
				self.assertEqual(self.docstatus("Employee IR", name), 1)

	def test_a_reviewed_cancel_of_either_double_issue_removes_only_its_own_timer(self):
		"""F4: both running timers belong to MM1. The cancel used to delete every open row of
		its holder -- the other Issue's timer included."""
		for cancelled in ("first", "second"):
			with self.subTest(cancelled=cancelled):
				mwo, mop, first, second, first_log, second_log = self._double_issue()
				name = first.name if cancelled == "first" else second
				frappe.flags[REVIEWED_REPAIR_FLAG] = {("Employee IR", name)}
				fx.reload(name, "Employee IR").cancel()
				frappe.flags.pop(REVIEWED_REPAIR_FLAG, None)
				kept = second_log if cancelled == "first" else first_log
				self.assertEqual(
					[t.name for t in fx.time_logs(mop) if not t.to_time], [kept]
				)
				self.assertEqual(self.docstatus("Employee IR", name), 2)

	def test_a_resumed_timer_goes_with_the_cancel_and_an_older_leftover_stays(self):
		"""Held by this Issue alone: its own row and a Resume Job's (after a Pause) are the
		Issue's custody; an open row from before its submit is not."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		leftover = self._add_time_log(
			mop, employee=w.employees.MM1, minutes_ago=2 * 24 * 60
		)
		issue = fx.issue(w, mwo, employee="MM1")
		submitted = self._backdate_issue(issue, 30)
		(own,) = [
			t.name for t in fx.time_logs(mop) if not t.to_time and t.name != leftover
		]
		frappe.db.set_value(
			"Manufacturing Operation Time Log",
			own,
			"to_time",
			add_to_date(submitted, minutes=5),
			update_modified=False,
		)  # paused 5 minutes in
		resumed = self._add_time_log(
			mop, employee=w.employees.MM1, from_time=add_to_date(submitted, minutes=20)
		)

		fx.reload(issue).cancel()

		remaining = {t.name for t in fx.time_logs(mop)}
		self.assertNotIn(resumed, remaining)
		self.assertIn(leftover, remaining)
		self.assertIn(own, remaining)  # closed: earlier custody, kept

	def test_cancel_clears_the_timer_and_the_issues_open_time_log(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, employee="MM1")
		self.assertIsNotNone(self._timer(mop))
		self.assertEqual([t.to_time for t in fx.time_logs(mop)], [None])

		fx.reload(issue).cancel()

		self.assertIsNone(self._timer(mop))
		self.assertEqual(fx.time_logs(mop), [])
		op = fx.operation(mop)
		self.assertEqual(
			(op.status, op.operation, op.employee), ("Not Started", None, None)
		)
		self.assertOnlyPointerOpen(mwo)
		# ... which is exactly what the next Issue starts from
		again = fx.issue(w, mwo, employee="MM2")
		self.assertEqual(
			[(t.employee, t.to_time) for t in fx.time_logs(mop)],
			[(w.employees.MM2, None)],
		)
		self.assertEqual(self.docstatus("Employee IR", again.name), 1)

	def test_closed_rows_and_other_holders_rows_are_kept(self):
		"""Only the Issue holder's OPEN rows are the Issue's own."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, employee="MM1")
		own_open = [t.name for t in fx.time_logs(mop)]
		paused = self._add_time_log(
			mop,
			employee=w.employees.MM1,
			to_time=add_to_date(get_datetime(), minutes=-10),
		)
		other = self._add_time_log(mop, employee=w.employees.MM2)

		fx.reload(issue).cancel()

		remaining = {t.name for t in fx.time_logs(mop)}
		self.assertEqual(remaining, {paused, other})
		self.assertFalse(remaining & set(own_open))

	def test_a_subcontracting_issue_cancel_clears_its_employee_less_time_log(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		with patch(f"{EMPLOYEE_IR_MODULE}.EmployeeIR.create_subcontracting_order"):
			issue = fx.submit_ir(
				fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], subcontractor="SC1"))
			)
		self.assertEqual(
			[(t.employee, t.to_time) for t in fx.time_logs(mop)], [(None, None)]
		)
		self.assertIsNotNone(self._timer(mop))

		fx.reload(issue).cancel()

		self.assertEqual(fx.time_logs(mop), [])
		self.assertIsNone(self._timer(mop))
		self.assertEqual(
			frappe.db.get_value("Manufacturing Operation", mop, "subcontractor"), None
		)

	def test_a_reviewed_repair_cancel_of_a_stale_issue_drops_only_the_stale_time_log(
		self,
	):
		"""Family C under the reviewed-repair flag: the stale Issue reopened a FINISHED operation
		whose legitimate custody left a closed time log. The cancel deletes the stale Issue's open
		row (the repair script's listed row is then already gone) and keeps the legitimate one."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		with fx.hidden("Employee IR", draft.name):  # as in TestIncident
			fx.work_cycle(w, mwo, department="MM", employee="MM2")
			fx.transfer(w, mwo, current="MM", to="PP")
		legitimate = [t.name for t in fx.time_logs(stale)]
		self.assertEqual(len(legitimate), 1)
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch("frappe.log_error"),
		):
			fx.submit_ir(draft)  # the incident's footprint
		stale_open = [t.name for t in fx.time_logs(stale) if not t.to_time]
		self.assertEqual(len(stale_open), 1)
		self.assertIsNotNone(self._timer(stale))

		frappe.flags[REVIEWED_REPAIR_FLAG] = {("Employee IR", draft.name)}
		fx.reload(draft).cancel()

		self.assertEqual([t.name for t in fx.time_logs(stale)], legitimate)
		self.assertIsNone(self._timer(stale))
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=draft.name)}, {1}
		)


# ==============================================================================================
# duplicate rows and empty documents
# ==============================================================================================


class TestRowsAndEmptyDocuments(_GuardHarness):
	def test_a_department_issue_with_the_same_operation_twice_is_rejected(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		doc = fx.department_ir(
			w, "Issue", [(mop, mwo), (mop, mwo)], current="MM", to="PP"
		)

		message = self.assertGuard(CurrentOperationError, fx.insert_ir, doc)
		self.assertIn("already on row 1", message)

	def test_a_department_issue_with_the_same_work_order_twice_is_rejected(self):
		"""Family B: two rows of one work order (here a twin operation) mint two successors."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		twin = fx.make_operation(w, mwo, "MM")
		doc = fx.department_ir(
			w, "Issue", [(mop, mwo), (twin, mwo)], current="MM", to="PP"
		)
		before = fx.snapshot(mwo)

		message = self.assertGuard(CurrentOperationError, fx.insert_ir, doc)

		self.assertIn(mwo, message)
		self.assertIn("already on row 1", message)
		self.assertEqual(fx.snapshot(mwo), before)

	def test_documents_without_rows_save_without_a_sql_error(self):
		"""``isin([])`` used to render ``IN ()``: SQL 1064 on the first save of an empty form."""
		w = self.world
		department_ir = fx.insert_ir(
			fx.department_ir(w, "Issue", [], current="MM", to="PP")
		)
		employee_ir = fx.insert_ir(fx.employee_ir(w, "Issue", []))

		self.assertEqual(self.docstatus("Department IR", department_ir.name), 0)
		self.assertEqual(self.docstatus("Employee IR", employee_ir.name), 0)


# ==============================================================================================
# no false positives on states and holders the client offers
# ==============================================================================================


class TestReceiveStatesAndHolders(_GuardHarness):
	"""``EmployeeIR.create_subcontracting_order`` (a service Purchase Order) is stubbed in the
	subcontracting tests: it is outside the guard and needs purchasing masters this world lacks."""

	def test_receive_accepts_exactly_the_states_the_client_offers(self):
		w = self.world
		for status, allowed in (
			("On Hold", True),
			("QC Completed", True),
			("QC Pending", False),
		):
			with self.subTest(status=status):
				mwo, mop = fx.make_work_order(w, "MM")
				fx.issue(w, mwo, employee="MM1")
				frappe.db.set_value(
					"Manufacturing Operation",
					mop,
					"status",
					status,
					update_modified=False,
				)
				doc = fx.employee_ir(w, "Receive", [mwo], employee="MM1")
				if allowed:
					fx.submit_ir(fx.insert_ir(doc))
					self.assertEqual(fx.operation(mop).status, "Finished")
					self.assertOnlyPointerOpen(mwo)
				else:
					message = self.assertGuard(CurrentOperationError, fx.insert_ir, doc)
					self.assertIn("QC Pending", message)

	def test_a_subcontracted_issue_and_receive_pass_the_guard(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")

		with patch(
			f"{EMPLOYEE_IR_MODULE}.EmployeeIR.create_subcontracting_order"
		) as order:
			fx.submit_ir(
				fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], subcontractor="SC1"))
			)
		order.assert_called_once()
		issued = frappe.db.get_value(
			"Manufacturing Operation",
			mop,
			["status", "subcontractor", "for_subcontracting", "employee"],
			as_dict=True,
		)
		self.assertEqual(
			(
				issued.status,
				issued.subcontractor,
				issued.for_subcontracting,
				issued.employee,
			),
			("WIP", w.subcontractors.SC1, 1, None),
		)

		# Another subcontractor cannot receive it; the one holding it can.
		self.assertGuard(
			CurrentOperationError,
			fx.insert_ir,
			fx.employee_ir(w, "Receive", [mwo], employee="MM1"),
		)
		fx.submit_ir(
			fx.insert_ir(fx.employee_ir(w, "Receive", [mwo], subcontractor="SC1"))
		)

		self.assertEqual(fx.operation(mop).status, "Finished")
		self.assertOnlyPointerOpen(mwo)
		# The successor carries no holder over (no_copy), so the transfer pickers -- and the
		# guard that mirrors them -- accept it.
		dir_issue, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		self.assertEqual(self.docstatus("Department IR", dir_issue.name), 1)

	def test_a_subcontracted_issue_can_be_cancelled_before_its_receive(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		with patch(f"{EMPLOYEE_IR_MODULE}.EmployeeIR.create_subcontracting_order"):
			issue = fx.submit_ir(
				fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], subcontractor="SC1"))
			)

		fx.reload(issue).cancel()

		cancelled = frappe.db.get_value(
			"Manufacturing Operation",
			mop,
			["status", "subcontractor", "operation"],
			as_dict=True,
		)
		self.assertEqual(
			(cancelled.status, cancelled.subcontractor, cancelled.operation),
			("Not Started", None, None),
		)
		self.assertOnlyPointerOpen(mwo)


# ==============================================================================================
# EOD sync hold
# ==============================================================================================


class TestEODHold(_GuardHarness):
	STATS_KEYS = ("total_mwos", "processed_mwos", "failed_mwos")

	def _groups(self, mwo):
		logs = frappe.db.get_all(
			"MOP Log",
			filters={
				"manufacturing_work_order": mwo,
				"is_synced": 0,
				"is_cancelled": 0,
			},
			fields=list(_MOP_LOG_GATHER_FIELDS),
			order_by="manufacturing_operation, flow_index asc, creation asc",
		)
		groups = _group_logs_by_company_and_mwo(logs)
		return groups[(self.world.company, mwo)]

	def _plan(self, mwo):
		failures = []
		stats = {key: 0 for key in self.STATS_KEYS}
		stats.update({"submitted_ses": [], "draft_ses": [], "artifact_skipped": []})
		result = _plan_mwo_group(
			(self.world.company, mwo),
			self._groups(mwo),
			failures,
			stats,
			sync_log_name=None,
			selective=True,
		)
		return result, failures, stats

	def test_logs_on_an_open_non_pointer_operation_hold_the_work_order(self):
		w = self.world
		mwo, stale, current = self.legacy_chain()
		fx.seed_balance(
			w, current, mwo, is_synced=0, creation=fx.days_ago(1), set_header=False
		)
		# The stale transaction's clones: the NEWEST unsynced logs, on the reopened operation.
		fx.seed_balance(w, stale, mwo, is_synced=0, set_header=False)
		before = fx.mop_logs(manufacturing_work_order=mwo)
		self.assertEqual(len([r for r in before if not r.is_synced]), 4)

		result, failures, stats = self._plan(mwo)

		self.assertIsNone(result)
		self.assertEqual([f["step"] for f in failures], ["ambiguous_current_operation"])
		self.assertEqual(failures[0]["last_mop"], current)
		self.assertIn(stale, failures[0]["error_message"])
		self.assertEqual(stats["failed_mwos"], 1)
		# Held means held: no log changed -- in particular, none was marked synced.
		self.assertEqual(fx.mop_logs(manufacturing_work_order=mwo), before)

	def test_the_real_incident_footprint_is_held_and_a_healthy_work_order_is_not(self):
		"""The footprint written by the REAL Issue controller: the stale draft is submitted in warn
		mode (the kill switch lets rule violations through, with an Error Log), which reopens the
		finished operation and clones its balance into unsynced MOP Logs. EOD must hold that work
		order rather than plan from the reopened operation; the healthy one next to it is not
		held."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		healthy, _ = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo], employee="MM1"))
		with fx.hidden("Employee IR", draft.name):  # as in TestIncident
			fx.work_cycle(w, [mwo, healthy], department="MM", employee="MM2")
			fx.transfer(w, [mwo, healthy], current="MM", to="PP")
		current = fx.pointer(mwo)
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch("frappe.log_error"),
		):
			fx.submit_ir(draft)
		self.assertEqual(fx.operation(stale).status, "WIP")  # the incident's footprint
		clones = fx.mop_logs(voucher_no=draft.name, is_synced=0)
		self.assertEqual(len(clones), 2)
		self.assertEqual({r.manufacturing_operation for r in clones}, {stale})
		unsynced = fx.mop_logs(manufacturing_work_order=mwo, is_synced=0)

		result, failures, stats = self._plan(mwo)

		self.assertIsNone(result)
		self.assertEqual([f["step"] for f in failures], ["ambiguous_current_operation"])
		self.assertEqual(failures[0]["last_mop"], current)
		self.assertIn(stale, failures[0]["error_message"])
		self.assertEqual(stats["failed_mwos"], 1)
		# Held means held: every unsynced log stays unsynced.
		self.assertEqual(
			fx.mop_logs(manufacturing_work_order=mwo, is_synced=0), unsynced
		)
		self.assertEqual(
			_open_non_pointer_operations(healthy, self._groups(healthy)),
			(fx.pointer(healthy), []),
		)

	def test_newest_log_on_a_finished_predecessor_is_not_held(self):
		w = self.world
		mwo, finished = fx.make_work_order(
			w, "MM", seed=False, operation_status="Finished"
		)
		current = fx.make_operation(w, mwo, "MM", previous_mop=finished)
		fx.set_pointer(mwo, current)
		fx.seed_balance(w, current, mwo, is_synced=0, creation=fx.days_ago(1))
		# Receive audit copies land on the (finished) source operation: newest, and normal.
		fx.seed_balance(w, finished, mwo, is_synced=0, set_header=False)

		pointer, stale = _open_non_pointer_operations(mwo, self._groups(mwo))
		self.assertEqual((pointer, stale), (current, []))
		_result, failures, _stats = self._plan(mwo)
		self.assertNotIn(
			"ambiguous_current_operation", [f.get("step") for f in failures]
		)

	def test_a_real_lifecycle_is_never_held(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		fx.transfer(w, mwo, current="MM", to="PP")

		pointer, stale = _open_non_pointer_operations(mwo, self._groups(mwo))
		self.assertEqual((pointer, stale), (fx.pointer(mwo), []))

	def test_revert_history_with_unsynced_logs_is_never_held(self):
		"""kggk_uat: an Employee Receive cancel leaves its minted operation as Revert history
		(status Not Started). Even with unsynced logs on it -- the newest of the work order -- it
		is not an open operation, so EOD does not hold the work order."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		_issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		minted = fx.pointer(mwo)
		with patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"):
			fx.reload(receive).cancel()
		self.assertEqual(
			(fx.operation(minted).department_ir_status, fx.pointer(mwo)),
			("Revert", mop),
		)
		fx.seed_balance(w, minted, mwo, is_synced=0, set_header=False)

		pointer, stale = _open_non_pointer_operations(mwo, self._groups(mwo))
		self.assertEqual((pointer, stale), (mop, []))
		_result, failures, _stats = self._plan(mwo)
		self.assertNotIn(
			"ambiguous_current_operation", [f.get("step") for f in failures]
		)


# ==============================================================================================
# Receive's Issue resolver
# ==============================================================================================


class TestIssueResolver(_GuardHarness):
	def _double_issued(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, employee="MM1")
		duplicate = fx.raw_employee_ir(
			w, "Issue", [(mop, mwo)], employee="MM1", docstatus=1
		)
		return mwo, mop, issue.name, duplicate

	def test_one_submitted_issue_resolves(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, employee="MM1")

		resolved = resolve_employee_ir_issue_voucher_for_receive(
			frappe._dict(emp_ir_id=None), frappe._dict(manufacturing_operation=mop)
		)
		self.assertEqual(resolved, issue.name)

	def test_two_submitted_issues_on_one_operation_are_ambiguous(self):
		_mwo, mop, issue, duplicate = self._double_issued()

		message = self.assertGuard(
			AmbiguousOperationError,
			resolve_employee_ir_issue_voucher_for_receive,
			frappe._dict(emp_ir_id=None),
			frappe._dict(manufacturing_operation=mop),
		)
		self.assertIn(issue, message)
		self.assertIn(duplicate, message)

	def test_a_receive_of_a_double_issued_operation_reports_the_ambiguity(self):
		w = self.world
		mwo, _mop, _issue, _duplicate = self._double_issued()
		receive = fx.insert_ir(fx.employee_ir(w, "Receive", [mwo], employee="MM1"))
		before = fx.snapshot(mwo)

		self.assertGuard(AmbiguousOperationError, fx.submit_ir, receive)

		self.assertEqual(fx.snapshot(mwo), before)
		self.assertEqual(self.docstatus("Employee IR", receive.name), 0)


# ==============================================================================================
# desk / API reopen of an operation
# ==============================================================================================


class TestManualReopen(_GuardHarness):
	def test_reopening_a_finished_operation_that_is_not_current_is_refused(self):
		w = self.world
		mwo, finished = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		doc = frappe.get_doc("Manufacturing Operation", finished)
		doc.status = "WIP"

		message = self.assertGuard(HistoryRewriteError, doc.save)

		self.assertIn(fx.pointer(mwo), message)
		self.assertEqual(fx.operation(finished).status, "Finished")
		self.assertOnlyPointerOpen(mwo)

	def test_reopening_the_current_operation_is_allowed(self):
		w = self.world
		_mwo, current = fx.make_work_order(w, "MM", operation_status="Finished")
		doc = frappe.get_doc("Manufacturing Operation", current)
		doc.status = "WIP"

		doc.save()

		self.assertEqual(fx.operation(current).status, "WIP")


# ==============================================================================================
# kill switch
# ==============================================================================================


class TestKillSwitch(_GuardHarness):
	def _warn_logs(self):
		return frappe.get_all(
			"Error Log",
			filters={
				"method": ["like", f"{WARN_LOG_TITLE}%"],
				"error": ["like", f"%{self.world.prefix}%"],
			},
			fields=["name", "method", "error"],
		)

	def tearDown(self):
		# tabError Log is MyISAM: its rows survive the rollback.
		for row in self._warn_logs():
			frappe.db.delete("Error Log", {"name": row.name})
		super().tearDown()

	def test_warn_mode_logs_and_lets_the_save_through(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		draft = fx.insert_ir(fx.employee_ir(w, "Issue", [mwo]))

		with patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}):
			transfer = fx.insert_ir(
				fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
			)

		self.assertEqual(self.docstatus("Department IR", transfer.name), 0)
		logs = self._warn_logs()
		self.assertTrue(logs)
		self.assertTrue(all(draft.name in row.error for row in logs))
		self.assertTrue(
			any(
				"Work Order Current Operation" in m and draft.name in m
				for m in self.messages()
			)
		)
		self.assertEqual(fx.pointer(mwo), mop)

	def test_warn_mode_really_switches_the_submit_rules_off(self):
		"""The emergency switch must not break the submit path -- and it must be understood: in
		warn mode a stale draft reopens its operation again (the incident's footprint), only
		with an Error Log. Cancel guards stay on (TestCancelGuards); what else stays on is listed
		under KILL SWITCH in the guard's module docstring."""
		w = self.world
		mwo, stale = fx.make_work_order(w, "MM")
		fx.work_cycle(w, mwo, department="MM", employee="MM1")
		draft = fx.raw_employee_ir(w, "Issue", [(stale, mwo)], employee="MM2")

		with patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}):
			fx.submit_ir(draft, "Employee IR")

		self.assertEqual(self.docstatus("Employee IR", draft), 1)
		self.assertEqual(fx.operation(stale).status, "WIP")
		self.assertEqual(len(fx.open_operations(mwo)), 2)
		logs = self._warn_logs()
		self.assertTrue(logs)
		self.assertTrue(any(stale in row.error for row in logs))

	def test_enforce_mode_is_the_default(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		fx.insert_ir(fx.employee_ir(w, "Issue", [mwo]))

		with patch.dict(frappe.local.conf, {"current_operation_guard": None}):
			self.assertGuard(
				OutstandingDraftError,
				fx.insert_ir,
				fx.department_ir(w, "Issue", [mwo], current="MM", to="PP"),
			)
		self.assertEqual(self._warn_logs(), [])

	def test_warn_mode_takes_no_work_order_lock_and_runs_no_recheck(self):
		"""The emergency lever must relieve lock problems too (a busy work order, a deadlock):
		in warn mode no save or submit takes the Manufacturing Operation / Work Order block or
		the NOWAIT re-check -- the pre-guard locking. Every path below would fail on a lock."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *args, **kwargs):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *args, **kwargs)

		forbidden = AssertionError("the guard took a lock in warn mode")
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(
				current_operation_guard,
				"lock_manufacturing_operations",
				side_effect=forbidden,
			),
			patch.object(
				current_operation_guard, "lock_work_orders", side_effect=forbidden
			),
			patch.object(frappe.db, "sql", recording_sql),
		):
			transfer = fx.insert_ir(
				fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
			)
			issue = fx.issue(w, mwo, employee="MM1", submit=False)
			fx.submit_ir(issue)
			receive = fx.receive(w, mwo, employee="MM1")

		self.assertEqual(self.docstatus("Department IR", transfer.name), 0)
		self.assertEqual(self.docstatus("Employee IR", receive.name), 1)
		self.assertEqual(fx.operation(mop).status, "Finished")
		# no terminal re-check and no confirmation read either (MOPLog.validate's own operation
		# lock is pre-guard behaviour and stays)
		self.assertFalse([s for s in statements if "LOCK IN SHARE MODE" in s])


# ==============================================================================================
# decisions come from locking reads, taken in the canonical order
# ==============================================================================================


class TestLockingReads(_GuardHarness):
	def _record_sql(self):
		"""Patch ``frappe.db.sql``; returns the list every statement (whitespace-normalised) is
		appended to."""
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *args, **kwargs):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *args, **kwargs)

		patcher = patch.object(frappe.db, "sql", recording_sql)
		patcher.start()
		self.addCleanup(patcher.stop)
		return statements, patcher

	def test_the_terminal_draft_recheck_locks_through_the_work_order_index(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		index = frappe.db.get_column_index(
			EIR_OPERATION_TABLE, EIR_OPERATION_MWO_COLUMN
		)
		self.assertTrue(
			index, "Employee IR Operation.manufacturing_work_order is not indexed"
		)
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *args, **kwargs):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *args, **kwargs)

		with (
			patch.object(frappe.db, "sql", recording_sql),
			patch("frappe.log_error") as log_error,
		):
			fx.insert_ir(fx.employee_ir(w, "Issue", [mwo]))

		recheck = [s for s in statements if "LOCK IN SHARE MODE NOWAIT" in s]
		self.assertTrue(recheck, "the terminal re-check fell back to a plain read")
		self.assertIn(f"FORCE INDEX (`{index.Key_name}`)", recheck[0])
		log_error.assert_not_called()

	def test_an_employee_receive_decides_under_locks_taken_after_its_pre_locks(self):
		"""A Receive submit takes its Tree / Series / Bin pre-locks and then the block in
		``before_validate`` -- before Frappe rewrites the Receive's own rows and before the row
		loop writes anything: Series < MOP < MWO < own rows < first operation / MOP Log write.
		(Rows written before the block made another transaction's terminal NOWAIT re-check fail
		on them: race suite, TestFormerDefects.)"""
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		fx.issue(w, mwo, employee="MM1")
		receive = fx.insert_ir(fx.employee_ir(w, "Receive", [mwo], employee="MM1"))
		receive = fx.reload(receive)
		statements, patcher = self._record_sql()

		receive.submit()
		patcher.stop()
		own_rows = next(
			(
				i
				for i, s in enumerate(statements)
				if s.startswith("UPDATE `tabEmployee IR Operation`")
			),
			None,
		)
		self.assertIsNotNone(own_rows, "the submit never wrote its rows")
		self.assertLess(
			next(
				i
				for i, s in enumerate(statements)
				if "FOR UPDATE" in s and "FROM `tabManufacturing Work Order`" in s
			),
			own_rows,
		)

		def first(predicate, default=None):
			return next((i for i, s in enumerate(statements) if predicate(s)), default)

		def last(predicate):
			hits = [i for i, s in enumerate(statements) if predicate(s)]
			return hits[-1] if hits else -1

		def locks(table):
			return lambda s: f"FROM `{table}`" in s and "FOR UPDATE" in s

		def writes(s):
			return s.startswith(
				(
					"UPDATE `tabManufacturing Operation`",
					"INSERT INTO `tabManufacturing Operation`",
					"INSERT INTO `tabMOP Log`",
				)
			)

		series = last(locks("tabSeries"))
		bins = last(locks("tabBin"))
		mwo_lock = first(locks("tabManufacturing Work Order"))
		self.assertIsNotNone(mwo_lock, "the Receive never locked its work order")
		pre_locks = max(series, bins)
		mop_lock = next(
			(
				i
				for i, s in enumerate(statements)
				if i > pre_locks and locks("tabManufacturing Operation")(s)
			),
			None,
		)
		self.assertIsNotNone(mop_lock, "no operation lock after the pre-locks")
		self.assertGreater(series, -1, "the Receive took no series pre-lock")
		self.assertLess(pre_locks, mop_lock)
		self.assertLess(mop_lock, mwo_lock)
		self.assertLess(mwo_lock, first(writes, len(statements)))

	def test_a_rest_receive_insert_takes_its_pre_locks_and_block_before_naming(self):
		"""Finding: a REST insert-and-submit of a Receive used to name itself (locking the
		Employee IR naming-series row) BEFORE its MOP / MWO block, while every other new Employee
		IR takes the block first and the naming row second -- a two-party deadlock (1213) on
		the naming row. Now: Series pre-lock < MOP < MWO < naming row < the insert."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		fx.issue(w, mwo, employee="MM1")
		gross = frappe.db.get_value("Manufacturing Operation", mop, "gross_wt")
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *args, **kwargs):
			statements.append((" ".join(str(query).split()), repr(args)))
			return real_sql(query, *args, **kwargs)

		with self.production_eir_naming(), patch.object(
			frappe.db, "sql", recording_sql
		):
			receive = frappe.client.insert(
				{
					"doctype": "Employee IR",
					"type": "Receive",
					"docstatus": 1,
					"company": w.company,
					"manufacturer": w.manufacturer,
					"department": w.departments.MM,
					"operation": w.operations.MM,
					"employee": w.employees.MM1,
					"subcontracting": "No",
					"custom_transfer_type": "",
					"employee_ir_operations": [
						{
							"manufacturing_operation": mop,
							"manufacturing_work_order": mwo,
							"received_gross_wt": gross,
						}
					],
				}
			)
		self.assertEqual(receive["docstatus"], 1)

		def first(predicate):
			return next(
				(i for i, (s, a) in enumerate(statements) if predicate(s, a)), None
			)

		def series_lock(s):
			return "FOR UPDATE" in s and (
				"`tabSeries`" in s or "`tabDocument Naming Rule`" in s
			)

		naming = first(lambda s, a: series_lock(s) and "EMP-IR-" in a)
		stock_series = first(lambda s, a: series_lock(s) and "EMP-IR-" not in a)
		mop_lock = first(
			lambda s, a: "FOR UPDATE" in s and "FROM `tabManufacturing Operation`" in s
		)
		mwo_lock = first(
			lambda s, a: "FOR UPDATE" in s and "FROM `tabManufacturing Work Order`" in s
		)
		insert = first(lambda s, a: s.startswith("INSERT INTO `tabEmployee IR`"))
		for label, position in (
			("stock entry series pre-lock", stock_series),
			("operation lock", mop_lock),
			("work order lock", mwo_lock),
			("naming-series lock", naming),
			("insert", insert),
		):
			self.assertIsNotNone(position, f"no {label}")
		self.assertLess(stock_series, mop_lock)
		self.assertLess(mop_lock, mwo_lock)
		self.assertLess(mwo_lock, naming)
		self.assertLess(naming, insert)

	def test_operations_are_locked_before_work_orders(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		transfer = fx.insert_ir(
			fx.department_ir(w, "Issue", [mwo], current="MM", to="PP")
		)
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *args, **kwargs):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", recording_sql):
			fx.submit_ir(transfer)

		locks = [s for s in statements if "FOR UPDATE" in s]
		mop_lock = next(
			i for i, s in enumerate(locks) if "`tabManufacturing Operation`" in s
		)
		mwo_lock = next(
			i for i, s in enumerate(locks) if "`tabManufacturing Work Order`" in s
		)
		self.assertLess(mop_lock, mwo_lock)


# ==============================================================================================
# frozen stock movement (EOD sync / reconciliation window): refused before any lock
# ==============================================================================================


class TestStockFreezeRefusesBeforeLocks(_GuardHarness):
	"""F2: while the EOD sync runs (or a reconciliation window is open) a save or submit is
	refused before the attempt takes ANY lock. The before_save / before_submit doc_events refuse
	it as well, but only after check_if_latest / before_insert took the Receive pre-locks and the
	block: a queued submit used to wait on -- and hold -- rows the EOD run was writing, and could
	end as "busy, retry" instead of the EOD message."""

	def _frozen(self, fn, *args, eod=True, window=False, message=EOD_MESSAGE):
		"""Run ``fn`` frozen; assert ``message`` and that no locking statement ran before it."""
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *a, **k):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *a, **k)

		frappe.db.savepoint("cog_frozen")
		with (
			patch(f"{EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=eod),
			patch(f"{RECON_WINDOW_MODULE}._enabled", return_value=window),
			patch(
				f"{RECON_WINDOW_MODULE}._department_window_status", return_value="open"
			),
			patch.object(frappe.db, "sql", recording_sql),
		):
			with self.assertRaisesRegex(frappe.ValidationError, message):
				fn(*args)
		frappe.db.rollback(save_point="cog_frozen")
		self.assertEqual(
			[s for s in statements if any(t in s for t in LOCKING_SQL)],
			[],
			"a lock was taken before the refusal",
		)

	def test_submits_of_saved_drafts_are_refused_before_any_lock(self):
		w = self.world
		issue_mwo, _ = fx.make_work_order(w, "MM")
		receive_mwo, _ = fx.make_work_order(w, "MM")
		transfer_mwo, _ = fx.make_work_order(w, "MM")
		in_transit_mwo, _ = fx.make_work_order(w, "MM")
		fx.issue(w, receive_mwo, employee="MM1")
		dir_issue, _ = fx.transfer(
			w, in_transit_mwo, current="MM", to="PP", receive=False
		)
		drafts = {
			"Employee Issue": fx.issue(w, issue_mwo, employee="MM1", submit=False),
			"Employee Receive": fx.receive(
				w, receive_mwo, employee="MM1", submit=False
			),
			"Department Issue": fx.insert_ir(
				fx.department_ir(w, "Issue", [transfer_mwo], current="MM", to="PP")
			),
			"Department Receive": fx.insert_ir(
				fx.department_ir(
					w,
					"Receive",
					[in_transit_mwo],
					current="PP",
					previous="MM",
					receive_against=dir_issue.name,
				)
			),
		}
		for label, draft in drafts.items():
			with self.subTest(label):
				self._frozen(fx.submit_ir, draft)
				self.assertEqual(self.docstatus(draft.doctype, draft.name), 0)

	def test_an_edited_draft_save_is_refused_before_any_lock(self):
		w = self.world
		first, _ = fx.make_work_order(w, "MM")
		second, second_mop = fx.make_work_order(w, "MM")
		doc = fx.reload(fx.issue(w, first, employee="MM1", submit=False))
		doc.employee_ir_operations[0].manufacturing_operation = second_mop
		doc.employee_ir_operations[0].manufacturing_work_order = second
		self._frozen(doc.save)

	def test_new_documents_are_refused_before_their_pre_locks_and_block(self):
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		other, _ = fx.make_work_order(w, "MM")
		fx.issue(w, mwo, employee="MM1")
		gross = frappe.db.get_value("Manufacturing Operation", mop, "gross_wt")
		rest_receive = {
			"doctype": "Employee IR",
			"type": "Receive",
			"docstatus": 1,
			"company": w.company,
			"manufacturer": w.manufacturer,
			"department": w.departments.MM,
			"operation": w.operations.MM,
			"employee": w.employees.MM1,
			"subcontracting": "No",
			"custom_transfer_type": "",
			"employee_ir_operations": [
				{
					"manufacturing_operation": mop,
					"manufacturing_work_order": mwo,
					"received_gross_wt": gross,
				}
			],
		}
		for label, fn in (
			(
				"REST insert-and-submit of a Receive",
				lambda: frappe.client.insert(rest_receive),
			),
			(
				"new Employee IR draft",
				lambda: fx.insert_ir(fx.employee_ir(w, "Issue", [other])),
			),
			(
				"new Department IR draft",
				lambda: fx.insert_ir(
					fx.department_ir(w, "Issue", [other], current="MM", to="PP")
				),
			),
		):
			with self.subTest(label):
				self._frozen(fn)

	def test_an_open_reconciliation_window_is_refused_before_any_lock_too(self):
		w = self.world
		mwo, _ = fx.make_work_order(w, "MM")
		fx.issue(w, mwo, employee="MM1")
		draft = fx.receive(w, mwo, employee="MM1", submit=False)
		self._frozen(
			fx.submit_ir,
			draft,
			eod=False,
			window=True,
			message="Stock transactions are temporarily blocked",
		)

	def test_a_draft_can_still_be_discarded(self):
		"""A blocking draft must stay removable during the window."""
		w = self.world
		mwo, _ = fx.make_work_order(w, "MM")
		draft = fx.issue(w, mwo, employee="MM1", submit=False)
		with patch(f"{EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=True):
			fx.reload(draft).discard()
		self.assertEqual(self.docstatus("Employee IR", draft.name), 2)

	def test_without_a_freeze_the_same_submit_locks_as_before(self):
		"""The recorder sits on the real path: unfrozen, this submit locks its operation."""
		w = self.world
		mwo, mop = fx.make_work_order(w, "MM")
		fx.issue(w, mwo, employee="MM1")
		draft = fx.receive(w, mwo, employee="MM1", submit=False)
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *a, **k):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *a, **k)

		with patch.object(frappe.db, "sql", recording_sql):
			fx.submit_ir(draft)
		self.assertTrue(
			[
				s
				for s in statements
				if "FOR UPDATE" in s and "FROM `tabManufacturing Operation`" in s
			]
		)


# ==============================================================================================
# what a cancel writes after its lock block: by primary key only
# ==============================================================================================


class TestCancelWritesLockOnlyTheirRows(_GuardHarness):
	"""A cancel writes MOP Log rows, cancelled Department IR / Stock Entry rows and casting-tree
	stamps after its MOP / MWO block. None of those filters is indexed on production: a filtered
	``UPDATE`` scans the whole table and, under REPEATABLE READ, keeps every row of it locked until
	the cancel commits -- every MOP Log writer of the site waited, and one already holding a row
	there closed a deadlock cycle with the block (lock_order RULE E). They go by primary key."""

	TABLES = (
		"tabMOP Log",
		"tabDepartment IR Operation",
		"tabStock Entry Detail",
		"tabEmployee IR",
		"tabEmployee IR Operation",
	)

	def _updates(self, fn):
		"""``fn()`` with every statement recorded; returns the UPDATEs of ``TABLES``."""
		statements = []
		real_sql = frappe.db.sql

		def recording_sql(query, *a, **k):
			statements.append(" ".join(str(query).split()))
			return real_sql(query, *a, **k)

		with patch.object(frappe.db, "sql", recording_sql):
			fn()
		updates = []
		for statement in statements:
			table = re.match(r"UPDATE `(tab[^`]+)`", statement)
			if table and table.group(1) in self.TABLES:
				updates.append((table.group(1), statement))
		return updates

	def assertByPrimaryKey(self, updates):
		for _table, statement in updates:
			where = statement.split(" WHERE ", 1)[-1]
			self.assertRegex(
				where,
				r"(^|[\s.(])`?name`?\s*(=|IN\b)",
				f"filtered UPDATE (locks the whole table): {statement}",
			)

	def test_an_employee_issue_cancel_flips_its_mop_logs_by_primary_key(self):
		w = self.world
		mwo, _ = fx.make_work_order(w, "MM")
		issue = fx.issue(w, mwo, employee="MM1")
		self.assertTrue(fx.mop_logs(voucher_no=issue.name))
		updates = self._updates(fx.reload(issue).cancel)
		self.assertIn("tabMOP Log", [t for t, _s in updates])
		self.assertByPrimaryKey(updates)
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=issue.name)}, {1}
		)

	def test_an_employee_receive_cancel_flips_its_mop_logs_by_primary_key(self):
		w = self.world
		mwo, _ = fx.make_work_order(w, "MM")
		_issue, receive = fx.work_cycle(w, mwo, department="MM", employee="MM1")
		with patch(f"{EMPLOYEE_IR_MODULE}.cancel_loss_stock_entries"):
			updates = self._updates(fx.reload(receive).cancel)
		self.assertIn("tabMOP Log", [t for t, _s in updates])
		self.assertByPrimaryKey(updates)
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=receive.name)}, {1}
		)

	def test_a_department_issue_cancel_writes_by_primary_key(self):
		"""kggk_uat keeps the transfer's operation (marked Revert) instead of deleting it, so no
		cancelled row naming it has to be cleared: the discarded Receive draft's row keeps it."""
		w = self.world
		mwo, _ = fx.make_work_order(w, "MM")
		dir_issue, _ = fx.transfer(w, mwo, current="MM", to="PP", receive=False)
		minted = fx.pointer(mwo)
		receive = fx.insert_ir(
			fx.department_ir(
				w,
				"Receive",
				[mwo],
				current="PP",
				previous="MM",
				receive_against=dir_issue.name,
			)
		)
		fx.reload(receive).discard()
		updates = self._updates(fx.reload(dir_issue).cancel)
		self.assertIn("tabMOP Log", [t for t, _s in updates])
		self.assertByPrimaryKey(updates)
		self.assertEqual(
			frappe.db.get_value(
				"Department IR Operation",
				{"parent": receive.name},
				"manufacturing_operation",
			),
			minted,
		)
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Operation", minted, "department_ir_status"
			),
			"Revert",
		)
		self.assertNotEqual(fx.pointer(mwo), minted)

	def test_a_department_receive_cancel_flips_its_mop_logs_by_primary_key(self):
		w = self.world
		mwo, _ = fx.make_work_order(w, "MM")
		_dir_issue, dir_receive = fx.transfer(w, mwo, current="MM", to="PP")
		updates = self._updates(fx.reload(dir_receive).cancel)
		self.assertByPrimaryKey(updates)
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=dir_receive.name)} or {1},
			{1},
		)
