"""Phase K-4 — Query-count performance smoke tests.

These tests wrap ``frappe.db.sql`` in a counter, run a representative flow
once with a fixture, and assert the SQL call count stays below a threshold
derived from the K-3 baseline. The thresholds catch performance regressions
without imposing brittle wall-clock assertions on shared CI.

Thresholds are set generously (baseline + 20 % slack) so noise doesn't
flake the test. They serve as early-warning canaries — a regression doubles
the count, not a 10 % drift.

Each test SKIPS cleanly if its fixture env var is unset, so the module
can run on any environment without false failures.
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

import frappe
from frappe.tests.utils import FrappeTestCase


@contextmanager
def count_sql_calls():
	"""Context manager that counts every ``frappe.db.sql`` invocation."""
	original = frappe.db.sql
	counter = {"n": 0}

	def _counter(*args, **kwargs):
		counter["n"] += 1
		return original(*args, **kwargs)

	frappe.db.sql = _counter
	try:
		yield counter
	finally:
		frappe.db.sql = original


def _skip_unless_fixture(env_var: str):
	"""Return a skip reason if the fixture env var is empty."""
	value = os.environ.get(env_var, "")
	if not value:
		return f"Skipping: env var {env_var} not set"
	return None


class TestMRReserveQueryCount(FrappeTestCase):
	"""K-3 candidate 1: `material_request.py:480` Warehouse N+1 batching.

	Baseline (operator measures): 2 DB calls × N items + ~20 overhead.
	For a 20-item MR: ~60 queries before batching, ~25 after.
	Threshold: set to `baseline + 20%` after measurement.
	"""

	def test_create_stock_entry_query_count(self):
		mr_name = os.environ.get("JEW_TEST_MR_NAME", "")
		threshold = int(os.environ.get("JEW_TEST_MR_RESERVE_QUERY_THRESHOLD", "0"))
		if not mr_name or threshold <= 0:
			self.skipTest(
				"Skipping: set JEW_TEST_MR_NAME and JEW_TEST_MR_RESERVE_QUERY_THRESHOLD "
				"(integer, baseline+20%) after running K-3 measurement."
			)

		from jewellery_erpnext.jewellery_erpnext.doc_events.material_request import (
			create_stock_entry,
		)

		mr_doc = frappe.get_doc("Material Request", mr_name)
		# Force the workflow state the hook checks
		mr_doc.workflow_state = "Material Reserved"
		mr_doc.custom_reserve_se = None

		with count_sql_calls() as counter:
			create_stock_entry(mr_doc, method="on_update_after_submit")

		self.assertLess(
			counter["n"],
			threshold,
			f"MR Reserve query count regressed: {counter['n']} >= {threshold}. "
			"Check whether a recent change reintroduced N+1 in the items loop.",
		)


class TestPCSubmitQueryCount(FrappeTestCase):
	"""K-3 candidate 2: `product_certification.py:170` (9 DB calls/row).

	Highest per-row count among scanned flows. Operator must batch the
	hottest of those 9 lookups (likely Item + Warehouse + Manufacturing
	Operation) before declaring K-3 PASS for PC submit.
	"""

	def test_create_stock_entry_query_count(self):
		pc_name = os.environ.get("JEW_TEST_PC_NAME", "")
		threshold = int(os.environ.get("JEW_TEST_PC_SUBMIT_QUERY_THRESHOLD", "0"))
		if not pc_name or threshold <= 0:
			self.skipTest(
				"Skipping: set JEW_TEST_PC_NAME and JEW_TEST_PC_SUBMIT_QUERY_THRESHOLD "
				"(integer, baseline+20%) after running K-3 measurement."
			)

		from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification import (
			create_stock_entry,
		)

		pc_doc = frappe.get_doc("Product Certification", pc_name)
		with count_sql_calls() as counter:
			create_stock_entry(pc_doc)

		self.assertLess(
			counter["n"],
			threshold,
			f"PC submit query count regressed: {counter['n']} >= {threshold}. "
			"Check whether the product_details loop reintroduced N+1.",
		)


class TestSNCSubmitQueryCount(FrappeTestCase):
	"""K-3 candidate 3: `serial_number_creator.py:758` balance_rows loop.

	2 DB calls/row. Moderate impact. Enable this canary only if K-3
	measurement on a representative SNC shows >100 ms in the balance_rows
	loop."""

	def test_to_prepare_data_query_count(self):
		snc_name = os.environ.get("JEW_TEST_SNC_NAME", "")
		threshold = int(os.environ.get("JEW_TEST_SNC_SUBMIT_QUERY_THRESHOLD", "0"))
		if not snc_name or threshold <= 0:
			self.skipTest(
				"Skipping: set JEW_TEST_SNC_NAME and JEW_TEST_SNC_SUBMIT_QUERY_THRESHOLD "
				"(integer, baseline+20%) after running K-3 measurement."
			)

		from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
			to_prepare_data_for_make_mnf_stock_entry,
		)

		snc_doc = frappe.get_doc("Serial Number Creator", snc_name)
		with count_sql_calls() as counter:
			to_prepare_data_for_make_mnf_stock_entry(snc_doc)

		self.assertLess(
			counter["n"],
			threshold,
			f"SNC submit query count regressed: {counter['n']} >= {threshold}. "
			"Check whether the source_table or balance_rows loop reintroduced N+1.",
		)


class TestQueryCountHelperItself(FrappeTestCase):
	"""Verify the `count_sql_calls` helper itself works — a meta-test that
	ensures K-4 canary tests can run without fixture setup."""

	def test_counter_increments_on_sql_call(self):
		with count_sql_calls() as counter:
			frappe.db.sql("SELECT 1")
			frappe.db.sql("SELECT 2")
		self.assertEqual(counter["n"], 2)

	def test_counter_zero_without_calls(self):
		with count_sql_calls() as counter:
			pass
		self.assertEqual(counter["n"], 0)

	def test_original_sql_restored_after_context_exit(self):
		original = frappe.db.sql
		with count_sql_calls():
			pass
		self.assertIs(frappe.db.sql, original)


if __name__ == "__main__":
	unittest.main()
