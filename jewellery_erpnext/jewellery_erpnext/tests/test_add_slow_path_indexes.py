# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Pins the slow-path index patch.

* THAT every missing index is built online, with the exact ``ALTER TABLE`` text, and a prefix
  only on TEXT columns.
* THAT a second run changes nothing.
* THAT the Serial and Batch Bundle index carries ERPNext v16.55's own name, so the upgrade's
  ``frappe.db.add_index`` finds it instead of building a duplicate.
* THAT an index with the same name is never rebuilt: left alone when its columns match, logged
  when they do not; an index already leading with the same columns, under any name, counts as
  present, and a shorter one does not.
* THAT a table missing a column is logged and skipped, and the other indexes still build.
* THAT a failed build is logged and does not stop the rest.
* THAT ``lock_wait_timeout`` is bounded during the run and restored afterwards, even on error.
* THAT no varchar index is single-column (Frappe's schema sync would drop it) and none leads
  with ``parent``.
* THAT ``patches.txt`` registers it once, after ``[post_model_sync]``.

DB-free: ``setUpClass`` is neutralised, ``frappe.db`` is a fake schema, ``frappe.log_error`` is
mocked.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_add_slow_path_indexes
"""

import os
import re
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import add_slow_path_indexes as idx

PATCH = "jewellery_erpnext.patches.add_slow_path_indexes"
ALTER = re.compile(
	r"ALTER TABLE `(?P<table>[^`]+)` ADD INDEX `(?P<index>[^`]+)` \((?P<cols>.+)\), "
	r"ALGORITHM=INPLACE, LOCK=NONE$"
)
COLUMN = re.compile(r"`(?P<name>[^`]+)`(?:\((?P<prefix>\d+)\))?")

#: The column types gke_customization's fixture produces on a migrated site.
TEXT_COLUMNS = {
	("tabStock Entry Detail", "manufacturing_operation"),
	("tabStock Entry Detail", "custom_manufacturing_work_order"),
	("tabBOM", "route"),
}


class _Schema:
	"""Answers the patch's ``information_schema`` reads, records its ``ALTER`` and ``SET``."""

	def __init__(self, indexes=None, missing_columns=None, fail=(), lock_wait=31536000):
		self.indexes = {table: dict(ix) for table, ix in (indexes or {}).items()}
		self.missing_columns = missing_columns or {}
		self.fail = set(fail)
		self.lock_wait = lock_wait
		self.lock_waits_seen = []
		self.ddl = []

	def sql(self, query, values=None):
		if query.startswith("SELECT @@SESSION.lock_wait_timeout"):
			return [(self.lock_wait,)]
		if query.startswith("SET SESSION lock_wait_timeout"):
			self.lock_wait = int(query.rsplit("=", 1)[1])
			self.lock_waits_seen.append(self.lock_wait)
			return []
		table = values[0]
		if "information_schema.statistics" in query:
			return [
				(name, column)
				for name, columns in sorted(self.indexes.get(table, {}).items())
				for column in columns
			]
		if "information_schema.columns" in query:
			columns = {c for t, _ix, spec in idx.SPECS if t == table for c, _p in spec}
			columns -= set(self.missing_columns.get(table, ()))
			return [
				(c, "text" if (table, c) in TEXT_COLUMNS else "varchar")
				for c in sorted(columns)
			]
		raise AssertionError(f"unexpected query: {query}")

	def sql_ddl(self, query):
		m = ALTER.match(query)
		assert m, query
		if m["index"] in self.fail:
			raise Exception("Lock wait timeout exceeded; try restarting transaction")
		self.ddl.append(query)
		columns = tuple(c["name"] for c in COLUMN.finditer(m["cols"]))
		self.indexes.setdefault(m["table"], {})[m["index"]] = columns

	def built(self):
		return [ALTER.match(q)["index"] for q in self.ddl]


class TestAddSlowPathIndexes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		patcher = patch.object(idx.frappe, "log_error")
		self.log_error = patcher.start()
		self.addCleanup(patcher.stop)

	def _run(self, schema, runs=1):
		with patch.object(idx.frappe, "db", schema), patch.object(
			idx.frappe, "get_traceback", return_value="tb"
		):
			for _ in range(runs):
				idx.execute()
		return schema

	def test_builds_every_missing_index_online(self):
		schema = self._run(_Schema())

		self.assertEqual(schema.built(), [name for _t, name, _s in idx.SPECS])
		self.assertIn(
			"ALTER TABLE `tabStock Entry Detail` ADD INDEX `sed_manufacturing_operation_idx` "
			"(`manufacturing_operation`(140)), ALGORITHM=INPLACE, LOCK=NONE",
			schema.ddl,
		)
		self.assertIn(
			"ALTER TABLE `tabBOM` ADD INDEX `bom_website_route_idx` "
			"(`show_in_website`, `route`(140)), ALGORITHM=INPLACE, LOCK=NONE",
			schema.ddl,
		)
		self.log_error.assert_not_called()

	def test_prefix_only_on_text_columns(self):
		schema = self._run(_Schema())

		for query in schema.ddl:
			m = ALTER.match(query)
			for column in COLUMN.finditer(m["cols"]):
				is_text = (m["table"], column["name"]) in TEXT_COLUMNS
				self.assertEqual(bool(column["prefix"]), is_text, query)

	def test_a_varchar_column_declared_with_a_prefix_is_indexed_whole(self):
		schema = _Schema()
		real = schema.sql

		def as_varchar(query, values=None):
			rows = real(query, values)
			if values and "information_schema.columns" in query:
				return [(c, "varchar") for c, _t in rows]
			return rows

		schema.sql = as_varchar
		self._run(schema)

		self.assertIn(
			"ALTER TABLE `tabStock Entry Detail` ADD INDEX `sed_manufacturing_operation_idx` "
			"(`manufacturing_operation`), ALGORITHM=INPLACE, LOCK=NONE",
			schema.ddl,
		)

	def test_a_second_run_changes_nothing(self):
		schema = self._run(_Schema(), runs=2)

		self.assertEqual(len(schema.ddl), len(idx.SPECS))
		self.log_error.assert_not_called()

	def test_bundle_index_uses_erpnext_v16_55_name(self):
		table, name, spec = idx.SPECS[0]

		self.assertEqual(table, "tabSerial and Batch Bundle")
		# frappe.db.get_index_name(["voucher_no", "voucher_type"]) on ERPNext v16.55.0
		self.assertEqual(name, "voucher_no_voucher_type_index")
		self.assertEqual(tuple(c for c, _p in spec), ("voucher_no", "voucher_type"))

	def test_erpnext_index_already_present_is_not_duplicated(self):
		schema = self._run(
			_Schema(
				{
					"tabSerial and Batch Bundle": {
						"voucher_no_voucher_type_index": ("voucher_no", "voucher_type")
					}
				}
			)
		)

		self.assertNotIn("voucher_no_voucher_type_index", schema.built())
		self.log_error.assert_not_called()

	def test_same_name_different_columns_is_logged_and_left(self):
		schema = self._run(
			_Schema({"tabItem": {"item_order_form_idx": ("order_form_id",)}})
		)

		self.assertNotIn("item_order_form_idx", schema.built())
		self.assertEqual(self.log_error.call_count, 1)
		self.assertIn("differs", self.log_error.call_args.kwargs["title"])

	def test_same_leading_columns_under_another_name_counts_as_present(self):
		schema = self._run(
			_Schema(
				{
					"tabSerial and Batch Bundle": {
						"sbb_voucher_no_idx": (
							"voucher_no",
							"voucher_type",
							"is_cancelled",
						)
					}
				}
			)
		)

		self.assertNotIn("voucher_no_voucher_type_index", schema.built())

	def test_a_shorter_index_does_not_count(self):
		schema = self._run(
			_Schema(
				{"tabSerial and Batch Bundle": {"voucher_no_index": ("voucher_no",)}}
			)
		)

		self.assertIn("voucher_no_voucher_type_index", schema.built())

	def test_missing_column_is_logged_and_the_rest_still_build(self):
		schema = self._run(
			_Schema(missing_columns={"tabStock Entry Detail": ("stock_entry",)})
		)

		self.assertNotIn("sed_stock_entry_idx", schema.built())
		self.assertEqual(len(schema.built()), len(idx.SPECS) - 1)
		self.assertEqual(self.log_error.call_count, 1)
		self.assertIn(
			"skip sed_stock_entry_idx", self.log_error.call_args.kwargs["title"]
		)

	def test_failed_build_is_logged_and_the_rest_still_build(self):
		schema = self._run(_Schema(fail={"sed_stock_entry_idx"}))

		self.assertNotIn("sed_stock_entry_idx", schema.built())
		self.assertEqual(len(schema.built()), len(idx.SPECS) - 1)
		self.assertEqual(self.log_error.call_count, 1)
		self.assertIn("not built", self.log_error.call_args.kwargs["title"])

	def test_failed_build_is_retried_by_the_next_run(self):
		schema = self._run(_Schema(fail={"sed_stock_entry_idx"}))
		schema.fail.clear()
		self._run(schema)

		self.assertIn("sed_stock_entry_idx", schema.built())

	def test_lock_wait_timeout_bounded_then_restored(self):
		schema = self._run(_Schema(lock_wait=31536000))

		self.assertEqual(schema.lock_waits_seen, [idx.LOCK_WAIT_TIMEOUT, 31536000])
		self.assertEqual(schema.lock_wait, 31536000)

	def test_lock_wait_timeout_restored_on_unexpected_error(self):
		schema = _Schema(lock_wait=50)
		with patch.object(
			idx, "_add_index_if_missing", side_effect=RuntimeError("boom")
		):
			with self.assertRaises(RuntimeError):
				self._run(schema)

		self.assertEqual(schema.lock_wait, 50)

	def test_no_varchar_index_is_single_column_and_none_leads_with_parent(self):
		for table, name, spec in idx.SPECS:
			self.assertNotEqual(spec[0][0], "parent", name)
			if len(spec) == 1:
				self.assertIn((table, spec[0][0]), TEXT_COLUMNS, name)

	def test_no_index_on_stock_reservation_entry(self):
		# Unordered Stock Reservation Entry reads decide which reservation is used; a new index
		# could change the order they come back in.
		self.assertFalse(
			[
				name
				for table, name, _spec in idx.SPECS
				if table == "tabStock Reservation Entry"
			]
		)

	def test_bundle_voucher_type_keeps_a_composite_index_for_erpnext_v16_55(self):
		# v16.55 drops the single-column voucher_type index; the cancel dialog's
		# SELECT DISTINCT voucher_type ... ORDER BY creation must still be index-only.
		self.assertIn(
			(
				"tabSerial and Batch Bundle",
				(("voucher_type", None), ("creation", None)),
			),
			[(table, spec) for table, _name, spec in idx.SPECS],
		)

	def test_registered_once_in_post_model_sync(self):
		path = os.path.join(os.path.dirname(idx.__file__), "..", "patches.txt")
		with open(path) as f:
			lines = [line.split("#")[0].strip() for line in f]

		self.assertEqual(lines.count(PATCH), 1)
		self.assertGreater(lines.index(PATCH), lines.index("[post_model_sync]"))
