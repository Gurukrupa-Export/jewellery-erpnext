# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Refining Stock Entries on their own naming counter
(``jewellery_erpnext.refining.stock_entry_naming``).

Pure-mock apart from frappe metadata and a few read-only lookups of names that do not
exist: nothing here writes to the database, so the module also runs on a read-only copy.

* gate off: every refining Stock Entry keeps the site's default series;
* gate on: every Stock Entry ``RefiningEntry`` creates is named from the configured series
  before insert -- checked in the source for all six creators, and by running five of them;
* a transaction that creates two (transfer, then dust receipt) uses ONE series;
* ``lock_order`` pre-locks the new counter row and not ``MAT-STE-``;
* ``ensure_refining_naming_series`` appends the option without reordering, is idempotent
  and never lowers the counter.

The two-connection lock test is in ``test_refining_stock_entry_naming_lock``.
"""

import ast
import inspect
import os
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.naming import InvalidNamingSeriesError
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.refining import stock_entry_naming as naming_mod
from jewellery_erpnext.refining.doctype.refining_entry import (
	refining_entry as refining_entry_mod,
)
from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	RefiningEntry,
)

SERIES = "RFN-STE-"
COMPANY = "KG GK Jewellers Private Limited"
# Item codes that cannot exist, so has_batch_no reads come back empty (no batch path).
NO_ITEM = "_T-RFN-NAMING-NO-ITEM"
NO_ITEM_2 = "_T-RFN-NAMING-NO-ITEM-2"

#: Every RefiningEntry method that creates a Stock Entry. A new creator must be added here
#: (and must call apply_refining_naming_series) or test_every_creator_is_known fails.
CREATORS = {
	"receive_from_supplier",
	"transfer_recovered_materials",
	"create_material_transfer_se",
	"create_dust_opening_receipt_se",
	"create_repack_se",
	"create_scrap_transfer_se",
	# kggk_uat only: the draft hand-back to Central Transit and the batch consolidation.
	"_create_central_transit_draft",
	"consolidate_refinery_batches",
}


@contextmanager
def gate(series):
	"""Set (or, with ``None``, remove) the site_config key for the duration."""
	conf = frappe.local.conf
	had, old = naming_mod.CONFIG_KEY in conf, conf.get(naming_mod.CONFIG_KEY)
	if series is None:
		conf.pop(naming_mod.CONFIG_KEY, None)
	else:
		conf[naming_mod.CONFIG_KEY] = series
	try:
		yield
	finally:
		if had:
			conf[naming_mod.CONFIG_KEY] = old
		else:
			conf.pop(naming_mod.CONFIG_KEY, None)


def default_series():
	"""What a new Stock Entry gets today (``MAT-STE-`` on KG GK)."""
	return frappe.new_doc("Stock Entry").naming_series


class _Stop(Exception):
	"""Raised by the stub insert to end a creator once its Stock Entry is being named."""


class _Captured(list):
	"""The inserted Stock Entries, plus the naming stubs the refining code pre-locked."""

	def __init__(self):
		super().__init__()
		self.prelocked = []


@contextmanager
def capture_stock_entries(stop_at_insert=True):
	"""Hand the refining code real, unsaved Stock Entries whose insert/submit are stubs.

	Yields a list of ``{"se", "stock_entry_type", "naming_series"}`` dicts, one per
	insert, with ``naming_series`` read AT insert -- the moment frappe names the doc.
	The up-front counter pre-lock is recorded in ``.prelocked`` instead of taking rows.
	"""
	real_new_doc = frappe.new_doc
	inserted = _Captured()

	def record_prelock(*docs):
		inserted.prelocked.extend(docs)

	def fake_new_doc(doctype, *args, **kwargs):
		doc = real_new_doc(doctype, *args, **kwargs)
		if doctype != "Stock Entry":
			return doc

		def insert(*a, **k):
			inserted.append(
				{
					"se": doc,
					"stock_entry_type": doc.stock_entry_type,
					"naming_series": doc.naming_series,
				}
			)
			if stop_at_insert:
				raise _Stop
			doc.name = f"_T-SE-{len(inserted)}"
			return doc

		doc.insert = insert
		doc.submit = MagicMock()
		return doc

	with ExitStack() as stack:
		stack.enter_context(patch.object(frappe, "new_doc", side_effect=fake_new_doc))
		stack.enter_context(
			patch.object(
				lock_order, "preallocate_series_for_docs", side_effect=record_prelock
			)
		)
		stack.enter_context(patch.object(frappe, "publish_progress"))
		stack.enter_context(patch.object(frappe, "publish_realtime"))
		stack.enter_context(patch.object(RefiningEntry, "db_set"))
		stack.enter_context(patch.object(RefiningEntry, "_stamp_batch_ownership"))
		yield inserted


def new_refining_entry(**values):
	re = frappe.new_doc("Refining Entry")
	re.company = COMPANY
	re.name = "_T-RFN-NAMING"
	re.update(values)
	return re


class TestGate(UnitTestCase):
	def test_off_when_key_absent(self):
		with gate(None):
			self.assertIsNone(naming_mod.refining_se_naming_series())

	def test_off_for_blank_or_non_text_values(self):
		for value in ("", "   ", 1, True, ["RFN-STE-"]):
			with self.subTest(value=value), gate(value):
				self.assertIsNone(naming_mod.refining_se_naming_series())

	def test_on_returns_stripped_series(self):
		with gate("  RFN-STE- "):
			self.assertEqual(naming_mod.refining_se_naming_series(), SERIES)

	def test_proposed_series_is_not_a_default(self):
		# The proposal is documentation until GK signs off: no config, no change.
		with gate(None):
			se = frappe.new_doc("Stock Entry")
			before = se.naming_series
			self.assertIsNone(naming_mod.apply_refining_naming_series(se))
			self.assertEqual(se.naming_series, before)
			self.assertNotEqual(se.naming_series, naming_mod.PROPOSED_SERIES)

	def test_apply_sets_series_when_on(self):
		with gate(SERIES):
			se = frappe.new_doc("Stock Entry")
			self.assertEqual(naming_mod.apply_refining_naming_series(se), SERIES)
			self.assertEqual(se.naming_series, SERIES)

	def test_proposed_names_fit_sixteen_characters(self):
		# GST e-invoice / e-waybill document numbers are limited to 16 characters.
		self.assertLessEqual(len(naming_mod.PROPOSED_SERIES + "99999"), 16)
		self.assertLessEqual(len(naming_mod.PROPOSED_SERIES + "99999-1"), 16)


def _refining_sources():
	"""``(path, source)`` for every non-test module under jewellery_erpnext/refining."""
	root = os.path.dirname(inspect.getfile(naming_mod))
	for dirpath, _dirs, files in os.walk(root):
		for fname in files:
			if fname.endswith(".py") and not fname.startswith("test_"):
				path = os.path.join(dirpath, fname)
				with open(path) as f:
					yield path, f.read()


def _is_new_stock_entry(node):
	"""``frappe.new_doc("Stock Entry")``."""
	return (
		isinstance(node, ast.Call)
		and isinstance(node.func, ast.Attribute)
		and node.func.attr == "new_doc"
		and node.args
		and isinstance(node.args[0], ast.Constant)
		and node.args[0].value == "Stock Entry"
	)


def _is_stock_entry_dict(node):
	"""``{"doctype": "Stock Entry", ...}`` as passed to get_doc."""
	return isinstance(node, ast.Dict) and any(
		isinstance(k, ast.Constant)
		and k.value == "doctype"
		and isinstance(v, ast.Constant)
		and v.value == "Stock Entry"
		for k, v in zip(node.keys, node.values)
	)


class TestEveryCreatorAppliesSeries(UnitTestCase):
	"""Source-level guard: covers every creator, including create_repack_se, which is
	too large to run under mocks."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		path = inspect.getfile(refining_entry_mod)
		with open(path) as f:
			cls.tree = ast.parse(f.read())

	def _creations(self):
		"""``(function, body, index, target)`` for each ``x = frappe.new_doc("Stock Entry")``."""
		found = []
		for func in ast.walk(self.tree):
			if not isinstance(func, ast.FunctionDef):
				continue
			for node in ast.walk(func):
				for field in ("body", "orelse", "finalbody"):
					body = getattr(node, field, None)
					if not isinstance(body, list):
						continue
					for i, stmt in enumerate(body):
						if isinstance(stmt, ast.Assign) and _is_new_stock_entry(
							stmt.value
						):
							found.append((func.name, body, i, stmt.targets[0].id))
		return found

	def test_every_creator_is_known(self):
		self.assertEqual({c[0] for c in self._creations()}, CREATORS)

	def test_series_applied_right_after_creation(self):
		for func, body, i, target in self._creations():
			with self.subTest(creator=func):
				nxt = body[i + 1]
				self.assertIsInstance(nxt, ast.Expr)
				self.assertIsInstance(nxt.value, ast.Call)
				self.assertEqual(
					getattr(nxt.value.func, "id", None), "apply_refining_naming_series"
				)
				self.assertEqual([a.id for a in nxt.value.args], [target])

	def test_nothing_overwrites_naming_series_later(self):
		for func in ast.walk(self.tree):
			if isinstance(func, ast.FunctionDef) and func.name in CREATORS:
				for node in ast.walk(func):
					if isinstance(node, ast.Assign):
						for t in node.targets:
							with self.subTest(creator=func.name):
								self.assertFalse(
									isinstance(t, ast.Attribute)
									and t.attr == "naming_series"
								)

	def test_no_other_stock_entry_creation_in_refining(self):
		# Only refining_entry.py creates Stock Entries, and only via frappe.new_doc.
		creators = []
		for path, source in _refining_sources():
			for node in ast.walk(ast.parse(source)):
				if _is_new_stock_entry(node) or _is_stock_entry_dict(node):
					creators.append(os.path.basename(path))
		self.assertEqual(set(creators), {"refining_entry.py"})
		self.assertEqual(len(creators), len(CREATORS))


class _CreatorHarness(UnitTestCase):
	"""Runs creators with stubbed insert/submit and the stock-reading helpers mocked."""

	def run_dust_receipt(self, series):
		re = new_refining_entry(refining_type="Scrap Refining")
		re._dust_shortfalls = [
			{"item_code": NO_ITEM, "qty": 2.0, "uom": "Gram", "purity": 91.6}
		]
		with (
			gate(series),
			capture_stock_entries() as inserted,
			patch.object(RefiningEntry, "get_dust_opening_batch", return_value="B-1"),
			patch.object(RefiningEntry, "set_dust_receipt_difference_account"),
		):
			with self.assertRaises(_Stop):
				re.create_dust_opening_receipt_se(target_warehouse="_T-WH-SUP")
		return inserted

	def run_scrap_transfer(self, series):
		re = new_refining_entry(
			refining_type="Scrap Refining",
			scrap_warehouse="_T-WH-SCRAP",
			refining_warehouse="_T-WH-REF",
			refining_loss=1.5,
		)
		with (
			gate(series),
			capture_stock_entries() as inserted,
			patch.object(RefiningEntry, "get_dust_item", return_value=NO_ITEM),
		):
			with self.assertRaises(_Stop):
				re.create_scrap_transfer_se()
		return inserted

	def run_transfer_recovered(self, series):
		re = new_refining_entry(status="Completed", refining_warehouse="_T-WH-REF")
		re.append("refined_gold", {"refining_gold_weight": 5})
		with (
			gate(series),
			capture_stock_entries() as inserted,
			patch.object(
				RefiningEntry, "_get_central_rm_warehouse", return_value="_T-WH-CRM"
			),
			patch.object(
				RefiningEntry, "_get_pure_gold_24kt_item", return_value=NO_ITEM
			),
		):
			with self.assertRaises(_Stop):
				re.transfer_recovered_materials()
		return inserted

	def run_receive_from_supplier(self, series):
		re = new_refining_entry(
			refining_type="Scrap Refining",
			is_external=1,
			docstatus=1,
			supplier_warehouse="_T-WH-SUP",
			refined_metal_item=NO_ITEM_2,
		)
		re.append("material_items", {"item_code": NO_ITEM, "qty": 2.0, "uom": "Gram"})
		with (
			gate(series),
			capture_stock_entries() as inserted,
			patch.object(
				RefiningEntry, "_get_source_dept_rm_warehouse", return_value="_T-WH-RM"
			),
			patch.object(
				RefiningEntry, "_recovered_output_customer", return_value=None
			),
			patch.object(RefiningEntry, "_receive_target_batch", return_value=None),
			patch.object(RefiningEntry, "_is_returned_intact", return_value=False),
			patch.object(RefiningEntry, "_assert_no_loss_output"),
			patch.object(RefiningEntry, "set_dust_receipt_difference_account"),
		):
			with self.assertRaises(_Stop):
				re.receive_from_supplier(recovery_weight=1.0)
		return inserted

	def run_external_scrap_submit(self, series):
		"""on_submit_external for a Scrap consignment whose source holds 3 of 5 g: a
		Material Transfer of 3 g, then a Material Receipt of the missing 2 g, in one
		transaction (the 16 Nov holder shape)."""
		re = new_refining_entry(
			refining_type="Scrap Refining",
			is_external=1,
			supplier="_T-SUPPLIER",
			supplier_warehouse="_T-WH-SUP",
			warehouse="_T-WH-SRC",
		)
		re.append(
			"material_items",
			{"item_code": NO_ITEM, "qty": 5.0, "uom": "Gram", "purity": 91.6},
		)
		real_get_value = frappe.local.db.get_value

		def get_value(doctype, *args, **kwargs):
			if doctype == "Bin":
				return 3.0
			return real_get_value(doctype, *args, **kwargs)

		with (
			gate(series),
			capture_stock_entries(stop_at_insert=False) as inserted,
			patch.object(frappe.local.db, "get_value", side_effect=get_value),
			patch.object(RefiningEntry, "get_dust_opening_batch", return_value="B-1"),
			patch.object(RefiningEntry, "set_dust_receipt_difference_account"),
			patch.object(RefiningEntry, "create_external_refining_po") as po,
		):
			re.on_submit_external()
		po.assert_called_once()
		return inserted


class TestCreatorsGateOff(_CreatorHarness):
	"""Gate off: the Stock Entry keeps the default series, i.e. today's behaviour."""

	def _assert_default(self, inserted, count=1):
		self.assertEqual(len(inserted), count)
		for row in inserted:
			self.assertEqual(row["naming_series"], default_series())
			self.assertNotEqual(row["naming_series"], SERIES)

	def test_dust_receipt(self):
		self._assert_default(self.run_dust_receipt(None))

	def test_scrap_transfer(self):
		self._assert_default(self.run_scrap_transfer(None))

	def test_transfer_recovered_materials(self):
		self._assert_default(self.run_transfer_recovered(None))

	def test_receive_from_supplier(self):
		self._assert_default(self.run_receive_from_supplier(None))

	def test_external_scrap_submit(self):
		inserted = self.run_external_scrap_submit(None)
		self._assert_default(inserted, count=2)
		# The up-front pre-lock pins the same default row the entries are named from.
		self.assertEqual(
			[s.stock_entry_type for s in inserted.prelocked],
			["Material Transfer", "Material Receipt"],
		)
		self.assertEqual(
			{s.naming_series for s in inserted.prelocked}, {default_series()}
		)


class TestCreatorsGateOn(_CreatorHarness):
	def _assert_series(self, inserted, stock_entry_type):
		self.assertEqual(len(inserted), 1)
		self.assertEqual(inserted[0]["stock_entry_type"], stock_entry_type)
		self.assertEqual(inserted[0]["naming_series"], SERIES)

	def test_dust_receipt(self):
		self._assert_series(self.run_dust_receipt(SERIES), "Material Receipt")

	def test_scrap_transfer(self):
		self._assert_series(self.run_scrap_transfer(SERIES), "Material Transfer")

	def test_transfer_recovered_materials(self):
		self._assert_series(self.run_transfer_recovered(SERIES), "Material Transfer")

	def test_receive_from_supplier(self):
		self._assert_series(self.run_receive_from_supplier(SERIES), "Manufacture")

	def test_transfer_then_receipt_share_one_counter(self):
		inserted = self.run_external_scrap_submit(SERIES)
		self.assertEqual(
			[r["stock_entry_type"] for r in inserted],
			["Material Transfer", "Material Receipt"],
		)
		self.assertEqual({r["naming_series"] for r in inserted}, {SERIES})
		# The up-front pre-lock takes the refining row before the first Bin, and the
		# entries then re-enter it: never MAT-STE-.
		self.assertEqual(
			[s.stock_entry_type for s in inserted.prelocked],
			["Material Transfer", "Material Receipt"],
		)
		# And lock_order resolves both to the same tabSeries row, so the receipt -- named
		# after the transfer's Bin locks -- re-enters a row the transaction already holds.
		with patch.object(
			lock_order, "document_naming_rule_for_doc", return_value=None
		):
			self.assertEqual(
				{lock_order.series_prefix_for_doc(r["se"]) for r in inserted}, {SERIES}
			)
			self.assertEqual(
				{lock_order.series_prefix_for_doc(s) for s in inserted.prelocked},
				{SERIES},
			)


class TestLockOrderFollowsSeries(UnitTestCase):
	"""The pre-lock key comes from naming_series, so it moves with the series."""

	def _se(self, series, stock_entry_type="Material Transfer"):
		se = frappe.new_doc("Stock Entry")
		se.company = COMPANY
		se.stock_entry_type = stock_entry_type
		with gate(series):
			naming_mod.apply_refining_naming_series(se)
		return se

	def test_prefix_is_the_refining_row(self):
		self.assertEqual(lock_order.series_prefix_for_doc(self._se(SERIES)), SERIES)

	def test_prefix_is_unchanged_with_gate_off(self):
		se = self._se(None)
		self.assertEqual(
			lock_order.series_prefix_for_doc(se),
			lock_order.series_prefix_for_doc(frappe.new_doc("Stock Entry")),
		)

	def test_dated_series_resolves_like_getseries(self):
		from frappe.utils import now_datetime

		se = self._se("RFN-STE-.YYYY.-")
		self.assertEqual(
			lock_order.series_prefix_for_doc(se),
			f"RFN-STE-{now_datetime().strftime('%Y')}-",
		)

	def _locked_series(self, *docs, dnr=None):
		with (
			patch.object(lock_order, "document_naming_rule_for_doc", return_value=dnr),
			patch.object(frappe.local.db, "sql") as sql,
			patch.object(frappe.local.db, "get_value") as get_value,
		):
			lock_order.preallocate_series_for_docs(*docs)
		series = [c.args[1][0] for c in sql.call_args_list if "tabSeries" in c.args[0]]
		return series, get_value

	def test_prelock_pins_refining_row_not_mat_ste(self):
		series, _ = self._locked_series(self._se(SERIES))
		self.assertEqual(series, [SERIES])

	def test_cascade_prelocks_one_row(self):
		series, _ = self._locked_series(
			self._se(SERIES), self._se(SERIES, "Material Receipt")
		)
		self.assertEqual(series, [SERIES])

	def test_a_naming_rule_still_wins(self):
		# frappe applies a matching Document Naming Rule before the naming series; the
		# pre-lock follows the rule and leaves tabSeries alone.
		se = self._se(SERIES)
		series, get_value = self._locked_series(se, dnr="_T-RULE")
		self.assertEqual(series, [])
		get_value.assert_called_once_with(
			"Document Naming Rule", "_T-RULE", "counter", for_update=True
		)


class _FakePropertySetters:
	"""Just enough of frappe.db / get_doc / make_property_setter for _ensure_option."""

	def __init__(self, value):
		self.value = value
		self.saves = 0
		self.created = []
		# Anything that is not a Property Setter goes to the real frappe: with a cold
		# cache frappe itself loads System Settings (now_datetime -> time zone) or meta.
		self.real_get_value = frappe.local.db.get_value
		self.real_get_doc = frappe.get_doc
		self.real_sql = frappe.local.db.sql

	def get_value(self, doctype, filters=None, fieldname="name", *args, **kwargs):
		if doctype != "Property Setter":
			return self.real_get_value(doctype, filters, fieldname, *args, **kwargs)
		if self.value is None:
			return None
		return (
			self.value if fieldname == "value" else "Stock Entry-naming_series-options"
		)

	def get_doc(self, doctype, *args, **kwargs):
		if doctype != "Property Setter":
			return self.real_get_doc(doctype, *args, **kwargs)
		fake = MagicMock()
		fake.flags = frappe._dict()
		fake.value = self.value

		def save(**kwargs):
			self.value = fake.value
			self.saves += 1

		fake.save.side_effect = save
		return fake

	def sql_reads_only(self, query, *args, **kwargs):
		"""Plain reads run for real (read-only session); writes are recorded, never run."""
		if _sql_verb(query) in _READ_VERBS:
			return self.real_sql(query, *args, **kwargs)
		return None

	def make_property_setter(self, args, **kwargs):
		self.created.append((args, kwargs))
		self.value = args["value"]


_READ_VERBS = {"SELECT", "SHOW", "EXPLAIN", "WITH"}
_WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "CREATE", "DROP"}


def _sql_verb(query):
	text = str(query).lstrip()
	return text.split(None, 1)[0].upper() if text else ""


@contextmanager
def fake_store(value):
	store = _FakePropertySetters(value)
	with (
		patch.object(frappe.local.db, "get_value", side_effect=store.get_value),
		patch.object(frappe.local.db, "sql", side_effect=store.sql_reads_only) as sql,
		patch.object(frappe, "get_doc", side_effect=store.get_doc),
		patch.object(
			frappe, "make_property_setter", side_effect=store.make_property_setter
		),
		patch.object(frappe, "clear_cache"),
	):
		store.sql = sql
		yield store


def assert_no_sql_writes(sql_mock):
	"""No INSERT/UPDATE/DELETE/REPLACE/DDL went through frappe.db.sql.

	Plain reads are allowed: with a cold cache (no Redis) frappe itself may read
	defaults or meta through frappe.db.sql, which says nothing about this code.
	"""
	for call in sql_mock.call_args_list:
		query = call.args[0] if call.args else ""
		assert _sql_verb(query) not in _WRITE_VERBS, query


class TestEnsureRefiningNamingSeries(UnitTestCase):
	CURRENT = "MAT-STE-\nMAT-STE-.YYYY.-"

	def test_noop_while_gate_off(self):
		with gate(None), fake_store(self.CURRENT) as store:
			self.assertIsNone(naming_mod.ensure_refining_naming_series())
			self.assertEqual(store.saves, 0)
			assert_no_sql_writes(store.sql)
		self.assertEqual(store.value, self.CURRENT)

	def test_appends_after_existing_options(self):
		with gate(SERIES), fake_store(self.CURRENT) as store:
			self.assertEqual(naming_mod.ensure_refining_naming_series(), SERIES)
		self.assertEqual(store.value, self.CURRENT + "\n" + SERIES)
		self.assertEqual(store.value.split("\n")[0], "MAT-STE-")
		self.assertEqual(store.saves, 1)

	def test_idempotent(self):
		with gate(SERIES), fake_store(self.CURRENT) as store:
			naming_mod.ensure_refining_naming_series()
			after_first = store.value
			naming_mod.ensure_refining_naming_series()
		self.assertEqual(store.value, after_first)
		self.assertEqual(store.value.count(SERIES), 1)
		self.assertEqual(store.saves, 1)

	def test_explicit_series_works_before_the_gate(self):
		# Lets GK seed the option and counter BEFORE switching the gate on.
		with gate(None), fake_store(self.CURRENT) as store:
			self.assertEqual(naming_mod.ensure_refining_naming_series(SERIES), SERIES)
		self.assertTrue(store.value.endswith("\n" + SERIES))

	def test_creates_setter_from_docfield_when_absent(self):
		meta = MagicMock()
		meta.get_field.return_value = frappe._dict(options="MAT-STE-.YYYY.-")
		with (
			gate(SERIES),
			fake_store(None) as store,
			patch.object(frappe, "get_meta", return_value=meta) as get_meta,
		):
			naming_mod.ensure_refining_naming_series()
		get_meta.assert_called_once_with("Stock Entry", cached=False)
		self.assertEqual(store.value, "MAT-STE-.YYYY.-\n" + SERIES)
		(args, kwargs), *_ = store.created
		options = args["value"].split("\n")
		self.assertEqual(options[-1], SERIES)
		self.assertNotEqual(options[0], SERIES)
		self.assertFalse(kwargs["validate_fields_for_doctype"])

	def test_refuses_when_series_would_become_default(self):
		with gate(SERIES), fake_store("") as store:
			self.assertRaises(
				frappe.ValidationError, naming_mod.ensure_refining_naming_series
			)
			assert_no_sql_writes(store.sql)
		self.assertEqual(store.saves, 0)

	def test_rejects_invalid_series_before_writing(self):
		with gate("RFN STE!"), fake_store(self.CURRENT) as store:
			self.assertRaises(
				InvalidNamingSeriesError, naming_mod.ensure_refining_naming_series
			)
			assert_no_sql_writes(store.sql)
		self.assertEqual(store.saves, 0)

	def test_counter_row_is_monotonic(self):
		with gate(SERIES), fake_store(self.CURRENT) as store:
			naming_mod.ensure_refining_naming_series()
		(query, params), _ = store.sql.call_args
		self.assertIn("INSERT INTO `tabSeries`", query)
		self.assertIn(naming_mod._MAX_ISSUED_SQL, query)
		self.assertIn("GREATEST(`tabSeries`.`current`, VALUES(`current`))", query)
		self.assertEqual(
			params, {"prefix": SERIES, "offset": len(SERIES) + 1, "like": SERIES + "%"}
		)

	def test_like_pattern_escapes_wildcards(self):
		self.assertEqual(naming_mod._counter_params("A_B%-")["like"], "A\\_B\\%-%")

	def test_max_issued_select_runs(self):
		# Read-only: the SELECT the seed inserts from parses and returns a number. For a
		# prefix nothing uses it is 0, so the first refining Stock Entry is RFN-STE-00001.
		value = frappe.db.sql(
			naming_mod._MAX_ISSUED_SQL, naming_mod._counter_params("_T-RFN-NAMING-")
		)[0][0]
		self.assertEqual(int(value), 0)

	def test_after_migrate_is_non_fatal(self):
		with (
			patch.object(
				naming_mod,
				"ensure_refining_naming_series",
				side_effect=RuntimeError("boom"),
			),
			patch.object(frappe.local.db, "savepoint") as savepoint,
			patch.object(frappe.local.db, "rollback") as rollback,
			patch.object(frappe, "log_error") as log_error,
			patch("builtins.print") as printed,
		):
			naming_mod.after_migrate()
		savepoint.assert_called_once()
		rollback.assert_called_once_with(save_point=savepoint.call_args.args[0])
		log_error.assert_called_once()
		self.assertIn("ensure_refining_naming_series", printed.call_args.args[0])

	def test_after_migrate_is_wired(self):
		hooks = frappe.get_hooks("after_migrate", app_name="jewellery_erpnext")
		self.assertIn(
			"jewellery_erpnext.refining.stock_entry_naming.after_migrate", hooks
		)
