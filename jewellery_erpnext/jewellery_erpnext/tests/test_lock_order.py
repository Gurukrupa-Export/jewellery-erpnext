# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the canonical lock-ordering helpers (jewellery_erpnext.lock_order)."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins,
	lock_bins_for_rows,
	preallocate_series,
	sorted_stock_rows,
	stock_lock_key,
)


class TestStockLockKey(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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

	def tearDown(self):
		return super().tearDown()


class TestSortedStockRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
		self.assertEqual(
			[r["item_code"] for r in rows], ["B", "A"]
		)  # original order intact

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

	def tearDown(self):
		return super().tearDown()


class TestLockBins(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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

	def tearDown(self):
		return super().tearDown()


class TestLockBinsForRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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

	def tearDown(self):
		return super().tearDown()


class TestPreallocateSeries(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(lock_order.frappe.db, "sql")
	def test_locks_unique_nonblank_prefixes_in_sorted_order(self, mock_sql):
		preallocate_series(["SE-", "SRE-", "SE-", "", None])
		self.assertEqual(mock_sql.call_count, 2)
		locked = [c[0][1][0] for c in mock_sql.call_args_list]
		self.assertEqual(locked, ["SE-", "SRE-"])
		self.assertIn("FOR UPDATE", mock_sql.call_args_list[0][0][0])

	def tearDown(self):
		return super().tearDown()


class TestSeriesPrefixForDoc(IntegrationTestCase):
	"""series_prefix_for_doc must resolve the exact tabSeries key getseries() would lock
	for a naming_series doctype, and return None (no lock) for hash/other naming."""

	@classmethod
	def setUpClass(cls):
		pass

	def _meta(self, autoname, naming_rule="Expression"):
		m = Mock()
		m.autoname = autoname
		m.get = Mock(return_value=naming_rule)
		return m

	def test_returns_none_for_hash_naming(self):
		with patch.object(
			lock_order.frappe, "get_meta", return_value=self._meta("hash")
		):
			doc = frappe._dict(doctype="Stock Ledger Entry")
			self.assertIsNone(lock_order.series_prefix_for_doc(doc))

	def test_resolves_naming_series_prefix(self):
		def fake_parse(key, doctype, doc, number_generator=None):
			# Emulate frappe: the '#####' part hands the accumulated prefix to the counter.
			number_generator("MAT-STE-2026-", 5)
			return "MAT-STE-2026-00001"

		with patch.object(
			lock_order.frappe, "get_meta", return_value=self._meta("naming_series:")
		), patch(
			"frappe.model.naming.parse_naming_series", side_effect=fake_parse
		), patch(
			"frappe.model.naming.get_default_naming_series",
			return_value="MAT-STE-.YYYY.-",
		):
			doc = frappe._dict(doctype="Stock Entry", naming_series="MAT-STE-.YYYY.-")
			self.assertEqual(lock_order.series_prefix_for_doc(doc), "MAT-STE-2026-")

	def test_swallows_parse_errors_and_returns_none(self):
		# Naming resolution must never break a submit — a parse error degrades to "no pin".
		with patch.object(
			lock_order.frappe, "get_meta", return_value=self._meta("naming_series:")
		), patch(
			"frappe.model.naming.parse_naming_series", side_effect=ValueError("boom")
		), patch("frappe.model.naming.get_default_naming_series", return_value="X-"):
			doc = frappe._dict(doctype="Stock Entry", naming_series="X-")
			self.assertIsNone(lock_order.series_prefix_for_doc(doc))

	def tearDown(self):
		return super().tearDown()


class TestPreallocateSeriesForDocs(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(lock_order, "document_naming_rule_for_doc", return_value=None)
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc")
	def test_naming_series_path_when_no_dnr(self, mock_prefix, mock_pre, mock_dnr):
		# No Document Naming Rule governs either doc -> both use their naming_series row.
		mock_prefix.side_effect = ["MAT-STE-2026-", None]
		d1 = frappe._dict(doctype="Stock Entry")
		d2 = frappe._dict(doctype="Stock Ledger Entry")
		lock_order.preallocate_series_for_docs(d1, None, d2)
		# None *docs* skipped before resolution; the DNR resolver runs for the 2 real docs.
		self.assertEqual(mock_dnr.call_count, 2)
		self.assertEqual(mock_prefix.call_count, 2)
		# None (hash-named) prefixes are filtered out; only real prefixes are pinned.
		mock_pre.assert_called_once_with(["MAT-STE-2026-"])

	@patch.object(lock_order.frappe.db, "get_value")
	@patch.object(lock_order, "document_naming_rule_for_doc")
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc")
	def test_dnr_governed_doc_pins_counter_not_naming_series(
		self, mock_prefix, mock_pre, mock_dnr, mock_getval
	):
		# When a Document Naming Rule governs the doc (F-003), pin its counter row
		# FOR UPDATE and do NOT also lock the naming_series row it never increments.
		mock_dnr.return_value = "KGJPL-RULE"
		d = frappe._dict(doctype="Stock Entry")
		lock_order.preallocate_series_for_docs(d)
		mock_prefix.assert_not_called()  # naming_series path skipped for DNR-governed docs
		mock_getval.assert_called_once_with(
			"Document Naming Rule", "KGJPL-RULE", "counter", for_update=True
		)
		mock_pre.assert_called_once_with([])  # no naming_series prefixes to pin

	@patch.object(lock_order.frappe.db, "get_value")
	@patch.object(lock_order, "document_naming_rule_for_doc")
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc")
	def test_named_doc_on_a_sharded_counter_pins_nothing(
		self, mock_prefix, mock_pre, mock_dnr, mock_getval
	):
		# SHARDED (a Document Naming Rule governs the doc): a doc that already carries a
		# name will never increment its counter again — frappe names inside insert() BEFORE
		# the before_save/before_submit hooks, and the update path never names at all. The
		# counter is per-(company x type), so nothing else wants that row either. Pin
		# nothing. This is the 55% of production 1213s that died on MAT-STE-.
		mock_dnr.return_value = "KGJPL-RULE"
		doc = frappe._dict(doctype="Stock Entry", name="MAT-STE-23943")
		lock_order.preallocate_series_for_docs(doc)
		mock_getval.assert_not_called()  # the rule's counter is NOT pinned
		mock_prefix.assert_not_called()
		mock_pre.assert_called_once_with([])

	@patch.object(lock_order, "document_naming_rule_for_doc", return_value=None)
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc", return_value="MAT-STE-")
	def test_named_doc_on_a_shared_series_still_pins_it(
		self, mock_prefix, mock_pre, mock_dnr
	):
		# NOT SHARDED (no rule matches): the doc falls back to a tabSeries row shared with
		# every other Stock Entry — including the nested ones an on_submit cascade mints
		# while already holding Bin locks. Skipping the pre-lock here would let the cascade
		# take that row AFTER its Bins: the inversion this module exists to prevent.
		# This is what makes the optimisation safe to deploy before the naming shard.
		doc = frappe._dict(doctype="Stock Entry", name="MAT-STE-23943")
		lock_order.preallocate_series_for_docs(doc)
		mock_pre.assert_called_once_with(["MAT-STE-"])

	@patch.object(lock_order, "document_naming_rule_for_doc", return_value=None)
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc", return_value="MAT-STE-")
	def test_unsaved_doc_always_pins_its_counter(self, mock_prefix, mock_pre, mock_dnr):
		# The stubs series_stubs() builds for Stock Entries a cascade mints LATER carry no
		# name, and are exactly the case the pre-lock exists for. Both a bare unnamed doc
		# and frappe's "new-<doctype>-<hash>" placeholder qualify.
		for name in (None, "", "new-stock-entry-blogmxotln"):
			with self.subTest(name=name):
				mock_pre.reset_mock()
				doc = frappe._dict(doctype="Stock Entry", name=name)
				lock_order.preallocate_series_for_docs(doc)
				mock_pre.assert_called_once_with(["MAT-STE-"])

	@patch.object(lock_order.frappe.db, "get_value")
	@patch.object(lock_order, "document_naming_rule_for_doc", return_value="KGJPL-RULE")
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc")
	def test_unsaved_doc_on_a_sharded_counter_pins_the_rule(
		self, mock_prefix, mock_pre, mock_dnr, mock_getval
	):
		# Post-shard a cascade's stub still pins — but its own per-type rule counter, never
		# the shared tabSeries row. That is what keeps the Bin-before-counter inversion from
		# re-forming once the blanket pre-lock is gone.
		lock_order.preallocate_series_for_docs(frappe._dict(doctype="Stock Entry"))
		mock_getval.assert_called_once_with(
			"Document Naming Rule", "KGJPL-RULE", "counter", for_update=True
		)
		mock_prefix.assert_not_called()
		mock_pre.assert_called_once_with([])

	@patch.object(lock_order, "document_naming_rule_for_doc", return_value=None)
	@patch.object(lock_order, "preallocate_series")
	@patch.object(lock_order, "series_prefix_for_doc", return_value="MAT-STE-")
	def test_mixed_docs_on_a_shared_series(self, mock_prefix, mock_pre, mock_dnr):
		# Pre-shard, BOTH the already-named parent and the stub resolve to the same shared
		# row, and both must contribute it — preallocate_series dedupes and sorts.
		named = frappe._dict(doctype="Stock Entry", name="MAT-STE-23943")
		stub = frappe._dict(doctype="Stock Entry")
		lock_order.preallocate_series_for_docs(named, stub)
		self.assertEqual(mock_prefix.call_count, 2)
		mock_pre.assert_called_once_with(["MAT-STE-", "MAT-STE-"])

	def tearDown(self):
		return super().tearDown()


class TestSeriesStubs(IntegrationTestCase):
	"""series_stubs must build one Stock Entry stub per DISTINCT type (order-
	preserving dedupe), each carrying company + stock_entry_type, so
	preallocate_series_for_docs can pin every nested SE type's naming counter."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_one_stub_per_distinct_type_with_fields(self):
		stubs = lock_order.series_stubs("Test Co", "Repack", "Manufacture", "Repack")
		self.assertEqual(len(stubs), 2)  # order-preserving dedupe
		self.assertEqual([s.stock_entry_type for s in stubs], ["Repack", "Manufacture"])
		for s in stubs:
			self.assertEqual(s.doctype, "Stock Entry")
			self.assertEqual(s.company, "Test Co")
			# The new_doc default naming_series must survive so the tabSeries
			# fallback stays resolvable for a company with no naming rules.
			self.assertTrue(s.get("naming_series"))

	def test_empty_types_gives_no_stubs(self):
		self.assertEqual(lock_order.series_stubs("Test Co"), [])

	@patch.object(lock_order.frappe.db, "get_value")
	@patch.object(lock_order, "document_naming_rule_for_doc")
	@patch.object(lock_order, "preallocate_series")
	def test_stubs_pin_one_dnr_per_type(self, mock_pre, mock_dnr, mock_getval):
		# Each per-type stub resolving a distinct rule -> each rule's counter is
		# pinned exactly once, in sorted order.
		mock_dnr.side_effect = ["RULE-B", "RULE-A"]
		stubs = lock_order.series_stubs("Test Co", "Repack", "Manufacture")
		lock_order.preallocate_series_for_docs(*stubs)
		self.assertEqual(mock_dnr.call_count, 2)
		pinned = [c.args[1] for c in mock_getval.call_args_list]
		self.assertEqual(pinned, ["RULE-A", "RULE-B"])  # sorted acquisition order
		mock_pre.assert_called_once_with([])

	def tearDown(self):
		return super().tearDown()


class TestDocumentNamingRuleForDoc(IntegrationTestCase):
	"""document_naming_rule_for_doc resolves the *active* rule frappe will actually use
	(matched by conditions), returns None when none governs the doc, and never raises."""

	@classmethod
	def setUpClass(cls):
		pass

	def _rule(self, name, conditions):
		return SimpleNamespace(
			name=name, document_type="Stock Entry", conditions=conditions
		)

	def test_returns_first_matching_active_rule(self):
		cond = SimpleNamespace(field="company", condition="=", value="KG GK")
		rule = self._rule("KGJPL-RULE", [cond])
		doc = frappe._dict(doctype="Stock Entry", company="KG GK")
		with patch.object(
			lock_order.frappe.cache_manager,
			"get_doctype_map",
			return_value=[SimpleNamespace(name="KGJPL-RULE")],
		), patch.object(lock_order.frappe, "get_cached_doc", return_value=rule), patch(
			"frappe.utils.evaluate_filters", return_value=True
		):
			self.assertEqual(lock_order.document_naming_rule_for_doc(doc), "KGJPL-RULE")

	def test_skips_non_matching_conditions(self):
		cond = SimpleNamespace(field="company", condition="=", value="KG GK")
		rule = self._rule("KGJPL-RULE", [cond])
		doc = frappe._dict(doctype="Stock Entry", company="Gurukrupa")
		with patch.object(
			lock_order.frappe.cache_manager,
			"get_doctype_map",
			return_value=[SimpleNamespace(name="KGJPL-RULE")],
		), patch.object(lock_order.frappe, "get_cached_doc", return_value=rule), patch(
			"frappe.utils.evaluate_filters", return_value=False
		):
			self.assertIsNone(lock_order.document_naming_rule_for_doc(doc))

	def test_returns_none_when_no_rules(self):
		doc = frappe._dict(doctype="Stock Entry")
		with patch.object(
			lock_order.frappe.cache_manager, "get_doctype_map", return_value=[]
		):
			self.assertIsNone(lock_order.document_naming_rule_for_doc(doc))

	def test_swallows_errors_and_returns_none(self):
		# Resolution must never break a submit -- any failure degrades to "no pin".
		doc = frappe._dict(doctype="Stock Entry")
		with patch.object(
			lock_order.frappe.cache_manager,
			"get_doctype_map",
			side_effect=RuntimeError("boom"),
		):
			self.assertIsNone(lock_order.document_naming_rule_for_doc(doc))

	def tearDown(self):
		return super().tearDown()


class TestLockWaitClause(IntegrationTestCase):
	"""_lock_wait_clause: how long a work-order lifecycle row lock may wait (RULE D / RULE E)."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_none_keeps_the_server_default(self):
		self.assertEqual(lock_order._lock_wait_clause(None), "")

	def test_zero_or_negative_never_waits(self):
		for wait in (0, -1, "0"):
			with self.subTest(wait=wait):
				self.assertEqual(lock_order._lock_wait_clause(wait), " NOWAIT")

	def test_positive_waits_that_many_seconds(self):
		for wait, clause in ((15, " WAIT 15"), ("7", " WAIT 7"), (50, " WAIT 50")):
			with self.subTest(wait=wait):
				self.assertEqual(lock_order._lock_wait_clause(wait), clause)

	def tearDown(self):
		return super().tearDown()


class TestUpdateByPrimaryKey(IntegrationTestCase):
	"""update_by_primary_key: set_value with a filter dict, but locking only the rows it changes
	(a filtered UPDATE no index serves locks the whole table under REPEATABLE READ)."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_reads_the_names_then_updates_them_by_primary_key(self):
		filters = {"docstatus": 2, "manufacturing_operation": "MOP-9"}
		with (
			patch.object(
				lock_order.frappe.db, "get_values", return_value=["R-2", "R-1", "R-2"]
			) as read,
			patch.object(lock_order.frappe.db, "set_value") as write,
		):
			names = lock_order.update_by_primary_key(
				"Department IR Operation", filters, "manufacturing_operation", None
			)
		self.assertEqual(names, ["R-1", "R-2"])
		read.assert_called_once_with(
			"Department IR Operation", filters, "name", pluck=True, order_by="name asc"
		)
		write.assert_called_once_with(
			"Department IR Operation",
			{
				"docstatus": 2,
				"manufacturing_operation": "MOP-9",
				"name": ("in", ["R-1", "R-2"]),
			},
			"manufacturing_operation",
			None,
			update_modified=True,
		)
		self.assertEqual(filters, {"docstatus": 2, "manufacturing_operation": "MOP-9"})

	def test_nothing_matching_writes_nothing(self):
		with (
			patch.object(lock_order.frappe.db, "get_values", return_value=[]),
			patch.object(lock_order.frappe.db, "set_value") as write,
		):
			self.assertEqual(
				lock_order.update_by_primary_key("Thing", {"a": 1}, "b", 2), []
			)
		write.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestLockRowsByName(IntegrationTestCase):
	"""_lock_rows_by_name and its two callers (lock_manufacturing_operations, lock_work_orders):
	one primary-key FOR UPDATE per distinct row, in sorted order, returning the locking reads."""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _row_for(query, values, as_dict=False):
		return [frappe._dict(name=values[0], status="WIP")]

	@patch.object(lock_order.frappe.db, "sql")
	def test_one_primary_key_lock_per_distinct_name_in_sorted_order(self, mock_sql):
		mock_sql.side_effect = self._row_for
		locked = lock_order._lock_rows_by_name(
			"tabThing", ("name", "status"), ["B", "A", "B", "", None], 15
		)
		self.assertEqual([c.args[1] for c in mock_sql.call_args_list], [("A",), ("B",)])
		for c in mock_sql.call_args_list:
			self.assertEqual(
				c.args[0],
				"SELECT `name`, `status` FROM `tabThing` WHERE name = %s FOR UPDATE WAIT 15",
			)
			self.assertTrue(c.kwargs.get("as_dict"))
		self.assertEqual(list(locked), ["A", "B"])

	@patch.object(lock_order.frappe.db, "sql")
	def test_wait_suffix_follows_the_requested_wait(self, mock_sql):
		mock_sql.return_value = []
		for wait, ending in (
			(None, "FOR UPDATE"),
			(0, "FOR UPDATE NOWAIT"),
			(3, "FOR UPDATE WAIT 3"),
		):
			with self.subTest(wait=wait):
				mock_sql.reset_mock()
				lock_order._lock_rows_by_name("tabThing", ("name",), ["A"], wait)
				self.assertTrue(
					mock_sql.call_args.args[0].endswith(ending), mock_sql.call_args
				)

	@patch.object(lock_order.frappe.db, "sql")
	def test_a_callable_wait_is_asked_once_per_statement(self, mock_sql):
		"""How the guard spreads ONE lock budget over the rows of a block: each statement waits
		what the callable says is left of it."""
		mock_sql.return_value = []
		left = iter([15, 9, 0])
		lock_order._lock_rows_by_name(
			"tabThing", ("name",), ["C", "A", "B"], lambda: next(left)
		)
		self.assertEqual(
			[
				(c.args[1], c.args[0].split("FOR UPDATE")[1])
				for c in mock_sql.call_args_list
			],
			[(("A",), " WAIT 15"), (("B",), " WAIT 9"), (("C",), " NOWAIT")],
		)

	@patch.object(lock_order.frappe.db, "sql")
	def test_missing_rows_are_left_out(self, mock_sql):
		mock_sql.side_effect = [[frappe._dict(name="A")], []]
		locked = lock_order._lock_rows_by_name("tabThing", ("name",), ["A", "B"], None)
		self.assertEqual(list(locked), ["A"])

	@patch.object(lock_order.frappe.db, "sql")
	def test_no_names_issue_no_statement(self, mock_sql):
		self.assertEqual(
			lock_order._lock_rows_by_name("tabThing", ("name",), [None, ""], 15), {}
		)
		mock_sql.assert_not_called()

	@patch.object(lock_order.frappe.db, "sql")
	def test_returned_rows_are_the_locking_reads_themselves(self, mock_sql):
		"""Callers decide from these rows: a plain re-read could show an older snapshot."""
		row = frappe._dict(name="A", status="Finished")
		mock_sql.return_value = [row]
		locked = lock_order._lock_rows_by_name(
			"tabThing", ("name", "status"), ["A"], None
		)
		self.assertIs(locked["A"], row)

	@patch.object(lock_order.frappe.db, "sql")
	def test_a_failed_lock_names_the_row_it_waited_for(self, mock_sql):
		"""The guard's WorkOrderBusyError then names the busy row, not every row it wanted."""
		error = frappe.QueryTimeoutError("1205")
		mock_sql.side_effect = [[frappe._dict(name="A")], error]
		with self.assertRaises(frappe.QueryTimeoutError) as cm:
			lock_order._lock_rows_by_name("tabThing", ("name",), ["C", "B", "A"], 15)
		self.assertIs(cm.exception, error)
		self.assertEqual(cm.exception.lock_row_name, "B")
		self.assertEqual(mock_sql.call_count, 2)  # C was never asked for

	@patch.object(lock_order, "_lock_rows_by_name", return_value={})
	def test_manufacturing_operations_lock_their_table_with_the_guard_fields(
		self, mock_rows
	):
		lock_order.lock_manufacturing_operations(["MOP-1"], wait=0)
		mock_rows.assert_called_once_with(
			"tabManufacturing Operation",
			lock_order.MANUFACTURING_OPERATION_LOCK_FIELDS,
			["MOP-1"],
			0,
		)

	@patch.object(lock_order, "_lock_rows_by_name", return_value={})
	def test_work_orders_lock_their_table_with_the_guard_fields(self, mock_rows):
		lock_order.lock_work_orders({"MWO-1"})
		mock_rows.assert_called_once_with(
			"tabManufacturing Work Order",
			lock_order.WORK_ORDER_LOCK_FIELDS,
			{"MWO-1"},
			None,
		)

	def test_lock_fields_cover_every_column_the_guard_decides_on(self):
		"""A column missing here reads as None from the locked row and silently passes a rule."""
		self.assertLessEqual(
			{
				"name",
				"manufacturing_work_order",
				"company",
				"department",
				"status",
				"department_ir_status",
				"operation",
				"employee",
				"subcontractor",
				"department_issue_id",
				"department_receive_id",
				# compared with the snapshot after the wait (the guard's snapshot check)
				"modified",
			},
			set(lock_order.MANUFACTURING_OPERATION_LOCK_FIELDS),
		)
		self.assertLessEqual(
			{"name", "docstatus", "manufacturing_operation"},
			set(lock_order.WORK_ORDER_LOCK_FIELDS),
		)

	def tearDown(self):
		return super().tearDown()
