# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the canonical lock-ordering helpers (jewellery_erpnext.lock_order)."""

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins,
	lock_bins_for_rows,
	preallocate_series,
	sorted_stock_rows,
	stock_lock_key,
)


class TestStockLockKey(FrappeTestCase):
	def test_none_is_coalesced_to_empty_string(self):
		self.assertEqual(stock_lock_key("ITEM", None, None), ("ITEM", "", ""))

	def test_none_warehouse_sorts_before_real_warehouse(self):
		self.assertLess(stock_lock_key("ITEM", None), stock_lock_key("ITEM", "WH-A"))

	def test_orders_by_item_then_warehouse_then_batch(self):
		keys = [
			stock_lock_key("B", "WH-1", "X"),
			stock_lock_key("A", "WH-2", "Z"),
			stock_lock_key("A", "WH-2", "A"),
			stock_lock_key("A", "WH-1", "M"),
		]
		ordered = sorted(keys)
		self.assertEqual(
			ordered,
			[
				("A", "WH-1", "M"),
				("A", "WH-2", "A"),
				("A", "WH-2", "Z"),
				("B", "WH-1", "X"),
			],
		)


class TestSortedStockRows(FrappeTestCase):
	def test_sorts_dicts_by_canonical_key(self):
		rows = [
			{"item_code": "B", "warehouse": "WH-2", "batch_no": "b"},
			{"item_code": "A", "warehouse": "WH-1", "batch_no": "a"},
		]
		out = sorted_stock_rows(rows)
		self.assertEqual([r["item_code"] for r in out], ["A", "B"])

	def test_does_not_mutate_input(self):
		rows = [{"item_code": "B"}, {"item_code": "A"}]
		sorted_stock_rows(rows)
		self.assertEqual([r["item_code"] for r in rows], ["B", "A"])  # original order intact

	def test_is_stable_for_equal_keys(self):
		# Two rows with identical (item, warehouse, batch) keep their original order so
		# competing reservations are unaffected by the sort.
		rows = [
			{"item_code": "A", "warehouse": "W", "batch_no": "x", "tag": "first"},
			{"item_code": "A", "warehouse": "W", "batch_no": "x", "tag": "second"},
		]
		out = sorted_stock_rows(rows)
		self.assertEqual([r["tag"] for r in out], ["first", "second"])

	def test_honors_warehouse_attr(self):
		rows = [
			{"item_code": "A", "t_warehouse": "WH-2"},
			{"item_code": "A", "t_warehouse": "WH-1"},
		]
		out = sorted_stock_rows(rows, warehouse_attr="t_warehouse")
		self.assertEqual([r["t_warehouse"] for r in out], ["WH-1", "WH-2"])

	def test_supports_object_rows(self):
		rows = [
			SimpleNamespace(item_code="B", warehouse="W", batch_no=None),
			SimpleNamespace(item_code="A", warehouse="W", batch_no=None),
		]
		out = sorted_stock_rows(rows)
		self.assertEqual([r.item_code for r in out], ["A", "B"])


class TestLockBins(FrappeTestCase):
	@patch.object(lock_order.frappe.db, "sql")
	def test_dedups_and_locks_each_unique_pair_once_in_sorted_order(self, mock_sql):
		mock_sql.return_value = [("BIN",)]
		pairs = [("ITEM-B", "WH-2"), ("ITEM-A", "WH-1"), ("ITEM-A", "WH-1")]
		lock_bins(pairs)
		# 2 unique pairs -> 2 statements, acquired in sorted order
		self.assertEqual(mock_sql.call_count, 2)
		first_args = mock_sql.call_args_list[0][0][1]
		second_args = mock_sql.call_args_list[1][0][1]
		self.assertEqual(first_args, ("ITEM-A", "WH-1"))
		self.assertEqual(second_args, ("ITEM-B", "WH-2"))

	@patch.object(lock_order.frappe.db, "sql")
	def test_uses_for_update(self, mock_sql):
		mock_sql.return_value = []
		lock_bins([("I", "W")])
		self.assertIn("FOR UPDATE", mock_sql.call_args[0][0])

	@patch.object(lock_order.frappe.db, "sql")
	def test_skips_pairs_with_missing_item_or_warehouse(self, mock_sql):
		mock_sql.return_value = []
		lock_bins([("I", None), (None, "W"), ("", "W"), ("I", "")])
		mock_sql.assert_not_called()

	@patch.object(lock_order.frappe.db, "sql")
	def test_returns_only_existing_bin_names(self, mock_sql):
		# First pair has a Bin, second does not.
		mock_sql.side_effect = [[("BIN-A",)], []]
		locked = lock_bins([("A", "W"), ("B", "W")])
		self.assertEqual(locked, ["BIN-A"])


class TestLockBinsForRows(FrappeTestCase):
	@patch.object(lock_order, "lock_bins")
	def test_expands_default_s_and_t_warehouse(self, mock_lock_bins):
		rows = [{"item_code": "A", "s_warehouse": "S1", "t_warehouse": "T1"}]
		lock_bins_for_rows(rows)
		pairs = mock_lock_bins.call_args[0][0]
		self.assertIn(("A", "S1"), pairs)
		self.assertIn(("A", "T1"), pairs)

	@patch.object(lock_order, "lock_bins")
	def test_honors_explicit_warehouse_attrs(self, mock_lock_bins):
		rows = [{"item_code": "A", "t_warehouse": "T1", "s_warehouse": "S1"}]
		lock_bins_for_rows(rows, "t_warehouse")
		pairs = mock_lock_bins.call_args[0][0]
		self.assertEqual(pairs, [("A", "T1")])


class TestPreallocateSeries(FrappeTestCase):
	@patch.object(lock_order.frappe.db, "sql")
	def test_locks_unique_nonblank_prefixes_in_sorted_order(self, mock_sql):
		preallocate_series(["SE-", "SRE-", "SE-", "", None])
		self.assertEqual(mock_sql.call_count, 2)
		locked = [c[0][1][0] for c in mock_sql.call_args_list]
		self.assertEqual(locked, ["SE-", "SRE-"])
		self.assertIn("FOR UPDATE", mock_sql.call_args_list[0][0][0])
