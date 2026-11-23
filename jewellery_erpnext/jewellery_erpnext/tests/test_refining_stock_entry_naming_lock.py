# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Two-connection lock test for the refining Stock Entry naming series.

With the series on, a refining Stock Entry gets its name while another transaction holds
``tabSeries['MAT-STE-']``, and a plain Stock Entry gets its name while another transaction
holds the refining row. Controls show the harness does see a block when there is one.

Runs ONLY on the throwaway site ``kggk-pi-test.localhost`` and is skipped everywhere else:
it takes real row locks on ``tabSeries``, and naming a Stock Entry increments (or, the first
time, inserts) a counter row. Every transaction is rolled back; nothing is committed::

    bench --site kggk-pi-test.localhost run-tests \\
        --module jewellery_erpnext.jewellery_erpnext.tests.test_refining_stock_entry_naming_lock

Each case names Stock Entries through frappe's ``set_new_name`` -- the Document Naming Rule
check and then ``getseries`` ``FOR UPDATE``, the first thing ``insert()`` does -- on a second
connection with a short lock-wait timeout, while the test's own connection holds a row.
"""

import threading
import time
import unittest

import frappe
from frappe.model.naming import getseries, set_new_name
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.refining import stock_entry_naming as naming_mod

TEST_SITE = "kggk-pi-test.localhost"
COMPANY = "KG GK Jewellers Private Limited"
SERIES = "RFN-STE-"
DEFAULT_ROW = "MAT-STE-"
#: Lock-wait timeout (seconds) for the second connection.
WAIT_S = 3


def _name_on_second_connection(site, sites_path, series, stock_entry_types):
	"""Name one Stock Entry per type in ONE transaction on a fresh connection, then roll
	back. Returns ``{"names", "error", "elapsed"}``."""
	result = {"names": None, "error": None, "elapsed": None}

	def run():
		frappe.init(site=site, sites_path=sites_path)
		started = time.monotonic()
		try:
			frappe.connect()
			frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (WAIT_S,))
			if series:
				frappe.local.conf[naming_mod.CONFIG_KEY] = series
			else:
				frappe.local.conf.pop(naming_mod.CONFIG_KEY, None)
			names = []
			for stock_entry_type in stock_entry_types:
				se = frappe.new_doc("Stock Entry")
				se.company = COMPANY
				se.stock_entry_type = stock_entry_type
				se.purpose = stock_entry_type
				naming_mod.apply_refining_naming_series(se)
				set_new_name(se)
				names.append(se.name)
			result["names"] = names
		except Exception as e:
			result["error"] = e
		finally:
			result["elapsed"] = time.monotonic() - started
			try:
				if getattr(frappe.local, "db", None):
					frappe.db.rollback()
			finally:
				frappe.destroy()

	worker = threading.Thread(target=run, daemon=True)
	worker.start()
	worker.join(WAIT_S * 5)
	if worker.is_alive():
		raise AssertionError("second connection did not finish")
	return result


class TestRefiningSeriesLockIsolation(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.local.site != TEST_SITE:
			raise unittest.SkipTest(f"runs only on the throwaway site {TEST_SITE}")
		for stock_entry_type in ("Material Transfer", "Material Receipt"):
			stub = frappe.new_doc("Stock Entry")
			stub.company = COMPANY
			stub.stock_entry_type = stock_entry_type
			if lock_order.document_naming_rule_for_doc(stub):
				# A naming rule wins over the naming series: the series cannot apply here.
				raise unittest.SkipTest(
					f"a Document Naming Rule names {stock_entry_type}"
				)
		if not frappe.db.exists("Series", DEFAULT_ROW):
			raise unittest.SkipTest(f"no tabSeries row {DEFAULT_ROW}")

	def setUp(self):
		frappe.db.rollback()

	def tearDown(self):
		# Releases whatever row the test's own connection is holding.
		frappe.db.rollback()

	def _hold(self, key):
		"""Take ``key``'s counter row on this connection, as a long refining (or other)
		submit does at insert, and keep it until tearDown."""
		getseries(key, 5)

	def _name(self, series, *stock_entry_types):
		return _name_on_second_connection(
			frappe.local.site,
			frappe.local.sites_path,
			series,
			stock_entry_types or ("Material Transfer",),
		)

	def assertNamedWithoutWaiting(self, result, prefix, count=1):
		self.assertIsNone(result["error"], msg=repr(result["error"]))
		self.assertEqual(len(result["names"]), count)
		for name in result["names"]:
			self.assertTrue(name.startswith(prefix), msg=name)
		self.assertLess(result["elapsed"], WAIT_S)

	def assertBlocked(self, result):
		self.assertIsInstance(result["error"], frappe.QueryTimeoutError)
		self.assertGreaterEqual(result["elapsed"], WAIT_S - 0.5)

	def test_refining_entry_named_while_mat_ste_is_held(self):
		self._hold(DEFAULT_ROW)
		self.assertNamedWithoutWaiting(self._name(SERIES), SERIES)

	def test_transfer_and_receipt_share_the_refining_row_while_mat_ste_is_held(self):
		self._hold(DEFAULT_ROW)
		result = self._name(SERIES, "Material Transfer", "Material Receipt")
		self.assertNamedWithoutWaiting(result, SERIES, count=2)
		first, second = (int(n[len(SERIES) :]) for n in result["names"])
		self.assertEqual(second, first + 1)

	def test_plain_entry_named_while_refining_row_is_held(self):
		self._hold(SERIES)
		self.assertNamedWithoutWaiting(self._name(None), DEFAULT_ROW)

	def test_control_gate_off_waits_behind_mat_ste(self):
		# Today's behaviour: a refining Stock Entry queues behind any MAT-STE- holder.
		self._hold(DEFAULT_ROW)
		self.assertBlocked(self._name(None))

	def test_control_refining_entries_still_serialise(self):
		# Two refining submits still queue on their shared row -- expected.
		self._hold(SERIES)
		self.assertBlocked(self._name(SERIES))
