# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Blocked variants for the Refining Entry form, fetched by arguments without locking the entry.

The form used to call the ``get_blocked_variants`` document method through ``frm.call``, which
posts the whole document to ``run_doc_method``; on a saved entry that runs ``check_if_latest`` ->
``SELECT ... FOR UPDATE`` on the Refining Entry row and every child table, so opening an entry
whose submission was running waited on that submission's lock (1205 on 16 Nov, RFN-SCP-26-00042).
The module-level ``get_blocked_variants_for`` must return exactly what the instance resolution
returns, check doctype-level read, and never load or lock the entry.

Pure: the data reads are mocked, except ``TestNoDocumentLoadOrLockingRead``, which runs the real
plain reads (SELECT only; safe on a read-only session) and records every statement.
The two-connection row-lock test is ``test_refining_form_locks``.
"""

import os
import re
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import frappe

from jewellery_erpnext.refining.constants import (
	REFINING_TYPE_SCRAP,
	REFINING_TYPE_UNUSED,
	REFINING_TYPE_WORK_ORDER,
)
from jewellery_erpnext.refining.doctype.refining_entry import refining_entry
from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	RefiningEntry,
	get_blocked_variants_for,
)

DOTTED = "jewellery_erpnext.refining.doctype.refining_entry.refining_entry.get_blocked_variants_for"
JS_PATH = os.path.join(os.path.dirname(refining_entry.__file__), "refining_entry.js")

# (variant, refining_type) rows per Manufacturing Setting. A blank / None type blocks every type;
# a blank variant is ignored.
ROWS = {
	"MS-A": [
		("ML", REFINING_TYPE_SCRAP),
		("FL", ""),
		("G", None),
		("D", REFINING_TYPE_UNUSED),
		("", REFINING_TYPE_SCRAP),
	],
	"MS-B": [("M", REFINING_TYPE_SCRAP)],
	"MS-EMPTY": [],
}


def _fake_resolve(company=None, manufacturer=None, throw=False):
	"""Stands in for resolve_manufacturing_setting: manufacturer first, then company, else None."""
	if manufacturer == "Mfr-B":
		return "MS-B"
	if manufacturer == "Mfr-Empty":
		return "MS-EMPTY"
	if company == "Co-1":
		return "MS-A"
	return None


# (company, refining_type, manufacturer) -> blocked list
CASES = [
	(("Co-1", REFINING_TYPE_SCRAP, "Mfr-A"), ["FL", "G", "ML"]),
	(("Co-1", REFINING_TYPE_UNUSED, "Mfr-A"), ["D", "FL", "G"]),
	(("Co-1", REFINING_TYPE_WORK_ORDER, "Mfr-A"), ["FL", "G"]),
	(("Co-1", None, "Mfr-A"), ["FL", "G"]),
	(("Co-1", "", "Mfr-A"), ["FL", "G"]),
	(("Co-1", REFINING_TYPE_SCRAP, None), ["FL", "G", "ML"]),
	(("Co-1", REFINING_TYPE_SCRAP, ""), ["FL", "G", "ML"]),
	(("Co-1", REFINING_TYPE_SCRAP, "Mfr-B"), ["M"]),
	(("Co-1", REFINING_TYPE_SCRAP, "Mfr-Empty"), []),
	(("Co-2", REFINING_TYPE_SCRAP, None), []),
	((None, REFINING_TYPE_SCRAP, None), []),
	(("", "", ""), []),
]


def _entry(company, refining_type, manufacturer):
	"""An unsaved Refining Entry holding the same values the form would post."""
	return frappe.get_doc(
		{
			"doctype": "Refining Entry",
			"company": company,
			"refining_type": refining_type,
			"manufacturer": manufacturer,
		}
	)


@contextmanager
def _restriction_data(table_exists=True):
	"""Mock the three reads behind the shared implementation; yields the resolver mock and the
	restriction-row reads so a test can assert what was asked for."""
	real_table_exists, real_get_all = frappe.db.table_exists, frappe.db.get_all
	row_reads = []

	def fake_table_exists(doctype, *args, **kwargs):
		if doctype == "Refining Variant Restriction":
			return table_exists
		return real_table_exists(doctype, *args, **kwargs)

	def fake_get_all(doctype, *args, **kwargs):
		if doctype != "Refining Variant Restriction":
			return real_get_all(doctype, *args, **kwargs)
		filters = kwargs.get("filters") or (args[0] if args else {})
		row_reads.append(filters)
		return [
			frappe._dict(variant=variant, refining_type=refining_type)
			for variant, refining_type in ROWS.get(filters["parent"], [])
		]

	with patch.object(
		refining_entry, "resolve_manufacturing_setting", side_effect=_fake_resolve
	) as resolver, patch.object(
		frappe.db, "table_exists", side_effect=fake_table_exists
	), patch.object(frappe.db, "get_all", side_effect=fake_get_all), patch.object(
		frappe, "has_permission", return_value=True
	):
		yield resolver, row_reads


class TestSameResultAsTheInstanceResolution(unittest.TestCase):
	def test_matches_the_instance_method_for_representative_inputs(self):
		for args, expected in CASES:
			with self.subTest(args=args):
				with _restriction_data():
					by_args = get_blocked_variants_for(*args)
					doc = _entry(*args)
					self.assertEqual(by_args, doc.get_blocked_variants())
					self.assertEqual(by_args, sorted(doc._restricted_variants()))
				self.assertEqual(by_args, expected)

	def test_keyword_call_as_the_desk_sends_it(self):
		with _restriction_data():
			self.assertEqual(
				get_blocked_variants_for(
					company="Co-1",
					refining_type=REFINING_TYPE_SCRAP,
					manufacturer="Mfr-A",
				),
				["FL", "G", "ML"],
			)
			self.assertEqual(get_blocked_variants_for(), [])

	def test_resolves_with_the_same_company_and_manufacturer(self):
		with _restriction_data() as (resolver, _reads):
			get_blocked_variants_for("Co-1", REFINING_TYPE_SCRAP, "Mfr-B")
			_entry("Co-1", REFINING_TYPE_SCRAP, "Mfr-B")._restricted_variants()
		self.assertEqual(
			[c.args for c in resolver.call_args_list],
			[("Co-1", "Mfr-B"), ("Co-1", "Mfr-B")],
		)

	def test_reads_the_resolved_settings_restriction_rows_only(self):
		with _restriction_data() as (_resolver, reads):
			get_blocked_variants_for("Co-1", REFINING_TYPE_SCRAP, "Mfr-A")
		self.assertEqual(
			reads,
			[
				{
					"parent": "MS-A",
					"parenttype": "Manufacturing Setting",
					"parentfield": "refining_variant_restrictions",
				}
			],
		)

	def test_fails_open_on_an_unmigrated_site(self):
		with _restriction_data(table_exists=False) as (resolver, reads):
			self.assertEqual(
				get_blocked_variants_for("Co-1", REFINING_TYPE_SCRAP, "Mfr-A"), []
			)
			self.assertEqual(
				_entry("Co-1", REFINING_TYPE_SCRAP, "Mfr-A").get_blocked_variants(), []
			)
		resolver.assert_not_called()
		self.assertEqual(reads, [])

	def test_returns_a_sorted_list(self):
		with _restriction_data():
			result = get_blocked_variants_for("Co-1", REFINING_TYPE_UNUSED, "Mfr-A")
		self.assertIsInstance(result, list)
		self.assertEqual(result, sorted(result))


class TestInstanceMethodIsAThinWrapper(unittest.TestCase):
	def test_wrapper_delegates_once_per_refining_type(self):
		doc = _entry("Co-1", REFINING_TYPE_SCRAP, "Mfr-A")
		shared = Mock(return_value=frozenset({"ML", "FL"}))
		with patch.object(refining_entry, "restricted_variants_for", shared):
			self.assertEqual(doc.get_blocked_variants(), ["FL", "ML"])
			self.assertEqual(doc.get_blocked_variants(), ["FL", "ML"])
			shared.assert_called_once_with("Co-1", REFINING_TYPE_SCRAP, "Mfr-A")

			doc.refining_type = REFINING_TYPE_UNUSED
			doc.get_blocked_variants()
		self.assertEqual(shared.call_count, 2)
		self.assertEqual(shared.call_args.args, ("Co-1", REFINING_TYPE_UNUSED, "Mfr-A"))

	def test_both_endpoints_stay_whitelisted(self):
		# The instance method stays for one release so a browser on the old form script works.
		self.assertIn(get_blocked_variants_for, frappe.whitelisted)
		self.assertNotIn(get_blocked_variants_for, frappe.guest_methods)
		self.assertIn(RefiningEntry.get_blocked_variants, frappe.whitelisted)
		self.assertIs(frappe.get_attr(DOTTED), get_blocked_variants_for)


class TestPermission(unittest.TestCase):
	def test_checks_doctype_level_read(self):
		check = Mock(return_value=True)
		with patch.object(frappe, "has_permission", check), patch.object(
			refining_entry, "restricted_variants_for", return_value=frozenset()
		):
			get_blocked_variants_for("Co-1", REFINING_TYPE_SCRAP, "Mfr-A")
		check.assert_called_once_with("Refining Entry", "read", throw=True)

	def test_refused_before_anything_is_read(self):
		shared = Mock(return_value=frozenset({"ML"}))
		with patch.object(
			frappe, "has_permission", side_effect=frappe.PermissionError
		), patch.object(refining_entry, "restricted_variants_for", shared):
			with self.assertRaises(frappe.PermissionError):
				get_blocked_variants_for("Co-1", REFINING_TYPE_SCRAP, "Mfr-A")
		shared.assert_not_called()

	def test_guest_is_refused_by_the_real_check(self):
		user = frappe.session.user
		self.addCleanup(frappe.set_user, user)
		shared = Mock(return_value=frozenset({"ML"}))
		frappe.set_user("Guest")
		with patch.object(refining_entry, "restricted_variants_for", shared):
			with self.assertRaises(frappe.PermissionError):
				get_blocked_variants_for("Co-1", REFINING_TYPE_SCRAP, "Mfr-A")
		shared.assert_not_called()


class TestNoDocumentLoadOrLockingRead(unittest.TestCase):
	"""Real reads against whatever Manufacturing Setting the site has, every statement recorded."""

	@contextmanager
	def _recording(self):
		real_sql, real_get_doc = frappe.db.sql, frappe.get_doc
		statements, docs = [], []

		def sql(query, *args, **kwargs):
			statements.append(str(query))
			return real_sql(query, *args, **kwargs)

		def get_doc(*args, **kwargs):
			docs.append((args, kwargs))
			return real_get_doc(*args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=sql), patch.object(
			frappe, "get_doc", side_effect=get_doc
		):
			yield statements, docs

	def _inputs(self):
		setting = frappe.db.get_value(
			"Manufacturing Setting",
			{},
			["company", "manufacturer"],
			as_dict=True,
			order_by="creation asc",
		)
		return (setting.company, setting.manufacturer) if setting else ("Co-1", None)

	def test_no_document_load_and_no_locking_statement(self):
		company, manufacturer = self._inputs()
		entry_tables = {"tabRefining Entry"} | {
			f"tab{df.options}"
			for df in frappe.get_meta("Refining Entry").get_table_fields()
		}
		for refining_type in (REFINING_TYPE_SCRAP, REFINING_TYPE_UNUSED, None):
			with self.subTest(refining_type=refining_type):
				with self._recording() as (statements, docs):
					result = get_blocked_variants_for(
						company, refining_type, manufacturer
					)
				self.assertEqual(docs, [], "must not load any document")
				self.assertTrue(statements, "the recorder saw no statement")
				for query in statements:
					self.assertNotRegex(
						query,
						re.compile(r"FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE", re.I),
					)
					self.assertFalse(
						[t for t in entry_tables if f"`{t}`" in query],
						f"must not read the entry or its child tables: {query}",
					)
					self.assertRegex(
						query.lstrip(), re.compile(r"^(SELECT|SHOW|\()", re.I)
					)
				# Same answer as the instance resolution on the live data.
				self.assertEqual(
					result,
					_entry(company, refining_type, manufacturer).get_blocked_variants(),
				)


class TestFormCallsTheArgumentEndpoint(unittest.TestCase):
	def setUp(self):
		with open(JS_PATH, encoding="utf-8") as f:
			self.js = f.read()
		match = re.search(
			r"\n\tload_variant_restrictions\(frm\) \{\n(.*?)\n\t\},\n", self.js, re.S
		)
		self.assertIsNotNone(match, "load_variant_restrictions(frm) not found")
		self.loader = match.group(1)

	def test_loader_calls_the_module_function_with_the_forms_values(self):
		self.assertIn("frappe\n\t\t\t.call({", self.loader)
		self.assertIn(f'method: "{DOTTED}"', self.loader)
		for field in ("company", "refining_type", "manufacturer"):
			self.assertIn(f"{field}: frm.doc.{field},", self.loader)

	def test_form_no_longer_posts_the_document_for_it(self):
		self.assertNotIn('frm.call("get_blocked_variants")', self.js)
		self.assertNotIn("frm.call('get_blocked_variants')", self.js)
