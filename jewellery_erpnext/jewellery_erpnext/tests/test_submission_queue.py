"""Unit tests for CustomSubmissionQueue: long-queue routing, the stuck-"Queued" guard and
the failure messages."""

from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import frappe
from frappe.core.doctype.submission_queue import (
	submission_queue as frappe_submission_queue,
)
from frappe.core.doctype.submission_queue.submission_queue import (
	SubmissionQueue,
	format_tb,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime
from MySQLdb import ProgrammingError

from jewellery_erpnext.jewellery_erpnext.customization.submission_queue import (
	submission_queue as je_submission_queue,
)
from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	CustomSubmissionQueue,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_submission_failure_messages import (
	LOCK_WAIT_SERIES,
	PERMISSION_TAX_TEMPLATE,
	RECONNECT_AFTER_LOCK_WAIT,
	SERIES_BUSY_SUBMIT,
	TAX_TEMPLATE_SUBMIT,
	VALIDATION_RECEIVE_DELAY,
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


class _QueueRowStore:
	"""Stands in for frappe.db: the Submission Queue row, the target's docstatus and the
	user's email. Nothing reaches the database."""

	def __init__(self, docstatus=0):
		self.exception = None
		self.docstatus = docstatus
		self.writes = []

	def set_value(self, doctype, name, fieldname, value=None, *args, **kwargs):
		values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		self.writes.append(values)
		if "exception" in values:
			self.exception = values["exception"]

	def get_value(self, doctype, filters=None, fieldname="name", *args, **kwargs):
		if doctype == "Submission Queue" and fieldname == "exception":
			return self.exception
		if fieldname == "docstatus":
			return self.docstatus
		if doctype == "User":
			return "operator@example.com"
		raise AssertionError(
			f"unexpected get_value({doctype!r}, {filters!r}, {fieldname!r})"
		)


class TestSubmissionQueueFailureMessages(IntegrationTestCase):
	"""DB-free: frappe's own background_submission runs; every database call, the job id,
	the realtime toast and the Notification Log are patched."""

	@classmethod
	def setUpClass(cls):
		pass

	def _queue_row(self, queued_seconds_ago=5):
		row = frappe.new_doc("Submission Queue")
		self.assertIsInstance(row, CustomSubmissionQueue)
		row.name = "sq-test"
		row.ref_doctype = "Employee IR"
		row.ref_docname = "EMP-IR-Labh-2026-61931"
		row.owner = "operator@example.com"
		row.creation = add_to_date(now_datetime(), seconds=-queued_seconds_ago)
		return row

	def _fail_on_the_base_path(
		self,
		stored_traceback,
		error,
		docstatus=0,
		queued_seconds_ago=5,
		action="Submit",
	):
		"""Run frappe's real background_submission with a target whose action raises
		`error`; frappe stores `stored_traceback` (the text a real chain produces)."""
		row = self._queue_row(queued_seconds_ago)
		target = SimpleNamespace(
			doctype=row.ref_doctype,
			name=row.ref_docname,
			submit=Mock(side_effect=error),
			cancel=Mock(side_effect=error),
		)
		store = _QueueRowStore(docstatus)
		calls = Mock()
		calls.set_value.side_effect = store.set_value
		calls.get_value.side_effect = store.get_value
		with (
			patch.object(SubmissionQueue, "update_job_id"),
			patch.object(
				frappe_submission_queue,
				"get_current_job",
				return_value=SimpleNamespace(id="job-1"),
			),
			patch.object(frappe, "get_traceback", return_value=stored_traceback),
			patch.object(frappe.db, "rollback", calls.rollback),
			patch.object(frappe.db, "set_value", calls.set_value),
			patch.object(frappe.db, "get_value", calls.get_value),
			patch.object(frappe, "publish_realtime", calls.publish_realtime),
			patch.object(
				je_submission_queue,
				"enqueue_create_notification",
				calls.enqueue_create_notification,
			),
		):
			row.background_submission(target, action)
		return row, store, calls

	def test_numbering_busy_explains_itself_and_keeps_the_traceback(self):
		_row, store, calls = self._fail_on_the_base_path(
			LOCK_WAIT_SERIES, frappe.QueryTimeoutError("1205")
		)

		# frappe's own Failed write is untouched; the message is appended after it.
		self.assertEqual(len(store.writes), 2)
		self.assertEqual(store.writes[0]["status"], "Failed")
		self.assertEqual(store.writes[0]["exception"], LOCK_WAIT_SERIES)
		self.assertEqual(store.exception, f"{LOCK_WAIT_SERIES}\n{SERIES_BUSY_SUBMIT}")
		self.assertTrue(store.exception.startswith(LOCK_WAIT_SERIES))
		# The form banner ("Previous Submission (Failed): ...") shows the last line.
		self.assertEqual(format_tb(store.exception), SERIES_BUSY_SUBMIT)

		# Classified only after frappe's rollback.
		names = [c[0] for c in calls.mock_calls]
		self.assertLess(names.index("rollback"), names.index("get_value"))

		calls.publish_realtime.assert_called_once()
		(event, payload), kwargs = calls.publish_realtime.call_args
		self.assertEqual(event, "msgprint")
		self.assertEqual(kwargs["user"], "operator@example.com")
		self.assertEqual(payload["indicator"], "red")
		self.assertIn(
			f"<strong>Employee IR EMP-IR-Labh-2026-61931</strong>: {SERIES_BUSY_SUBMIT}",
			payload["message"],
		)
		self.assertIn("/desk/submission-queue/sq-test", payload["message"])
		calls.enqueue_create_notification.assert_not_called()

	def test_after_60_seconds_the_message_goes_to_the_notification_log(self):
		_row, _store, calls = self._fail_on_the_base_path(
			LOCK_WAIT_SERIES, frappe.QueryTimeoutError("1205"), queued_seconds_ago=120
		)

		calls.publish_realtime.assert_not_called()
		calls.enqueue_create_notification.assert_called_once()
		(
			(recipients, notification),
			_kwargs,
		) = calls.enqueue_create_notification.call_args
		self.assertEqual(recipients, ["operator@example.com"])
		self.assertEqual(notification["document_type"], "Submission Queue")
		self.assertEqual(notification["document_name"], "sq-test")
		self.assertEqual(
			notification["subject"],
			f"<strong>Employee IR EMP-IR-Labh-2026-61931</strong>: {SERIES_BUSY_SUBMIT}",
		)

	def test_permission_failure_names_the_doctype_and_permission(self):
		_row, store, _calls = self._fail_on_the_base_path(
			PERMISSION_TAX_TEMPLATE, frappe.PermissionError("Not permitted")
		)

		self.assertEqual(store.writes[0]["exception"], PERMISSION_TAX_TEMPLATE)
		self.assertEqual(format_tb(store.exception), TAX_TEMPLATE_SUBMIT)

	def test_a_document_no_longer_in_its_draft_state_gets_no_nothing_changed(self):
		_row, store, _calls = self._fail_on_the_base_path(
			LOCK_WAIT_SERIES, frappe.QueryTimeoutError("1205"), docstatus=1
		)

		summary = format_tb(store.exception)
		self.assertTrue(summary.startswith("Submit failed: "))
		self.assertNotIn("Nothing was changed", summary)

	def test_other_failures_keep_frappes_message_and_traceback(self):
		with patch.object(SubmissionQueue, "notify") as frappe_notify:
			_row, store, _calls = self._fail_on_the_base_path(
				VALIDATION_RECEIVE_DELAY, frappe.ValidationError("receive delay")
			)

		frappe_notify.assert_called_once_with("Failed", "Submit")
		self.assertEqual(len(store.writes), 1)
		self.assertEqual(store.exception, VALIDATION_RECEIVE_DELAY)

	def test_a_wording_error_falls_back_to_frappes_message(self):
		# Raising from notify would send the job into the reconnect path, which rewrites
		# the stored traceback.
		with (
			patch.object(
				je_submission_queue,
				"describe_submission_failure",
				side_effect=RuntimeError("wording bug"),
			),
			patch.object(SubmissionQueue, "notify") as frappe_notify,
		):
			_row, store, _calls = self._fail_on_the_base_path(
				LOCK_WAIT_SERIES, frappe.QueryTimeoutError("1205")
			)

		frappe_notify.assert_called_once_with("Failed", "Submit")
		self.assertEqual(store.exception, LOCK_WAIT_SERIES)

	def test_the_message_is_appended_once(self):
		row, store, calls = self._fail_on_the_base_path(
			LOCK_WAIT_SERIES, frappe.QueryTimeoutError("1205")
		)
		with (
			patch.object(frappe.db, "set_value", calls.set_value),
			patch.object(frappe.db, "get_value", calls.get_value),
			patch.object(frappe, "publish_realtime"),
		):
			row.notify("Failed", "Submit")

		self.assertEqual(len(store.writes), 2)
		self.assertEqual(store.exception, f"{LOCK_WAIT_SERIES}\n{SERIES_BUSY_SUBMIT}")

	def test_finished_keeps_frappes_message(self):
		row = self._queue_row()
		with (
			patch.object(SubmissionQueue, "notify") as frappe_notify,
			patch.object(frappe.db, "get_value") as get_value,
		):
			row.notify("Finished", "Submit")

		frappe_notify.assert_called_once_with("Finished", "Submit")
		get_value.assert_not_called()

	def test_the_reconnect_path_never_claims_nothing_was_changed(self):
		# frappe's rollback after the lock wait raised 2014: the override reconnects and
		# records the failure, but nothing confirms the attempt was undone.
		row = self._queue_row()
		row.to_be_queued_doc = SimpleNamespace(
			doctype=row.ref_doctype, name=row.ref_docname
		)
		desync = ProgrammingError(
			2014, "Commands out of sync; you can't run this command now"
		)
		store = _QueueRowStore(docstatus=0)
		with (
			patch.object(SubmissionQueue, "background_submission", side_effect=desync),
			patch.object(
				frappe, "get_traceback", return_value=RECONNECT_AFTER_LOCK_WAIT
			),
			patch.object(frappe.db, "close"),
			patch.object(frappe.db, "connect"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "set_value", side_effect=store.set_value),
			patch.object(frappe.db, "get_value", side_effect=store.get_value),
			patch.object(frappe, "publish_realtime") as publish_realtime,
		):
			row.background_submission(row.to_be_queued_doc, "Submit")

		expected = (
			"Submit failed: Stock Entry numbering was busy with another posting. "
			"Reload the document and check its status before trying again."
		)
		self.assertEqual(store.writes[0]["status"], "Failed")
		self.assertEqual(store.writes[0]["exception"], RECONNECT_AFTER_LOCK_WAIT)
		self.assertTrue(store.exception.startswith(RECONNECT_AFTER_LOCK_WAIT))
		self.assertEqual(format_tb(store.exception), expected)
		self.assertNotIn("Nothing was changed", store.exception)
		self.assertIn(expected, publish_realtime.call_args.args[1]["message"])
