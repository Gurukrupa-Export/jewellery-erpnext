# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The current-operation audit and the reviewed stale-Issue repair, on seedless fixture worlds.

``jewellery_erpnext.mop_lineage_audit`` (``audit_current_operation_conflicts`` and the family-C
manifest builders) and ``patches/repair_current_operation_conflicts.py`` (plan / rehearse /
apply), exercised against the 2026-10-05 incident rebuilt through the REAL Employee IR /
Department IR controllers (``tests/_current_operation_fixtures.py``):

    draft D: Employee Issue of the current operations m1 / m2 of two work orders
    while D is hidden (the 10-01 snapshot race): Issue + Receive of m1 / m2 by the same
    employee (successors minted), then a transfer to Pre Polish
    D is submitted on the code production ran (no current-operation guard): m1 / m2 are
    reopened to WIP with an open time log each and unsynced clone MOP Logs.

ISOLATION
---------
Two harnesses. ``_RolledBack`` builds a world per test and rolls the whole transaction back in
``tearDown``; commits are stubbed and fail the test, RQ enqueues are recorded and fail it too.
Audit, classification, manifest, plan and proof tests run there -- the audit then runs in its
"select only inside the caller's transaction" mode, because the fixtures are pending writes.

The repair's rehearse / apply modes start every case with a rollback and refuse a caller with
pending writes, so ``_Committed`` COMMITS its prefixed worlds, lets apply commit for real and,
in ``tearDownClass``, purges everything carrying the run's prefix (``fixtures.purge`` plus the
Error Log rows whose message names it) and fails if any table still holds a row naming it.
The audit's and plan's own READ ONLY transaction is proven there too (no pending writes).

The EOD lock is patched open in the write tests: other suites on this disposable site toggle
it (``test_mop_settings``), and its refusal has its own test.
"""

import math
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.database.utils import get_query_type
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jewellery_erpnext import mop_lineage_audit as audit
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	current_operation_guard as guard,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx
from jewellery_erpnext.patches import repair_current_operation_conflicts as repair

BASE_PREFIX = "TCOR"
# Never this site: rehearse is refused on a manifest's production site.
PRODUCTION_SITE = "kggk-prod.example"
GUARD_ENTRY_POINTS = (
	"on_before_insert",
	"on_before_validate",
	"check_after_receive_prelocks",
	"final_draft_check",
	"preflight",
	"guard_cancel",
)
READ_QUERY_TYPES = frozenset(("select", "set", "start", "rollback", "desc", "show"))


class _RecordingQueue:
	def __init__(self, sink):
		self.sink = sink

	def enqueue_call(self, *args, **kwargs):
		self.sink.append((kwargs.get("kwargs") or {}).get("method"))
		return SimpleNamespace(id=kwargs.get("job_id"))


# ----------------------------------------------------------------------------------------------
# building the damaged states
# ----------------------------------------------------------------------------------------------


@contextmanager
def old_code():
	"""The code production ran before the fix: no current-operation guard on any path."""
	with ExitStack() as stack:
		for name in GUARD_ENTRY_POINTS:
			stack.enter_context(patch.object(guard, name, lambda *args, **kwargs: None))
		yield


def stale_issue_incident(world, employee="MM1"):
	"""EMP-IR-...-43405 in miniature (see the module docstring). Returns its names."""
	(mwo1, m1), (mwo2, m2) = (
		fx.make_work_order(world, "MM"),
		fx.make_work_order(world, "MM"),
	)
	mwos = [mwo1, mwo2]
	draft = fx.insert_ir(fx.employee_ir(world, "Issue", mwos, employee=employee))
	with fx.hidden("Employee IR", draft.name):
		legit_issue, legit_receive = fx.work_cycle(
			world, mwos, department="MM", employee=employee
		)
		fx.transfer(world, mwos, current="MM", to="PP")
	with old_code():
		fx.submit_ir(draft)
	return frappe._dict(
		mwos=mwos,
		stale_ops=[m1, m2],
		stale=draft.name,
		legit_issue=legit_issue.name,
		legit_receive=legit_receive.name,
		pointers={mwo: fx.pointer(mwo) for mwo in mwos},
	)


def double_issue(world, stale_employee="MM1"):
	"""The 21123 / 31995 / 38474 shape: Issue L of m, then a second Issue D of the SAME operation
	while it is still with the employee (old code), then the Receive of L mints the successor.

	Returns ``(mwo, m, L, D)``; the Receive is left to the caller.
	"""
	mwo, m = fx.make_work_order(world, "MM")
	draft = fx.insert_ir(fx.employee_ir(world, "Issue", [mwo], employee=stale_employee))
	with fx.hidden("Employee IR", draft.name):
		legit = fx.issue(world, mwo, department="MM", employee="MM1")
	with old_code():
		fx.submit_ir(draft)
	return mwo, m, legit.name, draft.name


def receive_of(world, mwo, issue_name, employee="MM1"):
	"""Employee Receive of the work order's current operation, against ``issue_name``."""
	return fx.submit_ir(
		fx.insert_ir(
			fx.employee_ir(
				world,
				"Receive",
				[mwo],
				department="MM",
				employee=employee,
				emp_ir_id=issue_name,
			)
		)
	)


def raw_department_ir(
	world, typ, rows, *, docstatus, current, to=None, receive_against=None
):
	"""A Department IR written straight to the database (legacy states only)."""
	name = fx.next_name(world, "DIR")
	frappe.get_doc(
		{
			"doctype": "Department IR",
			"name": name,
			"docstatus": docstatus,
			"type": typ,
			"company": world.company,
			"manufacturer": world.manufacturer,
			"current_department": world.departments[current],
			"next_department": world.departments[to] if to else None,
			"receive_against": receive_against,
		}
	).db_insert()
	world.created.append(("Department IR", name))
	for idx, (mop, mwo) in enumerate(rows, 1):
		frappe.get_doc(
			{
				"doctype": "Department IR Operation",
				"name": f"{name}-ROW-{idx}",
				"parent": name,
				"parenttype": "Department IR",
				"parentfield": "department_ir_operation",
				"idx": idx,
				"docstatus": docstatus,
				"manufacturing_operation": mop,
				"manufacturing_work_order": mwo,
			}
		).db_insert()
	return name


def raw_stock_entry(
	world, rows, *, employee_ir=None, docstatus=1, creation=None, **head
):
	"""A Stock Entry (header + rows naming operations) written straight to the database."""
	name = fx.next_name(world, "SE")
	creation = creation or now_datetime()
	values = {
		"doctype": "Stock Entry",
		"name": name,
		"docstatus": docstatus,
		"company": world.company,
		"stock_entry_type": head.pop(
			"stock_entry_type", "Material Transfer (WORK ORDER)"
		),
		"purpose": head.pop("purpose", "Material Transfer"),
		"employee_ir": employee_ir,
		"creation": creation,
		"modified": creation,
		"owner": "Administrator",
		"modified_by": "Administrator",
		**head,
	}
	frappe.get_doc(values).db_insert()
	for idx, (mop, mwo) in enumerate(rows, 1):
		frappe.get_doc(
			{
				"doctype": "Stock Entry Detail",
				"name": f"{name}-ROW-{idx}",
				"parent": name,
				"parenttype": "Stock Entry",
				"parentfield": "items",
				"idx": idx,
				"docstatus": docstatus,
				"item_code": world.item.metal,
				"qty": 1,
				"t_warehouse": world.department_warehouse.MM,
				"manufacturing_operation": mop,
				"custom_manufacturing_work_order": mwo,
				"creation": creation,
				"modified": creation,
			}
		).db_insert()
	return name


def raw_eod_run(world, items, *, started_on, completed_on=None):
	"""A MOP EOD Sync Log with ``items`` = ``[{mwo, mop, status, sync_stage, is_synced, ...}]``."""
	name = fx.next_name(world, "EOD")
	frappe.get_doc(
		{
			"doctype": "MOP EOD Sync Log",
			"name": name,
			"posting_date": get_datetime(started_on).date(),
			"trigger_type": "Scheduler",
			"status": "Completed",
			"started_on": started_on,
			"completed_on": completed_on or add_to_date(started_on, minutes=5),
			"creation": started_on,
			"modified": completed_on or add_to_date(started_on, minutes=5),
		}
	).db_insert()
	for idx, item in enumerate(items, 1):
		frappe.get_doc(
			{
				"doctype": "MOP EOD Sync Log Item",
				"name": f"{name}-ROW-{idx}",
				"parent": name,
				"parenttype": "MOP EOD Sync Log",
				"parentfield": "items",
				"idx": idx,
				"manufacturing_work_order": item["mwo"],
				"manufacturing_operation": item.get("mop"),
				"item_code": world.item.metal,
				"qty": 1,
				"status": item.get("status", "Synced"),
				"sync_stage": item.get("sync_stage", "Completed"),
				"is_synced": item.get("is_synced", 1),
				"stock_entry": item.get("stock_entry"),
			}
		).db_insert()
	return name


def stale_logs(stale, mop):
	return frappe.get_all(
		"MOP Log",
		filters={
			"voucher_type": "Employee IR",
			"voucher_no": stale,
			"manufacturing_operation": mop,
		},
		fields=["name", "creation"],
		order_by="creation asc",
	)


def mark_synced(names):
	"""What an EOD run's window-bounded mark does: a raw UPDATE, ``modified`` untouched."""
	frappe.db.sql(
		"UPDATE `tabMOP Log` SET is_synced = 1 WHERE name IN %(names)s",
		{"names": names},
	)


def manifest_for(stale, *, reviewed=True, production_site=PRODUCTION_SITE):
	data = audit.build_stale_issue_manifest(stale, production_site=production_site)
	if reviewed:
		data["reviewed_by"] = "Administrator"
		data["reviewed_on"] = audit._co_norm(add_to_date(now_datetime(), seconds=1))
	return data


@contextmanager
def recorded_statements():
	"""Every SQL statement run inside the block (``frappe.db.sql`` passes through)."""
	statements = []
	original = frappe.db.sql

	def recording(query, *args, **kwargs):
		statements.append(str(query))
		return original(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", recording):
		yield statements


def write_statements(statements):
	out = []
	for query in statements:
		text = query.strip()
		if text.upper().startswith("SET STATEMENT"):
			text = text.split(" FOR ", 1)[-1]
		if get_query_type(text) not in READ_QUERY_TYPES:
			out.append(text[:120])
	return out


# ----------------------------------------------------------------------------------------------
# harnesses
# ----------------------------------------------------------------------------------------------


class _RolledBack(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.run_prefix = fx.new_prefix(BASE_PREFIX)
		cls.world_count = 0

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		leaked = fx.purge(cls.run_prefix)
		if leaked:
			frappe.db.commit()
		super().tearDownClass()
		if leaked:
			raise AssertionError(
				f"{cls.__name__} committed rows (purged now): {leaked}"
			)

	def setUp(self):
		super().setUp()
		type(self).world_count += 1
		self.commits, self.enqueued = [], []
		for patcher in (
			patch.object(frappe.db, "commit", lambda *a, **k: self.commits.append(a)),
			patch(
				"frappe.utils.background_jobs.get_queue",
				lambda *a, **k: _RecordingQueue(self.enqueued),
			),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.world = fx.make_world(f"{self.run_prefix}-{self.world_count:02d}")
		frappe.local.message_log = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.value_cache.clear()
		fx.clear_caches(self.world)
		frappe.flags.pop(repair.REVIEWED_REPAIR_FLAG, None)
		frappe.local.message_log = []
		super().tearDown()
		self.assertEqual(self.commits, [], "a cascade committed mid-transaction")
		self.assertEqual(self.enqueued, [], "a cascade enqueued a job")


def _leftovers(prefix):
	"""``{check: rows}`` still naming ``prefix`` after the purge (empty = clean)."""
	like, anywhere = f"{prefix}%", f"%{prefix}%"
	checks = {
		"Manufacturing Work Order": (
			"tabManufacturing Work Order",
			"name LIKE %(like)s",
		),
		"Manufacturing Operation": (
			"tabManufacturing Operation",
			"name LIKE %(like)s OR manufacturing_work_order LIKE %(like)s",
		),
		"MOP Log": (
			"tabMOP Log",
			"manufacturing_work_order LIKE %(like)s OR voucher_no LIKE %(anywhere)s",
		),
		"Employee IR": ("tabEmployee IR", "name LIKE %(anywhere)s"),
		"Employee IR Operation": (
			"tabEmployee IR Operation",
			"parent LIKE %(anywhere)s",
		),
		"Department IR": ("tabDepartment IR", "name LIKE %(anywhere)s"),
		"Comment": (
			"tabComment",
			"reference_name LIKE %(anywhere)s OR content LIKE %(anywhere)s",
		),
		"Version": (
			"tabVersion",
			"docname LIKE %(anywhere)s OR data LIKE %(anywhere)s",
		),
		"Error Log": (
			"tabError Log",
			"reference_name LIKE %(anywhere)s OR method LIKE %(anywhere)s"
			" OR error LIKE %(anywhere)s",
		),
		"Company": ("tabCompany", "name LIKE %(anywhere)s"),
		"Warehouse": ("tabWarehouse", "name LIKE %(anywhere)s"),
		"Employee": ("tabEmployee", "name LIKE %(anywhere)s"),
		"Item": ("tabItem", "name LIKE %(anywhere)s"),
	}
	out = {}
	for check, (table, condition) in checks.items():
		count = frappe.db.sql(
			f"SELECT COUNT(*) FROM `{table}` WHERE {condition}",
			{"like": like, "anywhere": anywhere},
		)[0][0]
		if count:
			out[check] = count
	return out


class _Committed(IntegrationTestCase):
	"""Committed, prefixed worlds; ``tearDownClass`` purges them and fails on anything left."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.run_prefix = fx.new_prefix(BASE_PREFIX)
		cls.world_count = 0
		cls.worlds = []

	@classmethod
	def tearDownClass(cls):
		try:
			frappe.db.rollback()
		except Exception:
			frappe.db.connect()  # e.g. the server restarted mid-test: purge on a new connection
		fx.purge(cls.run_prefix)
		# The MSL refresh logs its own failures with no reference (a MyISAM table).
		frappe.db.sql(
			"DELETE FROM `tabError Log` WHERE error LIKE %(anywhere)s",
			{"anywhere": f"%{cls.run_prefix}%"},
		)
		frappe.db.commit()
		for world in cls.worlds:
			fx.clear_caches(world)
		left = _leftovers(cls.run_prefix)
		super().tearDownClass()
		if left:
			raise AssertionError(f"{cls.__name__} left rows behind: {left}")

	def setUp(self):
		super().setUp()
		type(self).world_count += 1
		self.world = fx.make_world(f"{self.run_prefix}-{self.world_count:02d}")
		type(self).worlds.append(self.world)
		eod_lock = patch.object(repair, "_eod_locked", return_value=False)
		eod_lock.start()
		self.addCleanup(eod_lock.stop)
		frappe.local.message_log = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.value_cache.clear()
		frappe.flags.pop(repair.REVIEWED_REPAIR_FLAG, None)
		frappe.local.message_log = []
		super().tearDown()

	def committed_incident(self):
		incident = stale_issue_incident(self.world)
		frappe.db.commit()
		return incident

	def assertUnchanged(self, incident, before):
		frappe.db.rollback()
		self.assertEqual(fx.snapshot(*incident.mwos), before)
		self.assertEqual(
			frappe.db.get_value("Employee IR", incident.stale, "docstatus"), 1
		)
		self.assertFalse(
			frappe.get_all(
				"Comment",
				filters={"reference_name": ["in", [incident.stale, *incident.mwos]]},
				pluck="name",
			)
		)


# ==============================================================================================
# the audit
# ==============================================================================================


class TestAuditIsReadOnly(_RolledBack):
	def test_the_audit_never_opens_its_own_transaction_inside_a_repair(self):
		# With nothing written yet, the wrapper would START TRANSACTION READ ONLY -- which
		# commits the caller's transaction and releases the repair's row locks.
		guard = audit._ReadOnlyAudit()
		with ExitStack() as stack:
			stack.enter_context(patch.object(frappe.db, "transaction_writes", 0))
			for callbacks in (frappe.db.before_commit, frappe.db.after_commit):
				stack.enter_context(patch.object(callbacks, "_functions", []))
			begin = stack.enter_context(patch.object(frappe.db, "begin"))
			stack.enter_context(
				patch.dict(frappe.flags, {repair.IN_PROGRESS_FLAG: True})
			)
			with guard:
				pass
		begin.assert_not_called()
		self.assertEqual(
			guard.summary()["mode"], "select_only_inside_caller_transaction"
		)

	def test_a_write_inside_the_audit_transaction_is_refused_and_reported(self):
		frappe.db.rollback()  # no pending writes: the audit opens its own READ ONLY transaction
		with self.assertRaisesRegex(AssertionError, "must be read-only"):
			with audit._ReadOnlyAudit() as read_only:
				self.assertTrue(read_only.own_transaction)
				with self.assertRaises(frappe.InReadOnlyMode):
					frappe.db.sql(
						"UPDATE `tabManufacturing Work Order` SET modified = modified "
						"WHERE name = %s",
						f"{self.run_prefix}-none",
					)

	def test_a_write_inside_the_callers_transaction_is_reported(self):
		mwo, _mop = fx.make_work_order(self.world, "MM")
		with self.assertRaisesRegex(AssertionError, "must be read-only"):
			with audit._ReadOnlyAudit() as read_only:
				self.assertFalse(
					read_only.own_transaction
				)  # the fixtures are pending writes
				frappe.db.set_value(
					"Manufacturing Work Order",
					mwo,
					"status",
					"Completed",
					update_modified=False,
				)

	def test_only_single_non_locking_selects_are_accepted(self):
		for query in (
			"UPDATE `tabMOP Log` SET is_synced = 1 WHERE name = 'x'",
			"SELECT name FROM `tabMOP Log` WHERE name = 'x' FOR UPDATE",
			"SELECT name FROM `tabMOP Log` WHERE name = 'x' LOCK IN SHARE MODE",
			"SELECT 1; DELETE FROM `tabMOP Log`",
		):
			with self.subTest(query=query), self.assertRaises(frappe.ValidationError):
				audit._co_select(query)


class TestAuditClassification(_RolledBack):
	def _family_a(self):
		"""A superseded operation put back In-Transit by a cancelled Department Receive."""
		w = self.world
		mwo, reopened = fx.make_work_order(w, "PP")
		frappe.db.set_value(
			"Manufacturing Operation", reopened, "department_ir_status", "In-Transit"
		)
		raw_department_ir(
			w,
			"Receive",
			[(reopened, mwo)],
			docstatus=2,
			current="PP",
			receive_against=None,
		)
		onward = raw_department_ir(
			w, "Issue", [(reopened, mwo)], docstatus=1, current="PP", to="FP"
		)
		successor = fx.make_operation(
			w,
			mwo,
			"FP",
			previous_mop=reopened,
			department_issue_id=onward,
			department_ir_status="In-Transit",
		)
		fx.set_pointer(mwo, successor)
		return mwo, reopened

	def _family_b(self):
		"""Twin operations minted by one Department Issue carrying the work order twice."""
		w = self.world
		mwo, source = fx.make_work_order(w, "MM")
		frappe.db.set_value("Manufacturing Operation", source, "status", "Finished")
		twice = raw_department_ir(
			w,
			"Issue",
			[(source, mwo), (source, mwo)],
			docstatus=1,
			current="MM",
			to="PP",
		)
		twins = [
			fx.make_operation(
				w,
				mwo,
				"PP",
				previous_mop=source,
				department_issue_id=twice,
				department_ir_status="In-Transit",
			)
			for _ in range(2)
		]
		fx.set_pointer(mwo, twins[1])
		return mwo, twins[0]

	def _unknown(self):
		w = self.world
		mwo, old = fx.make_work_order(w, "MM")
		fx.set_pointer(mwo, fx.make_operation(w, mwo, "MM", previous_mop=old))
		return mwo, old

	def test_a_transfer_closed_before_it_was_received_is_not_terminal(self):
		w = self.world
		mwo, sent = fx.make_work_order(w, "PP")
		onward = raw_department_ir(
			w, "Issue", [(sent, mwo)], docstatus=1, current="PP", to="FP"
		)
		in_transit = fx.make_operation(
			w,
			mwo,
			"FP",
			previous_mop=sent,
			department_issue_id=onward,
			department_ir_status="In-Transit",
		)
		frappe.db.set_value("Manufacturing Operation", sent, "status", "Finished")
		fx.set_pointer(mwo, in_transit)
		frappe.db.set_value("Manufacturing Operation", in_transit, "status", "Finished")

		section = audit.audit_current_operation_conflicts(mwos=[mwo])[
			"pointer_anomalies"
		]

		self.assertEqual(section["counts"]["closed_pointer_in_transit"], 1)
		self.assertEqual(section["counts"]["terminal"], 0)
		(row,) = section["rows"]
		self.assertEqual(row["anomaly"], "closed_pointer_in_transit")
		self.assertEqual(row["pointer"], in_transit)
		self.assertEqual(row["pointer_department_ir_status"], "In-Transit")

	def test_a_received_operation_closed_on_a_not_started_work_order_is_not_terminal(
		self,
	):
		w = self.world
		mwo, pointer = fx.make_work_order(w, "PP")
		frappe.db.set_value(
			"Manufacturing Operation",
			pointer,
			{"status": "Finished", "department_ir_status": "Received"},
		)
		frappe.db.set_value("Manufacturing Work Order", mwo, "status", "Not Started")

		section = audit.audit_current_operation_conflicts(mwos=[mwo])[
			"pointer_anomalies"
		]

		self.assertEqual(section["counts"]["closed_pointer_not_started_work_order"], 1)
		self.assertEqual(section["counts"]["terminal"], 0)

		frappe.db.set_value("Manufacturing Work Order", mwo, "status", "Completed")
		section = audit.audit_current_operation_conflicts(mwos=[mwo])[
			"pointer_anomalies"
		]
		self.assertEqual(section["counts"]["closed_pointer_not_started_work_order"], 0)
		self.assertEqual(section["counts"]["terminal"], 1)

	def test_families_are_assigned_from_evidence(self):
		incident = stale_issue_incident(self.world)
		mwo_a, op_a = self._family_a()
		mwo_b, op_b = self._family_b()
		mwo_u, op_u = self._unknown()
		scope = [*incident.mwos, mwo_a, mwo_b, mwo_u]
		writes_before = frappe.db.transaction_writes

		report = audit.audit_current_operation_conflicts(mwos=scope)

		self.assertEqual(frappe.db.transaction_writes, writes_before)
		self.assertEqual(report["read_only"]["write_statements"], 0)
		families = {
			r["manufacturing_operation"]: r["family"]
			for r in report["open_non_pointer"]["rows"]
		}
		self.assertEqual(
			families,
			{
				**{m: "C" for m in incident.stale_ops},
				op_a: "A",
				op_b: "B",
				op_u: "unknown",
			},
		)
		self.assertEqual(
			report["open_non_pointer"]["by_family"],
			{"C": 2, "A": 1, "B": 1, "unknown": 1},
		)
		row_c = next(
			r
			for r in report["open_non_pointer"]["rows"]
			if r["manufacturing_operation"] == incident.stale_ops[0]
		)
		self.assertEqual(
			[s["employee_ir"] for s in row_c["evidence"]["C"]["stale_issues"]],
			[incident.stale],
		)
		self.assertEqual(row_c["status"], "WIP")
		self.assertEqual(row_c["pointer"], incident.pointers[incident.mwos[0]])

	def test_the_43405_shaped_case_is_reported_in_every_section(self):
		incident = stale_issue_incident(self.world)

		report = audit.audit_current_operation_conflicts(mwos=incident.mwos)

		cases = report["stale_issue_cases"]["rows"]
		self.assertEqual([c["employee_ir"] for c in cases], [incident.stale])
		self.assertEqual(cases[0]["reopened_rows"], 2)
		self.assertTrue(cases[0]["whole_document_stale"])
		self.assertEqual(cases[0]["open_operations_left"], 2)
		self.assertEqual(
			{r["legitimate_issue"] for r in cases[0]["rows"]}, {incident.legit_issue}
		)

		pairs = report["duplicate_submitted_issues"]["rows"]
		self.assertEqual(
			[(p["late_document"], p["first_submitted"]) for p in pairs],
			[(incident.stale, incident.legit_issue)],
		)
		self.assertTrue(pairs[0]["inverted"])  # the stale draft was created first
		self.assertEqual({o["label"] for o in pairs[0]["operations"]}, {"REOPENED"})

		time_logs = report["open_time_log_fingerprint"]["rows"]
		self.assertEqual(
			sorted(t["manufacturing_operation"] for t in time_logs),
			sorted(incident.stale_ops),
		)
		self.assertEqual({t["opened_by_issue"] for t in time_logs}, {incident.stale})

		held = report["eod_ambiguity"]
		self.assertEqual(
			sorted(w["manufacturing_work_order"] for w in held["work_orders"]),
			sorted(incident.mwos),
		)
		# Every unsynced log on a stale operation is held -- the stale Issue's clones and the
		# earlier legitimate history alike (no EOD ran in the fixture world).
		unsynced = [
			log.name
			for log in fx.mop_logs(manufacturing_work_order=["in", incident.mwos])
			if log.manufacturing_operation in incident.stale_ops
			and not log.is_synced
			and not log.is_cancelled
		]
		self.assertEqual(sorted(r["mop_log"] for r in held["rows"]), sorted(unsynced))
		self.assertTrue(
			{r.name for r in fx.mop_logs(voucher_no=incident.stale)} <= set(unsynced)
		)
		self.assertEqual(report["pointer_anomalies"]["counts"]["normal"], 2)
		self.assertEqual(report["summary"]["open_non_pointer_by_family"], {"C": 2})


# ==============================================================================================
# the manifest
# ==============================================================================================


class TestStaleIssueManifest(_RolledBack):
	def test_a_manifest_is_built_from_evidence_with_reviewed_targets(self):
		w = self.world
		incident = stale_issue_incident(w)

		data = manifest_for(incident.stale, reviewed=False)

		self.assertEqual(data["kind"], audit.CO_MANIFEST_KIND)
		self.assertEqual(data["site"], frappe.local.site)
		self.assertEqual(data["production_site"], PRODUCTION_SITE)
		self.assertIsNone(data["reviewed_by"])
		case = data["cases"][0]
		self.assertEqual(case["status"], "ready", case["blockers"])
		self.assertEqual(case["blockers"], [])
		self.assertEqual(case["case_sha256"], audit.co_case_sha256(case))
		self.assertEqual({r["kind"] for r in case["rows"]}, {"reopened"})
		self.assertEqual(
			{r["minting_receive"] for r in case["rows"]}, {incident.legit_receive}
		)

		actions = case["actions"]
		self.assertEqual(actions["cancel_employee_ir"], incident.stale)
		self.assertEqual(
			sorted(actions["mop_logs_to_cancel"]),
			sorted(r.name for r in fx.mop_logs(voucher_no=incident.stale)),
		)
		for mop in incident.stale_ops:
			with self.subTest(operation=mop):
				open_logs = [t.name for t in fx.time_logs(mop) if not t.to_time]
				self.assertEqual(actions["time_logs_to_delete"][mop], open_logs)
				legit_log = next(t for t in fx.time_logs(mop) if t.to_time)
				target = actions["restore"][mop]
				self.assertEqual(target["status"], "Finished")
				self.assertEqual(target["operation"], w.operations.MM)
				self.assertEqual(target["employee"], w.employees.MM1)
				self.assertEqual(
					target["start_time"], audit._co_norm(legit_log.from_time)
				)
				self.assertIsNone(target["started_time"])
				self.assertIn(incident.legit_issue, case["evidence"][mop]["start_time"])
		self.assertEqual({k: v for k, v in case["required_absent"].items() if v}, {})
		self.assertEqual(case["eod_sync_proof"], {})

	def test_a_document_still_issuing_a_current_operation_is_not_a_family_c_case(self):
		w = self.world
		mwo, _mop = fx.make_work_order(w, "MM")
		current = fx.issue(w, mwo, department="MM", employee="MM1")

		case = manifest_for(current.name)["cases"][0]

		self.assertEqual(case["status"], "blocked")
		self.assertTrue(
			any("legitimate Issue for this row" in b for b in case["blockers"])
		)


class TestRequiredAbsentAttribution(_RolledBack):
	"""Later history on an older stale Issue's operations: caused (blocks) vs legitimate (listed)."""

	def test_work_booked_while_a_double_issued_operation_was_current_is_listed(self):
		w = self.world
		mwo, m, legit, stale = double_issue(w)
		during = raw_stock_entry(
			w, [(m, mwo)]
		)  # e.g. a Material Transfer to the holder
		receive = receive_of(w, mwo, legit)
		after = raw_stock_entry(w, [(m, mwo)])  # booked on the finished operation

		case = manifest_for(stale)["cases"][0]

		self.assertEqual([r["kind"] for r in case["rows"]], ["double_issued"])
		self.assertEqual(case["rows"][0]["minting_receive"], receive.name)
		required = case["required_absent"]
		self.assertEqual(
			[f["name"] for f in required["stock_entries_on_operations_after_submit"]],
			[after],
		)
		self.assertIn(
			"moved past it",
			required["stock_entries_on_operations_after_submit"][0]["why"],
		)
		listed = {
			(f["check"], f["name"]) for f in required["legitimate_later_artefacts"]
		}
		self.assertIn(("stock_entries_on_operations_after_submit", during), listed)
		self.assertIn(
			("other_mop_logs_after_submit", f"Employee IR {receive.name}"), listed
		)
		self.assertEqual(case["status"], "blocked")
		self.assertTrue(
			any(
				"stock_entries_on_operations_after_submit: 1 found" in b
				for b in case["blockers"]
			)
		)
		self.assertTrue(
			any("legitimate_later_artefacts" in r for r in case["needs_review"])
		)

	def test_nothing_is_legitimate_after_the_stale_issue_reassigned_the_operation(self):
		"""Same timeline, but the second Issue handed the operation to another employee: work
		booked before the Receive may be the stale holder's, so it blocks."""
		w = self.world
		mwo, m, legit, stale = double_issue(w, stale_employee="MM2")
		during = raw_stock_entry(w, [(m, mwo)])
		receive_of(w, mwo, legit, employee="MM2")  # the operation now names MM2

		case = manifest_for(stale)["cases"][0]

		self.assertEqual([r["kind"] for r in case["rows"]], ["double_issued"])
		self.assertIsNotNone(case["rows"][0]["successor"])
		found = case["required_absent"]["stock_entries_on_operations_after_submit"]
		self.assertEqual([f["name"] for f in found], [during])
		self.assertIn("reassigned", found[0]["why"])
		self.assertEqual(case["status"], "blocked")

	def test_eod_entries_and_the_stale_issues_own_entries_always_block(self):
		w = self.world
		incident = stale_issue_incident(w)
		m1, mwo1 = incident.stale_ops[0], incident.mwos[0]
		own = raw_stock_entry(w, [(m1, mwo1)], employee_ir=incident.stale)
		eod = raw_stock_entry(
			w, [(m1, mwo1)], stock_entry_type="Material Transfer to Department"
		)
		raw_eod_run(
			w,
			[{"mwo": mwo1, "mop": m1, "stock_entry": eod}],
			started_on=now_datetime(),
		)

		required = audit.co_required_absent(
			manifest_for(incident.stale)["cases"][0]["required_absent_spec"]
		)

		self.assertEqual([f["name"] for f in required["stock_entries_linked"]], [own])
		self.assertEqual(
			[f["name"] for f in required["stock_entries_on_operations_after_submit"]],
			[eod],
		)
		self.assertIn(
			"EOD", required["stock_entries_on_operations_after_submit"][0]["why"]
		)
		self.assertEqual(required["legitimate_later_artefacts"], [])


class TestEodSyncProof(_RolledBack):
	"""Already-synced stale logs are allowed only when no EOD moved the stale operation."""

	def test_verdicts(self):
		w = self.world
		incident = stale_issue_incident(w)
		m1, mwo1 = incident.stale_ops[0], incident.mwos[0]
		pointer = incident.pointers[mwo1]
		logs = stale_logs(incident.stale, m1)
		self.assertTrue(logs)
		mark_synced([log.name for log in logs])
		first = min(get_datetime(log.creation) for log in logs)
		later = add_to_date(first, seconds=30)

		def proof():
			return audit._co_eod_sync_proof(mwo1, m1, logs)

		def clear_runs():
			frappe.db.sql(
				"DELETE FROM `tabMOP EOD Sync Log Item` WHERE parent LIKE %(p)s",
				{"p": f"{w.prefix}%"},
			)
			frappe.db.sql(
				"DELETE FROM `tabMOP EOD Sync Log` WHERE name LIKE %(p)s",
				{"p": f"{w.prefix}%"},
			)

		with self.subTest("nothing explains the sync"):
			self.assertEqual(proof()["verdict"], "missing")

		with self.subTest("a run that ended before the logs never gathered them"):
			raw_eod_run(
				w,
				[{"mwo": mwo1, "mop": pointer}],
				started_on=add_to_date(first, minutes=-10),
				completed_on=add_to_date(first, minutes=-5),
			)
			self.assertEqual(proof()["verdict"], "missing")
			clear_runs()

		with self.subTest("a later run closed them while moving another operation"):
			run = raw_eod_run(w, [{"mwo": mwo1, "mop": pointer}], started_on=later)
			result = proof()
			self.assertEqual(result["verdict"], "proven")
			self.assertEqual(result["closed_by"]["eod_runs"], [run])
			clear_runs()

		with self.subTest("a held run (and the new hold) moved nothing"):
			raw_eod_run(
				w,
				[
					{
						"mwo": mwo1,
						"mop": m1,
						"status": "Failed",
						"sync_stage": "Build Stock Entry Row",
						"is_synced": 0,
					},
					{
						"mwo": mwo1,
						"mop": m1,
						"status": "Failed",
						"sync_stage": "Collect MOP Log",
						"is_synced": 0,
					},
				],
				started_on=later,
			)
			raw_eod_run(
				w,
				[{"mwo": mwo1, "mop": pointer}],
				started_on=add_to_date(later, hours=1),
			)
			self.assertEqual(proof()["verdict"], "proven")
			clear_runs()

		with self.subTest("a run that transferred the stale operation"):
			raw_eod_run(w, [{"mwo": mwo1, "mop": m1}], started_on=later)
			result = proof()
			self.assertEqual(result["verdict"], "contradicted")
			self.assertEqual(len(result["eod_items_naming_stale_operation"]), 1)
			clear_runs()

		with self.subTest("an EOD entry row names it although its item names another"):
			entry = raw_stock_entry(
				w, [(m1, mwo1)], stock_entry_type="Material Transfer to Department"
			)
			raw_eod_run(
				w,
				[{"mwo": mwo1, "mop": pointer, "stock_entry": entry}],
				started_on=later,
			)
			result = proof()
			self.assertEqual(result["verdict"], "contradicted")
			self.assertEqual(
				[
					r["stock_entry"]
					for r in result["eod_stock_entry_rows_naming_stale_operation"]
				],
				[entry],
			)
			clear_runs()
			frappe.db.sql(
				"DELETE FROM `tabStock Entry Detail` WHERE parent = %s", entry
			)
			frappe.db.sql("DELETE FROM `tabStock Entry` WHERE name = %s", entry)

		with self.subTest("the Serial Number Creator realization closed them"):
			order = f"{w.prefix}-PMO"
			frappe.db.set_value(
				"Manufacturing Work Order", mwo1, "manufacturing_order", order
			)
			snc = (
				{"custom_serial_number_creator": f"{w.prefix}-SNC"}
				if frappe.db.has_column("Stock Entry", "custom_serial_number_creator")
				else {}
			)
			realized = raw_stock_entry(
				w,
				[],
				stock_entry_type="Manufacture",
				purpose="Manufacture",
				manufacturing_order=order,
				creation=later,
				**snc,
			)
			result = proof()
			self.assertEqual(result["verdict"], "proven")
			self.assertEqual(
				result["closed_by"]["serial_number_creator_entries"], [realized]
			)

	def test_the_builder_blocks_synced_logs_without_a_proof(self):
		incident = stale_issue_incident(self.world)
		mark_synced(
			[log.name for log in stale_logs(incident.stale, incident.stale_ops[0])]
		)

		case = manifest_for(incident.stale)["cases"][0]

		self.assertEqual(case["status"], "blocked")
		self.assertTrue(
			any("EOD Sync Log proof is missing" in b for b in case["blockers"]),
			case["blockers"],
		)
		self.assertEqual(
			case["eod_sync_proof"][incident.stale_ops[0]]["verdict"], "missing"
		)


# ==============================================================================================
# the repair: plan, guards, rehearse, apply
# ==============================================================================================


class TestPlanWritesNothing(_RolledBack):
	def test_plan_reports_the_writes_it_would_make_and_makes_none(self):
		incident = stale_issue_incident(self.world)
		data = manifest_for(incident.stale)
		before = fx.snapshot(*incident.mwos)
		writes_before = frappe.db.transaction_writes

		with recorded_statements() as statements:
			report = repair.execute(manifest=data, mode="plan")

		self.assertEqual(write_statements(statements), [])
		self.assertEqual(frappe.db.transaction_writes, writes_before)
		self.assertEqual(fx.snapshot(*incident.mwos), before)
		self.assertEqual(report["status"], "planned")
		self.assertEqual(report["read_only"]["write_statements"], 0)
		case = report["cases"][0]
		self.assertEqual(case["verdict"], "would_apply", case)
		self.assertEqual(report["would_apply"], [incident.stale])
		writes = case["writes_apply_would_make"]
		self.assertEqual(
			sorted(writes["operations_restored"]), sorted(incident.stale_ops)
		)
		self.assertEqual(
			sorted(writes["mop_logs_set_is_cancelled"]),
			sorted(r.name for r in fx.mop_logs(voucher_no=incident.stale)),
		)

	def test_a_tampered_case_is_refused_by_its_hash(self):
		incident = stale_issue_incident(self.world)
		data = manifest_for(incident.stale)
		target = data["cases"][0]["actions"]["restore"][incident.stale_ops[0]]
		target["employee"] = self.world.employees.MM2

		case = repair.execute(manifest=data, mode="plan")["cases"][0]

		self.assertEqual(case["verdict"], "blocked")
		self.assertTrue(any("sha256" in p for p in case["manifest_problems"]))

	def test_editing_a_sealed_verdict_by_hand_breaks_the_seal(self):
		incident = stale_issue_incident(self.world)
		data = manifest_for(incident.stale)
		data["cases"][0]["needs_review"] = []  # e.g. a reviewer "clearing" a note
		data["cases"][0]["blockers"] = ["added by hand"]

		case = repair.execute(manifest=data, mode="plan")["cases"][0]

		self.assertEqual(case["verdict"], "blocked")
		self.assertTrue(any("sha256" in p for p in case["manifest_problems"]))

	def test_needs_review_notes_must_be_acknowledged_verbatim(self):
		incident = stale_issue_incident(self.world)
		data = manifest_for(incident.stale)
		case = data["cases"][0]
		note = "operation target kept as stored (test note)"
		case["needs_review"] = [note]
		case["case_sha256"] = audit.co_case_sha256(case)  # sealed as the builder would

		plan = repair.execute(manifest=data, mode="plan")["cases"][0]
		self.assertEqual(plan["verdict"], "blocked")
		self.assertTrue(any("not acknowledged" in p for p in plan["manifest_problems"]))

		case["review_acknowledged"] = [note.upper()]  # not verbatim
		plan = repair.execute(manifest=data, mode="plan")["cases"][0]
		self.assertTrue(any("not acknowledged" in p for p in plan["manifest_problems"]))

		case["review_acknowledged"] = [note]
		plan = repair.execute(manifest=data, mode="plan")["cases"][0]
		self.assertEqual(plan["manifest_problems"], [])
		self.assertEqual(plan["verdict"], "would_apply", plan)

	def test_would_apply_is_empty_while_a_global_guard_refuses(self):
		incident = stale_issue_incident(self.world)
		data = manifest_for(incident.stale)

		with patch.object(repair, "_eod_locked", return_value=True):
			report = repair.execute(manifest=data, mode="plan")

		self.assertTrue(report["guards"]["problems"])
		self.assertEqual(report["cases"][0]["verdict"], "would_apply")
		self.assertEqual(report["would_apply"], [])

	def test_write_modes_refuse_a_caller_with_pending_writes(self):
		incident = stale_issue_incident(self.world)
		data = manifest_for(incident.stale)

		with patch.object(repair, "_eod_locked", return_value=False):
			report = repair.execute(manifest=data, mode="rehearse")

		self.assertEqual(report["status"], "refused")
		self.assertIn("pending writes", " ".join(report["guards"]["problems"]))


class TestGlobalGuards(_RolledBack):
	def _guards(
		self, data, mode, ticket=None, site=None, eod_locked=False, read_only=False
	):
		with ExitStack() as stack:
			stack.enter_context(
				patch.object(repair, "_eod_locked", return_value=eod_locked)
			)
			stack.enter_context(
				patch.object(repair, "_open_recon_windows", return_value=[])
			)
			stack.enter_context(
				patch.dict(frappe.conf, {repair.READ_ONLY_SITE_KEY: int(read_only)})
			)
			if site:
				stack.enter_context(patch.object(frappe.local, "site", site))
			return repair._global_guards(data, mode, ticket)["problems"]

	def _data(self, **overrides):
		data = {
			"site": frappe.local.site,
			"production_site": PRODUCTION_SITE,
			"built_on": audit._co_norm(now_datetime()),
			"reviewed_by": "Administrator",
			"reviewed_on": audit._co_norm(add_to_date(now_datetime(), seconds=1)),
			"cases": [],
		}
		data.update(overrides)
		return data

	def test_rehearse_is_refused_on_a_read_only_site_and_on_the_production_site(self):
		self.assertEqual(self._guards(self._data(), "rehearse"), [])
		self.assertTrue(
			any(
				repair.READ_ONLY_SITE_KEY in p
				for p in self._guards(self._data(), "rehearse", read_only=True)
			)
		)
		self.assertTrue(
			any(
				"production site" in p
				for p in self._guards(
					self._data(production_site=frappe.local.site), "rehearse"
				)
			)
		)

	def test_apply_needs_a_review_a_ticket_its_own_site_and_the_administrator(self):
		self.assertEqual(self._guards(self._data(), "apply", ticket="INC-1"), [])
		cases = {
			"no review": (self._data(reviewed_by=None), "INC-1", None),
			"review predates the build": (
				self._data(
					reviewed_on=audit._co_norm(add_to_date(now_datetime(), days=-1))
				),
				"INC-1",
				None,
			),
			"no ticket": (self._data(), None, None),
			"another site": (self._data(site="some-other-site"), "INC-1", None),
		}
		for label, (data, ticket, site) in cases.items():
			with self.subTest(label):
				self.assertTrue(self._guards(data, "apply", ticket=ticket, site=site))
		with self.subTest("read-only site"):
			self.assertTrue(
				any(
					repair.READ_ONLY_SITE_KEY in p
					for p in self._guards(
						self._data(), "apply", "INC-1", read_only=True
					)
				)
			)
		with patch.dict(frappe.session, {"user": "not-the-administrator@example.com"}):
			self.assertTrue(
				any(
					"Administrator" in p
					for p in self._guards(self._data(), "apply", "INC-1")
				)
			)

	def test_the_read_only_key_accepts_any_truthy_spelling(self):
		for value in (1, "1", True, "true", "True", "yes", "on", "Y"):
			with self.subTest(value=value):
				self.assertTrue(repair._truthy(value))
		for value in (None, False, 0, "", " ", "0", "false", "no", "off", "none"):
			with self.subTest(value=value):
				self.assertFalse(repair._truthy(value))
		self.assertTrue(
			any(
				repair.READ_ONLY_SITE_KEY in p
				for p in self._guards_with_conf(self._data(), "apply", "INC-1", "true")
			)
		)

	def _guards_with_conf(self, data, mode, ticket, value):
		with ExitStack() as stack:
			stack.enter_context(patch.object(repair, "_eod_locked", return_value=False))
			stack.enter_context(
				patch.object(repair, "_open_recon_windows", return_value=[])
			)
			stack.enter_context(
				patch.dict(frappe.conf, {repair.READ_ONLY_SITE_KEY: value})
			)
			return repair._global_guards(data, mode, ticket)["problems"]

	def test_nothing_runs_while_the_eod_lock_is_held(self):
		self.assertTrue(
			any("EOD" in p for p in self._guards(self._data(), "plan", eod_locked=True))
		)


class TestAuditAndPlanReadOnlyTransaction(_Committed):
	def test_the_audit_and_the_plan_run_in_a_read_only_transaction(self):
		incident = self.committed_incident()
		data = manifest_for(incident.stale)
		before = fx.snapshot(*incident.mwos)

		with recorded_statements() as statements:
			report = audit.audit_current_operation_conflicts(mwos=incident.mwos)
			plan = repair.execute(manifest=data, mode="plan")

		self.assertEqual(
			report["read_only"],
			{"mode": "read_only_transaction", "write_statements": 0},
		)
		self.assertEqual(
			plan["read_only"], {"mode": "read_only_transaction", "write_statements": 0}
		)
		self.assertIn("START TRANSACTION READ ONLY", statements)
		self.assertEqual(write_statements(statements), [])
		self.assertEqual(frappe.db.transaction_writes, 0)
		self.assertEqual(plan["cases"][0]["verdict"], "would_apply")
		self.assertUnchanged(incident, before)


class TestRehearseAndApply(_Committed):
	def test_rehearse_applies_everything_then_rolls_back(self):
		incident = self.committed_incident()
		data = manifest_for(incident.stale)
		before = fx.snapshot(*incident.mwos)

		report = repair.execute(manifest=data, mode="rehearse")

		self.assertEqual(report["status"], "ok", report)
		case = report["cases"][0]
		self.assertEqual(case["status"], "rehearsed", case)
		for mop in incident.stale_ops:
			self.assertEqual(case["restored"][mop]["status"], "Finished")
		self.assertUnchanged(incident, before)

	def test_apply_restores_the_reviewed_state_and_a_rerun_is_a_no_op(self):
		w = self.world
		incident = self.committed_incident()
		data = manifest_for(incident.stale)
		targets = data["cases"][0]["actions"]["restore"]
		stale_time_logs = data["cases"][0]["actions"]["time_logs_to_delete"]
		ticket = f"{self.run_prefix}-TICKET"

		report = repair.execute(manifest=data, mode="apply", ticket=ticket)

		self.assertEqual(report["status"], "ok", report)
		self.assertEqual(report["cases"][0]["status"], "applied")
		frappe.db.rollback()  # read what was committed
		self.assertEqual(
			frappe.db.get_value("Employee IR", incident.stale, "docstatus"), 2
		)
		self.assertEqual(
			{r.is_cancelled for r in fx.mop_logs(voucher_no=incident.stale)}, {1}
		)
		for mwo, mop in zip(incident.mwos, incident.stale_ops, strict=True):
			with self.subTest(operation=mop):
				row = frappe.db.get_value(
					"Manufacturing Operation",
					mop,
					["status", "operation", "employee", "start_time", "started_time"],
					as_dict=True,
				)
				self.assertEqual(row.status, "Finished")
				self.assertEqual(row.operation, w.operations.MM)
				self.assertEqual(row.employee, w.employees.MM1)
				self.assertEqual(
					audit._co_norm(row.start_time), targets[mop]["start_time"]
				)
				self.assertIsNone(row.started_time)
				names = {t.name for t in fx.time_logs(mop)}
				self.assertFalse(names & set(stale_time_logs[mop]))
				self.assertEqual(fx.pointer(mwo), incident.pointers[mwo])
				self.assertEqual(fx.open_operations(mwo), [incident.pointers[mwo]])
		comments = frappe.get_all(
			"Comment",
			filters={"content": ["like", f"%{ticket}%"]},
			fields=["reference_doctype", "reference_name"],
		)
		self.assertEqual(
			sorted((c.reference_doctype, c.reference_name) for c in comments),
			sorted(
				[("Employee IR", incident.stale)]
				+ [("Manufacturing Operation", m) for m in incident.stale_ops]
				+ [("Manufacturing Work Order", m) for m in incident.mwos]
			),
		)
		after = fx.snapshot(*incident.mwos)

		rerun = repair.execute(manifest=data, mode="apply", ticket=ticket)
		plan = repair.execute(manifest=data, mode="plan")

		self.assertEqual(rerun["status"], "ok")
		self.assertEqual(rerun["cases"][0]["status"], "already_applied")
		self.assertEqual(plan["cases"][0]["verdict"], "already_applied")
		frappe.db.rollback()
		self.assertEqual(fx.snapshot(*incident.mwos), after)
		self.assertEqual(
			len(
				frappe.get_all("Comment", filters={"content": ["like", f"%{ticket}%"]})
			),
			len(comments),
		)

	def test_a_log_synced_after_the_review_aborts_with_a_diff(self):
		incident = self.committed_incident()
		data = manifest_for(incident.stale)
		synced = stale_logs(incident.stale, incident.stale_ops[0])[0].name
		mark_synced([synced])
		frappe.db.commit()
		before = fx.snapshot(*incident.mwos)

		report = repair.execute(manifest=data, mode="apply", ticket="INC-1")

		case = report["cases"][0]
		self.assertEqual(report["status"], "stopped")
		self.assertEqual(case["status"], "aborted", case)
		paths = {d["path"]: d for d in case["details"]["diff_vs_expected"]}
		self.assertEqual(
			(
				paths[f"mop_logs.{synced}.is_synced"]["expected"],
				paths[f"mop_logs.{synced}.is_synced"]["actual"],
			),
			(0, 1),
		)
		self.assertIn("voucher_mop_logs.synced_active", paths)
		self.assertUnchanged(incident, before)

		# Rebuilt now, the manifest carries no EOD Sync Log proof for that log: refused outright.
		rebuilt = manifest_for(incident.stale)
		self.assertEqual(rebuilt["cases"][0]["status"], "blocked")
		refused = repair.execute(manifest=rebuilt, mode="apply", ticket="INC-1")
		self.assertEqual(refused["cases"][0]["status"], "refused")
		self.assertTrue(
			any("EOD Sync Log proof" in p for p in refused["cases"][0]["problems"])
		)
		self.assertUnchanged(incident, before)

	def test_an_operation_changed_after_the_review_aborts_with_a_field_diff(self):
		incident = self.committed_incident()
		data = manifest_for(incident.stale)
		changed = incident.stale_ops[1]
		frappe.db.set_value("Manufacturing Operation", changed, "rpt_wt_issue", 9.876)
		frappe.db.commit()
		before = fx.snapshot(*incident.mwos)

		report = repair.execute(manifest=data, mode="apply", ticket="INC-1")

		case = report["cases"][0]
		self.assertEqual(case["status"], "aborted", case)
		self.assertIn("not the reviewed one", case["reason"])
		paths = {d["path"] for d in case["details"]["diff_vs_expected"]}
		self.assertIn(f"operations.{changed}.modified", paths)
		self.assertIn(f"operations.{changed}.rpt_wt_issue", paths)
		self.assertUnchanged(incident, before)

	def test_the_case_locks_share_one_wait_budget(self):
		incident = self.committed_incident()
		case = manifest_for(incident.stale)["cases"][0]
		now = [100.0]  # the clock; every lock statement below "takes" 6 s
		seen = []  # (when the statement ran, the wait it was given)

		def record(table, fields, names, wait):
			for _ in names:
				seen.append((now[0], wait() if callable(wait) else wait))
				now[0] += 6.0
			return {}

		real_sql = frappe.db.sql

		def sql(query, *args, **kwargs):
			if "FROM `tabMOP Log` WHERE name" in query:
				seen.append((now[0], query.rsplit("FOR UPDATE", 1)[1].strip()))
				now[0] += 6.0
				return []
			return real_sql(query, *args, **kwargs)

		lock_rows = "jewellery_erpnext.jewellery_erpnext.lock_order._lock_rows_by_name"
		with (
			patch.object(repair, "time", SimpleNamespace(monotonic=lambda: now[0])),
			patch(lock_rows, side_effect=record),
			patch.object(frappe.db, "sql", side_effect=sql),
		):
			repair._lock_case(case)
		frappe.db.rollback()

		deadline = 100.0 + repair.LOCK_WAIT_SECONDS
		self.assertGreater(len(seen), 3)
		for at, given in seen:
			left = max(1, math.ceil(deadline - at)) if at < deadline else 0
			if isinstance(given, str):  # a MOP Log statement's lock clause
				self.assertEqual(given, f"WAIT {left}" if left else "NOWAIT")
			else:
				self.assertEqual(given, left)
		self.assertEqual(seen[0][1], repair.LOCK_WAIT_SECONDS)
		self.assertIn(
			seen[-1][1], (0, "NOWAIT")
		)  # the budget ran out, it did not restart

	def test_a_case_that_builds_differently_under_the_locks_aborts(self):
		incident = self.committed_incident()
		data = manifest_for(incident.stale)
		before = fx.snapshot(*incident.mwos)
		build = audit.co_build_stale_issue_case

		def built_now(name):
			case = build(name)
			case["blockers"] = ["appeared after the review"]
			case["status"] = "blocked"
			case["case_sha256"] = audit.co_case_sha256(case)
			return case

		with patch.object(repair, "co_build_stale_issue_case", side_effect=built_now):
			report = repair.execute(manifest=data, mode="rehearse")

		case = report["cases"][0]
		self.assertEqual(case["status"], "aborted", case)
		self.assertIn("differs from the reviewed one", case["reason"])
		self.assertEqual(
			case["details"]["blockers"]["now"], ["appeared after the review"]
		)
		self.assertFalse(frappe.flags.get(repair.IN_PROGRESS_FLAG))
		self.assertUnchanged(incident, before)

	def test_apply_is_refused_without_a_review_or_a_ticket(self):
		incident = self.committed_incident()
		before = fx.snapshot(*incident.mwos)

		unreviewed = repair.execute(
			manifest=manifest_for(incident.stale, reviewed=False),
			mode="apply",
			ticket="INC-1",
		)
		no_ticket = repair.execute(manifest=manifest_for(incident.stale), mode="apply")

		for report in (unreviewed, no_ticket):
			self.assertEqual(report["status"], "refused")
			self.assertNotIn("cases", report)
		self.assertTrue(
			any("reviewed_by" in p for p in unreviewed["guards"]["problems"])
		)
		self.assertTrue(any("ticket" in p for p in no_ticket["guards"]["problems"]))
		self.assertUnchanged(incident, before)

	def test_rehearse_is_refused_on_the_manifests_production_site(self):
		incident = self.committed_incident()
		before = fx.snapshot(*incident.mwos)

		report = repair.execute(
			manifest=manifest_for(incident.stale, production_site=frappe.local.site),
			mode="rehearse",
		)

		self.assertEqual(report["status"], "refused")
		self.assertTrue(
			any("rehearse is refused" in p for p in report["guards"]["problems"])
		)
		self.assertUnchanged(incident, before)
