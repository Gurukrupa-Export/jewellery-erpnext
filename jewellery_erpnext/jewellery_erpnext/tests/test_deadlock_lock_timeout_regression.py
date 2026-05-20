"""Phase M-5 — Per-EG deadlock/lock-timeout regression tests.

One test class per EG-001..EG-020 plus EG-LOCK-OTHER and EG-DEADLOCK-OTHER.
Each test class regression-guards a specific Phase B/D/E/H/I patch.

SAFETY: every test method either:
  (a) runs as a pure unit test (mocked Frappe, no DB), OR
  (b) requires copied-site fixtures via env vars and SKIPS when absent.

Production-like site names refused via Phase J ``_assert_safe_site`` for any
test that calls into ``run_concurrent``. Pure-logic tests don't need a site.

Operator runs:
  bench --site <COPY-SITE> run-tests \\
    --module jewellery_erpnext.jewellery_erpnext.tests.test_deadlock_lock_timeout_regression
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.tests.concurrency_harness import (
	_SAFE_SITE_RE,
	_assert_safe_site,
	run_concurrent,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _skip_unless_safe_site():
	site = getattr(frappe.local, "site", "") or ""
	if os.environ.get("ALLOW_JEWELLERY_CONCURRENCY_TESTS") == "1":
		return None
	if not site or not _SAFE_SITE_RE.search(site):
		return f"Skipping: site {site!r} is not a copied/staging site"
	return None


def _skip_if_no_env(var: str):
	v = os.environ.get(var, "")
	if not v:
		return f"Skipping: env var {var} not set"
	return None


# ---------------------------------------------------------------------------
# EG-001A — MR Reserve Material validation (controlled-validation, not race)
# ---------------------------------------------------------------------------


class TestEG001A_ReserveMaterialValidation(FrappeTestCase):
	"""EG-001A: missing stock / zero valuation surface as row-level
	``frappe.ValidationError`` with item + warehouse in the message — NOT a
	traceback. Phase B preserved ``se_doc.flags.throw_batch_error = True``."""

	def test_throw_batch_error_flag_is_set(self):
		# Pure-source assertion: the production code path sets the flag.
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doc_events/material_request.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"throw_batch_error = True",
			src,
			"EG-001A regression: throw_batch_error flag missing from material_request.py — "
			"row-level diagnostics will be lost",
		)


# ---------------------------------------------------------------------------
# EG-001B — MR Reserve Material lock-wait-timeout
# ---------------------------------------------------------------------------


class TestEG001B_ReserveMaterialLockTimeout(FrappeTestCase):
	"""EG-001B: under contention, the D-001 JOIN idempotency lookup
	guarantees ≤1 active reserved SE per MR. Concurrent same-MR Reserve
	is tested by Phase J Scenario C; this class adds a static assertion
	that the D-001 JOIN is present."""

	def test_d001_join_present(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doc_events/material_request.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"JOIN `tabStock Entry Detail`",
			src,
			"D-001 fix missing: the idempotency lookup must JOIN through "
			"tabStock Entry Detail because material_request is a child-table "
			"column, not a parent column",
		)
		self.assertIn(
			"sed.material_request = %(mr)s",
			src,
			"D-001 fix incomplete: JOIN filter must reference sed.material_request",
		)

	def test_concurrent_reserve_via_harness(self):
		reason = _skip_unless_safe_site() or _skip_if_no_env("JEW_TEST_MR_NAME")
		if reason:
			self.skipTest(reason)
		summary = run_concurrent(
			site=frappe.local.site,
			action="reserve_material",
			payloads=[{"mr_name": os.environ["JEW_TEST_MR_NAME"]}] * 5,
			timeout=120,
		)
		# At most 1 active reserved SE post-race (per D-001 JOIN).
		cnt = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT se.name) FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
			WHERE sed.material_request = %(mr)s
			  AND se.add_to_transit = 1 AND se.docstatus < 2
			""",
			{"mr": os.environ["JEW_TEST_MR_NAME"]},
		)[0][0]
		self.assertLessEqual(
			cnt, 1, f"EG-001B: {cnt} duplicate reserved SEs. Summary: {summary}"
		)


# ---------------------------------------------------------------------------
# EG-002 — Product Certification lock timeout
# ---------------------------------------------------------------------------


class TestEG002_ProductCertificationLockTimeout(FrappeTestCase):
	"""EG-002: idempotency via Stock Entry.product_certification + anchor
	write before submit. Phase B + Phase H D-002."""

	def test_idempotency_lookup_in_create_stock_entry(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/product_certification/product_certification.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			'"product_certification": doc.name',
			src,
			"EG-002 idempotency lookup missing",
		)
		self.assertIn(
			"submit_with_retry(se_doc)",
			src,
			"EG-002 must submit via submit_with_retry helper",
		)

	def test_receive_path_idempotency_d002(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/product_certification/doc_events/utils.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"D-002",
			src,
			"D-002 Receive-path idempotency comment missing — patch may be reverted",
		)


# ---------------------------------------------------------------------------
# EG-003 — Serial Number Creator lock timeout + D-003 draft adoption
# ---------------------------------------------------------------------------


class TestEG003_SerialNumberCreatorLockTimeout(FrappeTestCase):
	"""EG-003: deterministic Bin loop sort + custom_serial_number_creator
	idempotency + D-003 draft adoption."""

	def test_uses_existing_custom_serial_number_creator(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/serial_number_creator/serial_number_creator.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			'"custom_serial_number_creator": self.name',
			src,
			"EG-003 idempotency lookup missing",
		)
		self.assertIn("D-003", src, "D-003 draft-adoption flag missing")
		self.assertIn(
			"ordered_source_rows = sorted", src, "EG-003 deterministic sort missing"
		)

	def test_no_rogue_serial_number_creator_field(self):
		"""D-004 cleanup: there must be NO Stock Entry.serial_number_creator field.
		Reuse the existing custom_serial_number_creator field instead."""
		import json

		cf_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"custom_fields/stock_entry.json"
		)
		with open(cf_path) as f:
			data = json.load(f)
		bad = [
			r for r in data["Stock Entry"] if r["fieldname"] == "serial_number_creator"
		]
		self.assertEqual(
			bad,
			[],
			"D-004 cleanup reverted: rogue Stock Entry.serial_number_creator field reintroduced",
		)


# ---------------------------------------------------------------------------
# EG-004 — Employee IR main_slip_inject deadlock
# ---------------------------------------------------------------------------


class TestEG004_EmployeeIRMainSlipInjectDeadlock(FrappeTestCase):
	"""EG-004: per-segment warehouse honor + idempotency via auto_created."""

	def test_per_segment_warehouse_honored(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/employee_ir/doc_events/main_slip_inject.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			'seg.get("s_warehouse") or source_wh',
			src,
			"EG-004 per-segment s_warehouse honoring missing — would collapse multi-WH "
			"injects onto single Bin (1213 risk)",
		)
		self.assertIn(
			'seg.get("t_warehouse") or dept_wh',
			src,
			"EG-004 per-segment t_warehouse honoring missing",
		)

	def test_segment_merge_full_key(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/employee_ir/doc_events/main_slip_inject.py"
		)
		with open(src_path) as f:
			src = f.read()
		# The full-key merge must include item, batch, source wh, target wh, inventory_type.
		self.assertIn('seg.get("s_warehouse")', src)
		self.assertIn('seg.get("t_warehouse")', src)
		self.assertIn('seg.get("batch_no")', src)
		self.assertIn('seg.get("inventory_type")', src)


# ---------------------------------------------------------------------------
# EG-005 — Naming Rule pressure (monitoring only)
# ---------------------------------------------------------------------------


class TestEG005_NamingRulePressure(FrappeTestCase):
	def test_monitoring_only(self):
		self.skipTest(
			"EG-005 is monitoring-only — naming-counter contention drops naturally "
			"once EG-001/002/003 stop duplicate document creation. No reproducible "
			"unit test; operator inspects post-deploy slow-query log."
		)


# ---------------------------------------------------------------------------
# EG-006 — PMO gemstone variant missing attributes
# ---------------------------------------------------------------------------


class TestEG006_PMOGemstoneVariant(FrappeTestCase):
	def test_resolver_throws_with_forensics(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/parent_manufacturing_order/parent_manufacturing_order.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"frappe.as_json(dict(row))",
			src,
			"EG-006 resolver missing row.as_dict() forensics in throw",
		)
		self.assertIn(
			"pmo_name=self.name", src, "EG-006 caller missing pmo_name context"
		)
		self.assertIn("bom_name=bom", src, "EG-006 caller missing bom_name context")


# ---------------------------------------------------------------------------
# EG-007 — Quotation Making Charge Price preflight
# ---------------------------------------------------------------------------


class TestEG007_QuotationMakingChargePreflight(FrappeTestCase):
	def test_throw_includes_gold_rate_bucket(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doc_events/bom_utils.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"Gold Rate Bucket",
			src,
			"EG-007 throw message missing gold-rate bucket context",
		)
		self.assertIn("Create a valid Making Charge Price", src)


# ---------------------------------------------------------------------------
# EG-008 — Quotation TimestampMismatch + job_id dedupe
# ---------------------------------------------------------------------------


class TestEG008_QuotationTimestampMismatch(FrappeTestCase):
	def test_enqueue_passes_docname_not_doc(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doc_events/quotation.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"quotation_name=self.name",
			src,
			"EG-008 enqueue must pass docname, not stale doc",
		)
		self.assertIn(
			'job_id=f"qtn-bom-{self.name}"',
			src,
			"EG-008 job_id deterministic key missing",
		)
		self.assertIn(
			"deduplicate=True",
			src,
			"EG-008 deduplicate flag missing (verified supported in Frappe v16)",
		)
		self.assertIn(
			"frappe.TimestampMismatchError",
			src,
			"EG-008 worker reload-once on TimestampMismatch missing",
		)


# ---------------------------------------------------------------------------
# EG-009 — MR Transfer to MOP validator message
# ---------------------------------------------------------------------------


class TestEG009_MRTransferToMOPMessage(FrappeTestCase):
	def test_message_includes_workflow_state(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doc_events/material_request.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"Material Transferred to MOP",
			src,
			"EG-009 message must reference the workflow state by name",
		)
		self.assertIn(
			"custom_manufacturing_operation",
			src,
			"EG-009 must point at the required field",
		)


# ---------------------------------------------------------------------------
# EG-010 — MOP EOD ImportError (restored function)
# ---------------------------------------------------------------------------


class TestEG010_MOPEODImportSmoke(FrappeTestCase):
	def test_function_exists(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		self.assertTrue(callable(get_loss_item_from_manufacturer_mapping))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
		side_effect=[None],
	)
	def test_d006_throws_on_no_variant_of(self, _mock):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			get_loss_item_from_manufacturer_mapping("X-NOT-A-VARIANT", "MFG")
		self.assertIn("variant template", str(ctx.exception).lower())

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
		side_effect=["M", None],
	)
	def test_d005_throws_on_missing_mapping(self, _mock):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			get_loss_item_from_manufacturer_mapping("M-X", "Manufacturer")
		self.assertIn("Manufacturer", str(ctx.exception))


# ---------------------------------------------------------------------------
# EG-011 — MOP EOD stock mismatch audit
# ---------------------------------------------------------------------------


class TestEG011_MOPEODStockMismatchAudit(FrappeTestCase):
	def test_audit_function_exists(self):
		from jewellery_erpnext.mop_lineage_audit import audit_mop_log_vs_physical

		self.assertTrue(callable(audit_mop_log_vs_physical))

	def test_per_mwo_aggregation_present(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/mop_settings/mop_eod_sync.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"failures_by_mwo",
			src,
			"EG-011 per-MWO failure aggregation missing — log_error spam will return",
		)


# ---------------------------------------------------------------------------
# EG-012 — Sales Order UnboundLocalError guard
# ---------------------------------------------------------------------------


class TestEG012_SalesOrderUnboundLocal(FrappeTestCase):
	def test_guard_before_doc_block(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doc_events/sales_order.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"if not row.serial_no or not row.bom:",
			src,
			"EG-012 guard missing — UnboundLocalError will recur",
		)


# ---------------------------------------------------------------------------
# EG-013 — Query Report %d format (needs DB evidence)
# ---------------------------------------------------------------------------


class TestEG013_QueryReportPercentFormat(FrappeTestCase):
	def test_no_percent_d_in_custom_report_files(self):
		# Scan custom report .py + .sql files for raw %d patterns.
		import glob
		import re

		patterns = re.compile(r"%d\b")
		matches = []
		for f in glob.glob(
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/report/**/*.sql",
			recursive=True,
		) + glob.glob(
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/report/**/*.py",
			recursive=True,
		):
			with open(f) as fp:
				if patterns.search(fp.read()):
					matches.append(f)
		self.assertEqual(
			matches,
			[],
			f"EG-013: found %d format in custom report files: {matches}. "
			"Replace with named params (%(name)s) and pass dict args.",
		)


# ---------------------------------------------------------------------------
# EG-014 — Serial and Batch Bundle posting_date guards
# ---------------------------------------------------------------------------


class TestEG014_SBBPostingDateGuards(FrappeTestCase):
	def test_pc_create_stock_entry_sets_posting_date(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/product_certification/product_certification.py"
		)
		with open(src_path) as f:
			src = f.read()
		# Accept either idiom: ``X = X or today()`` (utils.py style) OR
		# ``if not X: X = today()`` (product_certification.py style).
		# Both are semantically equivalent.
		one_liner = "se_doc.posting_date = se_doc.posting_date or frappe.utils.today()"
		if_form = (
			"if not se_doc.posting_date:" in src
			and "se_doc.posting_date = frappe.utils.today()" in src
		)
		self.assertTrue(
			one_liner in src or if_form,
			"EG-014 PC posting_date guard missing — neither one-liner nor if-form "
			"pattern found in product_certification.py",
		)

	def test_pc_utils_sets_posting_date(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/product_certification/doc_events/utils.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertEqual(
			src.count("se_doc.posting_date = se_doc.posting_date or"),
			2,
			"EG-014 utils.py must set posting_date at BOTH SE construction sites",
		)

	def test_main_slip_sets_posting_date(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/main_slip/main_slip.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"se_doc.posting_date = se_doc.posting_date or frappe.utils.today()",
			src,
			"EG-014 main_slip posting_date guard missing",
		)


# ---------------------------------------------------------------------------
# EG-015 — Employee IR None comparison
# ---------------------------------------------------------------------------


class TestEG015_EmployeeIRNoneComparison(FrappeTestCase):
	def test_flt_guard_in_get_loss_details(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/employee_ir/doc_events/validation_utils.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"flt(row.received_gross_wt) > flt(row.gross_wt)",
			src,
			"EG-015 flt guard missing — TypeError will recur",
		)


# ---------------------------------------------------------------------------
# EG-016 — MWO pending department diagnostic
# ---------------------------------------------------------------------------


class TestEG016_MWODiagnosticMessage(FrappeTestCase):
	def test_message_names_blocking_siblings(self):
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/manufacturing_work_order/manufacturing_work_order.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn(
			"Blocked by: {3}",
			src,
			"EG-016 diagnostic must include blocking-siblings list",
		)


# ---------------------------------------------------------------------------
# EG-017 / EG-018 / EG-019 — Runbook-only (no app code)
# ---------------------------------------------------------------------------


class TestEG017_COSEC_Runbook(FrappeTestCase):
	def test_no_cosec_code_in_app(self):
		import subprocess

		# Exclude the tests/ directory: this test file itself contains the
		# literal string "cosec" inside a grep command, which would create
		# a circular self-match.
		r = subprocess.run(
			[
				"grep",
				"-rni",
				"--include=*.py",
				"--exclude-dir=tests",
				"cosec",
				"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/",
			],
			capture_output=True,
			text=True,
		)
		hits = [line for line in r.stdout.splitlines() if "cosec" in line.lower()]
		self.assertEqual(
			hits,
			[],
			f"EG-017 runbook violated: jewellery_erpnext production code "
			f"contains COSEC integration: {hits}",
		)


class TestEG018_GST_Runbook(FrappeTestCase):
	def test_runbook_only(self):
		self.skipTest(
			"EG-018 is site_config encryption_key runbook only — no app code change. "
			"Operator verifies via bench --site SITE set-config encryption_key '<key>'."
		)


class TestEG019_Email_Runbook(FrappeTestCase):
	def test_no_sendmail_in_app(self):
		import subprocess

		# Exclude tests/ — this test file itself contains the literal
		# string "frappe.sendmail" inside a grep command.
		r = subprocess.run(
			[
				"grep",
				"-rn",
				"--include=*.py",
				"--exclude-dir=tests",
				"frappe.sendmail",
				"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/",
			],
			capture_output=True,
			text=True,
		)
		hits = [line for line in r.stdout.splitlines() if "frappe.sendmail" in line]
		self.assertEqual(
			hits,
			[],
			f"EG-019 runbook violated: jewellery_erpnext production code "
			f"contains direct sendmail calls: {hits}",
		)


# ---------------------------------------------------------------------------
# EG-020 — Main slip inject behavior (per-seg warehouse + MOP Log contract)
# ---------------------------------------------------------------------------


class TestEG020_MainSlipInjectPerSegWarehouse(FrappeTestCase):
	def test_builder_signature_honors_per_segment(self):
		# Same assertion as EG-004 but exercised by a different scenario
		# (EG-020 is the umbrella for the project-analysis-flagged regression).
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/employee_ir/doc_events/main_slip_inject.py"
		)
		with open(src_path) as f:
			src = f.read()
		# Both builders must honor per-seg warehouses
		self.assertEqual(
			src.count('seg.get("s_warehouse") or source_wh'),
			2,
			"EG-020: both _build_material_transfer_from_segments and "
			"_build_repack_from_purity_segments must honor per-seg s_warehouse",
		)


# ---------------------------------------------------------------------------
# EG-LOCK-OTHER — Department IR + other 1205
# ---------------------------------------------------------------------------


class TestEG_LOCK_OTHER_DepartmentIR(FrappeTestCase):
	def test_safe_submit_helper_handles_1205(self):
		"""The retry helper handles 1205 with savepoint+rollback+reload.
		Department IR paths that go through submit_with_retry inherit this."""
		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"utils/safe_submit.py"
		)
		with open(src_path) as f:
			src = f.read()
		self.assertIn("except QueryTimeoutError", src)
		self.assertIn("frappe.db.rollback(save_point=sp)", src)
		self.assertIn(
			"time.sleep",
			src,
			"1205 backoff must include sleep — instant retry burns CPU",
		)
		self.assertIn(
			"frappe.get_doc(doc.doctype, doc.name)",
			src,
			"1205 retry must reload the doc to clear stale state",
		)


# ---------------------------------------------------------------------------
# EG-DEADLOCK-OTHER — non-EG-004 1213 paths
# ---------------------------------------------------------------------------


class TestEG_DEADLOCK_OTHER_NoSavepointRetryOn1213(FrappeTestCase):
	def test_1213_path_does_not_touch_savepoint(self):
		"""1213 means InnoDB rolled back the WHOLE transaction. The savepoint
		is gone. Any catch site that calls rollback(save_point=...) inside
		except QueryDeadlockError would be unsafe."""
		import re

		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"utils/safe_submit.py"
		)
		with open(src_path) as f:
			src = f.read()
		# Find the QueryDeadlockError except block and check it does NOT
		# contain rollback(save_point=...).
		match = re.search(r"except QueryDeadlockError.*?(?=except|def |\Z)", src, re.S)
		self.assertIsNotNone(match, "QueryDeadlockError except block not found")
		block = match.group(0)
		self.assertNotIn(
			"rollback(save_point=",
			block,
			"EG-DEADLOCK-OTHER UNSAFE_1213_SAVEPOINT_RETRY pattern detected — "
			"1213 must re-raise, never savepoint-retry",
		)
		self.assertIn("raise", block, "1213 must re-raise")

	def test_snc_bin_loop_1213_path_does_not_touch_savepoint(self):
		"""Same assertion for the inline Bin loop in serial_number_creator."""
		import re

		src_path = (
			"apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
			"doctype/serial_number_creator/serial_number_creator.py"
		)
		with open(src_path) as f:
			src = f.read()
		match = re.search(
			r"except frappe\.exceptions\.QueryDeadlockError:.*?(?=except|def |\Z)",
			src,
			re.S,
		)
		self.assertIsNotNone(match)
		block = match.group(0)
		self.assertNotIn(
			"rollback(save_point=",
			block,
			"EG-003 Bin loop: 1213 must re-raise, not savepoint-retry",
		)


# ---------------------------------------------------------------------------
# Smoke — meta-tests that verify the test infra itself
# ---------------------------------------------------------------------------


class TestPhaseMSmoke(FrappeTestCase):
	def test_all_eg_classes_present(self):
		"""Sanity: this module defines a test class for every EG that has
		one in the plan §M-5 table."""
		import sys

		mod = sys.modules[__name__]
		expected = {
			"TestEG001A_ReserveMaterialValidation",
			"TestEG001B_ReserveMaterialLockTimeout",
			"TestEG002_ProductCertificationLockTimeout",
			"TestEG003_SerialNumberCreatorLockTimeout",
			"TestEG004_EmployeeIRMainSlipInjectDeadlock",
			"TestEG005_NamingRulePressure",
			"TestEG006_PMOGemstoneVariant",
			"TestEG007_QuotationMakingChargePreflight",
			"TestEG008_QuotationTimestampMismatch",
			"TestEG009_MRTransferToMOPMessage",
			"TestEG010_MOPEODImportSmoke",
			"TestEG011_MOPEODStockMismatchAudit",
			"TestEG012_SalesOrderUnboundLocal",
			"TestEG013_QueryReportPercentFormat",
			"TestEG014_SBBPostingDateGuards",
			"TestEG015_EmployeeIRNoneComparison",
			"TestEG016_MWODiagnosticMessage",
			"TestEG017_COSEC_Runbook",
			"TestEG018_GST_Runbook",
			"TestEG019_Email_Runbook",
			"TestEG020_MainSlipInjectPerSegWarehouse",
			"TestEG_LOCK_OTHER_DepartmentIR",
			"TestEG_DEADLOCK_OTHER_NoSavepointRetryOn1213",
		}
		actual = {name for name in dir(mod) if name.startswith("TestEG")}
		missing = expected - actual
		self.assertEqual(
			missing, set(), f"Phase M-5 EG test classes missing: {sorted(missing)}"
		)


if __name__ == "__main__":
	unittest.main()
