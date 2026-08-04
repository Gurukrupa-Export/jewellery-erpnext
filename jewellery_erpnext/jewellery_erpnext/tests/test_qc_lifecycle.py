# Copyright (c) 2026, Gurukrupa Exports and Contributors
# See license.txt

"""QC submit / force-approve lifecycle guards.

Two defects, both verified against real documents on a live site before the fix:

1. **Unbounded recursion in `force_approve`.** It recursed via
   `frappe.get_doc("QC", self.duplicate_qc).force_approve()` with no termination
   guard. A `duplicate_qc` cycle raised `RecursionError` — reproduced, not assumed.

2. **Re-entrant `self.save()` inside `on_submit`.** Worth stating precisely,
   because the obvious reading is wrong: the old code *did* persist
   `duplicate_qc` (the field is `allow_on_submit`, so the write survived
   `validate_update_after_submit`). The defect is that `save()` on an
   already-submitted document re-runs the entire validate chain from inside
   `on_submit`, and it is what made `force_approve` fragile — that path sets
   status to "Force Approved", re-enters `on_submit`, and `validate` throws
   "Not allowed to select 'Force Approved'".

The status propagation was extracted into `_apply_qc_outcome()` so
`force_approve` never re-enters a lifecycle hook.
"""

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.qc.qc import QC


class TestQCLifecycle(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.mop = frappe.db.sql(
			"""
			SELECT name, manufacturing_work_order
			FROM `tabManufacturing Operation`
			WHERE IFNULL(manufacturing_work_order, '') <> ''
			LIMIT 1
			""",
			as_dict=True,
		)

	def _make_qc(self, status):
		if not self.mop:
			self.skipTest("no Manufacturing Operation with a work order on this site")
		doc = frappe.get_doc(
			{
				"doctype": "QC",
				"manufacturing_operation": self.mop[0]["name"],
				"manufacturing_work_order": self.mop[0]["manufacturing_work_order"],
				"status": status,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def test_rejected_submit_persists_duplicate_qc(self):
		"""The replacement pointer must survive a reload, not just live in memory."""
		qc = self._make_qc("Rejected")
		qc.submit()

		persisted = frappe.db.get_value("QC", qc.name, "duplicate_qc")
		self.assertTrue(persisted, "duplicate_qc was not written to the database")
		self.assertEqual(persisted, qc.duplicate_qc)

	def test_rejected_submit_creates_exactly_one_replacement(self):
		qc = self._make_qc("Rejected")
		qc.submit()

		replacements = frappe.db.get_all(
			"QC", filters={"previous_qc": qc.name}, fields=["name", "status"]
		)
		self.assertEqual(len(replacements), 1)
		self.assertEqual(replacements[0]["status"], "Pending")

	def test_document_stays_submitted(self):
		"""A re-entrant save inside on_submit must not disturb docstatus."""
		qc = self._make_qc("Rejected")
		qc.submit()
		self.assertEqual(frappe.db.get_value("QC", qc.name, "docstatus"), 1)

	def test_force_approve_terminates_on_a_cycle(self):
		"""Reproduces the RecursionError the old implementation raised."""
		first = self._make_qc("Accepted")
		second = self._make_qc("Accepted")
		frappe.db.set_value("QC", first.name, "duplicate_qc", second.name)
		frappe.db.set_value("QC", second.name, "duplicate_qc", first.name)
		first.reload()
		second.reload()
		first.submit()
		second.submit()
		first.reload()

		first.force_approve()  # must return, not recurse

		self.assertEqual(
			frappe.db.get_value("QC", first.name, "status"), "Force Approved"
		)
		self.assertEqual(
			frappe.db.get_value("QC", second.name, "status"), "Force Approved"
		)

	def test_force_approve_walks_the_whole_chain(self):
		a = self._make_qc("Accepted")
		b = self._make_qc("Accepted")
		c = self._make_qc("Accepted")
		frappe.db.set_value("QC", a.name, "duplicate_qc", b.name)
		frappe.db.set_value("QC", b.name, "duplicate_qc", c.name)
		for doc in (a, b, c):
			doc.reload()
			doc.submit()
		a.reload()

		a.force_approve()

		for doc in (a, b, c):
			self.assertEqual(
				frappe.db.get_value("QC", doc.name, "status"),
				"Force Approved",
				f"{doc.name} was not force-approved",
			)

	def test_force_approve_no_longer_reenters_on_submit(self):
		"""`force_approve` must not call the lifecycle hook.

		Calling `on_submit()` re-ran its guard against a status that `validate`
		rejects outright, so the path depended on ordering luck.
		"""
		import inspect

		source = inspect.getsource(QC.force_approve)
		self.assertNotIn(
			"self.on_submit()",
			source,
			"force_approve must call _apply_qc_outcome(), not the on_submit hook",
		)

	def test_chain_guard_is_bounded(self):
		self.assertLessEqual(QC.MAX_FORCE_APPROVE_CHAIN, 50)
		self.assertGreater(QC.MAX_FORCE_APPROVE_CHAIN, 1)
