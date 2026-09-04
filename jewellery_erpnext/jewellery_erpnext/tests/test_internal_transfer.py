# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the internal-transfer Purchase Order/Purchase Receipt -> Purchase Receipt
/ Purchase Invoice fix.

Three whitelisted-method overrides share one pair of fill_purchase_*_references()
helpers in customization/utils/internal_transfer.py:
  - doc_events.purchase_order.make_purchase_receipt   (PO's "Create -> Purchase Receipt")
  - doc_events.purchase_order.make_purchase_invoice    (PO's "Create -> Purchase Invoice")
  - doc_events.purchase_receipt.make_purchase_invoice  (PR's "Create -> Purchase Invoice",
    a *different* core function than the one above, hence its own override)

DB-free per the suite convention: setUpClass is neutralised and frappe.qb/frappe.db
are mocked. `get_delivery_note_item_links` and `get_sales_invoice_item_links` each
issue two batched frappe.qb queries (a candidates lookup, then an already-consumed
exclusion lookup) -- `get_sales_invoice_item_links` also issues one frappe.get_all call
before those, to resolve po_detail -> the source Purchase Order Item's sales_order/
sales_order_item. frappe.qb is mocked wholesale and `frappe.qb.from_` is given a
`side_effect` list so calls return their query chains in the fixed order the
implementation issues them. The three `make_*` overrides delegate to core ERPNext's
real mappers via a local import inside the function body specifically so the origin
(e.g. `erpnext...purchase_order.make_purchase_receipt`) is what needs patching in
tests, not the jewellery_erpnext module attribute; the fill_purchase_*_references()
calls inside them are tested separately from the resolvers they call, so those tests
mock the fill_* functions directly rather than re-deriving resolver behaviour.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils import (
	internal_transfer as it_mod,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import purchase_order as po_events
from jewellery_erpnext.jewellery_erpnext.doc_events import purchase_receipt as pr_events


def _dn_query_chain(return_value):
	"""Mock chain matching: qb.from_(DNI).join(DN).on(...).select(...).where()x3.run()"""
	chain = MagicMock()
	chain.join.return_value.on.return_value.select.return_value.where.return_value.where.return_value.where.return_value.run.return_value = return_value
	return chain


def _pr_query_chain(return_value):
	"""Mock chain matching: qb.from_(PRI).join(PR).on(...).select(...).where()x2.run()"""
	chain = MagicMock()
	chain.join.return_value.on.return_value.select.return_value.where.return_value.where.return_value.run.return_value = return_value
	return chain


def _si_query_chain(return_value):
	"""Mock chain matching: qb.from_(SII).join(SI).on(...).select(...).where()x3.run()"""
	chain = MagicMock()
	chain.join.return_value.on.return_value.select.return_value.where.return_value.where.return_value.where.return_value.run.return_value = return_value
	return chain


def _pi_query_chain(return_value):
	"""Mock chain matching: qb.from_(PII).join(PI).on(...).select(...).where()x2.run()"""
	chain = MagicMock()
	chain.join.return_value.on.return_value.select.return_value.where.return_value.where.return_value.run.return_value = return_value
	return chain


class TestGetDeliveryNoteItemLinks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _row(self, name, sales_order, sales_order_item):
		return frappe._dict(
			name=name, sales_order=sales_order, sales_order_item=sales_order_item
		)

	def test_rows_without_sales_order_issue_no_query(self):
		with patch.object(it_mod, "frappe") as mock_frappe:
			result = it_mod.get_delivery_note_item_links([self._row("r1", None, None)])
		self.assertEqual(result, {})
		mock_frappe.qb.from_.assert_not_called()

	def test_no_candidates_returns_empty(self):
		row = self._row("poi-1", "SO-1", "soi-1")
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.qb.from_.side_effect = [_dn_query_chain([])]
			result = it_mod.get_delivery_note_item_links([row])
		self.assertEqual(result, {})

	def test_single_unambiguous_match(self):
		row = self._row("poi-1", "SO-1", "soi-1")
		dni = frappe._dict(
			name="dni-1", parent="DN-1", against_sales_order="SO-1", so_detail="soi-1"
		)
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.qb.from_.side_effect = [
				_dn_query_chain([dni]),
				_pr_query_chain([]),
			]
			result = it_mod.get_delivery_note_item_links([row])
		self.assertEqual(
			result, {"poi-1": {"delivery_note_item": "dni-1", "delivery_note": "DN-1"}}
		)

	def test_skips_candidate_already_consumed_by_submitted_receipt(self):
		row = self._row("poi-1", "SO-1", "soi-1")
		dni_a = frappe._dict(
			name="dni-a", parent="DN-A", against_sales_order="SO-1", so_detail="soi-1"
		)
		dni_b = frappe._dict(
			name="dni-b", parent="DN-B", against_sales_order="SO-1", so_detail="soi-1"
		)
		consumed = frappe._dict(delivery_note_item="dni-a")
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.qb.from_.side_effect = [
				_dn_query_chain([dni_a, dni_b]),
				_pr_query_chain([consumed]),
			]
			result = it_mod.get_delivery_note_item_links([row])
		self.assertEqual(result["poi-1"]["delivery_note_item"], "dni-b")

	def test_ambiguous_when_multiple_candidates_unconsumed(self):
		row = self._row("poi-1", "SO-1", "soi-1")
		dni_a = frappe._dict(
			name="dni-a", parent="DN-A", against_sales_order="SO-1", so_detail="soi-1"
		)
		dni_b = frappe._dict(
			name="dni-b", parent="DN-B", against_sales_order="SO-1", so_detail="soi-1"
		)
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.qb.from_.side_effect = [
				_dn_query_chain([dni_a, dni_b]),
				_pr_query_chain([]),
			]
			result = it_mod.get_delivery_note_item_links([row])
		self.assertNotIn("poi-1", result)


class TestGetSalesInvoiceItemLinks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _row(self, name, po_detail):
		return frappe._dict(name=name, po_detail=po_detail)

	def test_rows_without_po_detail_issue_no_query(self):
		with patch.object(it_mod, "frappe") as mock_frappe:
			result = it_mod.get_sales_invoice_item_links([self._row("r1", None)])
		self.assertEqual(result, {})
		mock_frappe.get_all.assert_not_called()

	def test_po_item_without_sales_order_returns_empty(self):
		row = self._row("pii-1", "poi-1")
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.get_all.return_value = [
				frappe._dict(name="poi-1", sales_order=None, sales_order_item=None)
			]
			result = it_mod.get_sales_invoice_item_links([row])
		self.assertEqual(result, {})
		mock_frappe.qb.from_.assert_not_called()

	def test_single_unambiguous_match(self):
		row = self._row("pii-1", "poi-1")
		sii = frappe._dict(
			name="sii-1", parent="SINV-1", sales_order="SO-1", so_detail="soi-1"
		)
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.get_all.return_value = [
				frappe._dict(name="poi-1", sales_order="SO-1", sales_order_item="soi-1")
			]
			mock_frappe.qb.from_.side_effect = [
				_si_query_chain([sii]),
				_pi_query_chain([]),
			]
			result = it_mod.get_sales_invoice_item_links([row])
		self.assertEqual(
			result,
			{"pii-1": {"sales_invoice_item": "sii-1", "sales_invoice": "SINV-1"}},
		)

	def test_skips_candidate_already_consumed_by_submitted_invoice(self):
		row = self._row("pii-1", "poi-1")
		sii_a = frappe._dict(
			name="sii-a", parent="SINV-A", sales_order="SO-1", so_detail="soi-1"
		)
		sii_b = frappe._dict(
			name="sii-b", parent="SINV-B", sales_order="SO-1", so_detail="soi-1"
		)
		consumed = frappe._dict(sales_invoice_item="sii-a")
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.get_all.return_value = [
				frappe._dict(name="poi-1", sales_order="SO-1", sales_order_item="soi-1")
			]
			mock_frappe.qb.from_.side_effect = [
				_si_query_chain([sii_a, sii_b]),
				_pi_query_chain([consumed]),
			]
			result = it_mod.get_sales_invoice_item_links([row])
		self.assertEqual(result["pii-1"]["sales_invoice_item"], "sii-b")

	def test_ambiguous_when_multiple_candidates_unconsumed(self):
		row = self._row("pii-1", "poi-1")
		sii_a = frappe._dict(
			name="sii-a", parent="SINV-A", sales_order="SO-1", so_detail="soi-1"
		)
		sii_b = frappe._dict(
			name="sii-b", parent="SINV-B", sales_order="SO-1", so_detail="soi-1"
		)
		with patch.object(it_mod, "frappe") as mock_frappe:
			mock_frappe.get_all.return_value = [
				frappe._dict(name="poi-1", sales_order="SO-1", sales_order_item="soi-1")
			]
			mock_frappe.qb.from_.side_effect = [
				_si_query_chain([sii_a, sii_b]),
				_pi_query_chain([]),
			]
			result = it_mod.get_sales_invoice_item_links([row])
		self.assertNotIn("pii-1", result)


class _FakeDoc:
	"""Stand-in for a frappe Document: plain attribute access/assignment (unlike
	frappe._dict, which is a real dict and would make `.items` resolve to
	dict.items, not the child-table field of the same name) plus the `.get()`
	method the production code calls."""

	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestFillPurchaseReceiptReferences(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(it_mod, "get_delivery_note_item_links")
	def test_fills_row_and_header_references(self, mock_resolver):
		row = _FakeDoc(name="r1", sales_order="SO-1", sales_order_item="soi-1", idx=1)
		doc = _FakeDoc(items=[row])
		mock_resolver.return_value = {
			"r1": {"delivery_note_item": "dni-1", "delivery_note": "DN-1"}
		}

		it_mod.fill_purchase_receipt_references(doc)

		self.assertEqual(row.delivery_note_item, "dni-1")
		self.assertEqual(doc.inter_company_reference, "DN-1")

	@patch.object(it_mod, "get_delivery_note_item_links")
	def test_row_without_sales_order_throws(self, mock_resolver):
		row = _FakeDoc(name="r1", sales_order=None, sales_order_item=None, idx=1)
		doc = _FakeDoc(items=[row])
		mock_resolver.return_value = {}

		with self.assertRaises(frappe.ValidationError):
			it_mod.fill_purchase_receipt_references(doc)

	@patch.object(it_mod, "get_delivery_note_item_links")
	def test_unresolved_row_throws(self, mock_resolver):
		row = _FakeDoc(name="r1", sales_order="SO-1", sales_order_item="soi-1", idx=1)
		doc = _FakeDoc(items=[row])
		mock_resolver.return_value = {}

		with self.assertRaises(frappe.ValidationError):
			it_mod.fill_purchase_receipt_references(doc)


class TestFillPurchaseInvoiceReferences(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(it_mod, "get_sales_invoice_item_links")
	def test_fills_row_and_header_references(self, mock_resolver):
		row = _FakeDoc(name="r1", po_detail="poi-1", idx=1)
		doc = _FakeDoc(items=[row])
		mock_resolver.return_value = {
			"r1": {"sales_invoice_item": "sii-1", "sales_invoice": "SINV-1"}
		}

		it_mod.fill_purchase_invoice_references(doc)

		self.assertEqual(row.sales_invoice_item, "sii-1")
		self.assertEqual(doc.inter_company_invoice_reference, "SINV-1")

	@patch.object(it_mod, "get_sales_invoice_item_links")
	def test_unresolved_row_throws(self, mock_resolver):
		row = _FakeDoc(name="r1", po_detail="poi-1", idx=1)
		doc = _FakeDoc(items=[row])
		mock_resolver.return_value = {}

		with self.assertRaises(frappe.ValidationError):
			it_mod.fill_purchase_invoice_references(doc)


class TestPurchaseOrderMakePurchaseReceiptOverride(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(po_events, "fill_purchase_receipt_references")
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	def test_non_internal_transfer_skips_fill(self, mock_core, mock_fill):
		doc = _FakeDoc(
			is_return=0, is_internal_supplier=0, represents_company="A", company="A"
		)
		mock_core.return_value = doc

		result = po_events.make_purchase_receipt("PO-1")

		self.assertIs(result, doc)
		mock_fill.assert_not_called()

	@patch.object(po_events, "fill_purchase_receipt_references")
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	def test_internal_transfer_calls_fill(self, mock_core, mock_fill):
		doc = _FakeDoc(
			is_return=0,
			is_internal_supplier=1,
			represents_company="Gurukrupa Export Private Limited",
			company="Gurukrupa Export Private Limited",
		)
		mock_core.return_value = doc

		result = po_events.make_purchase_receipt("PO-1")

		self.assertIs(result, doc)
		mock_fill.assert_called_once_with(doc)


class TestPurchaseOrderMakePurchaseInvoiceOverride(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(po_events, "fill_purchase_invoice_references")
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice")
	def test_non_internal_transfer_skips_fill(self, mock_core, mock_fill):
		doc = _FakeDoc(
			is_return=0, is_internal_supplier=0, represents_company="A", company="A"
		)
		mock_core.return_value = doc

		result = po_events.make_purchase_invoice("PO-1")

		self.assertIs(result, doc)
		mock_fill.assert_not_called()

	@patch.object(po_events, "fill_purchase_invoice_references")
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice")
	def test_internal_transfer_calls_fill(self, mock_core, mock_fill):
		doc = _FakeDoc(
			is_return=0,
			is_internal_supplier=1,
			represents_company="Gurukrupa Export Private Limited",
			company="Gurukrupa Export Private Limited",
		)
		mock_core.return_value = doc

		result = po_events.make_purchase_invoice("PO-1")

		self.assertIs(result, doc)
		mock_fill.assert_called_once_with(doc)


class TestPurchaseReceiptMakePurchaseInvoiceOverride(IntegrationTestCase):
	"""The Purchase Receipt's own "Create -> Purchase Invoice" button calls a
	different core function than the Purchase Order's, so it needs its own override
	(doc_events/purchase_receipt.py) even though the fix is identical -- these tests
	mirror TestPurchaseOrderMakePurchaseInvoiceOverride against that second entry
	point."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(pr_events, "fill_purchase_invoice_references")
	@patch(
		"erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice"
	)
	def test_non_internal_transfer_skips_fill(self, mock_core, mock_fill):
		doc = _FakeDoc(
			is_return=0, is_internal_supplier=0, represents_company="A", company="A"
		)
		mock_core.return_value = doc

		result = pr_events.make_purchase_invoice("PR-1")

		self.assertIs(result, doc)
		mock_fill.assert_not_called()

	@patch.object(pr_events, "fill_purchase_invoice_references")
	@patch(
		"erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice"
	)
	def test_internal_transfer_calls_fill(self, mock_core, mock_fill):
		doc = _FakeDoc(
			is_return=0,
			is_internal_supplier=1,
			represents_company="Gurukrupa Export Private Limited",
			company="Gurukrupa Export Private Limited",
		)
		mock_core.return_value = doc

		result = pr_events.make_purchase_invoice("PR-1")

		self.assertIs(result, doc)
		mock_fill.assert_called_once_with(doc)
