# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Two real connections: the Refining Entry form's blocked-variant load must not wait on the
entry's row lock.

On 16 Nov the form load of RFN-SCP-26-00042 timed out (1205) in ``check_if_latest`` while the
entry was being submitted: ``frm.call('get_blocked_variants')`` posted the whole document to
``run_doc_method``, which takes ``SELECT ... FOR UPDATE`` on the entry row and its child tables.
B (a second connection) plays the running submission and holds the row; A (``frappe.db``) is the
form. ``get_blocked_variants_for`` must answer at once, while the old path (``check_if_latest``)
is shown to wait and time out against the same lock.

Commits a throwaway draft ``tabRefining Entry`` row (``RFN-LOCKTEST-<hash>``) through B and
deletes it in cleanup. Only for a throwaway test site (``allow_tests``), e.g.
kggk-pi-test.localhost or CI's test_site; never the production copy. A uses
``SET SESSION innodb_lock_wait_timeout = 2`` only, never ``SET GLOBAL``.
"""

import time
import unittest

import frappe

from jewellery_erpnext.refining.constants import REFINING_TYPE_SCRAP
from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	get_blocked_variants_for,
)

LOCK_WAIT_S = 2
REFUSED_SITES = {"kggk-prod.localhost"}


class TestBlockedVariantsDoNotLockTheEntry(unittest.TestCase):
	def setUp(self):
		if frappe.db.db_type != "mariadb":
			self.skipTest("MariaDB row locks")
		if not frappe.conf.allow_tests or frappe.local.site in REFUSED_SITES:
			self.skipTest(
				"commits throwaway rows: only on an allow_tests throwaway site"
			)

		setting = frappe.db.get_value(
			"Manufacturing Setting",
			{},
			["company", "manufacturer"],
			as_dict=True,
			order_by="creation asc",
		)
		self.company = setting.company if setting else "_Lock Test Company"
		self.manufacturer = setting.manufacturer if setting else None
		self.name = f"RFN-LOCKTEST-{frappe.generate_hash(length=8)}"

		self.other = frappe.database.get_db(
			socket=frappe.db.socket,
			host=frappe.db.host,
			user=frappe.db.user,
			password=frappe.db.password,
			port=frappe.db.port,
			cur_db_name=frappe.db.cur_db_name,
		)
		self.other.connect()
		self.addCleanup(self.other.close)
		self.addCleanup(self._delete_row)
		self.other.sql(
			"""INSERT INTO `tabRefining Entry`
				(name, creation, modified, modified_by, owner, docstatus, idx,
				naming_series, refining_type, company, manufacturer, status)
			VALUES (%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
				'RFN-SCP-.YY.-.#####', %s, %s, %s, 'Draft')""",
			(self.name, REFINING_TYPE_SCRAP, self.company, self.manufacturer),
		)
		self.other.commit()
		self.modified = self.other.sql(
			"SELECT modified FROM `tabRefining Entry` WHERE name = %s", self.name
		)[0][0]
		self.other.rollback()

		frappe.db.rollback()
		old_timeout = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
		self.addCleanup(
			frappe.db.sql, f"SET SESSION innodb_lock_wait_timeout = {int(old_timeout)}"
		)
		self.addCleanup(frappe.db.rollback)
		frappe.db.sql(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_S}")

	def _delete_row(self):
		frappe.db.rollback()  # A may hold the row after check_if_latest
		self.other.rollback()  # releases B's FOR UPDATE
		self.other.sql("DELETE FROM `tabRefining Entry` WHERE name = %s", self.name)
		self.other.commit()

	def _hold_the_row(self):
		"""B: what the running submission holds on the entry, uncommitted."""
		self.other.sql(
			"SELECT name FROM `tabRefining Entry` WHERE name = %s FOR UPDATE", self.name
		)

	def _posted_doc(self):
		"""What frm.call() posted for this saved entry; run_doc_method builds the doc from it."""
		doc = frappe.get_doc(
			{
				"doctype": "Refining Entry",
				"name": self.name,
				"modified": str(self.modified),
				"docstatus": 0,
				"naming_series": "RFN-SCP-.YY.-.#####",
				"refining_type": REFINING_TYPE_SCRAP,
				"company": self.company,
				"manufacturer": self.manufacturer,
			}
		)
		doc._original_modified = doc.modified
		return doc

	def _new_endpoint(self):
		return get_blocked_variants_for(
			self.company, REFINING_TYPE_SCRAP, self.manufacturer
		)

	def test_answers_at_once_while_the_entry_row_is_locked(self):
		expected = sorted(self._posted_doc()._restricted_variants())
		self._new_endpoint()  # warm meta / table caches outside the timed call
		frappe.db.rollback()

		self._hold_the_row()

		started = time.perf_counter()
		result = self._new_endpoint()
		elapsed = time.perf_counter() - started
		self.assertLess(
			elapsed, 1.0, "the form's blocked-variant load waited on the entry"
		)
		self.assertEqual(result, expected)
		frappe.db.rollback()

		# Positive control: the lock is real, and the old path (what run_doc_method runs before
		# dispatching get_blocked_variants) waits on it until innodb_lock_wait_timeout.
		doc = self._posted_doc()
		started = time.perf_counter()
		with self.assertRaises(frappe.QueryTimeoutError):
			doc.check_if_latest()
		self.assertGreaterEqual(time.perf_counter() - started, LOCK_WAIT_S * 0.9)
		frappe.db.rollback()

	def test_same_result_as_the_old_path_when_nothing_holds_the_row(self):
		doc = self._posted_doc()
		doc.check_if_latest()  # the old path, unlocked: succeeds
		old = doc.get_blocked_variants()
		frappe.db.rollback()  # release A's own FOR UPDATE from check_if_latest
		self.assertEqual(self._new_endpoint(), old)
