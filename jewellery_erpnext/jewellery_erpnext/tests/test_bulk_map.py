# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for ``utils.bulk_map`` and the Stock Entry prefetch call sites.

Mocked/pure-logic style (see test_sample_goods_guard.py): ``setUpClass`` is a no-op,
fake docs are SimpleNamespace / frappe._dict, and every DB reader is patched — these
must stay runnable on a site with no fixtures.

``bulk_map`` is the behaviour-identical replacement for per-row ``frappe.db.get_value``
that PR #889 introduced, so the contract worth pinning is the equivalence itself:
a missing name and a NULL column must both resolve to ``None`` through
``(bulk_map(...).get(name) or {}).get(field)``.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import utils
from jewellery_erpnext.jewellery_erpnext.doc_events import stock_entry as se_events


class TestBulkMap(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, names, fields, rows):
		with patch.object(utils.frappe, "get_all", return_value=rows) as get_all:
			result = utils.bulk_map("Item", names, fields)
		return result, get_all

	def test_keys_by_name_and_keeps_fields(self):
		rows = [frappe._dict(name="ITM-1", has_batch_no=1, variant_of="D")]
		result, _ = self._run(["ITM-1"], ["has_batch_no", "variant_of"], rows)
		self.assertEqual(set(result), {"ITM-1"})
		self.assertEqual(result["ITM-1"].has_batch_no, 1)
		self.assertEqual(result["ITM-1"].variant_of, "D")

	def test_empty_names_issues_no_query(self):
		with patch.object(utils.frappe, "get_all") as get_all:
			self.assertEqual(utils.bulk_map("Item", [], ["has_batch_no"]), {})
			self.assertEqual(utils.bulk_map("Item", [None, ""], ["has_batch_no"]), {})
		get_all.assert_not_called()

	def test_falsy_names_dropped_and_deduped_before_query(self):
		_result, get_all = self._run(
			["ITM-1", "ITM-1", None, "", "ITM-2"], ["has_batch_no"], []
		)
		queried = get_all.call_args.kwargs["filters"]["name"][1]
		self.assertEqual(sorted(queried), ["ITM-1", "ITM-2"])

	def test_query_selects_name_plus_fields_unordered(self):
		_result, get_all = self._run(["ITM-1"], ["has_batch_no"], [])
		self.assertEqual(get_all.call_args[0][0], "Item")
		self.assertEqual(get_all.call_args.kwargs["fields"], ["name", "has_batch_no"])
		# order_by=None keeps frappe from appending the doctype's default sort
		self.assertIsNone(get_all.call_args.kwargs["order_by"])

	def test_get_value_equivalence_for_missing_row_and_null_column(self):
		"""The whole point of the helper: same None semantics as frappe.db.get_value."""
		rows = [frappe._dict(name="ITM-1", has_batch_no=None)]
		result, _ = self._run(["ITM-1", "ITM-GONE"], ["has_batch_no"], rows)
		# NULL column -> None
		self.assertIsNone((result.get("ITM-1") or {}).get("has_batch_no"))
		# missing row -> None (and no KeyError)
		self.assertIsNone((result.get("ITM-GONE") or {}).get("has_batch_no"))


class TestSyncMopLogPrefetch(IntegrationTestCase):
	"""``sync_mop_log_for_stock_entry`` dedups off one pre-loop snapshot query."""

	@classmethod
	def setUpClass(cls):
		pass

	def _se(self, items):
		return SimpleNamespace(
			name="SE-1", manufacturing_work_order="MWO-1", items=items
		)

	def _row(self, name, mop="MOP-1", item_code="D-1"):
		return frappe._dict(name=name, item_code=item_code, manufacturing_operation=mop)

	def test_snapshot_taken_once_for_many_rows(self):
		se = self._se([self._row("r1"), self._row("r2"), self._row("r3")])
		with patch.object(
			se_events.frappe, "get_all", return_value=[]
		) as get_all, patch.object(se_events, "create_mop_log") as create:
			se_events.sync_mop_log_for_stock_entry(se)
		self.assertEqual(get_all.call_count, 1)
		self.assertEqual(create.call_count, 3)

	def test_row_already_logged_is_skipped_others_are_not(self):
		se = self._se([self._row("r1"), self._row("r2")])
		existing = [frappe._dict(row_name="r1", manufacturing_operation="MOP-1")]
		with patch.object(
			se_events.frappe, "get_all", return_value=existing
		), patch.object(se_events, "create_mop_log") as create:
			se_events.sync_mop_log_for_stock_entry(se)
		self.assertEqual(create.call_count, 1)
		self.assertEqual(create.call_args[0][1].name, "r2")

	def test_same_row_name_different_operation_still_logged(self):
		"""Dedup key is the (row_name, manufacturing_operation) pair, not row_name."""
		se = self._se([self._row("r1", mop="MOP-2")])
		existing = [frappe._dict(row_name="r1", manufacturing_operation="MOP-1")]
		with patch.object(
			se_events.frappe, "get_all", return_value=existing
		), patch.object(se_events, "create_mop_log") as create:
			se_events.sync_mop_log_for_stock_entry(se)
		create.assert_called_once()

	def test_rows_without_operation_or_item_are_ignored(self):
		se = self._se([self._row("r1", mop=None), self._row("r2", item_code=None)])
		with patch.object(
			se_events.frappe, "get_all", return_value=[]
		) as get_all, patch.object(se_events, "create_mop_log") as create:
			se_events.sync_mop_log_for_stock_entry(se)
		# no MOP-bound row at all -> the snapshot query is skipped entirely
		get_all.assert_not_called()
		create.assert_not_called()

	def test_cancel_path_does_not_take_a_snapshot(self):
		se = self._se([self._row("r1")])
		with patch.object(se_events.frappe.db, "sql") as sql, patch.object(
			se_events.frappe, "get_all"
		) as get_all, patch.object(se_events, "create_mop_log") as create:
			se_events.sync_mop_log_for_stock_entry(se, is_cancelled=True)
		sql.assert_called_once()
		get_all.assert_not_called()
		create.assert_not_called()


class TestValidateItemsBomPrefetch(IntegrationTestCase):
	"""``validate_items`` checks Broken/Loss rows against one BOM Item fetch."""

	@classmethod
	def setUpClass(cls):
		pass

	def _se(self, stock_entry_type, items, bom_no="BOM-1"):
		return SimpleNamespace(
			stock_entry_type=stock_entry_type, bom_no=bom_no, items=items
		)

	def test_non_broken_loss_type_short_circuits(self):
		se = self._se("Manufacture", [frappe._dict(item_code="ITM-1")])
		with patch.object(se_events.frappe, "get_all") as get_all:
			se_events.validate_items(se)
		get_all.assert_not_called()

	def test_all_items_in_bom_passes_with_one_query(self):
		se = self._se(
			"Broken / Loss",
			[frappe._dict(item_code="ITM-1"), frappe._dict(item_code="ITM-2")],
		)
		with patch.object(
			se_events.frappe, "get_all", return_value=["ITM-1", "ITM-2"]
		) as get_all, patch.object(se_events.frappe, "throw") as throw:
			se_events.validate_items(se)
		self.assertEqual(get_all.call_count, 1)
		throw.assert_not_called()

	def test_item_absent_from_bom_throws(self):
		se = self._se("Broken / Loss", [frappe._dict(item_code="ITM-ROGUE")])
		with patch.object(
			se_events.frappe, "get_all", return_value=["ITM-1"]
		), patch.object(se_events.frappe, "throw", side_effect=RuntimeError) as throw:
			with self.assertRaises(RuntimeError):
				se_events.validate_items(se)
		self.assertIn("ITM-ROGUE", throw.call_args[0][0])
		self.assertIn("BOM-1", throw.call_args[0][0])
