# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Tests for log_table_maintenance: Error Log swap, Deleted Document and Version purges.

DB-free per the suite convention (setUpClass neutralised, frappe.db mocked), except
TestQueriesRunOnTheSite, which only runs the read-only SELECTs against the test site so a
broken query fails here rather than on production.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_log_table_maintenance
"""

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import log_table_maintenance as ltm


class _MockedDB(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		ltm.now()  # loads System Settings (timezone) before frappe.db is mocked
		self.db = MagicMock()
		self.db.db_type = "mariadb"
		for target, value in (("db", self.db), ("log_error", MagicMock())):
			patcher = patch.object(ltm.frappe, target, value)
			patcher.start()
			self.addCleanup(patcher.stop)

		patcher = patch.object(ltm, "_table_size", return_value={"data_gb": 1.0})
		patcher.start()
		self.addCleanup(patcher.stop)


class TestEnqueueTask(_MockedDB):
	def _enqueue(self, **kwargs):
		with (
			patch.object(ltm.frappe, "only_for") as only_for,
			patch.object(ltm.frappe, "enqueue", return_value=MagicMock()) as enqueue,
		):
			message = ltm.enqueue_task(**kwargs)

		only_for.assert_called_once_with("System Manager")
		return enqueue, message

	def test_error_log_task_passes_days(self):
		enqueue, message = self._enqueue(task="error_log", dry_run="0", days="3")

		enqueue.assert_called_once_with(
			"jewellery_erpnext.log_table_maintenance.shrink_error_log",
			queue="long",
			timeout=4 * 60 * 60,
			job_id="log-table-maintenance::error_log",
			deduplicate=True,
			dry_run=0,
			days=3,
		)
		self.assertIn("Queued", message)

	def test_deleted_document_task_passes_optimize_and_doctypes(self):
		enqueue, _message = self._enqueue(
			task="deleted_document", optimize=1, doctypes="Version"
		)

		kwargs = enqueue.call_args.kwargs
		self.assertEqual(
			enqueue.call_args.args[0],
			"jewellery_erpnext.log_table_maintenance.purge_deleted_documents",
		)
		self.assertEqual(
			(kwargs["dry_run"], kwargs["optimize"], kwargs["deleted_doctypes"]),
			(1, 1, "Version"),
		)

	def test_malformed_versions_task_passes_ref_doctypes(self):
		enqueue, _message = self._enqueue(task="malformed_versions", doctypes='["BOM"]')

		self.assertEqual(enqueue.call_args.kwargs["ref_doctypes"], '["BOM"]')

	def test_unknown_task_is_refused(self):
		with patch.object(ltm.frappe, "only_for"), patch.object(
			ltm.frappe, "enqueue"
		) as enqueue:
			self.assertRaises(
				frappe.ValidationError, ltm.enqueue_task, task="drop_everything"
			)

		enqueue.assert_not_called()

	def test_reports_a_task_already_in_progress(self):
		with patch.object(ltm.frappe, "only_for"), patch.object(
			ltm.frappe, "enqueue", return_value=None
		):
			self.assertIn("already", ltm.enqueue_task(task="error_log"))

	def test_needs_system_manager(self):
		with (
			patch.object(ltm.frappe, "only_for", side_effect=frappe.PermissionError),
			patch.object(ltm.frappe, "enqueue") as enqueue,
		):
			self.assertRaises(
				frappe.PermissionError, ltm.enqueue_task, task="error_log"
			)

		enqueue.assert_not_called()


class TestShrinkErrorLog(_MockedDB):
	def setUp(self):
		super().setUp()
		self.db.sql.return_value = [(1234,)]
		patcher = patch("frappe.core.doctype.log_settings.log_settings.clear_log_table")
		self.clear_log_table = patcher.start()
		self.addCleanup(patcher.stop)

	def test_dry_run_counts_the_rows_it_would_keep(self):
		summary = ltm.shrink_error_log(days=7, dry_run=1)

		self.assertEqual(summary["rows_kept"], 1234)
		self.assertEqual(self.db.sql.call_args.args[1], (7,))
		self.clear_log_table.assert_not_called()
		self.assertEqual(
			ltm.frappe.log_error.call_args.kwargs["title"],
			"Log table maintenance: error_log (dry run)",
		)

	def test_real_run_swaps_the_table_keeping_the_given_days(self):
		summary = ltm.shrink_error_log(days="3", dry_run=0)

		self.clear_log_table.assert_called_once_with("Error Log", days=3)
		self.assertIn("after", summary)

	def test_refuses_to_keep_no_days(self):
		for days in (0, -1, "", None):
			with self.subTest(days=days):
				self.assertRaises(
					frappe.ValidationError, ltm.shrink_error_log, days=days, dry_run=0
				)

		self.clear_log_table.assert_not_called()


class TestPurgeDeletedDocuments(_MockedDB):
	ROWS = [("dd1", "Version"), ("dd2", "Version"), ("dd3", "Error Log")]

	def setUp(self):
		super().setUp()
		patcher = patch.object(ltm, "_deleted_document_rows", return_value=self.ROWS)
		self.rows = patcher.start()
		self.addCleanup(patcher.stop)

	def test_dry_run_counts_by_deleted_doctype_without_deleting(self):
		summary = ltm.purge_deleted_documents(dry_run=1)

		self.rows.assert_called_once_with(["Version", "Error Log"])
		self.assertEqual(summary["rows"], {"Version": 2, "Error Log": 1})
		self.db.delete.assert_not_called()

	def test_real_run_deletes_by_name_in_chunks_and_commits_each(self):
		with patch.object(ltm, "CHUNK_SIZE", 2):
			ltm.purge_deleted_documents(dry_run=0)

		self.assertEqual(
			self.db.delete.call_args_list,
			[
				call("Deleted Document", {"name": ("in", ["dd1", "dd2"])}),
				call("Deleted Document", {"name": ("in", ["dd3"])}),
			],
		)
		# one commit per chunk, then one after the summary
		self.assertEqual(self.db.commit.call_count, 3)
		self.db.sql.assert_not_called()

	def test_optimize_rebuilds_the_table_after_deleting(self):
		ltm.purge_deleted_documents(dry_run=0, optimize=1)

		self.db.sql.assert_called_once_with("OPTIMIZE TABLE `tabDeleted Document`")

	def test_doctypes_can_be_narrowed(self):
		ltm.purge_deleted_documents(dry_run=1, deleted_doctypes="Version")

		self.rows.assert_called_once_with(["Version"])


class TestPurgeMalformedVersions(_MockedDB):
	def setUp(self):
		super().setUp()
		patcher = patch.object(
			ltm, "_malformed_version_rows", return_value=[("v1", "BOM"), ("v2", "Item")]
		)
		self.rows = patcher.start()
		self.addCleanup(patcher.stop)

	def test_dry_run_only_counts(self):
		summary = ltm.purge_malformed_versions(dry_run=1)

		self.rows.assert_called_once_with(["BOM", "Item"])
		self.assertEqual(summary["rows"], {"BOM": 1, "Item": 1})
		self.db.delete.assert_not_called()

	def test_real_run_deletes_them(self):
		ltm.purge_malformed_versions(dry_run=0, ref_doctypes='["BOM", "Item"]')

		self.db.delete.assert_called_once_with(
			"Version", {"name": ("in", ["v1", "v2"])}
		)


class TestParseList(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_forms(self):
		self.assertEqual(ltm._parse_list(None), [])
		self.assertEqual(
			ltm._parse_list('["Version", "Error Log"]'), ["Version", "Error Log"]
		)
		self.assertEqual(
			ltm._parse_list("Version, Error Log"), ["Version", "Error Log"]
		)
		self.assertEqual(ltm._parse_list(["BOM", ""]), ["BOM"])


class TestQueriesRunOnTheSite(IntegrationTestCase):
	"""Read-only: the SELECTs behind the dry runs are valid on a real database."""

	def test_row_queries(self):
		# A doctype nothing references: proves the SQL is valid without scanning real data.
		self.assertEqual(
			list(ltm._deleted_document_rows(["_Test No Such DocType"])), []
		)
		self.assertEqual(
			list(ltm._malformed_version_rows(["_Test No Such DocType"])), []
		)

	def test_table_size(self):
		size = ltm._table_size("Error Log")
		if frappe.db.db_type == "mariadb":
			self.assertEqual(
				set(size), {"estimated_rows", "data_gb", "index_gb", "free_gb"}
			)

	def test_error_log_dry_run(self):
		with patch.object(ltm.frappe, "log_error") as log_error, patch.object(
			ltm.frappe.db, "commit"
		):
			summary = ltm.shrink_error_log(days=7, dry_run=1)

		self.assertGreaterEqual(summary["rows_kept"], 0)
		log_error.assert_called_once()
