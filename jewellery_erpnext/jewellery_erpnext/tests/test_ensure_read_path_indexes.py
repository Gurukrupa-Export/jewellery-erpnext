# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins ``patches/ensure_read_path_indexes``, the online build of the locked-path read indexes.

* THAT a missing index is built online, with the exact ``ALTER TABLE`` text, in SPECS order,
  and a present one (same name, same columns) issues no DDL, so a second run changes nothing.
* THAT an index of the same name with other columns is logged and never rebuilt, and an index
  leading with the same columns under any name counts, while a shorter or reordered one does
  not. No index the app does not own is ever dropped or renamed.
* THAT the kggk_uat LEGACY name is renamed when alone, dropped only when an exact upstream-named
  twin exists, and left alone otherwise.
* THAT the session ``lock_wait_timeout`` is capped for the DDL (5 s, site_config override,
  clamped) and restored, also when the DDL fails.
* THAT a metadata-lock timeout is ``busy`` and any other DDL error ``failed``, each logged once,
  with no retry and no table-copy fallback, while the other specs still build -- and that
  neither ``execute`` nor ``after_migrate`` raises.
* THAT the dry run and ``report`` issue no DDL and write no Error Log row.
* THAT the patch is registered once, last in ``[post_model_sync]``, and ``after_migrate`` keeps
  the Material Request layout hook.

DB-free per the suite convention: ``setUpClass`` is neutralised, ``frappe.db`` is a fake
schema, and ``frappe.log_error`` / ``frappe.conf`` are mocked.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_ensure_read_path_indexes
"""

import io
import os
import re
from contextlib import redirect_stdout
from unittest.mock import patch

import frappe
import pymysql
from frappe.database.database import Database
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import hooks
from jewellery_erpnext.patches import ensure_read_path_indexes as perf

PATCH = "jewellery_erpnext.patches.ensure_read_path_indexes"
MOP, SBB, SBE = "tabMOP Log", "tabSerial and Batch Bundle", "tabSerial and Batch Entry"
UPSTREAM = "voucher_no_voucher_type_index"

_ONLINE = r", ALGORITHM=INPLACE, LOCK=NONE$"
ADD = re.compile(
	r"ALTER TABLE `(?P<table>[^`]+)` ADD INDEX `(?P<index>[^`]+)` \((?P<cols>.+)\)"
	+ _ONLINE
)
DROP = re.compile(
	r"ALTER TABLE `(?P<table>[^`]+)` DROP INDEX `(?P<index>[^`]+)`" + _ONLINE
)
RENAME = re.compile(
	r"ALTER TABLE `(?P<table>[^`]+)` RENAME INDEX `(?P<old>[^`]+)` TO `(?P<new>[^`]+)`"
	+ _ONLINE
)
PART = re.compile(r"`([^`]+)`(?:\((\d+)\))?")

EXPECTED_ALTERS = [
	"ALTER TABLE `tabMOP Log` ADD INDEX `mop_balance_idx` (`manufacturing_operation`, "
	"`is_cancelled`, `item_code`, `batch_no`, `creation`), ALGORITHM=INPLACE, LOCK=NONE",
	"ALTER TABLE `tabMOP Log` ADD INDEX `mop_voucher_cancel_idx` (`voucher_type`, `voucher_no`, "
	"`is_cancelled`), ALGORITHM=INPLACE, LOCK=NONE",
	"ALTER TABLE `tabSerial and Batch Bundle` ADD INDEX `voucher_no_voucher_type_index` "
	"(`voucher_no`, `voucher_type`), ALGORITHM=INPLACE, LOCK=NONE",
	"ALTER TABLE `tabSerial and Batch Entry` ADD INDEX `voucher_no_voucher_type_index` "
	"(`voucher_no`, `voucher_type`), ALGORITHM=INPLACE, LOCK=NONE",
]

_TYPES = {"creation": "datetime", "is_cancelled": "tinyint"}


def _parts(columns):
	"""``("a", ("b", 140))`` -> ``(("a", None), ("b", 140))``."""
	return tuple(c if isinstance(c, tuple) else (c, None) for c in columns)


def _spec(table, name):
	return next(cols for t, n, cols in perf.SPECS if (t, n) == (table, name))


class _Schema:
	"""Answers the patch's ``information_schema`` / session reads and applies its online DDL.

	Any DDL that is not an online ADD / DROP / RENAME INDEX fails the test, as does any query
	the patch is not expected to send.
	"""

	db_type = "mariadb"

	def __init__(
		self, indexes=None, column_types=None, missing_columns=None, ddl_errors=None
	):
		self.indexes = {
			table: {name: _parts(cols) for name, cols in ix.items()}
			for table, ix in (indexes or {}).items()
		}
		self.columns = {}
		for table, _name, cols in perf.SPECS:
			for c in cols:
				self.columns.setdefault(table, {"name": "varchar"})[c] = _TYPES.get(
					c, "varchar"
				)
		for table, types in (column_types or {}).items():
			self.columns.setdefault(table, {}).update(types)
		for table, cols in (missing_columns or {}).items():
			if cols is None:  # the whole table is absent
				self.columns.pop(table, None)
			for c in cols or ():
				self.columns.get(table, {}).pop(c, None)
		self.ddl_errors = ddl_errors or {}
		self.session = {"lock_wait_timeout": 86400, "max_statement_time": "0.500000"}
		self.session_sets = 0
		self.ddl = []  # (statement, session at the time it ran)
		self.queries = []

	def sql(self, query, values=None, as_dict=False):
		q = " ".join(query.split())
		self.queries.append(q)
		if q == "SELECT @@SESSION.lock_wait_timeout, @@SESSION.max_statement_time":
			return (
				(self.session["lock_wait_timeout"], self.session["max_statement_time"]),
			)
		if q == "SET SESSION lock_wait_timeout = %s, max_statement_time = 0":
			self.session_sets += 1
			self.session = {"lock_wait_timeout": values[0], "max_statement_time": 0}
			return ()
		if q == "SET SESSION lock_wait_timeout = %s, max_statement_time = %s":
			self.session_sets += 1
			self.session = {
				"lock_wait_timeout": values[0],
				"max_statement_time": values[1],
			}
			return ()
		if "information_schema.statistics" in q:
			assert (
				"table_schema = DATABASE()" in q and "index_type <> 'FULLTEXT'" in q
			), q
			assert "ORDER BY index_name, seq_in_index" in q, q
			return [
				(name, column, sub_part)
				for name, parts in sorted(self.indexes.get(values[0], {}).items())
				for column, sub_part in parts
			]
		if "information_schema.columns" in q:
			assert "table_schema = DATABASE()" in q, q
			return sorted(self.columns.get(values[0], {}).items())
		if "information_schema.tables" in q:
			return [
				{"table_name": t, "table_rows": 1, "data_mb": 1, "index_mb": 1}
				for t in values
			]
		if "information_schema.innodb_trx" in q:
			raise pymysql.err.OperationalError(
				1227, "Access denied; you need the PROCESS privilege"
			)
		if q.startswith("SELECT @@version AS version"):
			return [{"version": "10.6", "session_lock_wait_timeout": 86400}]
		raise AssertionError(f"unexpected query: {q}")

	def sql_ddl(self, query):
		self.ddl.append((query, dict(self.session)))
		for needle, error in self.ddl_errors.items():
			if needle in query:
				raise error
		if m := ADD.match(query):
			parts = tuple(
				(c, int(n) if n else None) for c, n in PART.findall(m["cols"])
			)
			self.indexes.setdefault(m["table"], {})[m["index"]] = parts
		elif m := DROP.match(query):
			del self.indexes[m["table"]][m["index"]]
		elif m := RENAME.match(query):
			table = self.indexes[m["table"]]
			table[m["new"]] = table.pop(m["old"])
		else:
			raise AssertionError(f"not an online index DDL: {query}")

	def statements(self):
		return [q for q, _session in self.ddl]


def _all_present():
	indexes = {}
	for table, name, cols in perf.SPECS:
		indexes.setdefault(table, {})[name] = cols
	return indexes


class TestEnsureReadPathIndexes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		log_error = patch.object(perf.frappe, "log_error")
		conf = patch.object(
			perf.frappe, "conf", frappe._dict()
		)  # no site_config override
		self.log_error = log_error.start()
		conf.start()
		self.addCleanup(log_error.stop)
		self.addCleanup(conf.stop)

	def _run(self, schema, fn=None, **kwargs):
		out = io.StringIO()
		with patch.object(perf.frappe, "db", schema), redirect_stdout(out):
			result = (fn or perf.ensure)(**kwargs)
		self.output = out.getvalue()
		return result

	@staticmethod
	def _status(report):
		return {(r["table"], r["index"]): r["status"] for r in report}

	def _log_titles(self):
		return [c.kwargs["title"] for c in self.log_error.call_args_list]

	# -- building ---------------------------------------------------------------------------

	def test_builds_every_missing_index_online(self):
		schema = _Schema()
		report = self._run(schema)

		self.assertEqual(schema.statements(), EXPECTED_ALTERS)
		self.assertEqual([r["status"] for r in report], ["built"] * 4)
		self.assertTrue(all(r["seconds"] is not None for r in report))
		self.log_error.assert_not_called()

	def test_second_run_issues_no_ddl(self):
		schema = _Schema()
		self._run(schema)
		sets_after_first = schema.session_sets
		report = self._run(schema)

		self.assertEqual(len(schema.ddl), 4)
		self.assertEqual(schema.session_sets, sets_after_first)
		self.assertEqual([r["status"] for r in report], ["present"] * 4)

	def test_present_indexes_issue_no_ddl_and_after_migrate_stays_quiet(self):
		schema = _Schema(_all_present())
		self._run(schema, fn=perf.after_migrate)

		self.assertEqual(schema.ddl, [])
		self.assertEqual(schema.session_sets, 0)
		self.assertEqual(self.output, "")
		self.log_error.assert_not_called()

	# -- definitions it does not own --------------------------------------------------------

	def test_same_name_other_columns_is_logged_never_rebuilt(self):
		schema = _Schema({MOP: {"mop_balance_idx": ("manufacturing_operation",)}})
		report = self._run(schema)

		self.assertEqual(self._status(report)[(MOP, "mop_balance_idx")], "differs")
		self.assertEqual(
			schema.indexes[MOP]["mop_balance_idx"], (("manufacturing_operation", None),)
		)
		self.assertNotIn(EXPECTED_ALTERS[0], schema.statements())
		self.assertEqual(len(schema.ddl), 3)
		self.assertEqual(
			self._log_titles(), ["ensure_read_path_indexes: mop_balance_idx differs"]
		)

	def test_index_leading_with_the_same_columns_under_any_name_counts(self):
		schema = _Schema(
			{
				SBE: {"voucher_batch": ("voucher_no", "voucher_type", "batch_no")},
				MOP: {"other_name": _spec(MOP, "mop_voucher_cancel_idx")},
			}
		)
		report = self._run(schema)

		status = self._status(report)
		self.assertEqual(status[(SBE, UPSTREAM)], "equivalent")
		self.assertEqual(status[(MOP, "mop_voucher_cancel_idx")], "equivalent")
		self.assertEqual(schema.statements(), [EXPECTED_ALTERS[0], EXPECTED_ALTERS[2]])
		self.log_error.assert_not_called()

	def test_shorter_or_reordered_prefix_does_not_count(self):
		schema = _Schema(
			{
				SBB: {"voucher_no_only": ("voucher_no",)},
				SBE: {"type_then_no": ("voucher_type", "voucher_no")},
			}
		)
		self._run(schema)

		self.assertIn(EXPECTED_ALTERS[2], schema.statements())
		self.assertIn(EXPECTED_ALTERS[3], schema.statements())
		# ... and the foreign indexes are still there, untouched.
		self.assertIn("voucher_no_only", schema.indexes[SBB])
		self.assertIn("type_then_no", schema.indexes[SBE])
		self.assertFalse(
			[q for q in schema.statements() if "DROP" in q or "RENAME" in q]
		)

	def test_missing_column_or_table_is_logged_and_the_rest_still_build(self):
		schema = _Schema(missing_columns={MOP: ("batch_no",), SBE: None})
		report = self._run(schema)

		status = self._status(report)
		self.assertEqual(status[(MOP, "mop_balance_idx")], "missing-columns")
		self.assertEqual(status[(SBE, UPSTREAM)], "missing-columns")
		self.assertEqual(schema.statements(), [EXPECTED_ALTERS[1], EXPECTED_ALTERS[2]])
		self.assertEqual(
			self._log_titles(),
			[
				"ensure_read_path_indexes: skip mop_balance_idx",
				"ensure_read_path_indexes: skip voucher_no_voucher_type_index",
			],
		)

	def test_text_column_gets_a_prefix_and_the_sub_part_is_compared(self):
		schema = _Schema(column_types={MOP: {"batch_no": "TEXT"}})
		self._run(schema)

		self.assertIn("`batch_no`(140), `creation`", schema.statements()[0])
		self.assertEqual(schema.indexes[MOP]["mop_balance_idx"][3], ("batch_no", 140))
		report = self._run(schema)  # the prefix index counts as present
		self.assertEqual(self._status(report)[(MOP, "mop_balance_idx")], "present")
		self.assertEqual(len(schema.ddl), 4)

	# -- LEGACY (kggk_uat names) ------------------------------------------------------------

	def test_legacy_name_alone_is_renamed_not_rebuilt(self):
		schema = _Schema({SBB: {"sbb_voucher_no_idx": ("voucher_no", "voucher_type")}})
		report = self._run(schema)

		self.assertEqual(self._status(report)[(SBB, UPSTREAM)], "renamed")
		self.assertIn(
			"ALTER TABLE `tabSerial and Batch Bundle` RENAME INDEX `sbb_voucher_no_idx` TO "
			"`voucher_no_voucher_type_index`, ALGORITHM=INPLACE, LOCK=NONE",
			schema.statements(),
		)
		self.assertNotIn(EXPECTED_ALTERS[2], schema.statements())
		self.assertNotIn("sbb_voucher_no_idx", schema.indexes[SBB])
		ddl = len(schema.ddl)
		self._run(schema)
		self.assertEqual(len(schema.ddl), ddl)

	def test_legacy_duplicate_dropped_only_with_an_exact_upstream_twin(self):
		schema = _Schema(
			{
				# exact twin under the upstream name -> the LEGACY copy goes
				SBE: {
					UPSTREAM: ("voucher_no", "voucher_type"),
					"sbe_voucher_no_idx": ("voucher_no", "voucher_type"),
				},
				# upstream name with other columns -> nothing is dropped
				SBB: {
					UPSTREAM: ("voucher_no",),
					"sbb_voucher_no_idx": ("voucher_no", "voucher_type"),
				},
			}
		)
		report = self._run(schema)

		status = self._status(report)
		self.assertEqual(status[(SBE, UPSTREAM)], "dropped-duplicate")
		self.assertEqual(status[(SBB, UPSTREAM)], "differs")
		self.assertNotIn("sbe_voucher_no_idx", schema.indexes[SBE])
		self.assertIn("sbb_voucher_no_idx", schema.indexes[SBB])
		drops = [q for q in schema.statements() if DROP.match(q)]
		self.assertEqual(
			drops,
			[
				"ALTER TABLE `tabSerial and Batch Entry` DROP INDEX `sbe_voucher_no_idx`, "
				"ALGORITHM=INPLACE, LOCK=NONE"
			],
		)

	def test_legacy_name_with_other_columns_is_left_alone(self):
		schema = _Schema({SBB: {"sbb_voucher_no_idx": ("voucher_no",)}})
		self._run(schema)

		self.assertIn(EXPECTED_ALTERS[2], schema.statements())
		self.assertEqual(
			schema.indexes[SBB]["sbb_voucher_no_idx"], (("voucher_no", None),)
		)

	# -- metadata-lock guard ----------------------------------------------------------------

	def test_lock_wait_capped_for_the_ddl_and_restored(self):
		schema = _Schema()
		self._run(schema)

		for _query, session in schema.ddl:
			self.assertEqual(session, {"lock_wait_timeout": 5, "max_statement_time": 0})
		self.assertEqual(
			schema.session,
			{"lock_wait_timeout": 86400, "max_statement_time": "0.500000"},
		)

	def test_lock_wait_restored_even_when_the_ddl_fails(self):
		schema = _Schema(ddl_errors={"mop_balance_idx": RuntimeError("boom")})
		report = self._run(schema)

		self.assertEqual(self._status(report)[(MOP, "mop_balance_idx")], "failed")
		self.assertEqual(schema.ddl[0][1]["lock_wait_timeout"], 5)
		self.assertEqual(
			schema.session,
			{"lock_wait_timeout": 86400, "max_statement_time": "0.500000"},
		)

	def test_lock_wait_from_site_config_argument_wins_and_is_clamped(self):
		with patch.object(perf.frappe, "conf", frappe._dict(je_index_ddl_lock_wait=12)):
			schema = _Schema()
			self._run(schema)
			self.assertEqual({s["lock_wait_timeout"] for _q, s in schema.ddl}, {12})

			schema = _Schema()
			self._run(schema, lock_wait=3)
			self.assertEqual({s["lock_wait_timeout"] for _q, s in schema.ddl}, {3})

		for raw, effective in (
			(0, 1),
			(-5, 1),
			(999, 60),
			("7", 7),
			("abc", 5),
			(None, 5),
		):
			with patch.object(
				perf.frappe, "conf", frappe._dict(je_index_ddl_lock_wait=raw)
			):
				self.assertEqual(perf.resolve_lock_wait(), effective, raw)

	def test_metadata_lock_timeout_is_busy_and_non_fatal(self):
		def schema():
			return _Schema(
				ddl_errors={
					"mop_balance_idx": frappe.QueryTimeoutError(
						"(1205, 'Lock wait timeout exceeded')"
					)
				}
			)

		s = schema()
		report = self._run(s)
		self.assertEqual(
			[r["status"] for r in report],
			["busy", "built", "built", "built"],
		)
		self.assertEqual(len([q for q in s.statements() if "mop_balance_idx" in q]), 1)
		self.assertEqual(
			self._log_titles(), ["ensure_read_path_indexes: mop_balance_idx busy"]
		)
		self.assertEqual(s.session["lock_wait_timeout"], 86400)

		for fn in (perf.execute, perf.after_migrate):
			s = schema()
			self._run(s, fn=fn)  # returns normally
			self.assertIn("mop_balance_idx: busy -- see Error Log", self.output)
			self.assertEqual(len(s.ddl), 4)

	def test_no_table_copy_fallback(self):
		error = pymysql.err.OperationalError(
			1846, "LOCK=NONE is not supported. Try LOCK=SHARED"
		)
		schema = _Schema(ddl_errors={"`tabSerial and Batch Bundle`": error})
		report = self._run(schema)

		self.assertEqual(self._status(report)[(SBB, UPSTREAM)], "failed")
		self.assertEqual(len([q for q in schema.statements() if SBB in q]), 1)
		self.assertTrue(
			all(
				q.endswith(", ALGORITHM=INPLACE, LOCK=NONE")
				for q in schema.statements()
			)
		)
		self.assertEqual(
			self._log_titles(), [f"ensure_read_path_indexes: {UPSTREAM} failed"]
		)

	# -- dry run / report -------------------------------------------------------------------

	def test_dry_run_issues_no_ddl_and_writes_no_error_log(self):
		schema = _Schema(
			{
				MOP: {"mop_balance_idx": ("manufacturing_operation",)},
				SBB: {"sbb_voucher_no_idx": ("voucher_no", "voucher_type")},
			}
		)
		report = self._run(schema, dry_run=True)

		self.assertEqual(
			[r["status"] for r in report],
			["differs", "would-build", "would-rename", "would-build"],
		)
		self.assertEqual(report[1]["sql"], EXPECTED_ALTERS[1])
		self.assertEqual(schema.ddl, [])
		self.assertEqual(schema.session_sets, 0)
		self.log_error.assert_not_called()

	def test_report_is_read_only(self):
		schema = _Schema()
		result = self._run(schema, fn=perf.report)

		self.assertEqual([r["status"] for r in result["indexes"]], ["would-build"] * 4)
		self.assertEqual(result["lock_wait"], 5)
		self.assertEqual({t["table_name"] for t in result["tables"]}, {MOP, SBB, SBE})
		self.assertTrue(result["open_transactions_over_30s"].startswith("unavailable"))
		self.assertEqual(schema.ddl, [])
		self.assertEqual(schema.session_sets, 0)
		self.log_error.assert_not_called()

	# -- wiring -----------------------------------------------------------------------------

	def test_after_migrate_swallows_an_unexpected_error(self):
		with patch.object(perf, "ensure", side_effect=RuntimeError("boom")):
			self._run(_Schema(), fn=perf.after_migrate)

		self.assertEqual(self._log_titles(), ["after_migrate: read path indexes"])
		self.assertIn(
			"WARNING: the read path indexes could not be checked", self.output
		)

	def test_non_mariadb_does_nothing(self):
		schema = _Schema()
		schema.db_type = "postgres"
		self.assertEqual(self._run(schema), [])
		self.assertEqual(schema.queries, [])

	def test_specs_are_composite_and_names_match_upstream(self):
		for _table, _name, columns in perf.SPECS:
			self.assertGreaterEqual(
				len(columns), 2
			)  # schema sync only drops single-column ones
		self.assertEqual(
			UPSTREAM, Database.get_index_name(["voucher_no", "voucher_type"])
		)
		self.assertEqual(_spec(SBB, UPSTREAM), ("voucher_no", "voucher_type"))
		self.assertEqual(_spec(SBE, UPSTREAM), ("voucher_no", "voucher_type"))
		self.assertTrue(set(perf.LEGACY) <= {(t, n) for t, n, _c in perf.SPECS})

	def test_registered_once_last_in_post_model_sync(self):
		path = os.path.join(frappe.get_app_path("jewellery_erpnext"), "patches.txt")
		with open(path) as f:
			lines = [line.strip() for line in f]
		entries = [line for line in lines if line and not line.startswith("#")]

		self.assertEqual(lines.count(PATCH), 1)
		self.assertGreater(lines.index(PATCH), lines.index("[post_model_sync]"))
		self.assertEqual(entries[-1], PATCH)

	def test_after_migrate_wired_and_mr_layout_hook_kept(self):
		# Membership and order only: other after_migrate hooks may be added beside these.
		wired = list(hooks.after_migrate)
		mr_layout = "jewellery_erpnext.patches.add_material_request_total_pcs_field.after_migrate"
		self.assertEqual(wired[0], mr_layout)
		self.assertEqual(wired.count(f"{PATCH}.after_migrate"), 1)
		self.assertEqual(len(wired), len(set(wired)))
		for path in wired:
			self.assertTrue(callable(frappe.get_attr(path)))
