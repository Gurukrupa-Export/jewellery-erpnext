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
