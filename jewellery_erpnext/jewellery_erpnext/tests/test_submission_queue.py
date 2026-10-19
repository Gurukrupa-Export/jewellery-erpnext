"""Unit tests for CustomSubmissionQueue: long-queue routing and the stuck-"Queued" guard."""

from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import frappe
from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.tests import IntegrationTestCase
from MySQLdb import ProgrammingError

from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	CustomSubmissionQueue,
)


class TestCustomSubmissionQueue(IntegrationTestCase):
	"""DB-free: every write and the connection handling are patched."""

	@classmethod
	def setUpClass(cls):
		pass

	def _queue_row(self, ref_doctype, ref_docname="DOC-TEST"):
		row = frappe.new_doc("Submission Queue")
		self.assertIsInstance(row, CustomSubmissionQueue)
		row.name = "sq-test"
		row.ref_doctype = ref_doctype
		row.ref_docname = ref_docname
		row.action_for_queuing = "Submit"
		row.to_be_queued_doc = SimpleNamespace(doctype=ref_doctype, name=ref_docname)
		return row

	def test_refining_entry_runs_on_the_long_queue(self):
		# Its transfers run 7k-12k batch lines: frappe's 600 s default killed RFN-SCP-26-00022.
		row = self._queue_row("Refining Entry", "RFN-TEST")
		with (
			patch.object(
				SubmissionQueue,
				"queued_doc",
				new_callable=PropertyMock,
				return_value=row.to_be_queued_doc,
			),
			patch.object(CustomSubmissionQueue, "queue_action") as queue_action,
		):
			row.after_insert()

		self.assertEqual(queue_action.call_args.args, ("background_submission",))
		kwargs = queue_action.call_args.kwargs
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["timeout"], 4500)
		self.assertEqual(kwargs["job_id"], "submit::Refining Entry::RFN-TEST")
		self.assertTrue(kwargs["deduplicate"])

	def test_other_doctypes_keep_the_frappe_default(self):
		row = self._queue_row("Sales Invoice")
		with (
			patch.object(SubmissionQueue, "after_insert") as frappe_after_insert,
			patch.object(CustomSubmissionQueue, "queue_action") as queue_action,
		):
			row.after_insert()

		frappe_after_insert.assert_called_once()
		queue_action.assert_not_called()

	def test_a_broken_connection_still_marks_the_row_failed(self):
		# The RQ timeout lands mid-query, frappe's own rollback then raises MySQL 2014 and
		# its set_value(status="Failed") never runs: the row stayed "Queued" for good.
		row = self._queue_row("Refining Entry", "RFN-TEST")
		desync = ProgrammingError(
			2014, "Commands out of sync; you can't run this command now"
		)
		with (
			patch.object(SubmissionQueue, "background_submission", side_effect=desync),
			patch.object(frappe.db, "close") as close,
			patch.object(frappe.db, "connect") as connect,
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(frappe.db, "commit") as commit,
			patch.object(CustomSubmissionQueue, "notify") as notify,
		):
			row.background_submission(row.to_be_queued_doc, "Submit")

		close.assert_called_once()
		connect.assert_called_once()
		(doctype, name, values), kwargs = set_value.call_args
		self.assertEqual((doctype, name), ("Submission Queue", "sq-test"))
		self.assertEqual(values["status"], "Failed")
		self.assertIn("Commands out of sync", values["exception"])
		self.assertTrue(values["ended_at"])
		self.assertFalse(kwargs["update_modified"])
		commit.assert_called_once()
		notify.assert_called_once_with("Failed", "Submit")

	def test_the_normal_path_adds_no_writes(self):
		row = self._queue_row("Refining Entry", "RFN-TEST")
		with (
			patch.object(
				SubmissionQueue, "background_submission"
			) as frappe_background_submission,
			patch.object(frappe.db, "connect") as connect,
			patch.object(frappe.db, "set_value") as set_value,
		):
			row.background_submission(row.to_be_queued_doc, "Submit")

		frappe_background_submission.assert_called_once_with(
			row.to_be_queued_doc, "Submit"
		)
		connect.assert_not_called()
		set_value.assert_not_called()
