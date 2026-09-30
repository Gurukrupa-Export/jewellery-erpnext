# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Customer-owned loss on an Employee IR: no dialog, one timeline note.

A draft save that books loss on customer-owned metal shows nothing; that pin is
``TestDraftSaveCustomerLossIsSilent`` in test_employee_ir_loss_baseline, next to the
save-path stub it reuses. After a successful submit, ``_announce_customer_loss_posted``
leaves one Info note on the timeline naming the metal actually posted, and raises no
dialog -- neither a ``msgprint`` nor a realtime one.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils import ownership_priority
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

_EIR = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"
PARENT_12_A = "GJCU0009-2F09-M-G-22KT-91.75-Y-12-A"


class _Doc:
	"""The parts of an Employee IR the submit-path methods read."""

	def __init__(self):
		self.doctype = "Employee IR"
		self.name = "EMP-IR-TEST-0001"
		self.docstatus = 0
		self.type = "Receive"
		self.flags = frappe._dict()
		self.employee_loss_details = []
		self.manually_book_loss_details = []
		self.comments = []

	def add_comment(self, comment_type, text):
		self.comments.append((comment_type, text))


class TestCustomerLossPostedNote(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	ROW = frappe._dict(
		stock_entry="MAT-STE-19778",
		customer="GJCU0009",
		source_item="M-G-22KT-91.75-Y",
		source_batch=PARENT_12_A,
		loss_item="ML-G-22KT-91.75-Y",
		scrap_batch="GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A",
		warehouse="Waxing Scrap - KGJPL",
		qty=0.01,
		stock_uom="Gram",
	)

	def _announce(self, doc, rows=None, error=None):
		with (
			patch(
				f"{_EIR}._posted_customer_loss_rows",
				return_value=rows or [],
				side_effect=error,
			),
			patch("frappe.publish_realtime") as publish,
			patch("frappe.msgprint") as msgprint,
			patch("frappe.log_error") as log_error,
		):
			EmployeeIR._announce_customer_loss_posted(doc)
		return publish, msgprint, log_error

	def test_a_posted_customer_loss_is_noted_on_the_timeline_only(self):
		doc = _Doc()
		publish, msgprint, log_error = self._announce(doc, rows=[self.ROW])

		self.assertEqual(len(doc.comments), 1)
		comment_type, text = doc.comments[0]
		self.assertEqual(comment_type, "Info")
		self.assertIn("MAT-STE-19778", text)
		self.assertIn("GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A", text)
		self.assertIn("customer-owned scrap", text)

		# No dialog: neither a realtime msgprint nor a direct one.
		publish.assert_not_called()
		msgprint.assert_not_called()
		log_error.assert_not_called()

	def test_nothing_is_announced_without_customer_rows(self):
		doc = _Doc()
		publish, msgprint, _log_error = self._announce(doc)
		self.assertEqual(doc.comments, [])
		publish.assert_not_called()
		msgprint.assert_not_called()

	def test_a_failure_is_logged_and_never_fails_the_submit(self):
		doc = _Doc()
		publish, _msgprint, log_error = self._announce(doc, error=RuntimeError("boom"))
		log_error.assert_called_once()
		publish.assert_not_called()

	def test_a_deadlock_is_not_swallowed(self):
		"""InnoDB rolled the whole submit back; carrying on would commit half of it."""
		with self.assertRaises(frappe.QueryDeadlockError):
			self._announce(_Doc(), error=frappe.QueryDeadlockError("1213"))


class TestCustomerLossFormatters(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_posted_lines_escape_codes_and_total_per_uom(self):
		rows = [
			dict(TestCustomerLossPostedNote.ROW, scrap_batch="<b>X</b>"),
			dict(
				TestCustomerLossPostedNote.ROW,
				loss_item="DL-1",
				qty=0.05,
				stock_uom="Carat",
			),
		]
		lines = ownership_priority.describe_customer_loss_posted(rows)
		self.assertIn("&lt;b&gt;X&lt;/b&gt;", lines[0])
		self.assertNotIn("<b>X</b>", lines[0])
		self.assertEqual(lines[-1], "Total: 0.01 Gram + 0.05 Carat")


class TestSubmitPathDeadlocks(IntegrationTestCase):
	"""A deadlock means InnoDB rolled the whole submit back; no step may swallow it.

	Logging it and carrying on would commit the rest of on_submit in a fresh
	transaction: an Employee IR reported submitted while it is still a Draft.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _refresh(self, error):
		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events."
				"main_slip_inject._resolve_source_warehouse_raw_material",
				return_value="MSL - T",
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking."
				"recalculate_msl_tracking",
				side_effect=error,
			),
			patch("frappe.log_error") as log_error,
		):
			EmployeeIR._refresh_msl_tracking(_Doc())
		return log_error

	def test_the_msl_refresh_lets_a_deadlock_through(self):
		with self.assertRaises(frappe.QueryDeadlockError):
			self._refresh(frappe.QueryDeadlockError("1213"))

	def test_the_msl_refresh_still_only_logs_other_failures(self):
		self._refresh(RuntimeError("boom")).assert_called_once()
