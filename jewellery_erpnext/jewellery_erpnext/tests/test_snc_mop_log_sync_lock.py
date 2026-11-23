"""Serial Number Creator marks MOP Logs synced by primary key, not by operation range.

20 Nov 2026 (kggk-prod): EMP-IR-Labh-2026-65488 and -65664 died with 1213 at the MOP Log
INSERT a few seconds into a Serial Number Creator submit. The submit's
``UPDATE tabMOP Log ... WHERE manufacturing_operation IN ... AND is_synced = 0`` had no
``manufacturing_operation`` index to use, range-scanned ``mop_eod_sync_idx`` and next-key
locked every unsynced log, which is where every new MOP Log is inserted.

Portable unit tests: every database touch is mocked, nothing is read or written.
"""

import inspect
import re
from unittest.mock import patch

from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator import (
	serial_number_creator as snc_mod,
)


class TestMarkMopLogsSynced(UnitTestCase):
	def _run(self, mop_names, found):
		with patch.object(
			snc_mod.frappe, "get_all", return_value=list(found)
		) as get_all, patch.object(snc_mod.frappe.db, "sql") as sql:
			count = snc_mod.mark_mop_logs_synced(mop_names)
		return count, get_all, sql

	def test_no_operations_touch_nothing(self):
		for empty in (None, [], [None, ""]):
			count, get_all, sql = self._run(empty, ["x"])
			self.assertEqual(count, 0)
			get_all.assert_not_called()
			sql.assert_not_called()

	def test_reads_deduped_sorted_operations_without_a_lock(self):
		_count, get_all, _sql = self._run(["MOP-B", "MOP-A", "MOP-B", None], ["L1"])
		args, kwargs = get_all.call_args
		self.assertEqual(args, ("MOP Log",))
		self.assertEqual(
			kwargs["filters"],
			{
				"manufacturing_operation": ["in", ["MOP-A", "MOP-B"]],
				"is_cancelled": 0,
				"is_synced": 0,
			},
		)
		self.assertEqual(kwargs["pluck"], "name")
		self.assertNotIn("for_update", kwargs)

	def test_update_is_keyed_on_name_only(self):
		count, _get_all, sql = self._run(["MOP-A"], ["L2", "L1"])
		self.assertEqual(count, 2)
		sql.assert_called_once()
		query, params = sql.call_args.args
		flat = " ".join(query.split())
		self.assertIn("UPDATE `tabMOP Log` SET is_synced = 1", flat)
		self.assertIn("WHERE name IN %s", flat)
		self.assertNotIn("manufacturing_operation", flat)
		self.assertEqual(params, (("L2", "L1"),))

	def test_nothing_open_issues_no_update(self):
		count, _get_all, sql = self._run(["MOP-A"], [])
		self.assertEqual(count, 0)
		sql.assert_not_called()

	def test_large_sets_update_in_chunks(self):
		names = [f"L{i:05d}" for i in range(2 * snc_mod.MOP_LOG_SYNC_CHUNK + 1)]
		count, _get_all, sql = self._run(["MOP-A"], names)
		self.assertEqual(count, len(names))
		sizes = [len(c.args[1][0]) for c in sql.call_args_list]
		self.assertEqual(
			sizes, [snc_mod.MOP_LOG_SYNC_CHUNK, snc_mod.MOP_LOG_SYNC_CHUNK, 1]
		)
		sent = [n for c in sql.call_args_list for n in c.args[1][0]]
		self.assertEqual(sent, names)


class TestNoOperationRangeUpdateLeft(UnitTestCase):
	def test_module_has_no_mop_log_update_filtered_by_operation(self):
		"""Regression guard: the range UPDATE must not come back anywhere in the module."""
		source = inspect.getsource(snc_mod)
		for stmt in re.findall(
			r"UPDATE\s+`tabMOP Log`.*?(?=\"\"\")", source, flags=re.S
		):
			self.assertNotRegex(
				stmt, r"WHERE\s+manufacturing_operation", msg=" ".join(stmt.split())
			)

	def test_submit_path_uses_the_helper(self):
		source = inspect.getsource(snc_mod.to_prepare_data_for_make_mnf_stock_entry)
		self.assertIn("mark_mop_logs_synced(", source)
