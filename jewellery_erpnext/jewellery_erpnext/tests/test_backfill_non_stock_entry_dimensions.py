# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the guards on the non-Stock-Entry inventory-dimension backfill.

The patch writes SUBMITTED Stock Ledger Entry rows, so what matters is not that it writes
but what it refuses to write: a dimension whose column is load-bearing for negative-stock
validation, a fieldname that is not a safe SQL identifier, and a bundle that spans more
than one batch and therefore has no single lane.

Pass 2 (``_repair_source_rows``) is pinned for a different reason: it exists only because
``get_sl_entries`` re-derives a voucher's dimensions from the persisted child row on
CANCEL, so a repaired ledger row above a blank source row would emit an unbalanced
reversal. Its child-doctype resolution is therefore guarded the same way the fieldnames
are.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import (
	backfill_non_stock_entry_inventory_dimensions as backfill,
)

DIMENSION_MOD = "erpnext.stock.doctype.inventory_dimension.inventory_dimension"

_INVENTORY_TYPE_DIM = {
	"source_fieldname": "inventory_type",
	"fieldname": "inventory_type",
	"dimension_name": "Inventory Type",
	"validate_negative_stock": 0,
}
_CUSTOMER_DIM = {
	"source_fieldname": "customer",
	"fieldname": "customer",
	"dimension_name": "Customer",
	"validate_negative_stock": 0,
}


class _Meta:
	def __init__(self, fields):
		self._fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self._fields


class _BackfillTestCase(IntegrationTestCase):
	"""House-pattern base: no DB fixtures; setUpClass is a deliberate no-op."""

	@classmethod
	def setUpClass(cls):
		pass

	def _fields(self, dimensions, sle_fields=("inventory_type", "customer")):
		with patch(
			f"{DIMENSION_MOD}.get_inventory_dimensions",
			return_value=[frappe._dict(d) for d in dimensions],
		), patch.object(backfill.frappe, "get_meta", return_value=_Meta(sle_fields)):
			return backfill._dimension_fields()


class TestDimensionFieldGuards(_BackfillTestCase):
	def test_registered_dimensions_are_returned(self):
		fields = self._fields([_INVENTORY_TYPE_DIM, _CUSTOMER_DIM])
		self.assertEqual(
			fields, {"inventory_type": "inventory_type", "customer": "customer"}
		)

	def test_negative_stock_dimension_is_skipped(self):
		"""The column would then feed validate_inventory_dimension_negative_stock."""
		dim = dict(_INVENTORY_TYPE_DIM, validate_negative_stock=1)
		self.assertEqual(self._fields([dim]), {})

	def test_unsafe_fieldname_is_skipped(self):
		"""Target fieldnames are user-editable and are interpolated as SQL identifiers."""
		dim = dict(_INVENTORY_TYPE_DIM, fieldname="inventory_type`; DROP TABLE x; --")
		self.assertEqual(self._fields([dim]), {})

	def test_dimension_without_a_batch_source_is_skipped(self):
		"""A Batch cannot answer for it, so it is left alone rather than guessed at."""
		dim = {
			"source_fieldname": "project",
			"fieldname": "project",
			"dimension_name": "Project",
			"validate_negative_stock": 0,
		}
		self.assertEqual(self._fields([dim]), {})

	def test_unmaterialised_column_is_skipped(self):
		self.assertEqual(self._fields([_CUSTOMER_DIM], sle_fields=()), {})


class TestBundleBatchResolution(_BackfillTestCase):
	def _resolve(self, entries):
		with patch.object(
			backfill.frappe,
			"get_all",
			return_value=[frappe._dict(e) for e in entries],
		):
			return backfill._bundle_batches(["BUNDLE-1"])

	def test_single_batch_bundle_resolves(self):
		out = self._resolve([{"parent": "BUNDLE-1", "batch_no": "B-1"}])
		self.assertEqual(out, {"BUNDLE-1": "B-1"})

	def test_multi_batch_bundle_is_left_unresolved(self):
		out = self._resolve(
			[
				{"parent": "BUNDLE-1", "batch_no": "B-1"},
				{"parent": "BUNDLE-1", "batch_no": "B-2"},
			]
		)
		self.assertEqual(out, {})

	def test_repeated_batch_rows_still_resolve(self):
		"""One batch across several serial entries is still one lane."""
		out = self._resolve(
			[
				{"parent": "BUNDLE-1", "batch_no": "B-1"},
				{"parent": "BUNDLE-1", "batch_no": "B-1"},
			]
		)
		self.assertEqual(out, {"BUNDLE-1": "B-1"})

	def test_no_bundles_issues_no_query(self):
		with patch.object(backfill.frappe, "get_all") as get_all:
			self.assertEqual(backfill._bundle_batches([None, ""]), {})
		get_all.assert_not_called()


class TestSourceRowRepairGuards(_BackfillTestCase):
	"""Pass 2 reaches a table name into SQL, so the same identifier discipline applies."""

	def _child_doctypes(self, voucher_types, items_options, exists=True):
		def _sql(*args, **kwargs):
			return [(vt,) for vt in voucher_types]

		meta = type(
			"_M", (), {"get_field": lambda self, f: frappe._dict(options=items_options)}
		)()

		with patch.object(backfill.frappe.db, "sql", side_effect=_sql), patch.object(
			backfill.frappe.db, "exists", return_value=exists
		), patch.object(backfill.frappe, "get_meta", return_value=meta):
			return backfill._source_child_doctypes()

	def test_child_doctype_is_read_off_the_items_field(self):
		"""Not assumed to be f'{voucher_type} Item' -- a differently named child still works."""
		out = self._child_doctypes(
			["Stock Reconciliation"], "Stock Reconciliation Item"
		)
		self.assertEqual(out, {"Stock Reconciliation": "Stock Reconciliation Item"})

	def test_unsafe_child_doctype_is_skipped(self):
		out = self._child_doctypes(
			["Sales Invoice"], "Sales Invoice Item`; DROP TABLE x; --"
		)
		self.assertEqual(out, {})

	def test_missing_doctype_is_skipped(self):
		out = self._child_doctypes(["Ghost Voucher"], "Ghost Item", exists=False)
		self.assertEqual(out, {})

	def test_voucher_without_an_items_field_is_skipped(self):
		out = self._child_doctypes(["Sales Invoice"], None)
		self.assertEqual(out, {})
