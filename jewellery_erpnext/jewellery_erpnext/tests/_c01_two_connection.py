"""Two-connection held-row tests for C-01 (D1 4.3; plan T-01a..d) on the THROWAWAY site.

A second connection takes one naming row ``FOR UPDATE`` and keeps its transaction open; the first
connection, with a 3 s session ``innodb_lock_wait_timeout``, submits a fixture inside the
naming-lock probe. Both connections are rolled back. Needs the committed drafts of
``_c01_harness.build_fixtures``. Not auto-discovered (leading underscore)::

    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._c01_two_connection.run_all

    bench --site kggk-pi-test.localhost run-tests \\
        --module jewellery_erpnext.jewellery_erpnext.tests._c01_two_connection

``run_all`` prints JSON for either build (baseline 0863ac34 = "before" evidence). The test class
asserts the C-01 column, so it fails on the baseline by design.

| Case | Fixture | Held row | Baseline (expected evidence) | C-01 (asserted) |
|---|---|---|---|---|
| C1 | no-mint receive (I1) | Repack rule | 1205 after ~3 s in preallocate (rule row) | done < 1 s, no naming event |
| C2 = T-01a | no-mint receive (I1) | ``MAT-STE-`` | rule taken, 1205 at the series row | done < 1 s, neither row |
| C3 = T-01b | Process-Loss-only (I3) | Repack rule | 1205 at the rule row | done, only ``MAT-STE-`` taken |
| C4 = T-01b | Process-Loss-only (I3) | ``MAT-STE-`` | rule taken, 1205 at the series row | 1205 at the series row, rule never asked |
| C5 | I3, predicate forced to () | ``MAT-STE-`` | n/a | NamingCounterBusy < 0.5 s, nothing posted |
| T-01c | gain receive (I4) | none | both rows | both rows, no counter after the Bins |
| T-01d | EIR (I3) open, then SNC | (the EIR) | SNC waits at the rule row | SNC waits only at ``MAT-STE-`` |
"""

from __future__ import annotations

import json
import unittest
from contextlib import ExitStack, contextmanager
from time import monotonic
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.jewellery_erpnext.tests._c01_harness import (
	THROWAWAY_SITE,
	expected_counters,
	fixture_drafts,
	require_throwaway_site,
)
from jewellery_erpnext.jewellery_erpnext.tests.naming_lock_probe import (
	NamingLockProbe,
	assert_naming_order,
	stock_entry_counter_keys,
)

HOLD_SQL = {
	"series": "SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE",
	"rule": "SELECT `counter` FROM `tabDocument Naming Rule` WHERE name = %s FOR UPDATE",
}
SE_LINK = {
	"Employee IR": "employee_ir",
	"Serial Number Creator": "custom_serial_number_creator",
	"Product Certification": "product_certification",
}


def repack_rule(company):
	"""The Repack counter row of ``company`` as ``("rule", name)`` or ``("series", prefix)``."""
	return expected_counters(company, ["Repack"])[0]


def series_row(company):
	return expected_counters(company, ["Process Loss"])[0]


@contextmanager
def two_connections():
	"""``(primary, secondary)``; both rolled back and the primary restored on exit."""
	primary = frappe.local.db
	frappe.connect()
	secondary = frappe.local.db
	frappe.local.db = primary
	try:
		yield primary, secondary
	finally:
		for db in (secondary, primary):
			try:
				db.rollback()
			except Exception:
				pass
		frappe.local.db = primary
		secondary.close()


@contextmanager
def on(db):
	saved = frappe.local.db
	frappe.local.db = db
	try:
		yield
	finally:
		frappe.local.db = saved


def _innermost_app_frame(exc):
	tb, frame = exc.__traceback__, None
	while tb is not None:
		if "jewellery_erpnext" in tb.tb_frame.f_code.co_filename:
			code = tb.tb_frame.f_code
			frame = f"{code.co_filename.rsplit('jewellery_erpnext/', 1)[-1]}:{tb.tb_lineno} {code.co_name}"
		tb = tb.tb_next
	return frame


def _submit(doctype, name, *, mint_types=None):
	"""Submit on the CURRENT connection inside the probe; never commits."""
	record = {"doctype": doctype, "name": name}
	with ExitStack() as stack:
		if mint_types is not None:
			from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
				receive_naming,
			)

			stack.enter_context(
				patch.object(
					receive_naming, "receive_mint_types", return_value=tuple(mint_types)
				)
			)
		probe = stack.enter_context(NamingLockProbe())
		doc = frappe.get_doc(doctype, name)
		started = monotonic()
		try:
			doc.submit()
			record["outcome"] = "submitted"
		except Exception as exc:
			record["outcome"] = type(exc).__name__
			record["message"] = str(exc)[:300]
			record["wait_frame"] = _innermost_app_frame(exc)
		record["seconds"] = round(monotonic() - started, 3)
	record["company"] = doc.company
	record["plan"] = sorted(
		t
		for _company, t in (
			frappe.flags.get(lock_order.PLANNED_COUNTERS_FLAG) or {}
		).get((doctype, name), set())
	)
	record["events"] = probe.report()
	record["probe"] = probe
	record["posted"] = frappe.db.count(
		"Stock Entry", {SE_LINK[doctype]: name, "docstatus": 1}
	)
	return record


def held_row_case(doctype, name, hold, *, wait=3, mint_types=None):
	"""Hold ``hold`` (``"rule"`` = the company's Repack row, ``"series"`` = its MAT-STE- row,
	``None`` = nothing) on a second connection and submit ``doctype/name`` on the first."""
	company = frappe.db.get_value(doctype, name, "company")
	held = {"rule": repack_rule, "series": series_row}[hold](company) if hold else None
	with two_connections() as (primary, secondary):
		if held:
			secondary.sql(HOLD_SQL[held[0]], (held[1],))
		primary.sql(f"SET SESSION innodb_lock_wait_timeout = {int(wait)}")
		try:
			record = _submit(doctype, name, mint_types=mint_types)
		finally:
			primary.sql("SET SESSION innodb_lock_wait_timeout = DEFAULT")
	record["held"] = held
	# After both rollbacks nothing the attempt posted may remain.
	record["posted_after_rollback"] = frappe.db.count(
		"Stock Entry", {SE_LINK[doctype]: name, "docstatus": 1}
	)
	return record


def coupled_queue_case(eir_name, snc_name, *, wait=3):
	"""T-01d: an Employee IR receive is mid-transaction on the first connection; a Serial Number
	Creator submits on the second. Where does the SNC wait?"""
	with two_connections() as (primary, secondary):
		eir = _submit("Employee IR", eir_name)
		with on(secondary):
			secondary.sql(f"SET SESSION innodb_lock_wait_timeout = {int(wait)}")
			try:
				snc = _submit("Serial Number Creator", snc_name)
			finally:
				secondary.sql("SET SESSION innodb_lock_wait_timeout = DEFAULT")
	snc["asked_rule_row"] = any(e["kind"] == "rule" for e in snc["events"])
	return {"eir": eir, "snc": snc}


def _strip(record):
	record.pop("probe", None)
	return record


def run_all(wait=3):
	require_throwaway_site()
	drafts = fixture_drafts()
	out = {}

	def _case(key, scenario, hold, **kw):
		if scenario not in drafts:
			out[key] = {"skipped": f"no {scenario} draft"}
			return
		out[key] = _strip(
			held_row_case("Employee IR", drafts[scenario], hold, wait=wait, **kw)
		)

	_case("C1", "I1", "rule")
	_case("C2_T01a", "I1", "series")
	_case("C3_T01b", "I3", "rule")
	_case("C4_T01b", "I3", "series")
	_case("C5_held", "I3", "series", mint_types=())
	_case("C5_free", "I3", None, mint_types=())
	_case("T01c", "I4", None)
	sncs = frappe.get_all(
		"Serial Number Creator", {"docstatus": 0}, pluck="name", limit=1
	)
	if "I3" in drafts and sncs:
		pair = coupled_queue_case(drafts["I3"], sncs[0], wait=wait)
		out["T01d"] = {k: _strip(v) for k, v in pair.items()}
	else:
		out["T01d"] = {
			"skipped": "needs the I3 draft and a Serial Number Creator draft"
		}
	print(json.dumps(out, indent=1, default=str))
	return out


class TestC01TwoConnection(IntegrationTestCase):
	"""Asserts the C-01 column of the module table. Skipped off the throwaway site."""

	@classmethod
	def setUpClass(cls):
		if frappe.local.site != THROWAWAY_SITE:
			raise unittest.SkipTest(
				f"C-01 two-connection tests run only on {THROWAWAY_SITE}"
			)
		super().setUpClass()
		cls.drafts = fixture_drafts()
		cls.rules, cls.series = stock_entry_counter_keys()

	def _draft(self, scenario):
		if scenario not in self.drafts:
			self.skipTest(
				f"no committed {scenario} draft (run _c01_harness.build_fixtures)"
			)
		return self.drafts[scenario]

	def test_c1_no_mint_receive_ignores_a_held_repack_rule(self):
		r = held_row_case("Employee IR", self._draft("I1"), "rule")
		self.assertEqual(r["outcome"], "submitted", r)
		self.assertLess(r["seconds"], 1.0)
		self.assertEqual(r["events"], [])

	def test_c2_t01a_no_mint_receive_finishes_while_mat_ste_is_held(self):
		r = held_row_case("Employee IR", self._draft("I1"), "series")
		self.assertEqual(r["outcome"], "submitted", r)
		self.assertLess(r["seconds"], 1.0)
		self.assertEqual(r["events"], [])

	def test_c3_t01b_process_loss_receive_ignores_a_held_repack_rule(self):
		r = held_row_case("Employee IR", self._draft("I3"), "rule")
		self.assertEqual(r["outcome"], "submitted", r)
		company = r["company"]
		self.assertNotIn(repack_rule(company)[1], [e["key"] for e in r["events"]])
		assert_naming_order(
			r["probe"], "eir", [series_row(company)], self.rules, self.series
		)

	def test_c4_t01b_process_loss_receive_waits_only_on_mat_ste(self):
		r = held_row_case("Employee IR", self._draft("I3"), "series")
		self.assertEqual(r["outcome"], "QueryTimeoutError", r)
		self.assertIn("lock_order.py", r["wait_frame"] or "")
		self.assertNotIn("rule", [e["kind"] for e in r["events"]])
		self.assertEqual(r["posted_after_rollback"], 0)

	def test_c5_unplanned_counter_fails_fast_when_busy(self):
		r = held_row_case("Employee IR", self._draft("I3"), "series", mint_types=())
		self.assertEqual(r["outcome"], "NamingCounterBusy", r)
		self.assertIn("lock_stock_entry_counter_nowait", r["wait_frame"] or "")
		self.assertLess(
			r["seconds"], 3.0
		)  # refused, never queued for the lock wait timeout
		self.assertEqual(r["posted_after_rollback"], 0)
		free = held_row_case("Employee IR", self._draft("I3"), None, mint_types=())
		self.assertEqual(free["outcome"], "submitted", free)
		key = series_row(free["company"])[1]
		first = next(e for e in free["events"] if e["key"] == key)
		self.assertTrue(first["nowait"], first)

	def test_t01c_repack_needing_receive_pins_both_before_the_bins(self):
		r = held_row_case("Employee IR", self._draft("I4"), None)
		self.assertEqual(r["outcome"], "submitted", r)
		plan = {"Material Transfer (WORK ORDER)", "Repack"}
		if any(e["key"] == series_row(r["company"])[1] for e in r["events"]):
			plan.add("Process Loss")
		assert_naming_order(
			r["probe"],
			"eir",
			expected_counters(r["company"], plan),
			self.rules,
			self.series,
		)

	def test_t01d_snc_behind_an_open_receive_never_asks_the_repack_rule(self):
		sncs = frappe.get_all(
			"Serial Number Creator", {"docstatus": 0}, pluck="name", limit=1
		)
		if not sncs:
			self.skipTest("no Serial Number Creator draft")
		pair = coupled_queue_case(self._draft("I3"), sncs[0])
		self.assertEqual(pair["eir"]["outcome"], "submitted", pair["eir"])
		snc = pair["snc"]
		if "Repack" not in snc["plan"]:
			self.assertFalse(snc["asked_rule_row"], snc)
		if snc["outcome"] == "QueryTimeoutError":
			# Behind the open receive's MAT-STE- row, at the SNC's own pre-lock.
			self.assertIn("lock_order.py", snc["wait_frame"] or "")
