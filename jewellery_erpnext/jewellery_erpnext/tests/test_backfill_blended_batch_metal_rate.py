# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the Batch Rate backfill: its scope, its recovery order, and its cascade.

The patch repairs batches the Repack-Metal Conversion blend zeroed. It runs against a live
production database, so these tests care less about the happy path than about the edges that
decide blast radius:

* WHICH batches it selects -- the voucher-type filter, the submitted-only filter, and the
  creation cutoff at the date the blend was introduced. Without that cutoff the gk candidate
  set is 718 batches, 591 of them from 2024, none of which the blend ever touched.
* WHICH recovery source wins when more than one is available.
* THAT the cascade to dependent batches actually runs.

The fake below is deliberately query-text aware. An earlier version answered every
``sql_list`` the same way, which meant the entire scope filter could be deleted with the
suite still green. Where a WHERE clause is load-bearing, a test asserts it is present.

DB-free per the suite convention: ``setUpClass`` is neutralized and ``frappe.db`` is mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import backfill_blended_batch_metal_rate as backfill

ALLOY_ITEM = "M-Genia-221"


class _Site:
	"""A tiny in-memory site: batches, their origin rows, and the vouchers behind them.

	``roots`` names the batches the real _conversion_roots query would select, so selection is
	driven by that query rather than by the fake answering every call identically.
	"""

	def __init__(
		self,
		batches=None,
		origins=None,
		roots=None,
		bundles=None,
		basic_rates=None,
		alloy_items=(ALLOY_ITEM,),
		has_column=True,
	):
		self.batches = batches or {}
		self.origins = origins or {}
		self.roots = list(roots or [])
		self.bundles = bundles or {}
		self.basic_rates = basic_rates or {}
		self.alloy_items = set(alloy_items)
		self._has_column = has_column
		self.writes = []
		self.mirror_updates = []
		self.queries = []

	def has_column(self, doctype, column):
		return self._has_column

	def table_exists(self, table):
		return True

	def count(self, doctype, filters=None):
		return 1 if (filters or {}).get("parent") in self.alloy_items else 4

	def get_value(self, doctype, name=None, fieldname=None, as_dict=False, **kw):
		if doctype == "Batch":
			row = self.batches.get(name)
			if not row:
				return None
			return frappe._dict(row, name=name) if as_dict else row.get(fieldname)
		if doctype == "Item":
			group = "Alloy" if name in self.alloy_items else "Metal - V"
			return frappe._dict(item_group=group)
		if doctype == "Stock Entry Detail":
			return self.basic_rates.get(name, 0.0)
		return None

	def set_value(self, doctype, name, fieldname, value, update_modified=True):
		self.writes.append((name, fieldname, value))
		self.batches.setdefault(name, {})[fieldname] = value

	def sql_list(self, query, values=None):
		self.queries.append(query)
		if "Repack-Metal Conversion" in query:
			return [
				n
				for n in self.roots
				if not self.batches.get(n, {}).get("custom_metal_rate")
			]

		healed = set((values or {}).get("healed", ()))
		return [
			name
			for name, rows in self.origins.items()
			if not self.batches.get(name, {}).get("custom_metal_rate")
			and any(r.get("batch_no") in healed for r in rows)
		]

	def sql(self, query, values=None, as_dict=False):
		self.queries.append(query)
		if query.strip().upper().startswith("UPDATE"):
			self.mirror_updates.append(values)
			return []
		voucher = (values or (None,))[0]
		return [frappe._dict(r) for r in self.bundles.get(voucher, [])]

	def commit(self):
		pass


def _run(site):
	def _get_all(doctype, filters=None, fields=None, **kw):
		if doctype == "Batch MultiSelect":
			return [frappe._dict(r) for r in site.origins.get(filters["parent"], [])]
		if doctype == "Batch":
			wanted = set(filters.get("name", ("in", []))[1])
			return [
				frappe._dict(site.batches[n], name=n)
				for n in wanted
				if n in site.batches
			]
		return []

	with patch.object(frappe, "db", site), patch.object(frappe, "get_all", _get_all):
		backfill.execute()
	return site


def _rates_written(site):
	return [v for _, f, v in site.writes if f == "custom_metal_rate"]


def _healed(site):
	return {name for name, f, _ in site.writes if f == "custom_metal_rate"}


class _BackfillTestCase(IntegrationTestCase):
	"""House-pattern base: no DB fixtures; setUpClass is a deliberate no-op."""

	@classmethod
	def setUpClass(cls):
		pass


class TestBackfillScope(_BackfillTestCase):
	"""What the patch is allowed to touch. These decide blast radius on a live site."""

	def test_missing_custom_field_is_a_no_op(self):
		"""custom_fields/ is not auto-loaded on this bench, so the column may never exist."""
		site = _run(_Site(has_column=False))
		self.assertEqual(site.writes, [])
		self.assertEqual(site.queries, [])

	def test_the_root_query_filters_by_voucher_type_submitted_and_cutoff(self):
		"""All three clauses are load-bearing, so all three are asserted on the query text.

		Dropping the voucher-type clause turns the patch into a candidate generator over every
		zero-rate batch on the site; dropping docstatus lets drafts and cancellations in;
		dropping the cutoff pulls in the 591 pre-2025 batches on gk the blend never touched.
		"""
		site = _run(_Site(roots=[]))
		root_query = next(q for q in site.queries if "Repack-Metal Conversion" in q)

		self.assertIn("se.stock_entry_type = 'Repack-Metal Conversion'", root_query)
		self.assertIn("se.docstatus = 1", root_query)
		self.assertIn("b.creation >= %(cutoff)s", root_query)
		self.assertEqual(backfill.BLEND_INTRODUCED, "2025-07-22")

	def test_the_mirror_update_only_fills_zeros(self):
		"""Other stock entries consumed this batch too; ones already carrying a rate keep it."""
		site = _Site(
			batches={
				"BAD": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
			},
			origins={
				"BAD": [{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}]
			},
			roots=["BAD"],
		)
		_run(site)
		update = next(q for q in site.queries if q.strip().upper().startswith("UPDATE"))

		self.assertIn("IFNULL(custom_metal_rate, 0) = 0", update)
		self.assertEqual(site.mirror_updates, [(159000.0, "BAD")])

	def test_a_batch_already_holding_a_rate_is_never_selected(self):
		site = _Site(
			batches={
				"GOOD": {"item": "M-G-22KT-91.75-Y", "custom_metal_rate": 146027.61},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
			},
			origins={
				"GOOD": [
					{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}
				]
			},
			roots=["GOOD"],
		)
		_run(site)
		self.assertEqual(site.writes, [])

	def test_a_second_run_updates_nothing(self):
		site = _Site(
			batches={
				"BAD": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
			},
			origins={
				"BAD": [{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}]
			},
			roots=["BAD"],
		)
		_run(site)
		self.assertTrue(site.writes)

		site.writes.clear()
		site.mirror_updates.clear()
		_run(site)
		self.assertEqual(site.writes, [], msg="the patch is not idempotent")
		self.assertEqual(site.mirror_updates, [])


class TestBackfillRecoveryOrder(_BackfillTestCase):
	"""Which source wins. Every test here makes MORE THAN ONE source available."""

	def _site(self, source_rate, basic_rate):
		return _Site(
			batches={
				"BAD": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 0,
					"reference_name": "SE-1",
					"custom_voucher_detail_no": "ROW-1",
				},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": source_rate},
			},
			origins={
				"BAD": [{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}]
			},
			roots=["BAD"],
			basic_rates={"ROW-1": basic_rate},
		)

	def test_the_blend_beats_the_voucher_row(self):
		"""Both available and distinct -- the source master must win."""
		site = _run(self._site(source_rate=159000.0, basic_rate=333.0))
		self.assertAlmostEqual(_rates_written(site)[0], 159000.0, places=4)

	def test_the_voucher_row_is_the_last_resort(self):
		"""The target row's OWN basic_rate -- lane-scoped by construction."""
		site = _run(self._site(source_rate=0.0, basic_rate=145876.678899083))
		self.assertAlmostEqual(_rates_written(site)[0], 145876.678899083, places=6)

	def test_the_voucher_bundle_average_is_not_a_recovery_source(self):
		"""Rejected deliberately.

		It keyed only on voucher_no, so it pooled every ownership lane, both the metal and the
		alloy pool and every item, with no purity conversion. On MAT-STE-17890 that average is
		145876.6788990825688073 -- basic_rate again by a longer route -- while a multi-lane
		conversion would have blended one customer's gold rate into another customer's batch.
		"""
		self.assertFalse(
			hasattr(backfill, "_rate_from_submitted_bundles"),
			msg="the cross-lane bundle average is back",
		)

	def test_a_batch_with_no_recoverable_rate_is_left_alone(self):
		"""Every source exhausted. Writing a zero over a zero helps nobody."""
		site = _run(self._site(source_rate=0.0, basic_rate=0.0))
		self.assertEqual(site.writes, [])
		self.assertEqual(site.mirror_updates, [])


class TestBackfillBlend(_BackfillTestCase):
	"""The blend arithmetic itself, with rates that are actually non-zero."""

	def test_purity_conversion_applies_to_a_recovered_rate(self):
		"""24KT source into a 22KT target. Deleting the conversion must fail this."""
		site = _Site(
			batches={
				"BAD": {"item": "M-G-22KT-91.75-Y", "custom_metal_rate": 0},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
			},
			origins={
				"BAD": [{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}]
			},
			roots=["BAD"],
		)
		_run(site)
		self.assertAlmostEqual(
			_rates_written(site)[0], 159000.0 * 91.75 / 100, places=4
		)

	def test_matching_purities_inherit_the_rate_unchanged(self):
		site = _Site(
			batches={
				"BAD": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
			},
			origins={
				"BAD": [{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}]
			},
			roots=["BAD"],
		)
		_run(site)
		self.assertAlmostEqual(_rates_written(site)[0], 159000.0, places=4)

	def test_the_alloy_source_is_blended_into_the_alloy_pool(self):
		"""The real MAT-STE-17890 shape. Alloy must not dilute the metal rate."""
		site = _Site(
			batches={
				"BAD": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 0,
					"custom_alloy_rate": 0,
				},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
				"ALY": {
					"item": ALLOY_ITEM,
					"custom_alloy_rate": 62.0,
					"custom_metal_rate": 0,
				},
			},
			origins={
				"BAD": [
					{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0},
					{"batch_no": "ALY", "item_code": ALLOY_ITEM, "qty": 0.45},
				]
			},
			roots=["BAD"],
		)
		_run(site)

		self.assertAlmostEqual(_rates_written(site)[0], 159000.0, places=4)
		alloy = [v for _, f, v in site.writes if f == "custom_alloy_rate"]
		self.assertEqual(len(alloy), 1, msg="the alloy pool was not written")
		self.assertAlmostEqual(alloy[0], 62.0, places=4)

	def test_an_existing_alloy_rate_is_not_overwritten(self):
		"""Same contract as the runtime guard: a set rate is not clobbered by the backfill."""
		site = _Site(
			batches={
				"BAD": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 0,
					"custom_alloy_rate": 99.0,
				},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
				"ALY": {
					"item": ALLOY_ITEM,
					"custom_alloy_rate": 62.0,
					"custom_metal_rate": 0,
				},
			},
			origins={
				"BAD": [
					{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0},
					{"batch_no": "ALY", "item_code": ALLOY_ITEM, "qty": 0.45},
				]
			},
			roots=["BAD"],
		)
		_run(site)
		self.assertEqual([v for _, f, v in site.writes if f == "custom_alloy_rate"], [])

	def test_sources_are_qty_weighted(self):
		site = _Site(
			batches={
				"BAD": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"A": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 100.0},
				"B": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 200.0},
			},
			origins={
				"BAD": [
					{"batch_no": "A", "item_code": "M-G-24KT-99.9-Y", "qty": 1.0},
					{"batch_no": "B", "item_code": "M-G-24KT-99.9-Y", "qty": 3.0},
				]
			},
			roots=["BAD"],
		)
		_run(site)
		self.assertAlmostEqual(_rates_written(site)[0], 175.0, places=6)


class TestBackfillCascade(_BackfillTestCase):
	"""Roots first, then whatever carried their zero forward.

	On kg-gk only 2 batches are primary corruption; the other 7 inherited through
	carry_rates_from_source_batches. A no-op cascade would leave those 7 broken.
	"""

	def _two_level_site(self):
		return _Site(
			batches={
				"ROOT": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
				"CHILD": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"GRANDCHILD": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
			},
			origins={
				"ROOT": [
					{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}
				],
				"CHILD": [
					{"batch_no": "ROOT", "item_code": "M-G-24KT-99.9-Y", "qty": 2.0}
				],
				"GRANDCHILD": [
					{"batch_no": "CHILD", "item_code": "M-G-24KT-99.9-Y", "qty": 1.0}
				],
			},
			roots=["ROOT"],
		)

	def test_the_cascade_reaches_dependents_and_their_dependents(self):
		"""CHILD is not a conversion root; it only becomes a candidate once ROOT is healed."""
		site = _run(self._two_level_site())

		self.assertEqual(_healed(site), {"ROOT", "CHILD", "GRANDCHILD"})
		self.assertAlmostEqual(
			site.batches["CHILD"]["custom_metal_rate"], 159000.0, places=4
		)
		self.assertAlmostEqual(
			site.batches["GRANDCHILD"]["custom_metal_rate"], 159000.0, places=4
		)

	def test_the_root_is_repaired_before_its_dependent(self):
		"""Order matters: a dependent blended before its root would inherit the zero again."""
		site = _run(self._two_level_site())
		order = [name for name, field, _ in site.writes if field == "custom_metal_rate"]

		self.assertLess(order.index("ROOT"), order.index("CHILD"))
		self.assertLess(order.index("CHILD"), order.index("GRANDCHILD"))

	def test_a_root_created_after_its_dependent_still_converges(self):
		"""Creation order alone is not enough, so the walk is a fixpoint.

		R2 is a conversion root created AFTER D, which derives from the earlier root R1. A single
		roots-first pass would blend R2 against D while D still reads 0, storing a wrong non-zero
		rate that every later query excludes -- unfixable by re-running.
		"""
		site = _Site(
			batches={
				"SRC": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 159000.0,
					"creation": "2026-01-01",
				},
				"R1": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 0,
					"creation": "2026-05-01",
				},
				"D": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 0,
					"creation": "2026-06-01",
				},
				"R2": {
					"item": "M-G-24KT-99.9-Y",
					"custom_metal_rate": 0,
					"creation": "2026-07-01",
				},
			},
			origins={
				"R1": [{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0}],
				"D": [{"batch_no": "R1", "item_code": "M-G-24KT-99.9-Y", "qty": 2.0}],
				"R2": [{"batch_no": "D", "item_code": "M-G-24KT-99.9-Y", "qty": 1.0}],
			},
			roots=["R1", "R2"],
		)
		_run(site)

		self.assertEqual(_healed(site), {"R1", "D", "R2"})
		self.assertAlmostEqual(
			site.batches["R2"]["custom_metal_rate"], 159000.0, places=4
		)

	def test_a_cycle_in_the_origin_graph_terminates(self):
		"""A lists B as a source and B lists A. The seen set has to stop the walk."""
		site = _Site(
			batches={
				"A": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"B": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 0},
				"SRC": {"item": "M-G-24KT-99.9-Y", "custom_metal_rate": 159000.0},
			},
			origins={
				"A": [
					{"batch_no": "SRC", "item_code": "M-G-24KT-99.9-Y", "qty": 5.0},
					{"batch_no": "B", "item_code": "M-G-24KT-99.9-Y", "qty": 1.0},
				],
				"B": [{"batch_no": "A", "item_code": "M-G-24KT-99.9-Y", "qty": 1.0}],
			},
			roots=["A"],
		)
		_run(site)
		self.assertIn("A", _healed(site))

	def test_only_batches_restricts_the_run_to_the_named_root(self):
		"""The trial-run escape hatch: one root, and deliberately no cascade."""
		site = self._two_level_site()
		with patch.object(backfill, "ONLY_BATCHES", ["ROOT"]):
			_run(site)

		self.assertEqual(_healed(site), {"ROOT"})
