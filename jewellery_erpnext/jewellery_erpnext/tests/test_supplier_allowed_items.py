# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Unit tests for the per-supplier Metal whitelist (Supplier.custom_allowed_item_group).

The defining property under test is the *scope*: the whitelist gates Metal item groups and
nothing else. Every other item group -- Diamond, Gemstone, Finding, Design, Consumable and
the deprecated Metal DNU -- must pass through untouched no matter what the supplier's table
says. The second property is that an empty table is a strict deny for metal, not an
implicit allow.

DB-free per the suite convention: setUpClass is neutralized and frappe lookups are mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import supplier_allowed_items as sai

METAL_GROUPS = frozenset({"Metal - T", "Metal - V", "Metal Sub - V"})

ITEM_GROUPS = {
	"M-G-22KT-91.9-Y": "Metal - V",
	"M-G-18KT-75.4-Y": "Metal - V",
	"ML": "Metal - T",
	"M-SUB-01": "Metal Sub - V",  # descendant group, still in scope
	"M-DNU-01": "Metal DNU",  # deprecated group, deliberately OUT of scope
	"D-RND-01": "Diamond - V",
	"F-CLASP-01": "Finding - V",
}


def _doc(supplier, item_codes, doctype="Purchase Order"):
	"""A minimal stand-in for a buying document."""
	return frappe._dict(
		doctype=doctype,
		name="TEST-001",
		supplier=supplier,
		items=[
			frappe._dict(idx=i + 1, item_code=code) for i, code in enumerate(item_codes)
		],
	)


def _item_group(_doctype, item_code, _fieldname):
	return ITEM_GROUPS.get(item_code)


class TestSupplierMetalWhitelistScope(IntegrationTestCase):
	"""validate() must gate Metal only, and treat an empty table as deny-all-metal."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		groups = patch.object(sai, "get_metal_item_groups", return_value=METAL_GROUPS)
		cached = patch.object(sai.frappe, "get_cached_value", side_effect=_item_group)
		for p in (groups, cached):
			p.start()
			self.addCleanup(p.stop)

	def _allow(self, *item_codes):
		p = patch.object(sai, "get_allowed_metal_items", return_value=set(item_codes))
		p.start()
		self.addCleanup(p.stop)

	def test_non_metal_items_pass_with_an_empty_whitelist(self):
		# The whole point of the feature: Diamond/Finding are never restricted.
		self._allow()
		sai.validate(_doc("SUP-001", ["D-RND-01", "F-CLASP-01"]))

	def test_metal_item_blocked_when_whitelist_is_empty(self):
		self._allow()
		with self.assertRaises(frappe.ValidationError):
			sai.validate(_doc("SUP-001", ["M-G-22KT-91.9-Y"]))

	def test_metal_item_allowed_when_listed(self):
		self._allow("M-G-22KT-91.9-Y")
		sai.validate(_doc("SUP-001", ["M-G-22KT-91.9-Y"]))

	def test_metal_item_blocked_when_absent_from_a_populated_whitelist(self):
		self._allow("M-G-18KT-75.4-Y")
		with self.assertRaises(frappe.ValidationError):
			sai.validate(_doc("SUP-001", ["M-G-22KT-91.9-Y"]))

	def test_message_names_the_offending_row_and_item(self):
		self._allow("M-G-18KT-75.4-Y")
		doc = _doc("SUP-001", ["D-RND-01", "M-G-18KT-75.4-Y", "M-G-22KT-91.9-Y"])
		with self.assertRaises(frappe.ValidationError) as ctx:
			sai.validate(doc)
		message = str(ctx.exception)
		self.assertIn("Row #3", message)
		self.assertIn("M-G-22KT-91.9-Y", message)
		# The compliant rows must not be reported.
		self.assertNotIn("Row #1", message)
		self.assertNotIn("Row #2", message)

	def test_metal_dnu_is_out_of_scope(self):
		# Metal DNU is the deprecated group and is deliberately not restricted.
		self._allow()
		sai.validate(_doc("SUP-001", ["M-DNU-01"]))

	def test_descendant_metal_group_is_in_scope(self):
		self._allow()
		with self.assertRaises(frappe.ValidationError):
			sai.validate(_doc("SUP-001", ["M-SUB-01"]))

	def test_metal_template_group_is_in_scope(self):
		self._allow()
		with self.assertRaises(frappe.ValidationError):
			sai.validate(_doc("SUP-001", ["ML"]))

	def test_document_without_supplier_is_skipped(self):
		self._allow()
		sai.validate(_doc(None, ["M-G-22KT-91.9-Y"]))

	def test_rows_without_an_item_code_are_skipped(self):
		self._allow()
		sai.validate(_doc("SUP-001", [None]))

	def test_unknown_item_is_left_to_link_validation(self):
		# An item_code with no Item record resolves to no group -- not our error to raise.
		self._allow()
		sai.validate(_doc("SUP-001", ["DOES-NOT-EXIST"]))

	def test_applies_to_every_wired_buying_doctype(self):
		self._allow()
		for doctype in (
			"Purchase Order",
			"Purchase Receipt",
			"Purchase Invoice",
			"Supplier Quotation",
		):
			with self.subTest(doctype=doctype):
				with self.assertRaises(frappe.ValidationError):
					sai.validate(_doc("SUP-001", ["M-G-22KT-91.9-Y"], doctype=doctype))


class TestMetalScopeResolution(IntegrationTestCase):
	"""get_metal_item_groups() = roots + nested-set descendants, missing roots skipped."""

	@classmethod
	def setUpClass(cls):
		pass

	# NOTE: patch db.exists itself, not the whole frappe.db -- patch.object on frappe.db
	# yields an AsyncMock whose calls return truthy coroutines, silently defeating the
	# side_effect and making the "missing root" case look present.

	def test_roots_and_descendants_are_collected(self):
		descendants = {"Metal - T": ["Metal Sub - T"], "Metal - V": ["Metal Sub - V"]}
		with patch.object(sai.frappe.db, "exists", return_value=True), patch.object(
			sai,
			"get_descendants_of",
			side_effect=lambda _dt, name, **kw: descendants[name],
		):
			self.assertEqual(
				sai.get_metal_item_groups(),
				frozenset({"Metal - T", "Metal - V", "Metal Sub - T", "Metal Sub - V"}),
			)

	def test_missing_root_is_skipped_not_raised(self):
		with patch.object(
			sai.frappe.db, "exists", side_effect=lambda _dt, name: name == "Metal - V"
		), patch.object(sai, "get_descendants_of", return_value=[]) as mock_descendants:
			self.assertEqual(sai.get_metal_item_groups(), frozenset({"Metal - V"}))
			mock_descendants.assert_called_once()

	def test_metal_dnu_is_not_a_configured_root(self):
		self.assertNotIn("Metal DNU", sai.METAL_ROOT_ITEM_GROUPS)


class TestBlockedItemsAndLinkQuery(IntegrationTestCase):
	"""The dropdown filter excludes exactly the non-whitelisted metal items."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_blocked_is_the_complement_of_the_whitelist(self):
		metal_items = ["M-G-22KT-91.9-Y", "M-G-18KT-75.4-Y", "ML"]
		with patch.object(
			sai, "get_metal_item_groups", return_value=METAL_GROUPS
		), patch.object(sai.frappe, "get_all", return_value=metal_items), patch.object(
			sai, "get_allowed_metal_items", return_value={"M-G-18KT-75.4-Y"}
		):
			self.assertEqual(
				sai.get_blocked_metal_items("SUP-001"), ["M-G-22KT-91.9-Y", "ML"]
			)

	def test_nothing_blocked_when_the_site_has_no_metal_groups(self):
		with patch.object(sai, "get_metal_item_groups", return_value=frozenset()):
			self.assertEqual(sai.get_blocked_metal_items("SUP-001"), [])

	def test_query_injects_the_exclusion_and_delegates(self):
		# __wrapped__ skips validate_and_sanitize_search_inputs, which would hit the DB.
		captured = {}

		def fake_item_query(doctype, txt, searchfield, start, page_len, filters):
			captured.update(filters)
			return []

		with patch.object(
			sai, "get_blocked_metal_items", return_value=["M-G-22KT-91.9-Y"]
		):
			with patch("erpnext.controllers.queries.item_query", fake_item_query):
				sai.supplier_item_query.__wrapped__(
					"Item",
					"M-G",
					"name",
					0,
					20,
					{"supplier": "SUP-001", "is_purchase_item": 1},
				)

		self.assertEqual(captured["name"], ["not in", ["M-G-22KT-91.9-Y"]])
		self.assertEqual(captured["is_purchase_item"], 1)

	def test_query_is_untouched_without_a_supplier(self):
		captured = {}

		def fake_item_query(doctype, txt, searchfield, start, page_len, filters):
			captured.update(filters)
			return []

		with patch("erpnext.controllers.queries.item_query", fake_item_query):
			sai.supplier_item_query.__wrapped__("Item", "M-G", "name", 0, 20, {})

		self.assertNotIn("name", captured)


class TestSupplierRowValidation(IntegrationTestCase):
	"""The Supplier's own table must stay coherent: metal groups, matching items, no dupes."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		groups = patch.object(sai, "get_metal_item_groups", return_value=METAL_GROUPS)
		cached = patch.object(sai.frappe, "get_cached_value", side_effect=_item_group)
		for p in (groups, cached):
			p.start()
			self.addCleanup(p.stop)

	def _supplier(self, rows):
		return frappe._dict(
			custom_allowed_item_group=[
				frappe._dict(idx=i + 1, item_group=group, item_code=code)
				for i, (group, code) in enumerate(rows)
			]
		)

	def test_valid_rows_pass(self):
		sai.validate_supplier_rows(
			self._supplier([("Metal - V", "M-G-22KT-91.9-Y"), ("Metal - T", "ML")])
		)

	def test_empty_table_passes(self):
		sai.validate_supplier_rows(frappe._dict(custom_allowed_item_group=[]))

	def test_non_metal_group_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			sai.validate_supplier_rows(self._supplier([("Diamond - V", "D-RND-01")]))

	def test_item_outside_the_stated_group_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			sai.validate_supplier_rows(
				self._supplier([("Metal - T", "M-G-22KT-91.9-Y")])
			)

	def test_duplicate_item_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			sai.validate_supplier_rows(
				self._supplier(
					[("Metal - V", "M-G-22KT-91.9-Y"), ("Metal - V", "M-G-22KT-91.9-Y")]
				)
			)
