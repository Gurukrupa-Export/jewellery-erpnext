# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Pins the one-time Error Log clean-up patch: what it calls, and that it never blocks a deploy.

* THAT it calls Frappe's ``clear_log_table`` for Error Log with seven days, once.
* THAT a failure is logged and the same call is queued once, with a job id and deduplication,
  instead of raising: a raise would fail ``bench migrate`` and with it the whole deploy.
* THAT a failure to queue the retry is logged and still does not raise.
* THAT ``patches.txt`` registers the patch exactly once, after ``[post_model_sync]``.

DB-free per the suite convention: ``setUpClass`` is neutralised and every frappe call the
patch makes is mocked.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_keep_last_7_days_of_error_log
"""

import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import keep_last_7_days_of_error_log as cleanup

CLEAR_LOG_TABLE = "frappe.core.doctype.log_settings.log_settings.clear_log_table"
PATCH = "jewellery_erpnext.patches.keep_last_7_days_of_error_log"


class TestKeepLast7DaysOfErrorLog(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		for target in ("log_error", "enqueue"):
			patcher = patch.object(cleanup.frappe, target)
			setattr(self, target, patcher.start())
			self.addCleanup(patcher.stop)
		patcher = patch.object(cleanup.frappe, "db", MagicMock())
		self.db = patcher.start()
		self.addCleanup(patcher.stop)

	def test_keeps_the_last_seven_days_of_error_log(self):
		with patch(CLEAR_LOG_TABLE) as clear_log_table:
			cleanup.execute()

		clear_log_table.assert_called_once_with("Error Log", days=7)
		self.log_error.assert_not_called()
		self.enqueue.assert_not_called()

	def test_a_failure_is_logged_and_retried_once_in_the_background(self):
		with patch(
			CLEAR_LOG_TABLE, side_effect=Exception("Lock wait timeout exceeded")
		):
			cleanup.execute()

		self.db.rollback.assert_called_once()
		self.log_error.assert_called_once()
		self.enqueue.assert_called_once_with(
			CLEAR_LOG_TABLE,
			queue="long",
			timeout=3600,
			job_id="error-log-keep-7-days",
			deduplicate=True,
			doctype="Error Log",
			days=7,
		)

	def test_a_failure_to_queue_the_retry_is_logged_and_not_raised(self):
		self.enqueue.side_effect = Exception("Redis unreachable")

		with patch(
			CLEAR_LOG_TABLE, side_effect=Exception("Lock wait timeout exceeded")
		):
			cleanup.execute()

		self.assertEqual(self.log_error.call_count, 2)

	def test_registered_once_after_post_model_sync(self):
		path = os.path.join(frappe.get_app_path("jewellery_erpnext"), "patches.txt")
		with open(path) as f:
			lines = [line.strip() for line in f]

		self.assertEqual(lines.count(PATCH), 1)
		self.assertGreater(lines.index(PATCH), lines.index("[post_model_sync]"))
