# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Pins the index patch of the Employee IR Receive submit path.

* THAT a missing index is built online, with the exact ``ALTER TABLE`` text.
* THAT a second run changes nothing.
* THAT an index with the same name is never rebuilt: left alone when its columns match,
  logged when they do not.
* THAT an index already leading with the same columns, under any name, counts as present,
  and a shorter one does not.
* THAT a table missing a column is logged and skipped, and the other indexes still build.
* THAT ``_indexes`` reads each index's columns in index order.
* THAT a fresh install runs it, and ``patches.txt`` registers it once, after
  ``[post_model_sync]``.

DB-free per the suite convention: ``setUpClass`` is neutralised, ``frappe.db`` is a fake
schema, and ``frappe.log_error`` is mocked.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_restore_missing_perf_indexes
"""

import os
import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import install
from jewellery_erpnext.patches import restore_missing_perf_indexes as perf

PATCH = "jewellery_erpnext.patches.restore_missing_perf_indexes"
ALTER = re.compile(
	r"ALTER TABLE `(?P<table>[^`]+)` ADD INDEX `(?P<index>[^`]+)` \((?P<cols>[^)]+)\), "
	r"ALGORITHM=INPLACE, LOCK=NONE$"
)


class _Schema:
	"""Answers the patch's two ``information_schema`` reads and applies its ``ALTER``."""

	def __init__(self, indexes=None, missing_columns=None):
		self.indexes = {table: dict(ix) for table, ix in (indexes or {}).items()}
		self.missing_columns = missing_columns or {}
		self.ddl = []

	def sql(self, query, values=None):
		table = values[0]
		if "information_schema.statistics" in query:
			return [
				(name, column)
				for name, columns in sorted(self.indexes.get(table, {}).items())
				for column in columns
			]
		if "information_schema.columns" in query:
			columns = {c for t, _ix, cols in perf.SPECS if t == table for c in cols}
			return [
				(c,) for c in sorted(columns - set(self.missing_columns.get(table, ())))
			]
		raise AssertionError(f"unexpected query: {query}")

	def sql_ddl(self, query):
		self.ddl.append(query)
		m = ALTER.match(query)
		assert m, query
		columns = tuple(c.strip(" `") for c in m["cols"].split(","))
		self.indexes.setdefault(m["table"], {})[m["index"]] = columns

	def built(self):
		return [ALTER.match(q)["index"] for q in self.ddl]


class TestRestoreMissingPerfIndexes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		patcher = patch.object(perf.frappe, "log_error")
		self.log_error = patcher.start()
		self.addCleanup(patcher.stop)

	def _run(self, schema, runs=1):
		with patch.object(perf.frappe, "db", schema):
			for _ in range(runs):
				perf.execute()
		return schema

	def test_builds_every_missing_index_online(self):
		schema = self._run(_Schema())

		self.assertEqual(
			schema.built(),
			[
				"mop_balance_idx",
				"mop_voucher_cancel_idx",
				"sbb_voucher_no_idx",
				"sbe_voucher_no_idx",
			],
		)
		self.assertEqual(
			schema.ddl[0],
			"ALTER TABLE `tabMOP Log` ADD INDEX `mop_balance_idx` (`manufacturing_operation`, "
			"`is_cancelled`, `item_code`, `batch_no`, `creation`), ALGORITHM=INPLACE, LOCK=NONE",
		)
		self.log_error.assert_not_called()

	def test_a_second_run_changes_nothing(self):
		schema = self._run(_Schema(), runs=2)

		self.assertEqual(len(schema.ddl), 4)
		self.log_error.assert_not_called()

	def test_same_name_same_columns_is_left_alone(self):
		schema = self._run(
			_Schema(
				{"tabMOP Log": {"mop_balance_idx": perf.SPECS[0][2]}},
			)
		)

		self.assertNotIn("mop_balance_idx", schema.built())
		self.log_error.assert_not_called()

	def test_same_name_other_columns_is_logged_not_rebuilt(self):
		schema = self._run(
			_Schema({"tabMOP Log": {"mop_balance_idx": ("manufacturing_operation",)}})
		)

		self.assertNotIn("mop_balance_idx", schema.built())
		self.assertEqual(
			schema.indexes["tabMOP Log"]["mop_balance_idx"],
			("manufacturing_operation",),
		)
		self.log_error.assert_called_once()
		self.assertIn(
			"mop_balance_idx differs", self.log_error.call_args.kwargs["title"]
		)

	def test_an_index_leading_with_the_same_columns_counts_as_present(self):
		schema = self._run(
			_Schema(
				{
					"tabSerial and Batch Entry": {
						"voucher_batch": ("voucher_no", "voucher_type", "batch_no")
					},
					# a shorter prefix does not serve the lookup
					"tabSerial and Batch Bundle": {"voucher_no_only": ("voucher_no",)},
				}
			)
		)

		self.assertNotIn("sbe_voucher_no_idx", schema.built())
		self.assertIn("sbb_voucher_no_idx", schema.built())
		self.log_error.assert_not_called()

	def test_a_missing_column_is_logged_and_the_rest_still_build(self):
		schema = self._run(_Schema(missing_columns={"tabMOP Log": ("batch_no",)}))

		self.assertEqual(
			schema.built(),
			["mop_voucher_cancel_idx", "sbb_voucher_no_idx", "sbe_voucher_no_idx"],
		)
		self.log_error.assert_called_once()
		self.assertIn("skip mop_balance_idx", self.log_error.call_args.kwargs["title"])

	def test_indexes_reads_columns_in_index_order(self):
		schema = _Schema(
			{
				"tabMOP Log": {
					"b_idx": ("voucher_no",),
					"a_idx": ("manufacturing_operation", "creation"),
				}
			}
		)
		with patch.object(perf.frappe, "db", schema), patch.object(
			schema, "sql", wraps=schema.sql
		) as sql:
			indexes = perf._indexes("tabMOP Log")

		self.assertEqual(
			indexes,
			{
				"a_idx": ("manufacturing_operation", "creation"),
				"b_idx": ("voucher_no",),
			},
		)
		self.assertIn("ORDER BY index_name, seq_in_index", sql.call_args.args[0])

	def test_a_fresh_install_runs_it(self):
		with patch(
			"jewellery_erpnext.patches.add_customer_gold_rate_snapshot_fields.execute"
		), patch(
			"jewellery_erpnext.patches.add_batch_component_field.execute"
		), patch.object(perf, "execute") as execute:
			install._run_schema_patches()

		execute.assert_called_once_with()

	def test_registered_once_after_post_model_sync(self):
		path = os.path.join(frappe.get_app_path("jewellery_erpnext"), "patches.txt")
		with open(path) as f:
			lines = [line.strip() for line in f]

		self.assertEqual(lines.count(PATCH), 1)
		self.assertGreater(lines.index(PATCH), lines.index("[post_model_sync]"))
