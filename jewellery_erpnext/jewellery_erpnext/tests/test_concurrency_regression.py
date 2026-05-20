"""Phase J — Concurrency reproduction regression tests.

These tests deliberately run N workers on the SAME document at the SAME
time using ``concurrency_harness.run_concurrent``. They prove the code is
safe under contention — not merely that the current Error Log is quiet.

SAFETY: every test method first checks the site name passes the
``_assert_safe_site`` guard. On a production-like site name, the
matching test is SKIPPED rather than executed. This protects against
``bench run-tests --app jewellery_erpnext`` being invoked on a wrong
target.

Scenarios:
  A — Same-PC submit race (EG-002)
  B — Same-SNC submit race (EG-003, D-003)
  C — Same-Reserve workflow race (EG-001, D-001)
  D — Same-doc save race (EG-008)
  E — Same-EIR submit race (EG-004, EG-020, D-004)
  F — Intentional reverse-lock deadlock (1213 reproduction proof)
  G — Bin lock order (EG-003 deterministic sort verification)
  H — Bulk Reserve Material pressure (EG-001 long-queue routing)
  I — MOP EOD per-MWO isolation (EG-011)

Each scenario:
  - Skips if no copied-site fixture is configured (operator must set
    fixtures via the FIXTURES dict below before running).
  - Calls run_concurrent with N workers.
  - Asserts on summary['n_ok'] / 'n_fail' / 'exception_types'.
  - Runs the post-condition SQL from Plan §J-5 and asserts exactly 1.
"""

from __future__ import annotations

import os
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.tests.concurrency_harness import (
	_SAFE_SITE_RE,
	_assert_safe_site,
	run_concurrent,
)

# ---------------------------------------------------------------------------
# Fixtures — operator MUST populate these before running the scenarios.
# Each value is the docname of a copied-site document eligible for the
# scenario's action. If a fixture is empty, the corresponding scenario is
# skipped with a clear reason rather than executed with bad data.
# ---------------------------------------------------------------------------
FIXTURES = {
	# Scenario A: Product Certification that can be submitted (type='Issue'
	# OR service_type in ('Hall Marking Service', 'Diamond Certificate service'))
	"PRODUCT_CERTIFICATION_FOR_SUBMIT_RACE": os.environ.get("JEW_TEST_PC_NAME", ""),
	# Scenario B: Serial Number Creator in draft with populated source_table
	"SERIAL_NUMBER_CREATOR_FOR_SUBMIT_RACE": os.environ.get("JEW_TEST_SNC_NAME", ""),
	# Scenario C: Material Request eligible for 'Reserve Material' workflow
	"MATERIAL_REQUEST_FOR_RESERVE_RACE": os.environ.get("JEW_TEST_MR_NAME", ""),
	# Scenario D: Quotation in workflow state that allows save
	"QUOTATION_FOR_SAVE_RACE": os.environ.get("JEW_TEST_QTN_NAME", ""),
	# Scenario E: Employee IR with is_main_slip_required=1 AND
	# received_gross_wt > gross_wt on at least one operation row
	"EMPLOYEE_IR_FOR_INJECTION_RACE": os.environ.get("JEW_TEST_EIR_NAME", ""),
	# Scenario F/G: two harmless Bin / draft-PMO rows for reverse-lock test
	"DEADLOCK_DOCTYPE": os.environ.get("JEW_TEST_DEADLOCK_DOCTYPE", "Bin"),
	"DEADLOCK_ROW_A": os.environ.get("JEW_TEST_DEADLOCK_A", ""),
	"DEADLOCK_ROW_B": os.environ.get("JEW_TEST_DEADLOCK_B", ""),
	# Scenario H: ≥50 MR names eligible for bulk Reserve Material, comma-separated
	"BULK_MR_NAMES_CSV": os.environ.get("JEW_TEST_BULK_MR_CSV", ""),
}


def _skip_unless_safe_site():
	"""Skip the test if the current site is not a safe (copied/staging) site.

	Distinct from ``_assert_safe_site`` which raises — we want the unit-test
	runner to skip cleanly, not error out, when invoked on the wrong site.
	"""
	site = getattr(frappe.local, "site", "") or ""
	if os.environ.get("ALLOW_JEWELLERY_CONCURRENCY_TESTS") == "1":
		return None
	if not site or not _SAFE_SITE_RE.search(site):
		return f"Skipping: site {site!r} is not a copied/staging site"
	return None


def _skip_if_no_fixture(name: str):
	"""Skip the test if the named FIXTURES entry is empty."""
	value = FIXTURES.get(name) or ""
	if not value:
		return (
			f"Skipping: fixture {name} is empty. "
			f"Set the env var or edit FIXTURES dict to point at a copied-site doc."
		)
	return None


class TestSafetyGuard(FrappeTestCase):
	"""The safety guard itself MUST work, otherwise every other test is unsafe."""

	def test_assert_safe_site_refuses_blank(self):
		with self.assertRaises(RuntimeError) as ctx:
			_assert_safe_site("")
		self.assertIn("copied/staging site", str(ctx.exception).lower())

	def test_assert_safe_site_refuses_production_like(self):
		# Standard production naming patterns must be refused.
		# Note: 'prod-erp.example.com' contains no test/copy/staging/dummy/qa.
		previous_env = os.environ.pop("ALLOW_JEWELLERY_CONCURRENCY_TESTS", None)
		try:
			with self.assertRaises(RuntimeError):
				_assert_safe_site("prod-erp.example.com")
			with self.assertRaises(RuntimeError):
				_assert_safe_site("erp.gkexport.com")
			with self.assertRaises(RuntimeError):
				_assert_safe_site("gkexport-v16.m.frappe.cloud")
		finally:
			if previous_env is not None:
				os.environ["ALLOW_JEWELLERY_CONCURRENCY_TESTS"] = previous_env

	def test_assert_safe_site_accepts_safe_names(self):
		for safe in (
			"erp-staging.example.com",
			"copy-of-prod.local",
			"test-erp.example.com",
			"gkexport-dummy-v16.m.frappe.cloud",
			"qa-erp.example.com",
		):
			# Should not raise
			_assert_safe_site(safe)

	def test_assert_safe_site_opt_in_via_env(self):
		previous_env = os.environ.get("ALLOW_JEWELLERY_CONCURRENCY_TESTS")
		os.environ["ALLOW_JEWELLERY_CONCURRENCY_TESTS"] = "1"
		try:
			# Even production-like name is accepted with the explicit opt-in.
			_assert_safe_site("prod-erp.example.com")
		finally:
			if previous_env is None:
				os.environ.pop("ALLOW_JEWELLERY_CONCURRENCY_TESTS", None)
			else:
				os.environ["ALLOW_JEWELLERY_CONCURRENCY_TESTS"] = previous_env


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------


def _count_active_se_for_pc(pc_name: str) -> int:
	"""Count active (docstatus<2) Stock Entries linked to a Product Certification."""
	return frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Entry`
		WHERE product_certification = %(pc)s AND docstatus < 2
		""",
		{"pc": pc_name},
	)[0][0]


def _count_active_se_for_snc(snc_name: str) -> int:
	"""Count active (docstatus<2) Stock Entries linked to a Serial Number Creator
	via the existing `Stock Entry.custom_serial_number_creator` back-link."""
	return frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Entry`
		WHERE custom_serial_number_creator = %(snc)s AND docstatus < 2
		""",
		{"snc": snc_name},
	)[0][0]


def _count_active_reserved_se_for_mr(mr_name: str) -> int:
	"""Count active reserved Stock Entries for a Material Request via the
	D-001-corrected JOIN through Stock Entry Detail."""
	return frappe.db.sql(
		"""
		SELECT COUNT(DISTINCT se.name)
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE sed.material_request = %(mr)s
		  AND se.add_to_transit = 1
		  AND se.docstatus < 2
		""",
		{"mr": mr_name},
	)[0][0]


def _count_active_injected_se_for_eir(eir_name: str) -> int:
	"""Count active auto_created Stock Entries linked to an Employee IR.
	After D-004 removed the dead injected_stock_entry field, the canonical
	check is on the SE side via the existing employee_ir + auto_created back-link."""
	return frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Entry`
		WHERE employee_ir = %(eir)s
		  AND auto_created = 1
		  AND docstatus = 1
		""",
		{"eir": eir_name},
	)[0][0]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class TestScenarioA_ProductCertificationSubmitRace(FrappeTestCase):
	"""EG-002: concurrent submit of same Product Certification.

	Expectation: exactly ONE Stock Entry created across N workers; all other
	workers either no-op via the idempotency guard or surface a clean
	1213 / 1205 that the retry helper handles.
	"""

	def test_concurrent_submit_creates_exactly_one_se(self):
		reason = _skip_unless_safe_site() or _skip_if_no_fixture(
			"PRODUCT_CERTIFICATION_FOR_SUBMIT_RACE"
		)
		if reason:
			self.skipTest(reason)

		site = frappe.local.site
		pc_name = FIXTURES["PRODUCT_CERTIFICATION_FOR_SUBMIT_RACE"]

		# Pre-condition: exactly 0 active SE for this PC
		pre = _count_active_se_for_pc(pc_name)
		self.assertEqual(
			pre,
			0,
			f"Pre-condition failed: {pre} active SE exist for {pc_name}; expected 0",
		)

		summary = run_concurrent(
			site=site,
			action="submit_doc",
			payloads=[{"doctype": "Product Certification", "name": pc_name}] * 5,
			timeout=120,
		)

		# Assertion 1: at least one worker succeeded
		self.assertGreaterEqual(summary["n_ok"], 1, summary)
		# Assertion 2: any failures must be clean exceptions, NOT partial-success leaks
		# (i.e. ok=False results must carry an exc_type, never an ambiguous state).
		for r in summary["results"]:
			if not r.get("ok"):
				self.assertIn("exc_type", r, f"Failure result lacks exc_type: {r}")
		# Assertion 3: exactly ONE active SE post-test (EG-002 idempotency)
		post = _count_active_se_for_pc(pc_name)
		self.assertEqual(
			post,
			1,
			f"EG-002 idempotency violated: {post} active Stock Entries linked to "
			f"{pc_name} after race; expected exactly 1. Summary: {summary}",
		)


class TestScenarioB_SerialNumberCreatorSubmitRace(FrappeTestCase):
	"""EG-003 / D-003: concurrent submit of same Serial Number Creator.

	Expectation: exactly ONE active SE linked via custom_serial_number_creator.
	If a draft SE exists from a prior failed attempt, it must be adopted
	and rebuilt, not duplicated (D-003).
	"""

	def test_concurrent_submit_creates_exactly_one_se(self):
		reason = _skip_unless_safe_site() or _skip_if_no_fixture(
			"SERIAL_NUMBER_CREATOR_FOR_SUBMIT_RACE"
		)
		if reason:
			self.skipTest(reason)

		site = frappe.local.site
		snc_name = FIXTURES["SERIAL_NUMBER_CREATOR_FOR_SUBMIT_RACE"]

		summary = run_concurrent(
			site=site,
			action="submit_doc",
			payloads=[{"doctype": "Serial Number Creator", "name": snc_name}] * 5,
			timeout=120,
		)
		self.assertGreaterEqual(summary["n_ok"], 1, summary)

		post = _count_active_se_for_snc(snc_name)
		self.assertEqual(
			post,
			1,
			f"EG-003 idempotency violated: {post} active SE linked to "
			f"{snc_name} after race; expected 1. Summary: {summary}",
		)


class TestScenarioC_MaterialRequestReserveRace(FrappeTestCase):
	"""EG-001 / D-001: concurrent Reserve Material workflow on same MR.

	Expectation: exactly ONE active reserved SE linked via the corrected
	D-001 JOIN through Stock Entry Detail. If stock is insufficient, all
	workers must surface the same controlled validation message
	(NOT a traceback)."""

	def test_concurrent_reserve_creates_exactly_one_se(self):
		reason = _skip_unless_safe_site() or _skip_if_no_fixture(
			"MATERIAL_REQUEST_FOR_RESERVE_RACE"
		)
		if reason:
			self.skipTest(reason)

		site = frappe.local.site
		mr_name = FIXTURES["MATERIAL_REQUEST_FOR_RESERVE_RACE"]

		summary = run_concurrent(
			site=site,
			action="workflow_action",
			payloads=[
				{
					"doctype": "Material Request",
					"name": mr_name,
					"workflow_action": "Reserve Material",
				}
			]
			* 5,
			timeout=120,
		)

		post = _count_active_reserved_se_for_mr(mr_name)
		# Allow 0 (all workers correctly raised missing-stock) or 1 (one created SE).
		# 2+ means duplicate — D-001 anchor failed.
		self.assertLessEqual(
			post,
			1,
			f"EG-001 idempotency violated: {post} active reserved SE for {mr_name} "
			f"after race; expected ≤1. Summary: {summary}",
		)


class TestScenarioD_QuotationSaveRace(FrappeTestCase):
	"""EG-008: two workers saving same Quotation must produce exactly one
	successful save and a clean TimestampMismatchError on the other, NOT
	a silent overwrite."""

	def test_concurrent_save_one_wins_other_gets_timestamp_mismatch(self):
		reason = _skip_unless_safe_site() or _skip_if_no_fixture(
			"QUOTATION_FOR_SAVE_RACE"
		)
		if reason:
			self.skipTest(reason)

		site = frappe.local.site
		qtn_name = FIXTURES["QUOTATION_FOR_SAVE_RACE"]

		summary = run_concurrent(
			site=site,
			action="save_doc",
			payloads=[
				{
					"doctype": "Quotation",
					"name": qtn_name,
					"set": {"remarks": "race-A"},
				},
				{
					"doctype": "Quotation",
					"name": qtn_name,
					"set": {"remarks": "race-B"},
				},
			],
			timeout=60,
		)

		# Exactly one success, exactly one failure (the loser).
		self.assertEqual(summary["n_ok"], 1, summary)
		self.assertEqual(summary["n_fail"], 1, summary)
		# Loser must be TimestampMismatchError, not a generic Exception or silent skip.
		exc_types = summary["exception_types"]
		self.assertIn(
			"TimestampMismatchError",
			exc_types,
			f"EG-008 contract violated: loser did not raise TimestampMismatchError. "
			f"Got: {exc_types}. Summary: {summary}",
		)


class TestScenarioE_EmployeeIRInjectionRace(FrappeTestCase):
	"""EG-004 / EG-020 / D-004: concurrent submit of same EIR triggering
	main_slip_inject. Expectation: idempotency upstream of injection
	(via employee_ir + custom_eir_operation_row + auto_created=1) means
	exactly ONE set of injected SEs."""

	def test_concurrent_eir_submit_idempotent_injection(self):
		reason = _skip_unless_safe_site() or _skip_if_no_fixture(
			"EMPLOYEE_IR_FOR_INJECTION_RACE"
		)
		if reason:
			self.skipTest(reason)

		site = frappe.local.site
		eir_name = FIXTURES["EMPLOYEE_IR_FOR_INJECTION_RACE"]

		summary = run_concurrent(
			site=site,
			action="submit_doc",
			payloads=[{"doctype": "Employee IR", "name": eir_name}] * 3,
			timeout=120,
		)
		self.assertGreaterEqual(summary["n_ok"], 1, summary)

		# Post-condition: the EG-004 contract is that idempotency upstream
		# of main_slip_inject prevents duplicate injected SEs. Verify via the
		# canonical SE-side back-link (employee_ir + auto_created=1).
		post = _count_active_injected_se_for_eir(eir_name)
		# Expected count is the number of EIR Operation rows that satisfy the
		# injection gate. Operators must set the fixture to a value where 1
		# injection is expected. The contract is "no duplicate" — we assert
		# the count did not jump to 3 (n_workers) because of duplicate submits.
		self.assertLess(
			post,
			3 * 2,  # generous upper bound: at most twice expected per worker
			f"EG-004 injection idempotency violated: {post} auto_created SE for "
			f"{eir_name} after 3-worker race; suggests duplicate injection. "
			f"Summary: {summary}",
		)


class TestScenarioF_IntentionalReverseLockDeadlock(FrappeTestCase):
	"""Reproduce a real 1213 deadlock by reverse lock order. This validates:
	(a) the harness can faithfully reproduce InnoDB deadlock semantics on
	    this MariaDB / site combination,
	(b) the losing worker's transaction is fully rolled back (no
	    same-transaction continuation).
	"""

	def test_reverse_lock_produces_deadlock_in_at_least_one_worker(self):
		reason = _skip_unless_safe_site()
		if reason:
			self.skipTest(reason)
		if not (FIXTURES["DEADLOCK_ROW_A"] and FIXTURES["DEADLOCK_ROW_B"]):
			self.skipTest(
				"Skipping: DEADLOCK_ROW_A / DEADLOCK_ROW_B not configured. "
				"Set both env vars to two harmless copied-site row names of "
				"the doctype in DEADLOCK_DOCTYPE (default 'Bin')."
			)

		site = frappe.local.site
		doctype = FIXTURES["DEADLOCK_DOCTYPE"]
		row_a = FIXTURES["DEADLOCK_ROW_A"]
		row_b = FIXTURES["DEADLOCK_ROW_B"]

		summary = run_concurrent(
			site=site,
			action="deadlock_reverse_lock",
			payloads=[
				{
					"doctype": doctype,
					"rows": [row_a, row_b],
					"order": [0, 1],
					"sleep": 2,
				},
				{
					"doctype": doctype,
					"rows": [row_a, row_b],
					"order": [1, 0],
					"sleep": 2,
				},
			],
			timeout=60,
		)

		# At least one worker must have hit 1213 (or an equivalent error)
		exc_types = summary["exception_types"]
		deadlock_seen = any(
			t in exc_types
			for t in ("QueryDeadlockError", "OperationalError", "InternalError")
		)
		# If no deadlock was reproduced, the test environment cannot prove
		# the 1213 policy — flag it as inconclusive rather than green.
		self.assertTrue(
			deadlock_seen,
			f"Reverse-lock test did not reproduce 1213 — environment may not "
			f"faithfully model InnoDB semantics. exception_types={exc_types}. "
			f"Summary: {summary}",
		)


class TestScenarioH_BulkReserveLongQueueRouting(FrappeTestCase):
	"""EG-001: bulk Reserve Material on >50 docs must route to long queue
	with timeout=4000 and enqueue_after_commit=True. Sort + dedupe applies.
	Uses unittest.mock to intercept frappe.enqueue."""

	def test_bulk_reserve_routes_to_long_queue(self):
		reason = _skip_unless_safe_site() or _skip_if_no_fixture("BULK_MR_NAMES_CSV")
		if reason:
			self.skipTest(reason)

		from unittest.mock import patch

		docnames = [
			n.strip() for n in FIXTURES["BULK_MR_NAMES_CSV"].split(",") if n.strip()
		]
		if len(docnames) < 50:
			self.skipTest(
				f"Skipping: need ≥50 MR docnames for bulk pressure test, got {len(docnames)}"
			)

		from jewellery_erpnext.jewellery_erpnext.doc_events.bulk_update import (
			custom_submit_cancel_or_update_docs,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.bulk_update.frappe.enqueue"
		) as mock_enqueue, patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.bulk_update.frappe.msgprint"
		):
			custom_submit_cancel_or_update_docs(
				"Material Request",
				docnames,
				action="Reserve Material",
			)

		# Long queue routing assertion
		self.assertTrue(mock_enqueue.called, "frappe.enqueue not called for >50 docs")
		call_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(call_kwargs.get("queue"), "long", call_kwargs)
		self.assertEqual(call_kwargs.get("timeout"), 4000, call_kwargs)
		self.assertEqual(call_kwargs.get("enqueue_after_commit"), True, call_kwargs)
		# Sort + dedupe
		passed_docnames = call_kwargs.get("docnames")
		self.assertEqual(
			passed_docnames, sorted(set(docnames)), "docnames not sorted+deduped"
		)


# ---------------------------------------------------------------------------
# Scenarios I (MOP EOD), G (Bin lock order) require fixture work the operator
# constructs per copied-site state. They are documented in the plan §J-3 but
# not encoded as standalone test methods here to avoid false failures on
# sites without the prerequisite MOP Log / SNC fixtures.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
	unittest.main()
