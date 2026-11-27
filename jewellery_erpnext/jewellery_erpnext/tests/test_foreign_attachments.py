# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Tests for foreign_attachments: the guard that points GK attachment paths at GK, and repair.

The path check, the guard, the repair sweep and the enqueue wrapper are DB-free per the suite
convention: setUpClass is neutralised, frappe lookups are mocked, and file checks run against a
temporary folder. TestThroughFrappe goes through Frappe itself on the test site: the real
doc_events dispatch on an unsaved Item, and core's attach hook. Its only write, the remote File
row, stays inside the class transaction that IntegrationTestCase rolls back.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_foreign_attachments
"""

import os
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.core.doctype.file.utils import attach_files_to_document
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import foreign_attachments as fa

ORIGIN = "https://gk.example.com"
OWN_SITE = "https://kggk.example.com"
GUARD = "jewellery_erpnext.foreign_attachments.normalize_foreign_attachments"
MISSING = "/files/_test_foreign_attachment_missing.jpg"


@contextmanager
def _origin(value):
	"""Run with ``foreign_attachment_origin`` set to ``value`` (None: absent)."""
	with patch.dict(frappe.local.conf):
		frappe.local.conf.pop(fa.ORIGIN_KEY, None)
		if value is not None:
			frappe.local.conf[fa.ORIGIN_KEY] = value
		with patch.object(fa, "get_url", return_value=OWN_SITE):
			yield


class _Doc(SimpleNamespace):
	"""Document stand-in: get()/set() over attributes, meta listing the given attach fields."""

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)


def _doc(**values):
	meta = MagicMock()
	meta.get.return_value = [
		SimpleNamespace(fieldname=fieldname) for fieldname in values
	]
	return _Doc(meta=meta, **values)


class _SiteFiles(IntegrationTestCase):
	"""DB-free base: fa.get_files_path points at a temporary site folder with a few real files."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)

		for is_private, relative in (
			(False, "present.jpg"),
			(False, "with space.jpg"),
			(False, "Home/nested.jpg"),
			(True, "secret.pdf"),
		):
			path = os.path.join(
				tmp.name, "private" if is_private else "public", "files", relative
			)
			os.makedirs(os.path.dirname(path), exist_ok=True)
			open(path, "w").close()

		def get_files_path(*parts, is_private=False):
			return os.path.join(
				tmp.name, "private" if is_private else "public", "files", *parts
			)

		patcher = patch.object(fa, "get_files_path", get_files_path)
		patcher.start()
		self.addCleanup(patcher.stop)


class TestIsMissingLocalFile(_SiteFiles):
	def test_missing_public_and_private_files_are_flagged(self):
		self.assertTrue(fa.is_missing_local_file("/files/gone.jpg"))
		self.assertTrue(fa.is_missing_local_file("/private/files/gone.pdf"))
		self.assertTrue(fa.is_missing_local_file("/files/Home/gone.jpg"))

	def test_files_on_disk_are_not_flagged(self):
		self.assertFalse(fa.is_missing_local_file("/files/present.jpg"))
		self.assertFalse(fa.is_missing_local_file("/files/Home/nested.jpg"))
		self.assertFalse(fa.is_missing_local_file("/private/files/secret.pdf"))

	def test_public_and_private_folders_are_not_mixed_up(self):
		self.assertTrue(fa.is_missing_local_file("/files/secret.pdf"))
		self.assertTrue(fa.is_missing_local_file("/private/files/present.jpg"))

	def test_quoted_and_query_string_forms_of_a_present_file_are_not_flagged(self):
		self.assertFalse(fa.is_missing_local_file("/files/with%20space.jpg"))
		self.assertFalse(fa.is_missing_local_file("/files/with space.jpg"))
		self.assertFalse(fa.is_missing_local_file("/files/present.jpg?fid=abc123"))

	def test_values_that_are_not_local_paths_are_ignored(self):
		for value in (
			None,
			"",
			42,
			"https://gk.example.com/files/gone.jpg",
			"/api/method/frappe.utils.print_format.download_pdf",
			"files/gone.jpg",
			"/filesgone.jpg",
		):
			with self.subTest(value=value):
				self.assertFalse(fa.is_missing_local_file(value))

	def test_paths_without_a_file_name_or_leaving_the_folder_are_left_alone(self):
		for value in (
			"/files/",
			"/files//",
			"/files/../site_config.json",
			"/private/files/a/../../x",
		):
			with self.subTest(value=value):
				self.assertFalse(fa.is_missing_local_file(value))


class TestNormalizeForeignAttachments(_SiteFiles):
	def test_missing_paths_are_pointed_at_the_origin(self):
		doc = _doc(image="/files/gone.jpg", top_view_finish="/private/files/gone.pdf")

		with _origin(ORIGIN):
			fa.normalize_foreign_attachments(doc, "before_validate")

		self.assertEqual(doc.image, ORIGIN + "/files/gone.jpg")
		self.assertEqual(doc.top_view_finish, ORIGIN + "/private/files/gone.pdf")

	def test_local_files_absolute_urls_and_empty_fields_are_untouched(self):
		doc = _doc(
			image="/files/present.jpg",
			front_view="https://cdn.example.com/a.jpg",
			top_view=None,
		)

		with _origin(ORIGIN):
			fa.normalize_foreign_attachments(doc, "before_validate")

		self.assertEqual(doc.image, "/files/present.jpg")
		self.assertEqual(doc.front_view, "https://cdn.example.com/a.jpg")
		self.assertIsNone(doc.top_view)

	def test_nothing_changes_without_an_origin(self):
		doc = _doc(image="/files/gone.jpg")

		with _origin(None):
			fa.normalize_foreign_attachments(doc, "before_validate")

		self.assertEqual(doc.image, "/files/gone.jpg")
		doc.meta.get.assert_not_called()

	def test_origin_is_trimmed(self):
		doc = _doc(image="/files/gone.jpg")

		with _origin(f"  {ORIGIN}/  "):
			fa.normalize_foreign_attachments(doc, "before_validate")

		self.assertEqual(doc.image, ORIGIN + "/files/gone.jpg")

	def test_origin_without_a_scheme_or_pointing_at_this_site_is_ignored(self):
		for origin in ("gk.example.com", OWN_SITE, OWN_SITE + "/"):
			with self.subTest(origin=origin):
				doc = _doc(image="/files/gone.jpg")

				with _origin(origin):
					fa.normalize_foreign_attachments(doc, "before_validate")

				self.assertEqual(doc.image, "/files/gone.jpg")

	def test_only_attach_fields_are_read(self):
		doc = _doc(image="/files/gone.jpg")

		with _origin(ORIGIN):
			fa.normalize_foreign_attachments(doc, "before_validate")

		doc.meta.get.assert_called_once_with(
			"fields", {"fieldtype": ["in", ("Attach", "Attach Image")]}
		)


def _meta(fields, issingle=0, istable=0):
	meta = MagicMock(issingle=issingle, istable=istable)
	meta.get.side_effect = lambda key, filters=None: (
		[SimpleNamespace(fieldname=fieldname) for fieldname in fields]
		if key == "fields"
		else 0
	)
	return meta


class TestRepair(_SiteFiles):
	"""repair() against a mocked database: one doctype, two pages of rows."""

	PAGES = (
		[
			frappe._dict(
				name="BOM-1",
				image="/files/gone.jpg",
				top_view_finish="/files/present.jpg",
			),
			frappe._dict(
				name="BOM-2",
				image="/private/files/gone.pdf",
				top_view_finish="/files/gone2.jpg",
			),
		],
		[frappe._dict(name="BOM-3", image="/files/present.jpg", top_view_finish=None)],
	)

	def setUp(self):
		super().setUp()
		fa.now()  # loads System Settings (timezone) before frappe.db is mocked
		self.queries = []
		pages = list(self.PAGES)

		def sql(query, values=None, as_dict=False):
			self.queries.append((query, dict(values)))
			return pages.pop(0) if pages else []

		self.db = MagicMock()
		self.db.exists.return_value = True
		self.db.get_table_columns.return_value = [
			"name",
			"image",
			"top_view_finish",
			"item",
		]
		self.db.sql.side_effect = sql

		for target, value in (
			("db", self.db),
			(
				"get_meta",
				MagicMock(
					return_value=_meta(["image", "top_view_finish", "no_column"])
				),
			),
			("log_error", MagicMock()),
		):
			patcher = patch.object(fa.frappe, target, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_dry_run_counts_without_writing(self):
		with _origin(ORIGIN):
			summary = fa.repair(dry_run=1, doctypes=["BOM"])

		bom = summary["doctypes"]["BOM"]
		self.assertEqual((bom["rows"], bom["values"], bom["private"]), (2, 3, 1))
		self.assertEqual(bom["fields"], {"image": 2, "top_view_finish": 1})
		self.assertEqual(
			[sample["name"] for sample in bom["samples"]], ["BOM-1", "BOM-2"]
		)
		self.assertEqual(
			summary["totals"],
			{"rows": 2, "values": 3, "private": 1, "distinct_paths": 3},
		)
		self.db.set_value.assert_not_called()
		self.db.commit.assert_called_once()
		fa.frappe.log_error.assert_called_once()
		self.assertEqual(
			fa.frappe.log_error.call_args.kwargs["title"],
			"Foreign attachment repair (dry run)",
		)

	def test_repair_rewrites_with_set_value_and_commits_each_page(self):
		with _origin(ORIGIN):
			fa.repair(dry_run=0, doctypes=["BOM"])

		self.assertEqual(
			self.db.set_value.call_args_list,
			[
				(
					("BOM", "BOM-1", {"image": ORIGIN + "/files/gone.jpg"}),
					{"update_modified": False},
				),
				(
					(
						"BOM",
						"BOM-2",
						{
							"image": ORIGIN + "/private/files/gone.pdf",
							"top_view_finish": ORIGIN + "/files/gone2.jpg",
						},
					),
					{"update_modified": False},
				),
			],
		)
		# one commit per non-empty page, then one after the summary
		self.assertEqual(self.db.commit.call_count, 3)
		self.assertEqual(
			fa.frappe.log_error.call_args.kwargs["title"], "Foreign attachment repair"
		)

	def test_pages_by_name_and_reads_only_attach_columns(self):
		with _origin(ORIGIN):
			fa.repair(dry_run=1, doctypes=["BOM"])

		self.assertEqual(
			[values["after"] for _query, values in self.queries], ["", "BOM-2", "BOM-3"]
		)
		query, values = self.queries[0]
		self.assertIn("SELECT `name`, `image`, `top_view_finish` FROM `tabBOM`", query)
		self.assertIn("`name` > %(after)s", query)
		self.assertNotIn("no_column", query)
		self.assertEqual(
			(values["public"], values["private"]), ("/files/%", "/private/files/%")
		)

	def test_repair_needs_an_origin(self):
		with _origin(None):
			self.assertRaises(
				frappe.ValidationError, fa.repair, dry_run=0, doctypes=["BOM"]
			)

		self.db.sql.assert_not_called()

	def test_doctypes_missing_on_the_site_are_skipped(self):
		self.db.exists.return_value = False

		with _origin(ORIGIN):
			summary = fa.repair(dry_run=1, doctypes=["Product Return Order"])

		self.assertIn("skipped", summary["doctypes"]["Product Return Order"])
		self.db.sql.assert_not_called()

	def test_doctypes_argument_forms(self):
		self.assertEqual(fa._parse_doctypes(None), list(fa.DOCTYPES))
		self.assertEqual(fa._parse_doctypes('["BOM", "Item"]'), ["BOM", "Item"])
		self.assertEqual(fa._parse_doctypes("Item"), ["Item"])


class TestCheckPathsOnOrigin(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_counts_replies_and_quotes_paths(self):
		replies = {"/files/a%20b.jpg": 200, "/files/gone.jpg": 404}
		head = MagicMock(
			side_effect=lambda url, **kwargs: SimpleNamespace(
				status_code=replies[url[len(ORIGIN) :]]
			)
		)

		with patch("requests.head", head):
			result = fa.check_paths_on_origin(
				ORIGIN, {"/files/a b.jpg", "/files/gone.jpg"}
			)

		self.assertEqual(result["checked"], 2)
		self.assertEqual(result["status_counts"], {"200": 1, "404": 1})
		self.assertEqual(result["examples_404"], ["/files/gone.jpg"])

	def test_connection_errors_are_counted_not_raised(self):
		import requests

		with patch(
			"requests.head", MagicMock(side_effect=requests.ConnectionError("down"))
		):
			result = fa.check_paths_on_origin(ORIGIN, {"/files/a.jpg"})

		self.assertEqual(result["status_counts"], {"ConnectionError": 1})


class TestEnqueueRepair(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_queues_one_deduplicated_job_on_long(self):
		with (
			patch.object(fa.frappe, "only_for") as only_for,
			patch.object(fa.frappe, "enqueue", return_value=MagicMock()) as enqueue,
		):
			message = fa.enqueue_repair(dry_run="1", check_origin="1")

		only_for.assert_called_once_with("System Manager")
		enqueue.assert_called_once_with(
			"jewellery_erpnext.foreign_attachments.repair",
			queue="long",
			timeout=4 * 60 * 60,
			job_id=fa.JOB_ID,
			deduplicate=True,
			dry_run=1,
			check_origin=1,
			doctypes=None,
		)
		self.assertIn("Queued", message)

	def test_reports_a_run_already_in_progress(self):
		with patch.object(fa.frappe, "only_for"), patch.object(
			fa.frappe, "enqueue", return_value=None
		):
			self.assertIn("already", fa.enqueue_repair())

	def test_needs_system_manager(self):
		with (
			patch.object(fa.frappe, "only_for", side_effect=frappe.PermissionError),
			patch.object(fa.frappe, "enqueue") as enqueue,
		):
			self.assertRaises(frappe.PermissionError, fa.enqueue_repair)

		enqueue.assert_not_called()


class TestThroughFrappe(IntegrationTestCase):
	"""The real hook dispatch and core attach hook on the test site; rolled back per class."""

	def _new_item(self, image):
		item = frappe.new_doc("Item")
		item.item_code = item.name = "_Test Foreign Attachment Item"
		item.image = image
		return item

	def _attach_errors_logged(self, item):
		# Spy instead of counting rows: tabError Log is MyISAM, so a real row would survive
		# the class rollback and stay on the test site.
		with patch.object(frappe, "log_error") as log_error:
			attach_files_to_document(item, "on_update")

		return sum(
			1
			for logged in log_error.call_args_list
			if logged.kwargs.get("title") == "Error Attaching File"
			and logged.kwargs.get("reference_name") == item.name
		)

	def test_guard_is_wired_on_before_validate(self):
		doc_events = frappe.get_hooks("doc_events")
		for doctype in fa.DOCTYPES:
			with self.subTest(doctype=doctype):
				self.assertIn(GUARD, doc_events[doctype]["before_validate"])

	def test_item_before_validate_points_missing_image_at_origin(self):
		item = self._new_item(MISSING)

		with _origin(ORIGIN):
			item.run_method("before_validate")

		self.assertEqual(item.image, ORIGIN + MISSING)

	def test_core_attach_hook_logs_an_error_for_the_relative_path(self):
		self.assertEqual(self._attach_errors_logged(self._new_item(MISSING)), 1)

	def test_core_attach_hook_logs_nothing_once_pointed_at_origin(self):
		item = self._new_item(MISSING)

		with _origin(ORIGIN):
			item.run_method("before_validate")

		self.assertEqual(self._attach_errors_logged(item), 0)
