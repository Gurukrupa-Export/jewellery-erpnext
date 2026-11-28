# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Pins the one-time Error Log clean-up patch: what it calls, and that it never blocks a deploy.

* THAT it calls Frappe's ``clear_log_table`` for Error Log with seven days, once.
* THAT it estimates the rows it would keep with the same cutoff, and skips the copy when they
  are too big to copy during a deploy: on a full disk MyISAM waits instead of failing.
* THAT a failure changes nothing, prints the manual command, and is recorded with
  ``defer_insert``, because Error Log itself may be the locked table.
* THAT recording the failure can never raise: a raise would fail ``bench migrate`` and with it
  the whole deploy.
* THAT nothing is queued for a retry on the live site.
* THAT ``patches.txt`` registers the patch exactly once, after ``[post_model_sync]``.

DB-free per the suite convention: ``setUpClass`` is neutralised and every frappe call the
patch makes is mocked.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_keep_last_7_days_of_error_log
"""

import io
import os
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import keep_last_7_days_of_error_log as cleanup

CLEAR_LOG_TABLE = "frappe.core.doctype.log_settings.log_settings.clear_log_table"
PATCH = "jewellery_erpnext.patches.keep_last_7_days_of_error_log"
GB = 1024**3


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

		self.db = MagicMock(db_type="mariadb")
		self.site_rows, self.average_row = 1000, 8000
		self.db.sql.side_effect = self._sql
		patcher = patch.object(cleanup.frappe, "db", self.db)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _sql(self, query, values=None):
		if "COUNT(*)" in query:
			return [(self.site_rows,)]
		return [(self.average_row,)]

	def _execute(self, clear_log_table=None):
		out = io.StringIO()
		with patch(
			CLEAR_LOG_TABLE, clear_log_table or MagicMock()
		) as clear, redirect_stdout(out):
			cleanup.execute()
		return clear, out.getvalue()

	def test_keeps_the_last_seven_days_of_error_log(self):
		clear, out = self._execute()

		clear.assert_called_once_with("Error Log", days=7)
		self.assertIn("clean-up done", out)
		self.log_error.assert_not_called()
		self.enqueue.assert_not_called()

	def test_the_estimate_uses_the_same_cutoff_as_the_copy(self):
		self._execute()

		count_query, values = self.db.sql.call_args_list[0].args
		self.assertIn("`creation` > NOW() - INTERVAL %s DAY", count_query)
		self.assertEqual(values, (7,))

	def test_skips_when_the_kept_rows_are_too_big_to_copy_during_a_deploy(self):
		self.site_rows, self.average_row = 1_000_000, 8000  # about 7.5 GB

		clear, out = self._execute()

		clear.assert_not_called()
		self.assertIn("skipped", out)
		self.assertIn('clear-log-table --doctype "Error Log" --days 7', out)

	def test_exactly_at_the_cap_is_still_copied(self):
		self.site_rows, self.average_row = 1, 5 * GB

		clear, _out = self._execute()

		clear.assert_called_once()

	def test_a_failure_changes_nothing_and_is_recorded_without_raising(self):
		_clear, out = self._execute(
			MagicMock(side_effect=Exception("Lock wait timeout exceeded"))
		)

		self.db.rollback.assert_called_once()
		self.log_error.assert_called_once()
		self.assertTrue(self.log_error.call_args.kwargs["defer_insert"])
		self.assertIn('clear-log-table --doctype "Error Log" --days 7', out)
		self.enqueue.assert_not_called()

	def test_recording_the_failure_can_never_raise(self):
		self.log_error.side_effect = Exception("Lock wait timeout exceeded")

		self._execute(MagicMock(side_effect=Exception("Lock wait timeout exceeded")))

		self.log_error.assert_called_once()

	def test_a_failing_estimate_is_handled_the_same_way(self):
		self.db.sql.side_effect = Exception("Table is marked as crashed")

		clear, out = self._execute()

		clear.assert_not_called()
		self.log_error.assert_called_once()
		self.assertIn("changed nothing", out)

	def test_registered_once_after_post_model_sync(self):
		path = os.path.join(frappe.get_app_path("jewellery_erpnext"), "patches.txt")
		with open(path) as f:
			lines = [line.strip() for line in f]

		self.assertEqual(lines.count(PATCH), 1)
		self.assertGreater(lines.index(PATCH), lines.index("[post_model_sync]"))
