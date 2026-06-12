# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for ``validate_pcs`` — the per-MR-item PCS distribution when a single
Material Request Item's qty is split across multiple batch rows.

Rule: the MR Item's PCS is the authoritative total; the first batch row keeps
the bulk and every extra batch row defaults to 1 (never 0) so a row physically
holding stock is never shown as 0 pcs. The total is preserved (e.g. 71 → 70 + 1)
and the pass is idempotent (the MOP Stock Entry re-runs it on already-split rows).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import validate_pcs


class _Row:
	def __init__(self, mri, pcs, batch_no=None):
		self.material_request_item = mri
		self.pcs = pcs
		self.batch_no = batch_no


class _SE:
	def __init__(self, items):
		self.items = items
		self.flags = frappe._dict()


class TestValidatePcsBatchSplit(FrappeTestCase):
	def _run(self, items, mri_pcs):
		def _gv(doctype, name, field):
			return mri_pcs.get(name)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_value",
			side_effect=_gv,
		):
			validate_pcs(_SE(items))

	def test_split_gives_extra_row_minimum_one(self):
		"""MAT-STE-51278: MR item pcs 71 split across two batches → 70 + 1."""
		items = [
			_Row("n5c7c76keg", 71, "B05C00-02"),
			_Row("n5c7c76keg", 71, "B05C00-04"),
		]
		self._run(items, {"n5c7c76keg": 71})
		self.assertEqual([r.pcs for r in items], [70, 1])
		self.assertEqual(sum(r.pcs for r in items), 71)

	def test_idempotent_on_already_split_rows(self):
		"""Re-running on rows already at 70/1 (the MOP SE copies the reserve SE
		and runs before_validate again) keeps 70/1, not 69/1."""
		items = [
			_Row("n5c7c76keg", 70, "B05C00-02"),
			_Row("n5c7c76keg", 1, "B05C00-04"),
		]
		self._run(items, {"n5c7c76keg": 71})
		self.assertEqual([r.pcs for r in items], [70, 1])

	def test_three_batch_split_preserves_total(self):
		items = [_Row("n5", 71, "B1"), _Row("n5", 71, "B2"), _Row("n5", 71, "B3")]
		self._run(items, {"n5": 71})
		self.assertEqual([r.pcs for r in items], [69, 1, 1])
		self.assertEqual(sum(r.pcs for r in items), 71)

	def test_single_row_untouched(self):
		items = [_Row("n5", 71, "B1")]
		self._run(items, {"n5": 71})
		self.assertEqual(items[0].pcs, 71)

	def test_total_too_small_keeps_whole_count_on_first(self):
		"""1 pc can't be spread one-each across two batches without driving the
		first row below 1 — keep the whole count on the first (prior behaviour)."""
		items = [_Row("n5", 1, "B1"), _Row("n5", 1, "B2")]
		self._run(items, {"n5": 1})
		self.assertEqual([r.pcs for r in items], [1, 0])

	def test_rows_without_mr_item_are_ignored(self):
		items = [_Row(None, 5, "B1"), _Row("", 9, "B2")]
		self._run(items, {})
		self.assertEqual([r.pcs for r in items], [5, 9])

	def test_distinct_mr_items_independent(self):
		items = [
			_Row("a", 71, "B1"),
			_Row("a", 71, "B2"),
			_Row("b", 10, "B3"),
		]
		self._run(items, {"a": 71, "b": 10})
		self.assertEqual([r.pcs for r in items], [70, 1, 10])


"""Regression guard for Stock Entry Detail ``transfer_qty`` precision.

A tiny but genuine metal loss (e.g. 0.005 g) is booked on the auto-created
"Process Loss" Stock Entry. ERPNext's ``StockEntry.set_transfer_qty`` rounds
``transfer_qty`` to the field precision and throws *"Qty in Stock UOM can not
be zero."* when it lands on 0. Core ``Stock Entry Detail.qty`` already carries
precision 3, but ``transfer_qty`` had no override and fell back to System
Settings ``float_precision`` (2), where ``flt(0.005, 2)`` rounds to 0.00 under
Banker's Rounding.

A property setter (``property_setter/stock_entry_detail.json``, applied via
``migrate.after_migrate`` → ``create_property_setter``) pins ``transfer_qty``
to precision 3 so the loss survives as a real stock movement. This test fails
if that customization is missing or regressed.
"""


class TestStockEntryDetailPrecision(FrappeTestCase):
	def test_transfer_qty_precision_is_three(self):
		# The property setter must be in effect so transfer_qty matches qty (3).
		self.assertEqual(frappe.get_precision("Stock Entry Detail", "transfer_qty"), 3)
		self.assertEqual(frappe.get_precision("Stock Entry Detail", "qty"), 3)

	def test_sub_precision_loss_is_not_zeroed(self):
		# At precision 3 a 0.005 g loss is representable; at precision 2 (the
		# regressed state) it would round to 0.00 and trip the zero-qty throw.
		precision = frappe.get_precision("Stock Entry Detail", "transfer_qty")
		self.assertNotEqual(flt(0.005, precision), 0)
