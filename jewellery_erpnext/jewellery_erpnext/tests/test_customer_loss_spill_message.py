# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Wording and timing of the Employee IR customer-loss messages.

The draft-save preview (``_warn_customer_loss_spill``) must never claim a posting. On
EMP-IR-Labh-2026-14111 it said 0.01 g "was booked" and blamed company stock the
operation never held, and the submit then failed. The note left after a successful
submit (``_announce_customer_loss_posted``) is the only past-tense message, and it
goes out only once the submit commits.
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


def _spill(batch_no, qty, customer="GJCU0009"):
	return {
		"customer": customer,
		"item_code": "M-G-22KT-91.75-Y",
		"batch_no": batch_no,
		"qty": qty,
	}


def _loss(batch_no, qty, item_code="M-G-22KT-91.75-Y"):
	return {"item_code": item_code, "batch_no": batch_no, "proportionally_loss": qty}


class _Doc:
	"""The parts of an Employee IR the two message methods read."""

	def __init__(self, spill=(), automatic=(), manual=(), overflow=(), docstatus=0):
		self.doctype = "Employee IR"
		self.name = "EMP-IR-TEST-0001"
		self.docstatus = docstatus
		self.type = "Receive"
		self.flags = frappe._dict(
			customer_loss_spill=list(spill), loss_overflow=list(overflow)
		)
		self.employee_loss_details = [frappe._dict(row) for row in automatic]
		self.manually_book_loss_details = [frappe._dict(row) for row in manual]
		self.comments = []

	def add_comment(self, comment_type, text):
		self.comments.append((comment_type, text))


class TestCustomerLossPreview(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _preview(self, doc, ownership=None):
		with (
			patch("frappe.msgprint") as msgprint,
			patch(f"{_EIR}.batch_priority_map", return_value=ownership or {}),
		):
			EmployeeIR._warn_customer_loss_spill(doc)
		return msgprint

	def _only_message(self, msgprint):
		self.assertEqual(msgprint.call_count, 1)
		args, kwargs = msgprint.call_args
		return args[0], kwargs

	def _assert_future_tense(self, message):
		self.assertIn("Nothing has been posted yet", message)
		self.assertNotIn("was booked", message)
		self.assertNotIn("could not absorb", message)

	def test_the_incident_customer_only_operation(self):
		doc = _Doc(
			spill=[_spill(PARENT_12_A, 0.01)], automatic=[_loss(PARENT_12_A, 0.01)]
		)
		message, kwargs = self._only_message(self._preview(doc))

		self._assert_future_tense(message)
		self.assertIn("no company-owned metal", message)
		self.assertIn(f"M-G-22KT-91.75-Y / {PARENT_12_A}: 0.01", message)
		self.assertEqual(kwargs["title"], "Customer Material Will Absorb Loss")
		self.assertEqual(kwargs["indicator"], "orange")

	def test_a_split_names_both_shares(self):
		doc = _Doc(
			spill=[_spill(PARENT_12_A, 0.004)],
			automatic=[
				_loss("KG2F083-MGL229175Y0-9FZ49", 0.006),
				_loss(PARENT_12_A, 0.004),
			],
		)
		message, _kwargs = self._only_message(self._preview(doc))

		self._assert_future_tense(message)
		self.assertIn("Of the <strong>0.01</strong> g process loss", message)
		self.assertIn(
			"<strong>0.006</strong> g is booked on company-owned metal", message
		)
		self.assertIn("<strong>0.004</strong> g on customer-owned material", message)

	def test_a_manual_row_on_customer_metal_is_previewed(self):
		ownership = {
			"GJCU-MANUAL": frappe._dict(
				inventory_type="Customer Goods", customer="GJCU0009"
			)
		}
		doc = _Doc(manual=[_loss("GJCU-MANUAL", 0.02)])
		message, _kwargs = self._only_message(self._preview(doc, ownership))

		self._assert_future_tense(message)
		self.assertIn("GJCU-MANUAL: 0.02 (booked manually)", message)

	def test_a_manual_row_on_company_metal_is_not(self):
		ownership = {
			"KG-MANUAL": frappe._dict(inventory_type="Regular Stock", customer=None)
		}
		doc = _Doc(manual=[_loss("KG-MANUAL", 0.02)])
		self.assertEqual(self._preview(doc, ownership).call_count, 0)

	def test_overflow_is_called_out_on_the_company_share(self):
		doc = _Doc(
			spill=[_spill(PARENT_12_A, 0.1)],
			automatic=[
				_loss("KG2F083-MGL229175Y0-9FZ49", 6.0),
				_loss(PARENT_12_A, 0.1),
			],
			overflow=[{"mwo": "MWO-1", "operation": "MOP-1", "qty": 4.0}],
		)
		message, _kwargs = self._only_message(self._preview(doc))
		self.assertIn("beyond the operation's recorded balance", message)

	def test_no_customer_metal_no_message(self):
		doc = _Doc(automatic=[_loss("KG2F083-MGL229175Y0-9FZ49", 0.01)])
		self.assertEqual(self._preview(doc).call_count, 0)

	def test_a_submitted_document_is_never_previewed(self):
		doc = _Doc(
			spill=[_spill(PARENT_12_A, 0.01)],
			automatic=[_loss(PARENT_12_A, 0.01)],
			docstatus=1,
		)
		with patch("frappe.msgprint") as msgprint:
			EmployeeIR.validate_process_loss(doc)
		msgprint.assert_not_called()


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
			patch("frappe.log_error") as log_error,
		):
			EmployeeIR._announce_customer_loss_posted(doc)
		return publish, log_error

	def test_a_posted_customer_loss_is_recorded_after_commit(self):
		doc = _Doc()
		publish, log_error = self._announce(doc, rows=[self.ROW])

		self.assertEqual(len(doc.comments), 1)
		comment_type, text = doc.comments[0]
		self.assertEqual(comment_type, "Info")
		self.assertIn("MAT-STE-19778", text)
		self.assertIn("GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A", text)
		self.assertIn("customer-owned scrap", text)

		publish.assert_called_once()
		args, kwargs = publish.call_args
		self.assertEqual(args[0], "msgprint")
		self.assertEqual(args[1]["message"], text)
		self.assertTrue(kwargs["after_commit"])
		self.assertEqual(kwargs["user"], frappe.session.user)
		log_error.assert_not_called()

	def test_nothing_is_announced_without_customer_rows(self):
		doc = _Doc()
		publish, _log_error = self._announce(doc)
		self.assertEqual(doc.comments, [])
		publish.assert_not_called()

	def test_a_failure_is_logged_and_never_fails_the_submit(self):
		doc = _Doc()
		publish, log_error = self._announce(doc, error=RuntimeError("boom"))
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

	def test_the_incident_preview_line(self):
		self.assertEqual(
			ownership_priority.describe_customer_spill([_spill(PARENT_12_A, 0.01)]),
			[
				f"<strong>GJCU0009</strong> &mdash; M-G-22KT-91.75-Y / {PARENT_12_A}: 0.01"
			],
		)

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
