# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Regression tests for code-review findings on doc_events/delivery_note.py.

Mocked/pure-logic style (see test_bulk_map.py): ``setUpClass`` is a no-op, fake
docs are SimpleNamespace / frappe._dict, and every DB reader is patched — these
must stay runnable on a site with no fixtures.

Pins four fixes:
  1. ``bom_cache`` is keyed by ``row.bom`` (not ``row.idx``), so rows sharing a
     BOM issue exactly one ``frappe.get_doc("BOM", ...)`` call.
  2. ``_match_einvoice_item`` raises on an operator it doesn't implement instead
     of silently dropping the clause and letting the first row win.
  3. The E Invoice Item prefetch uses ``order_by="creation desc"`` so
     ``_match_einvoice_item``'s first-match result agrees with the single-row
     ``frappe.db.get_value`` tie-break it replaces.
  4. The certification/gemstone E Invoice Item lookups are hoisted out of the
     per-row / per-gemstone-line loops, matching the hallmarking lookup.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import delivery_note as dn_module


def _row(idx, bom=None, against_sales_order=None):
	return SimpleNamespace(
		idx=idx,
		bom=bom,
		against_sales_order=against_sales_order,
		amount=0,
		custom_diamond_pcs=0,
		custom_gemstone_pcs=0,
		custom_other_weight=0,
		custom_metal_weight=0,
		custom_finding_weight=0,
		custom_diamond_weight=0,
		custom_gemstone_weight=0,
		custom_gross_weight=0,
	)


class TestBomCacheKeyedByBom(IntegrationTestCase):
	"""Finding #2: bom_cache must key off row.bom, not row.idx."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_two_rows_sharing_a_bom_issue_a_single_get_doc_call(self):
		self_doc = SimpleNamespace(
			items=[
				_row(1, bom="BOM-SHARED", against_sales_order="SO-1"),
				_row(2, bom="BOM-SHARED", against_sales_order="SO-1"),
			],
			calculate_taxes_and_totals=MagicMock(),
		)
		fake_bom = SimpleNamespace(
			total_diamond_pcs=1,
			total_gemstone_pcs=1,
			total_other_weight=1,
			total_metal_weight=1,
			finding_weight=1,
			total_diamond_weight_in_gms=1,
			total_gemstone_weight_in_gms=1,
			gross_weight=1,
		)
		with (
			patch.object(dn_module.frappe, "get_doc", return_value=fake_bom) as get_doc,
			patch.object(dn_module, "update_dn_einvoice_items") as update_items,
			patch.object(dn_module, "set_gst_details"),
			patch.object(dn_module, "apply_einvoice_item_tax"),
		):
			dn_module.validate(self_doc, None)

		get_doc.assert_called_once_with("BOM", "BOM-SHARED")
		passed_cache = update_items.call_args[0][1]
		self.assertEqual(list(passed_cache), ["BOM-SHARED"])

	def test_update_dn_einvoice_items_reuses_prebuilt_cache_entry(self):
		bom_doc = SimpleNamespace(
			metal_detail=[],
			finding_detail=[],
			diamond_detail=[],
			gemstone_detail=[],
			hallmarking_amount=0,
			certification_amount=0,
		)
		self_doc = SimpleNamespace(
			customer="CUST-1",
			sales_type="Finished Goods",
			items=[_row(1, bom="BOM-1"), _row(2, bom="BOM-1")],
		)
		self_doc.set = lambda *a, **k: None
		self_doc.append = lambda *a, **k: None
		with (
			patch.object(dn_module.frappe.db, "get_value", return_value=None),
			patch.object(
				dn_module, "_matching_e_invoice_item_parents", return_value=[]
			),
			patch.object(dn_module.frappe, "get_all", return_value=[]),
			patch.object(dn_module.frappe, "get_doc") as get_doc,
		):
			dn_module.update_dn_einvoice_items(self_doc, {"BOM-1": bom_doc})

		get_doc.assert_not_called()


class TestMatchEinvoiceItemOperators(IntegrationTestCase):
	"""Finding #3: unsupported filter operators must raise, not silently match."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_unsupported_operator_raises(self):
		rows = [frappe._dict(name="EII-1", hsn_code="1", uom="Nos", field="x")]
		with self.assertRaises(ValueError):
			dn_module._match_einvoice_item(rows, {"field": ("like", "%x%")})

	def test_operator_is_casefolded(self):
		rows = [frappe._dict(name="EII-1", hsn_code="1", uom="Nos", field="x")]
		result = dn_module._match_einvoice_item(rows, {"field": ("IN", ["x"])})
		self.assertEqual(result, ("EII-1", "1", "Nos"))

	def test_is_not_set_operand_is_casefolded(self):
		rows = [frappe._dict(name="EII-1", hsn_code="1", uom="Nos", field=None)]
		result = dn_module._match_einvoice_item(rows, {"field": ("is", "Not Set")})
		self.assertEqual(result, ("EII-1", "1", "Nos"))


class TestEinvoiceItemPrefetchOrdering(IntegrationTestCase):
	"""Finding #1: the E Invoice Item prefetch must match get_value's tie-break."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_order_by_matches_get_value_default_tie_break(self):
		self_doc = SimpleNamespace(
			customer="CUST-1", sales_type="Finished Goods", items=[]
		)
		self_doc.set = lambda *a, **k: None
		self_doc.append = lambda *a, **k: None
		with (
			patch.object(dn_module.frappe.db, "get_value", return_value=None),
			patch.object(
				dn_module, "_matching_e_invoice_item_parents", return_value=[]
			),
			patch.object(dn_module.frappe, "get_all", return_value=[]) as get_all,
		):
			dn_module.update_dn_einvoice_items(self_doc)

		self.assertEqual(get_all.call_args.kwargs["order_by"], "creation desc")


class TestHoistedEinvoiceItemLookups(IntegrationTestCase):
	"""Finding #4: certification/gemstone lookups run once, not per row/line."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_certification_and_gemstone_lookups_run_once(self):
		einvoice_rows = [
			frappe._dict(
				name="EII-CERT", hsn_code="9999", uom="Nos", is_for_certification=1
			),
			frappe._dict(name="EII-GEM", hsn_code="7103", uom="Nos", is_for_gemstone=1),
		]

		def fake_get_all(doctype, *args, **kwargs):
			if doctype == "E Invoice Item":
				return einvoice_rows
			if doctype == "Sales Type Multiselect":
				return ["EII-GEM"]
			return []

		gemstone_line = SimpleNamespace(
			is_customer_item=False,
			se_rate=10,
			quantity=1,
			gemstone_rate_for_specified_quantity=10,
		)
		bom_1 = SimpleNamespace(
			metal_detail=[],
			finding_detail=[],
			diamond_detail=[],
			gemstone_detail=[gemstone_line, gemstone_line],
			hallmarking_amount=0,
			certification_amount=50,
		)
		bom_2 = SimpleNamespace(
			metal_detail=[],
			finding_detail=[],
			diamond_detail=[],
			gemstone_detail=[gemstone_line, gemstone_line],
			hallmarking_amount=0,
			certification_amount=50,
		)
		self_doc = SimpleNamespace(
			customer="CUST-1",
			sales_type="Finished Goods",
			items=[_row(1, bom="BOM-1"), _row(2, bom="BOM-2")],
		)
		appended = []
		self_doc.set = lambda *a, **k: None
		self_doc.append = lambda doctype, data: appended.append(data)

		with (
			patch.object(dn_module.frappe.db, "get_value", return_value=None),
			patch.object(dn_module.frappe, "get_all", side_effect=fake_get_all),
			patch.object(
				dn_module.frappe,
				"get_doc",
				side_effect=lambda doctype, name: {"BOM-1": bom_1, "BOM-2": bom_2}[
					name
				],
			),
			patch.object(
				dn_module,
				"_match_einvoice_item",
				wraps=dn_module._match_einvoice_item,
			) as match_mock,
		):
			dn_module.update_dn_einvoice_items(self_doc)

		cert_calls = [
			c for c in match_mock.call_args_list if "is_for_certification" in c[0][1]
		]
		gemstone_calls = [
			c for c in match_mock.call_args_list if "is_for_gemstone" in c[0][1]
		]
		# 2 DN rows each with certification_amount set, and 2 gemstone_detail
		# lines per row (4 total) would call these 2x / 4x if not hoisted.
		self.assertEqual(len(cert_calls), 1)
		self.assertEqual(len(gemstone_calls), 1)
