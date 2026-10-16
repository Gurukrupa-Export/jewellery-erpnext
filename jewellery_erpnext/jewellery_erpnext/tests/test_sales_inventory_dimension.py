# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the sales-side inventory-dimension lane.

The defect these cover: ``Sales Invoice Item.inventory_type`` /
``Delivery Note Item.inventory_type`` were never written, so ERPNext tagged every sales SLE
NULL. Once erpnext 16.34.1 started comparing an outward serialized SLE against the serial's
last inward SLE, a piece sold and returned on a credit note became un-issuable.

What is pinned here is not "the field gets filled" but the decisions that make filling it
CORRECT: the lane comes from the batch (so it agrees with what a later Stock Entry row
resolves to), the batch beats a stale row value, the (type, customer) pair stays coherent,
and the ``to_`` mirror stays gated on an internal-customer transfer.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import (
	inventory_dimension as inv_dim,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doc_events.inventory_dimension"

_ITEM_FIELDS = (
	"inventory_type",
	"customer",
	"to_inventory_type",
	"to_customer",
)


class _Doc(SimpleNamespace):
	"""SimpleNamespace with Frappe-style ``.get()``."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _Row(_Doc):
	"""Sales item row that also answers ``.set()`` like a real Document."""

	def set(self, key, value):
		setattr(self, key, value)


class _Meta:
	def __init__(self, fields):
		self._fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self._fields


def _row(**attrs):
	attrs.setdefault("doctype", "Sales Invoice Item")
	attrs.setdefault("batch_no", None)
	attrs.setdefault("serial_and_batch_bundle", None)
	attrs.setdefault("item_code", "M-1")
	attrs.setdefault("inventory_type", None)
	attrs.setdefault("customer", None)
	attrs.setdefault("target_warehouse", None)
	return _Row(**attrs)


class _SalesDimensionTestCase(IntegrationTestCase):
	"""House-pattern base: no DB fixtures; setUpClass is a deliberate no-op."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(
		self, rows, batches=(), bundle_entries=(), fields=_ITEM_FIELDS, **doc_attrs
	):
		doc_attrs.setdefault("doctype", "Sales Invoice")
		doc_attrs.setdefault("docstatus", 0)
		doc_attrs.setdefault("is_internal_customer", 0)
		doc = _Doc(items=list(rows), **doc_attrs)

		def _get_all(doctype, **kwargs):
			if doctype == "Batch":
				return [frappe._dict(b) for b in batches]
			if doctype == "Serial and Batch Entry":
				return [frappe._dict(e) for e in bundle_entries]
			return []

		with patch.object(
			inv_dim.frappe, "get_meta", return_value=_Meta(fields)
		), patch.object(inv_dim.frappe, "get_all", side_effect=_get_all) as get_all:
			inv_dim.set_sales_inventory_type(doc)

		return doc, get_all


# ------------------------------------------------- the lane comes from the batch
class TestLaneComesFromBatch(_SalesDimensionTestCase):
	def test_customer_goods_batch_stamps_row(self):
		"""The whole point: a returned customer piece must not be booked as company stock."""
		row = _row(batch_no="B-1")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": "CUST-1",
				}
			],
		)
		self.assertEqual(row.inventory_type, "Customer Goods")
		self.assertEqual(row.customer, "CUST-1")

	def test_regular_stock_batch_clears_customer(self):
		"""Rule 2: a non-customer lane never carries a customer."""
		row = _row(batch_no="B-1", customer="CUST-STALE")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Regular Stock",
					"custom_customer": None,
				}
			],
		)
		self.assertEqual(row.inventory_type, "Regular Stock")
		self.assertIsNone(row.customer)

	def test_batch_beats_stale_row_value(self):
		"""Rule 1: the batch is the physical truth; a stale row value must not win."""
		row = _row(batch_no="B-1", inventory_type="Regular Stock")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": "CUST-1",
				}
			],
		)
		self.assertEqual(row.inventory_type, "Customer Goods")

	def test_no_batch_falls_back_to_regular_stock(self):
		"""Matches the blanket default a later outward Stock Entry row will carry."""
		row = _row()
		self._run([row])
		self.assertEqual(row.inventory_type, "Regular Stock")
		self.assertIsNone(row.customer)

	def test_customer_goods_without_customer_downgrades(self):
		"""Rule 3: such batches exist in production; emitting the pair would fail the submit."""
		row = _row(batch_no="B-1")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": None,
				}
			],
		)
		self.assertEqual(row.inventory_type, "Regular Stock")
		self.assertIsNone(row.customer)


# ------------------------------------------------------------- bundle resolution
class TestBundleResolution(_SalesDimensionTestCase):
	def test_batch_resolved_through_bundle(self):
		"""A v16 serialized sales row carries its batch in a bundle, not in batch_no."""
		row = _row(serial_and_batch_bundle="BUNDLE-1")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": "CUST-1",
				}
			],
			bundle_entries=[{"parent": "BUNDLE-1", "batch_no": "B-1"}],
		)
		self.assertEqual(row.inventory_type, "Customer Goods")
		self.assertEqual(row.customer, "CUST-1")

	def test_multi_batch_bundle_is_not_guessed(self):
		"""No single lane, so fall back rather than adopt an arbitrary batch."""
		row = _row(serial_and_batch_bundle="BUNDLE-1")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": "CUST-1",
				}
			],
			bundle_entries=[
				{"parent": "BUNDLE-1", "batch_no": "B-1"},
				{"parent": "BUNDLE-1", "batch_no": "B-2"},
			],
		)
		self.assertEqual(row.inventory_type, "Regular Stock")

	def test_batch_no_wins_over_bundle_lookup(self):
		"""A row that already names its batch needs no bundle query at all."""
		row = _row(batch_no="B-1", serial_and_batch_bundle="BUNDLE-1")
		_, get_all = self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Regular Stock",
					"custom_customer": None,
				}
			],
		)
		queried = [call.args[0] for call in get_all.call_args_list]
		self.assertNotIn("Serial and Batch Entry", queried)


# -------------------------------------------------------------- the to_ mirror
class TestTargetMirror(_SalesDimensionTestCase):
	def test_internal_customer_transfer_mirrors(self):
		"""ERPNext reads to_<field> for the inward leg of an internal transfer."""
		row = _row(batch_no="B-1", target_warehouse="WH-B")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": "CUST-1",
				}
			],
			is_internal_customer=1,
		)
		self.assertEqual(row.to_inventory_type, "Customer Goods")
		self.assertEqual(row.to_customer, "CUST-1")

	def test_ordinary_invoice_is_not_mirrored(self):
		"""ERPNext never reads to_<field> here, so it must not be invented."""
		row = _row(batch_no="B-1", target_warehouse="WH-B")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Regular Stock",
					"custom_customer": None,
				}
			],
		)
		self.assertFalse(hasattr(row, "to_inventory_type"))

	def test_internal_transfer_without_target_warehouse_is_not_mirrored(self):
		row = _row(batch_no="B-1")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Regular Stock",
					"custom_customer": None,
				}
			],
			is_internal_customer=1,
		)
		self.assertFalse(hasattr(row, "to_inventory_type"))


# ------------------------------------------------------------------ no-op guards
class TestNoOpGuards(_SalesDimensionTestCase):
	def test_cancelled_document_is_untouched(self):
		row = _row(batch_no="B-1")
		self._run([row], docstatus=2)
		self.assertIsNone(row.inventory_type)

	def test_doctype_without_the_dimension_is_untouched(self):
		"""The has_field guard is load-bearing -- see the Stock Entry twin."""
		row = _row(batch_no="B-1")
		self._run([row], fields=())
		self.assertIsNone(row.inventory_type)

	def test_customer_field_absent_is_not_written(self):
		row = _row(batch_no="B-1")
		self._run(
			[row],
			batches=[
				{
					"name": "B-1",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": "CUST-1",
				}
			],
			fields=("inventory_type",),
		)
		self.assertEqual(row.inventory_type, "Customer Goods")
		self.assertIsNone(row.customer)

	def test_empty_item_table_issues_no_queries(self):
		_, get_all = self._run([])
		get_all.assert_not_called()

	def test_batches_are_fetched_in_one_query(self):
		"""A jewellery invoice carries hundreds of rows; this must not be O(rows)."""
		rows = [_row(batch_no=f"B-{i}") for i in range(25)]
		_, get_all = self._run(
			rows,
			batches=[
				{
					"name": f"B-{i}",
					"custom_inventory_type": "Regular Stock",
					"custom_customer": None,
				}
				for i in range(25)
			],
		)
		batch_calls = [c for c in get_all.call_args_list if c.args[0] == "Batch"]
		self.assertEqual(len(batch_calls), 1)
