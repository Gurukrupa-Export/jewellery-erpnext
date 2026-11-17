# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Two-connection check for the after-commit MSL refresh (incident plan T-08).

Meant for the throwaway site ``kggk-pi-test.localhost`` (restored from a production
backup, so it has real employee MSL warehouses with ledger history). It needs two DB
connections and one real commit: the refresh job rewrites the chosen warehouse's
``custom_msl_tracking``, a derived cache that is recomputed from the ledger, so the
committed rows match what the Warehouse button would write.

Guard: skipped unless ``allow_tests`` is set and the site is neither
``kggk-prod.localhost`` nor ``kggk-uat.localhost``; also skipped when the site has no
employee Raw Material warehouse with stock ledger entries.

Connection B holds the Warehouse row FOR UPDATE, as a concurrent Warehouse save or
refresh would; connection A runs with a 2 s session lock wait:
  * before the fix, the recompute ran inside the submit: it waits on that row and gets
    a 1205, which the old ``except Exception`` swallowed (E-W708);
  * now the submit side returns at once and never touches the row;
  * after B releases, the job writes exactly the full recompute from the ledger.
"""

import time
import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, get_first_day, getdate, today

from jewellery_erpnext.jewellery_erpnext.doc_events import (
	warehouse_tracking as wt,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

INTENDED_SITE = "kggk-pi-test.localhost"
REFUSED_SITES = ("kggk-prod.localhost", "kggk-uat.localhost")
LOCK_WAIT = 2

_RESOLVE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events."
	"main_slip_inject._resolve_source_warehouse_raw_material"
)


class _NoRedisCache:
	"""Keeps the job to one deterministic pass whether or not Redis is running."""

	def set_value(self, *args, **kwargs):
		pass

	def get_value(self, *args, **kwargs):
		return None

	def delete_value(self, *args, **kwargs):
		pass


def _expected_rows(warehouse):
	"""Same two ledger views the Warehouse grid is built from."""
	prec = wt._tracking_precision()
	month_start = get_first_day(getdate(today()))
	month = {
		r["item_code"]: r
		for r in wt.get_warehouse_item_tracking(
			{"warehouse": warehouse, "from_date": month_start}
		)
	}
	total = {
		r["item_code"]: r
		for r in wt.get_warehouse_item_tracking({"warehouse": warehouse})
	}
	rows = []
	for item_code in sorted(set(month) | set(total)):
		m, t = month.get(item_code) or {}, total.get(item_code) or {}
		pending = flt(t.get("pending_qty"), prec)
		issue = flt(m.get("issue_qty"), prec)
		receive = flt(m.get("receive_qty"), prec)
		loss = flt(m.get("loss_qty"), prec)
		opening = flt(pending - (issue - receive - loss), prec)
		if any((opening, issue, receive, loss, pending)):
			rows.append((item_code, opening, issue, receive, loss, pending))
	return rows


class TestMslRefreshOutsideSubmitLocks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		site = getattr(frappe.local, "site", None)
		if not frappe.conf.allow_tests or site in REFUSED_SITES:
			raise unittest.SkipTest(
				f"two-connection test: run on {INTENDED_SITE} (allow_tests), not {site}"
			)
		super().setUpClass()
		found = frappe.db.sql(
			"""
			SELECT w.name FROM `tabWarehouse` w
			WHERE IFNULL(w.employee, '') != '' AND w.warehouse_type = 'Raw Material'
				AND w.disabled = 0
				AND EXISTS (
					SELECT 1 FROM `tabStock Ledger Entry` sle
					WHERE sle.warehouse = w.name AND sle.is_cancelled = 0
				)
			ORDER BY w.name
			LIMIT 1
			"""
		)
		if not found:
			raise unittest.SkipTest("no employee MSL warehouse with ledger entries")
		cls.warehouse = found[0][0]

	def setUp(self):
		super().setUp()
		with self.primary_connection():
			previous = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
			frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (LOCK_WAIT,))

		def _restore():
			with self.primary_connection():
				frappe.db.rollback()
				frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (previous,))

		self.addCleanup(_restore)

	def _hold_warehouse_row(self):
		with self.secondary_connection():
			frappe.db.sql(
				"SELECT name FROM `tabWarehouse` WHERE name = %s FOR UPDATE",
				(self.warehouse,),
			)

	def _release_warehouse_row(self):
		with self.secondary_connection():
			frappe.db.rollback()

	def test_inline_recompute_waits_on_the_warehouse_row(self):
		"""The pre-fix path: recomputing inside the submit transaction blocks on the
		Warehouse row and ends in a 1205 (which the old code logged and swallowed)."""
		self._hold_warehouse_row()
		try:
			with self.primary_connection():
				start = time.monotonic()
				with self.assertRaises(frappe.QueryTimeoutError):
					wt.recalculate_msl_tracking(self.warehouse)
				self.assertGreaterEqual(time.monotonic() - start, LOCK_WAIT - 0.2)
				frappe.db.rollback()
		finally:
			self._release_warehouse_row()

	def test_submit_side_never_touches_the_warehouse_row(self):
		self._hold_warehouse_row()
		try:
			with self.primary_connection():
				frappe.db.after_commit.reset()
				with patch(_RESOLVE, return_value=self.warehouse), patch(
					"frappe.enqueue"
				) as enqueue:
					start = time.monotonic()
					EmployeeIR.__new__(EmployeeIR)._refresh_msl_tracking()
					elapsed = time.monotonic() - start
					self.assertLess(elapsed, 0.5)
					self.assertEqual(len(frappe.db.after_commit._functions), 1)
					frappe.db.rollback()  # the submit fails: nothing is scheduled
					self.assertEqual(len(frappe.db.after_commit._functions), 0)
				enqueue.assert_not_called()
		finally:
			self._release_warehouse_row()

	def test_job_after_release_writes_the_full_recompute(self):
		"""Nothing holds the row now (each test releases it): the job's committed rows
		equal a fresh build from the ledger."""
		with self.primary_connection():
			with patch.object(frappe, "cache", _NoRedisCache()):
				wt.refresh_msl_tracking_job(self.warehouse)  # commits
			expected = _expected_rows(self.warehouse)
			doc = frappe.get_doc("Warehouse", self.warehouse)
			prec = wt._tracking_precision()
			actual = sorted(
				(
					r.item_code,
					flt(r.opening_qty, prec),
					flt(r.issue_qty, prec),
					flt(r.receive_qty, prec),
					flt(r.loss_qty, prec),
					flt(r.pending_qty, prec),
				)
				for r in doc.get("custom_msl_tracking") or []
			)
		self.assertEqual(actual, expected)
