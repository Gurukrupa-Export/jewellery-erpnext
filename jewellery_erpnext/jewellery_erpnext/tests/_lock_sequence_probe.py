"""Lock-sequence probe for the multi-counter cascades (C-09a; test register T-06 / T-10).

THROWAWAY SITE ONLY. Every entry point refuses any site except ``kggk-pi-test.localhost``
(the bench's default_site is the production copy). The leading underscore keeps it out of
``bench run-tests --app`` discovery and out of CI, like ``_lock_contention_stress.py``: it
needs a restored backup with real documents.

What it checks
--------------
One real flow runs inside one transaction with ``frappe.local.db.sql`` wrapped. Each
statement is classified, and everything is rolled back at the end (commits and begins are
swallowed, jobs and realtime events dropped):

* **counter**: a ``tabSeries`` or ``tabDocument Naming Rule`` row taken ``FOR UPDATE``,
  updated or inserted. The key is the series key or the rule name.
* **bin**: a ``tabBin`` row taken ``FOR UPDATE``, updated or inserted.
* **late class**: Stock Reservation Entry, Manufacturing Operation, MOP Log and
  Manufacturing Work Order locking reads or writes.

A flow FAILS when a naming counter is first taken after the first Bin event. The only
exception is the terminal Purchase Order counter that ``lock_order.py`` documents (taken
last, followed only by commit). A counter first taken after a late-class event but before
any Bin event is reported as a warning.

How to run
----------
Run it before and after the shard rehearsal (D3 section 3) on the same restore. Redis cache
and queue must be up, ``allow_tests`` set and the scheduler paused on the test site::

    bench --site kggk-pi-test.localhost run-tests \\
        --module jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe

For a printed report without unittest::

    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe.run_all

Pin documents with ``--kwargs "{'docs': {'refining_submit_wo': '<Refining Entry>'}}"``,
or with the ``LOCK_PROBE_DOCS`` environment variable (JSON, same shape). Otherwise each
flow picks a candidate itself, and is skipped when the site has none. A submit flow
prefers a draft and otherwise inserts a ``frappe.copy_doc`` clone of the newest submitted
document; the clone can fail on stock that has since moved, which is reported as an
error, never as a pass. Jobs enqueued through ``frappe.enqueue`` are dropped; flush the
queue Redis after the run in case a module enqueued through another import.

Expected results
----------------
* Base 0863ac34, before the shard: Work Order Refining shows a late ``MAT-STE-``; Receive
  Unused/Loose Material shows a late ``a0h37l6l3v`` (the Repack rule).
* Base 0863ac34, shard active: in addition, Scrap Refining shows a late Material Receipt
  rule, complete_refining a late Material Transfer rule, create_snc late
  Repack-Metal Conversion and Material Transfer (WORK ORDER) rules, and Receive
  Unused/Loose a late Material Receive (WORK ORDER) rule.
* With C-09a, in both modes: no late counter in any flow.
"""

import json
import os
import re
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, flt

PROBE_SITE = "kggk-pi-test.localhost"

COUNTER_TABLES = ("tabSeries", "tabDocument Naming Rule")
BIN_TABLE = "tabBin"
LATE_TABLES = {
	"tabStock Reservation Entry": "sre",
	"tabManufacturing Operation": "mop",
	"tabMOP Log": "mop_log",
	"tabManufacturing Work Order": "mwo",
}
# Counters lock_order.py keeps terminal on purpose: taken last, followed only by commit.
TERMINAL_RULE_DOCTYPES = ("Purchase Order",)

_TARGET_TABLE = re.compile(
	r"^\s*(?:select\b.*?\bfrom|update|insert\s+(?:ignore\s+)?into|delete\s+from)"
	r"\s+(?:`([^`]+)`|(\w+))",
	re.IGNORECASE | re.DOTALL,
)
_FOR_UPDATE = re.compile(r"\bfor\s+update\b", re.IGNORECASE)
_WHERE_NAME = re.compile(r"\bwhere\s+`?name`?\s*=\s*(%\((\w+)\)s|%s)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Statement classification
# ---------------------------------------------------------------------------


@dataclass
class LockEvent:
	seq: int
	kind: str  # "counter" | "bin" | one of LATE_TABLES' values
	table: str
	op: str  # "lock" | "update" | "insert" | "delete"
	key: object
	sql: str


def _operation(sql):
	words = sql.lstrip().split(None, 1)
	head = words[0].lower() if words else ""
	if head == "select":
		return "lock" if _FOR_UPDATE.search(sql) else None
	if head in ("update", "insert", "delete"):
		return head
	return None


def _target_table(sql):
	match = _TARGET_TABLE.match(sql)
	if not match:
		return None
	return match.group(1) or match.group(2)


def _where_name(sql, values):
	"""The value bound to ``WHERE name = ...``; for an INSERT, the first value."""
	match = _WHERE_NAME.search(sql)
	if match:
		if match.group(2) and isinstance(values, dict):
			return values.get(match.group(2))
		if match.group(1) == "%s" and isinstance(values, (list, tuple)):
			index = sql[: match.start(1)].count("%s")
			if index < len(values):
				return values[index]
	if isinstance(values, (list, tuple)) and values:
		return values[0]
	if isinstance(values, dict) and values:
		return next(iter(values.values()))
	return None


def classify(query, values=None):
	"""``(kind, table, op, key)`` for a statement the probe cares about, else ``None``."""
	sql = str(query)
	op = _operation(sql)
	if not op:
		return None
	table = _target_table(sql)
	if table in COUNTER_TABLES:
		return "counter", table, op, _where_name(sql, values)
	if table == BIN_TABLE:
		# lock_bins binds (item_code, warehouse); a set_value / update binds the Bin name.
		if _WHERE_NAME.search(sql):
			key = _where_name(sql, values)
		elif isinstance(values, (list, tuple)):
			key = tuple(values[:2])
		else:
			key = None
		return "bin", table, op, key
	if table in LATE_TABLES:
		return LATE_TABLES[table], table, op, _where_name(sql, values)
	return None


class LockSequenceProbe:
	"""Record the counter / Bin / late-class statements run on this connection."""

	def __init__(self):
		self.events = []

	def __enter__(self):
		db = frappe.local.db
		self._db = db
		self._previous = db.__dict__.get("sql")
		real_sql = db.sql

		def _sql(query, *args, **kwargs):
			values = args[0] if args else kwargs.get("values")
			found = classify(query, values)
			if found:
				kind, table, op, key = found
				self.events.append(
					LockEvent(
						len(self.events),
						kind,
						table,
						op,
						key,
						" ".join(str(query).split())[:160],
					)
				)
			return real_sql(query, *args, **kwargs)

		db.sql = _sql
		return self

	def __exit__(self, *exc):
		if self._previous is None:
			self._db.__dict__.pop("sql", None)
		else:
			self._db.sql = self._previous
		return False


def analyse(events, terminal=frozenset()):
	"""Return ``(late, warnings)``: counters first taken after the first Bin event, and
	counters first taken after the first late-class event (but before any Bin event).
	Each maps the counter key to the event that first took it."""
	late_kinds = set(LATE_TABLES.values())
	first_bin = next((e.seq for e in events if e.kind == "bin"), None)
	first_late = next((e.seq for e in events if e.kind in late_kinds), None)
	first_taken = {}
	for event in events:
		if event.kind == "counter":
			first_taken.setdefault(event.key, event)

	late, warnings = {}, {}
	for key, event in first_taken.items():
		if key in terminal:
			continue
		if first_bin is not None and event.seq > first_bin:
			late[key] = event
		elif first_late is not None and event.seq > first_late:
			warnings[key] = event
	return late, warnings


@dataclass
class ProbeReport:
	flow: str
	docname: str
	events: list
	late: dict
	warnings: dict
	terminal: set = field(default_factory=set)

	def describe(self, limit=60):
		lines = [f"{self.flow} on {self.docname}: {len(self.events)} events"]
		first_bin = next((e for e in self.events if e.kind == "bin"), None)
		if first_bin:
			lines.append(f"  first Bin event #{first_bin.seq}: {first_bin.sql}")
		for key, event in self.late.items():
			lines.append(
				f"  LATE counter {key!r} first taken at #{event.seq}: {event.sql}"
			)
		for key, event in self.warnings.items():
			lines.append(
				f"  warning: counter {key!r} first taken after a late-class event, "
				f"#{event.seq}: {event.sql}"
			)
		lines.append("  timeline (counter / bin / late-class):")
		for event in self.events[:limit]:
			lines.append(
				f"    #{event.seq:<4} {event.kind:<8} {event.op:<6} {event.key!r}"
			)
		if len(self.events) > limit:
			lines.append(f"    ... {len(self.events) - limit} more")
		return "\n".join(lines)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def _require_probe_site():
	site = getattr(frappe.local, "site", None)
	if site != PROBE_SITE:
		frappe.throw(
			f"The lock-sequence probe runs only on {PROBE_SITE}, not on {site}."
		)


def _draft_for_submit(doctype, name):
	"""The document itself when it is a draft; else an inserted clone of it."""
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus == 0:
		return doc
	clone = frappe.copy_doc(doc)
	clone.insert()
	return clone


def _find_refining(refining_type, is_external):
	filters = {
		"refining_type": refining_type,
		"is_external": is_external,
		"parent_refining_entry": ["is", "not set"],
	}
	for docstatus in (0, 1):
		name = frappe.db.get_value(
			"Refining Entry",
			{**filters, "docstatus": docstatus},
			"name",
			order_by="creation desc",
		)
		if name:
			return name
	return None


def _find_recovery_verified():
	return frappe.db.get_value(
		"Refining Entry",
		{"docstatus": 1, "status": "Recovery Verified", "is_external": 0},
		"name",
		order_by="modified desc",
	)


def _find_snc_mwo():
	from jewellery_erpnext.customer_subcontracting.sub_utils import snc

	for name in frappe.get_all(
		"Manufacturing Work Order",
		filters={"docstatus": 1, "snc_requirement": "Need", "snc_done": 0},
		pluck="name",
		order_by="modified desc",
		limit=50,
	):
		try:
			if snc.validate_button_visibility(name):
				return name
		except Exception:
			continue
	return None


def _unused_loose_payload(mop):
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		get_make_scrap_entry_rows,
	)

	rows = (get_make_scrap_entry_rows(mop) or {}).get("rows") or []
	header = frappe.db.get_value(
		"Manufacturing Operation",
		mop,
		["manufacturing_work_order", "manufacturing_order", "department"],
		as_dict=True,
	)
	items = []
	for idx, row in enumerate(rows, 1):
		qty = flt(row.get("available_to_receive_qty"))
		if qty <= 0:
			continue
		items.append(
			{
				"idx": idx,
				"stock_reservation_entry": row.get("stock_reservation_entry"),
				"stock_reservation_entry_detail": row.get(
					"stock_reservation_entry_detail"
				),
				"item_code": row.get("item_code"),
				"s_warehouse": row.get("s_warehouse"),
				"qty": qty,
				"pcs": cint(row.get("available_to_receive_pcs"))
				if row.get("is_pcs_item")
				else 0,
				"batch_no": row.get("batch_no"),
			}
		)
		break  # one row is enough to mint both entries
	return {
		"manufacturing_work_order": header.manufacturing_work_order,
		"manufacturing_operation": mop,
		"manufacturing_order": header.manufacturing_order,
		"department": header.department,
		"receive_items": items,
	}


def _find_unused_loose_mop():
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		_preflight_unused_loose_targets,
	)

	for name in frappe.get_all(
		"Manufacturing Operation",
		filters={
			"status": ["not in", ["Finished", "Not Started"]],
			"manufacturing_work_order": ["is", "set"],
		},
		pluck="name",
		order_by="modified desc",
		limit=100,
	):
		try:
			payload = _unused_loose_payload(name)
			if not payload["receive_items"]:
				continue
			# Read-only: rows whose unused/loose target is missing would block the action.
			_preflight_unused_loose_targets(payload)
		except Exception:
			continue
		return name
	return None


def _find_draft(doctype):
	return frappe.db.get_value(
		doctype, {"docstatus": 0}, "name", order_by="modified desc"
	)


def _submit(doc):
	doc.flags.ignore_permissions = True
	doc.submit()


def _complete_refining(doc):
	doc.complete_refining()


def _create_snc(mwo):
	from jewellery_erpnext.customer_subcontracting.sub_utils import snc

	snc.create_snc(mwo)


def _receive_unused_loose(payload):
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		create_scrap_wo_stock_entry,
	)

	create_scrap_wo_stock_entry(
		payload, request_id=f"PROBE-{frappe.generate_hash(length=10)}"
	)


@dataclass
class Flow:
	description: str
	find: Callable[[], Optional[str]]
	prepare: Callable[[str], object]
	run: Callable[[object], None]


FLOWS = {
	# D3 audit #1
	"refining_submit_wo": Flow(
		"Refining submit, Work Order Refining (SRE cancels, then Material Transfer)",
		lambda: _find_refining("Work Order Refining", 0),
		lambda name: _draft_for_submit("Refining Entry", name),
		_submit,
	),
	"refining_submit_scrap": Flow(
		"Refining submit, Scrap Refining (Material Transfer, then dust Material Receipt)",
		lambda: _find_refining("Scrap Refining", 0),
		lambda name: _draft_for_submit("Refining Entry", name),
		_submit,
	),
	# D3 audit #2 (the Purchase Order counter is terminal)
	"refining_submit_external": Flow(
		"External Refining submit (Material Transfer, Material Receipt, then PO)",
		lambda: _find_refining("Scrap Refining", 1),
		lambda name: _draft_for_submit("Refining Entry", name),
		_submit,
	),
	# D3 audit #3
	"complete_refining": Flow(
		"complete_refining (Manufacture, MOP Log rows, then scrap Material Transfer)",
		_find_recovery_verified,
		lambda name: frappe.get_doc("Refining Entry", name),
		_complete_refining,
	),
	# D3 audit #4
	"create_snc": Flow(
		"create_snc (Make Receive, Repack-Metal Conversions, Material Transfer (WO))",
		_find_snc_mwo,
		lambda name: name,
		_create_snc,
	),
	# D3 audit #5 (T-06)
	"receive_unused_loose": Flow(
		"MOP Receive Unused/Loose Material (Material Receive (WO), then Repack)",
		_find_unused_loose_mop,
		_unused_loose_payload,
		_receive_unused_loose,
	),
	# D3 audit #6 and #7: latent (no documents on the 16 Nov copy)
	"main_slip_submit": Flow(
		"Main Slip submit (per loss row: Repack, then Material Transfer to Department)",
		lambda: _find_draft("Main Slip"),
		lambda name: frappe.get_doc("Main Slip", name),
		_submit,
	),
	"gemstone_conversion_submit": Flow(
		"Gemstone Conversion submit (Repack-Gemstone Conversion, then Resize transfer)",
		lambda: _find_draft("Gemstone Conversion"),
		lambda name: frappe.get_doc("Gemstone Conversion", name),
		_submit,
	),
}


def _pinned_docs():
	try:
		return json.loads(os.environ.get("LOCK_PROBE_DOCS") or "{}")
	except ValueError:
		return {}


def _terminal_counter_keys():
	return set(
		frappe.get_all(
			"Document Naming Rule",
			filters={"document_type": ["in", list(TERMINAL_RULE_DOCTYPES)]},
			pluck="name",
		)
	)


@contextmanager
def _isolated():
	"""One transaction, always rolled back."""
	_require_probe_site()
	frappe.db.rollback()
	with ExitStack() as stack:
		stack.enter_context(patch.object(frappe.db, "commit", lambda *a, **k: None))
		stack.enter_context(patch.object(frappe.db, "begin", lambda *a, **k: None))
		stack.enter_context(patch("frappe.enqueue", lambda *a, **k: None))
		stack.enter_context(patch("frappe.publish_realtime", lambda *a, **k: None))
		stack.enter_context(patch("frappe.publish_progress", lambda *a, **k: None))
		try:
			yield
		finally:
			frappe.db.rollback()


def run_probe(flow, docname=None):
	"""Run one flow under the probe and return its ProbeReport, or ``None`` when the site
	has no document to run it on. Everything the flow wrote is rolled back."""
	_require_probe_site()
	spec = FLOWS[flow]
	docname = docname or _pinned_docs().get(flow) or spec.find()
	if not docname:
		return None
	with _isolated():
		# Counters taken while preparing (a clone's own name) are not the flow's.
		target = spec.prepare(docname)
		with LockSequenceProbe() as probe:
			spec.run(target)
	terminal = _terminal_counter_keys()
	late, warnings = analyse(probe.events, terminal)
	return ProbeReport(flow, docname, probe.events, late, warnings, terminal)


def run_all(docs=None):
	"""``bench execute`` entry point: run every flow and print its report."""
	_require_probe_site()
	frappe.set_user("Administrator")
	docs = {**_pinned_docs(), **(docs or {})}
	results = {}
	for flow, spec in FLOWS.items():
		try:
			report = run_probe(flow, docs.get(flow))
		except Exception as exc:
			results[flow] = f"ERROR: {exc!r}"
			print(f"{flow}: ERROR {exc!r}")
			continue
		if report is None:
			results[flow] = "SKIPPED: no candidate document"
			print(f"{flow}: skipped, no candidate document ({spec.description})")
			continue
		results[flow] = "FAIL" if report.late else "PASS"
		print(f"{results[flow]}: {report.describe()}")
	return results


# ---------------------------------------------------------------------------
# unittest entry point (explicit --module run on the probe site only)
# ---------------------------------------------------------------------------


class TestMultiCounterLockSequence(IntegrationTestCase):
	"""No naming counter is first taken after a Bin lock in the C-09a cascades."""

	@classmethod
	def setUpClass(cls):
		if getattr(frappe.local, "site", None) != PROBE_SITE:
			raise unittest.SkipTest(
				f"the lock-sequence probe runs only on {PROBE_SITE}"
			)
		frappe.set_user("Administrator")

	def _check(self, flow):
		report = run_probe(flow)
		if report is None:
			self.skipTest(f"{flow}: no candidate document on this site")
		for key in report.warnings:
			print(
				f"{flow}: warning, counter {key!r} first taken after a late-class event"
			)
		self.assertFalse(report.late, report.describe())

	def test_refining_submit_work_order(self):
		self._check("refining_submit_wo")

	def test_refining_submit_scrap(self):
		self._check("refining_submit_scrap")

	def test_refining_submit_external(self):
		self._check("refining_submit_external")

	def test_complete_refining(self):
		self._check("complete_refining")

	def test_create_snc(self):
		self._check("create_snc")

	def test_receive_unused_loose_material(self):
		# T-06: the Repack rule row must be taken before the receive's Bins.
		self._check("receive_unused_loose")

	def test_main_slip_submit(self):
		self._check("main_slip_submit")

	def test_gemstone_conversion_submit(self):
		self._check("gemstone_conversion_submit")
