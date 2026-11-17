"""Test-only probe: which naming counters a transaction locks, and whether before or after its
first Bin lock (C-01, lock_order RULE F). Never imported by app code.

It wraps the connection's ``sql`` / ``commit`` / ``rollback`` (instance attributes), so it sees
every statement whatever issued it: ``getseries`` (query builder ``FOR UPDATE`` then ``UPDATE`` /
``INSERT``), ``DocumentNamingRule.apply`` (``get_value(..., for_update=True)``),
``preallocate_series`` and the stamping ``INSERT ... ON DUPLICATE KEY UPDATE`` (direct SQL a
``getseries`` patch would miss), and any site Server Script.

Usage::

    rules, series = stock_entry_counter_keys()
    with NamingLockProbe() as probe:
        eir.submit()
    assert_naming_order(probe, "eir", [("series", "MAT-STE-")], rules, series)

Rules:

* A statement on ```tabBin``` that locks (``FOR UPDATE`` / ``FOR SHARE`` / ``LOCK IN SHARE
  MODE``) or writes (INSERT / UPDATE / DELETE / REPLACE) marks the Bins as held.
* A statement on ```tabSeries``` or ```tabDocument Naming Rule``` counts when it locks, inserts
  or updates and its row key resolves: the first VALUES item of an INSERT, otherwise the value
  bound to ``name = <placeholder>``. Unresolvable statements are ignored.
* The FIRST time a ``(kind, key)`` appears in a transaction is recorded, with
  ``after_bins``, ``nowait`` and the stack. COMMIT and a full ROLLBACK start a new
  transaction; a savepoint rollback does not (InnoDB keeps locks taken after a savepoint).
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field

import frappe

_MISSING = object()

_BIN = "`tabBin`"
_SERIES = "`tabSeries`"
_RULE = "`tabDocument Naming Rule`"
_PLACEHOLDER = r"(%\((\w+)\)s|%s|'([^']*)')"
_NAME_EQ_RE = re.compile(r"(?<!\w)`?name`?\s*=\s*" + _PLACEHOLDER, re.IGNORECASE)
_VALUES_RE = re.compile(r"\bVALUES\s*\(\s*" + _PLACEHOLDER, re.IGNORECASE)
_LOCKING = ("FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE")
_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")

# Late counters that exist today and that C-01 does not change (D1 1.5), by the frame that
# takes them. Innermost match wins.
_STACK_CATEGORIES = (
	("batch-series", "erpnext/stock/doctype/batch/batch.py", "autoname"),
	("mould-series", "employee_ir/doc_events/mould_utils.py", "create_mould"),
	("mould-series", "mould/doc_events/utils.py", "crate_autoname"),
	("stamping", "doc_events/serial_no.py", "reserve_stamping_sequence"),
	("stamping", "doc_events/serial_no.py", "_ensure_stamping_series_row"),
	(
		"fg-serial",
		"manufacturing_operation/manufacturing_operation.py",
		"create_manufacturing_entry",
	),
)

ALLOWED_LATE = {
	"eir": {"batch-series", "mould-series"},
	"snc": {"batch-series", "fg-serial", "stamping"},
	"pc": {"batch-series"},
}


@dataclass
class NamingEvent:
	seq: int
	kind: str  # "series" | "rule"
	key: str
	after_bins: bool
	nowait: bool
	sql: str
	stack: list = field(default_factory=list)

	def as_dict(self):
		return {
			"seq": self.seq,
			"kind": self.kind,
			"key": self.key,
			"after_bins": self.after_bins,
			"nowait": self.nowait,
			"sql": self.sql,
		}


class NamingLockProbe:
	"""Context manager recording naming-counter acquisitions on one connection."""

	def __init__(self, db=None, keep_stack=True):
		self.db = db if db is not None else frappe.local.db
		self.keep_stack = keep_stack
		self.events = []
		self.bins_held = False
		self._held = set()
		self._seq = 0
		self._saved = {}
		self._orig = {}

	# -- installation ---------------------------------------------------------------
	def __enter__(self):
		for name in ("sql", "commit", "rollback"):
			self._saved[name] = self.db.__dict__.get(name, _MISSING)
			self._orig[name] = getattr(self.db, name)
		self.db.sql = self._sql
		self.db.commit = self._commit
		self.db.rollback = self._rollback
		return self

	def __exit__(self, *exc):
		for name, value in self._saved.items():
			if value is _MISSING:
				self.db.__dict__.pop(name, None)
			else:
				setattr(self.db, name, value)
		return False

	# -- wrappers -------------------------------------------------------------------
	def _sql(self, query, values=(), *args, **kwargs):
		self.observe(query, values)
		return self._orig["sql"](query, values, *args, **kwargs)

	def _commit(self, *args, **kwargs):
		out = self._orig["commit"](*args, **kwargs)
		if not getattr(self.db, "_disable_transaction_control", 0):
			self._new_transaction()
		return out

	def _rollback(self, *args, save_point=None, **kwargs):
		out = self._orig["rollback"](*args, save_point=save_point, **kwargs)
		if not save_point and not getattr(self.db, "_disable_transaction_control", 0):
			self._new_transaction()
		return out

	def _new_transaction(self):
		self._held = set()
		self.bins_held = False

	# -- classification ---------------------------------------------------------------
	def observe(self, query, values=()):
		sql = " ".join((query if isinstance(query, str) else str(query)).split())
		upper = sql.upper()
		head = upper.lstrip("( ").split(" ", 1)[0]
		locking = any(clause in upper for clause in _LOCKING)
		writing = head in _WRITES

		if _BIN in sql and (locking or writing):
			self.bins_held = True
			return
		if _SERIES in sql:
			kind = "series"
		elif _RULE in sql:
			kind = "rule"
		else:
			return
		if not (locking or head in ("INSERT", "REPLACE", "UPDATE")):
			return
		key = _row_key(sql, values, head)
		if key is None or (kind, key) in self._held:
			return
		self._held.add((kind, key))
		self._seq += 1
		self.events.append(
			NamingEvent(
				seq=self._seq,
				kind=kind,
				key=key,
				after_bins=self.bins_held,
				nowait=" NOWAIT" in upper,
				sql=sql[:160],
				stack=_stack() if self.keep_stack else [],
			)
		)

	def report(self):
		return [e.as_dict() for e in self.events]


def _row_key(sql, values, head):
	match = None
	if head in ("INSERT", "REPLACE"):
		match = _VALUES_RE.search(sql)
	if match is None:
		match = _NAME_EQ_RE.search(sql)
	if match is None:
		return None
	_whole, named, literal = match.group(1), match.group(2), match.group(3)
	if literal is not None:
		return literal
	if named:
		return (values or {}).get(named) if isinstance(values, dict) else None
	if isinstance(values, (list, tuple)):
		position = sql[: match.start(1)].count("%s")
		return values[position] if position < len(values) else None
	return None


def _stack():
	return [(f.filename, f.lineno, f.name) for f in traceback.extract_stack()[:-3]]


def _format_stack(frames):
	return "".join(f"  File {f}, line {n}, in {name}\n" for f, n, name in frames)


def classify(event, se_rules, se_series):
	"""``se-counter`` for a Stock Entry naming counter, else the late-counter category, else
	``unknown``."""
	if event.kind == "rule" and event.key in se_rules:
		return "se-counter"
	if event.kind == "series" and event.key in se_series:
		return "se-counter"
	for filename, _lineno, name in reversed(event.stack):
		path = filename.replace("\\", "/")
		for category, suffix, function in _STACK_CATEGORIES:
			if path.endswith(suffix) and name == function:
				return category
	return "unknown"


def stock_entry_counter_keys():
	"""``(rule names, tabSeries keys)`` a Stock Entry can name from on this site.

	Run OUTSIDE a probe. The series keys are resolved with the same resolver the pre-lock
	uses (``lock_order.series_prefix_for_doc``), one stub per ``naming_series`` option.
	"""
	from jewellery_erpnext.jewellery_erpnext.lock_order import series_prefix_for_doc

	rules = set(
		frappe.get_all(
			"Document Naming Rule", {"document_type": "Stock Entry"}, pluck="name"
		)
	)
	options = frappe.get_meta("Stock Entry").get_field("naming_series").options or ""
	series = set()
	for option in options.split("\n"):
		option = option.strip()
		if not option:
			continue
		stub = frappe.new_doc("Stock Entry")
		stub.naming_series = option
		prefix = series_prefix_for_doc(stub)
		if prefix:
			series.add(prefix)
	return rules, series


def assert_naming_order(probe, scenario, expected_pre_bins_se, se_rules, se_series):
	"""Fail unless the recorded transaction keeps lock_order RULE F.

	1. No Stock Entry counter is first taken after the Bins unless NOWAIT.
	2. The Stock Entry counters taken before the Bins are exactly ``expected_pre_bins_se``,
	   in order, as ``[(kind, key), ...]``.
	3. Every other late, waiting counter is in ``ALLOWED_LATE[scenario]``; ``unknown`` never is.

	Returns a JSON-able summary.
	"""
	allowed = ALLOWED_LATE[scenario]
	problems = []
	pre_bins_se = []
	late = []
	for event in probe.events:
		category = classify(event, se_rules, se_series)
		if category == "se-counter":
			if not event.after_bins:
				pre_bins_se.append((event.kind, event.key))
			elif not event.nowait:
				problems.append(
					f"Stock Entry counter first taken after Bins: {event.as_dict()}"
				)
			continue
		if event.after_bins and not event.nowait:
			late.append(category)
			if category not in allowed:
				problems.append(
					f"late {category} counter not allowed for {scenario}: {event.as_dict()}\n"
					+ _format_stack(event.stack[-12:])
				)
	if pre_bins_se != list(expected_pre_bins_se):
		problems.append(
			f"pre-Bin Stock Entry counters {pre_bins_se} != expected {list(expected_pre_bins_se)}"
		)
	if problems:
		raise AssertionError("\n".join(problems))
	return {
		"scenario": scenario,
		"pre_bins_se": pre_bins_se,
		"late": sorted(set(late)),
		"events": probe.report(),
	}
