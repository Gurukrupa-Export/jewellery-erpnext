# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for ``customization/utils/ownership_priority``.

Deliberately pure-logic: dicts in, allocations out, no Batch records and no DB
writes. ``create_test_data.py`` creates zero Batch documents, so an
integration-shaped test of the ownership rule could not run in CI at all -- it
would silently pass against an empty fixture set. Everything here is exercised
through the public helpers with stubbed inputs.

The single most important test in this file is
``test_single_tier_matches_legacy_split``: it re-implements the pre-waterfall
proportional split from ``employee_ir.book_metal_loss`` verbatim and asserts the
new allocator reproduces it **row for row** whenever only one ownership tier is
funded. That is the regression guard for every site with no customer-owned metal.
"""

import random
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils import (
	ownership_priority as opri,
)

PREC = 3


def _row(key, qty, inv=None, no_wastage=False):
	return {"key": key, "qty": qty, "inv": inv, "no_wastage": no_wastage}


def _allocate(rows, total, precision=PREC):
	return opri.tiered_allocate(
		rows,
		total,
		rank_of=lambda r: opri.loss_rank(r.get("inv"), r.get("no_wastage")),
		qty_of=lambda r: r["qty"],
		precision=precision,
		key_of=lambda r: r["key"],
	)


def _legacy_split(rows, loss, precision=PREC):
	"""The pre-waterfall flat proportional split, re-implemented verbatim.

	Mirrors ``employee_ir.book_metal_loss`` -- proportional by balance across every
	row, each rounded independently, then the residual anchored on the largest-loss
	row (``max`` returns the FIRST maximal element).
	"""
	total_qty = sum(r["qty"] for r in rows)
	out = {r["key"]: 0.0 for r in rows}
	if total_qty == 0 or loss <= 0:
		return {}
	for r in rows:
		out[r["key"]] = (r["qty"] * loss) / total_qty
	positive = [r for r in rows if out[r["key"]] > 0]
	if positive:
		for r in positive:
			out[r["key"]] = flt(out[r["key"]], precision)
		target = flt(loss, precision)
		distributed = flt(sum(out[r["key"]] for r in positive), precision)
		residual = flt(target - distributed, precision)
		if residual:
			anchor = max(positive, key=lambda r: out[r["key"]])
			out[anchor["key"]] = flt(out[anchor["key"]] + residual, precision)
	return {k: v for k, v in out.items() if flt(v, precision) > 0}


class TestRanks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_consume_prefers_customer_then_regular_then_pure(self):
		self.assertLess(
			opri.consume_rank("Customer Goods"), opri.consume_rank("Regular Stock")
		)
		self.assertLess(
			opri.consume_rank("Regular Stock"), opri.consume_rank("Pure Metal")
		)

	def test_customer_stock_ranks_with_customer_goods_on_consume(self):
		# Regression: the old INVENTORY_TYPE_PRIORITY dict had no "Customer Stock"
		# key, so it fell to the .get(..., 99) default and sorted dead LAST.
		self.assertEqual(
			opri.consume_rank("Customer Stock"), opri.consume_rank("Customer Goods")
		)

	def test_loss_prefers_regular_then_pure_then_customer(self):
		self.assertLess(opri.loss_rank("Regular Stock"), opri.loss_rank("Pure Metal"))
		self.assertLess(opri.loss_rank("Pure Metal"), opri.loss_rank("Customer Goods"))
		self.assertEqual(
			opri.loss_rank("Customer Stock"), opri.loss_rank("Customer Goods")
		)

	def test_blank_inventory_type_ranks_as_regular_stock_for_loss(self):
		# MOP Log.batch_no is a Data field with no referential integrity, so an
		# unresolvable batch is routine. It must absorb loss like company metal,
		# never sort behind the customer.
		self.assertEqual(opri.loss_rank(None), opri.loss_rank("Regular Stock"))
		self.assertEqual(opri.loss_rank(""), opri.loss_rank("Regular Stock"))

	def test_unknown_inventory_type_ranks_as_regular_stock_for_loss(self):
		self.assertEqual(opri.loss_rank("Nonsense"), opri.loss_rank("Regular Stock"))

	def test_unknown_inventory_type_ranks_last_on_consume(self):
		self.assertEqual(opri.consume_rank("Nonsense"), opri.UNKNOWN_RANK)
		self.assertGreater(
			opri.consume_rank("Nonsense"), opri.consume_rank("Pure Metal")
		)

	def test_no_wastage_ranks_behind_every_ordinary_customer(self):
		self.assertGreater(
			opri.loss_rank("Customer Goods", no_wastage=True),
			opri.loss_rank("Customer Goods"),
		)
		self.assertEqual(opri.loss_rank("Customer Goods", True), opri.NO_WASTAGE_RANK)

	def test_is_customer_rank_matches_customer_tiers(self):
		self.assertFalse(opri.is_customer_rank(opri.loss_rank("Regular Stock")))
		self.assertFalse(opri.is_customer_rank(opri.loss_rank("Pure Metal")))
		self.assertTrue(opri.is_customer_rank(opri.loss_rank("Customer Goods")))
		self.assertTrue(opri.is_customer_rank(opri.loss_rank("Customer Goods", True)))


class TestTieredAllocate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_regular_absorbs_before_customer(self):
		rows = [_row("CG", 10.0, "Customer Goods"), _row("RS", 4.0, "Regular Stock")]
		alloc, info = _allocate(rows, 3.0)
		self.assertEqual(alloc, {"RS": 3.0})
		self.assertEqual(info.ranks_touched, [opri.loss_rank("Regular Stock")])

	def test_waterfall_spills_only_after_regular_is_exhausted(self):
		rows = [
			_row("CG", 10.0, "Customer Goods"),
			_row("RS", 4.0, "Regular Stock"),
			_row("PM", 3.0, "Pure Metal"),
		]
		alloc, info = _allocate(rows, 9.0)
		self.assertEqual(alloc, {"RS": 4.0, "PM": 3.0, "CG": 2.0})
		self.assertEqual(flt(sum(alloc.values()), PREC), 9.0)
		self.assertTrue(any(opri.is_customer_rank(r) for r in info.ranks_touched))

	def test_tier_is_capped_at_its_own_balance(self):
		rows = [_row("RS", 4.0, "Regular Stock"), _row("CG", 10.0, "Customer Goods")]
		alloc, _info = _allocate(rows, 6.0)
		self.assertEqual(alloc["RS"], 4.0)
		self.assertEqual(alloc["CG"], 2.0)

	def test_overflow_anchors_on_first_tier_not_on_the_customer(self):
		# Loss exceeds every balance combined. The excess must land on company
		# metal -- anchoring it on the last (customer) tier would drive that row's
		# received_gross_weight negative and mint a customer-owned scrap batch for
		# metal that never existed.
		rows = [_row("RS", 4.0, "Regular Stock"), _row("CG", 10.0, "Customer Goods")]
		alloc, info = _allocate(rows, 20.0)
		self.assertEqual(alloc["CG"], 10.0)
		self.assertEqual(alloc["RS"], 10.0)
		self.assertEqual(info.overflow, 6.0)
		self.assertEqual(flt(sum(alloc.values()), PREC), 20.0)

	def test_customer_only_operation_books_on_the_customer(self):
		rows = [_row("CG", 10.0, "Customer Goods")]
		alloc, info = _allocate(rows, 2.0)
		self.assertEqual(alloc, {"CG": 2.0})
		self.assertTrue(any(opri.is_customer_rank(r) for r in info.ranks_touched))

	def test_blank_inventory_type_does_not_split_the_regular_tier(self):
		rows = [_row("A", 5.0, None), _row("B", 5.0, "Regular Stock")]
		alloc, info = _allocate(rows, 4.0)
		self.assertEqual(alloc, {"A": 2.0, "B": 2.0})
		self.assertEqual(info.ranks_touched, [opri.loss_rank("Regular Stock")])

	def test_no_wastage_batch_is_funded_last(self):
		rows = [
			_row("NW", 10.0, "Customer Goods", no_wastage=True),
			_row("CG", 4.0, "Customer Goods"),
			_row("RS", 2.0, "Regular Stock"),
		]
		alloc, _info = _allocate(rows, 6.0)
		self.assertEqual(alloc, {"RS": 2.0, "CG": 4.0})
		self.assertNotIn("NW", alloc)

	def test_zero_qty_tier_is_not_funded(self):
		rows = [_row("RS", 0.0, "Regular Stock"), _row("CG", 5.0, "Customer Goods")]
		alloc, _info = _allocate(rows, 2.0)
		self.assertEqual(alloc, {"CG": 2.0})

	def test_zero_and_negative_total_allocate_nothing(self):
		rows = [_row("RS", 5.0, "Regular Stock")]
		self.assertEqual(_allocate(rows, 0.0)[0], {})
		self.assertEqual(_allocate(rows, -3.0)[0], {})

	def test_empty_rows_allocate_nothing(self):
		self.assertEqual(_allocate([], 5.0)[0], {})

	def test_all_zero_qty_allocates_nothing(self):
		alloc, info = _allocate([_row("A", 0.0, "Regular Stock")], 5.0)
		self.assertEqual(alloc, {})
		self.assertEqual(info.ranks_touched, [])

	def test_float_dust_total_allocates_nothing(self):
		alloc, _info = _allocate([_row("RS", 5.0, "Regular Stock")], 0.0001)
		self.assertEqual(alloc, {})

	def test_deterministic_across_shuffled_input(self):
		rows = [
			_row("RS1", 3.0, "Regular Stock"),
			_row("RS2", 3.0, "Regular Stock"),
			_row("CG", 6.0, "Customer Goods"),
			_row("PM", 1.0, "Pure Metal"),
		]
		baseline, _ = _allocate(rows, 8.5)
		rng = random.Random(4)
		for _ in range(25):
			shuffled = rows[:]
			rng.shuffle(shuffled)
			alloc, _info = _allocate(shuffled, 8.5)
			self.assertEqual(
				flt(sum(alloc.values()), PREC), flt(sum(baseline.values()), PREC)
			)
			self.assertEqual(set(alloc), set(baseline))


class TestLegacyParity(IntegrationTestCase):
	"""A single funded tier must reproduce the old flat split row for row."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_single_tier_matches_legacy_split(self):
		rng = random.Random(7)
		for _ in range(400):
			n = rng.randint(1, 6)
			rows = [
				_row(f"B{i}", round(rng.uniform(0.001, 50), 3), "Regular Stock")
				for i in range(n)
			]
			total = sum(r["qty"] for r in rows)
			loss = round(rng.uniform(0.001, total * 1.4), 3)
			alloc, _info = _allocate(rows, loss)
			self.assertEqual(alloc, _legacy_split(rows, loss))

	def test_single_tier_over_capacity_matches_legacy(self):
		# The legacy split deliberately let a row exceed its own balance when the
		# loss did; the per-tier cap must stay bypassed for a lone tier.
		rows = [_row("A", 1.0, "Regular Stock"), _row("B", 3.0, "Regular Stock")]
		alloc, _info = _allocate(rows, 8.0)
		self.assertEqual(alloc, _legacy_split(rows, 8.0))
		self.assertEqual(flt(sum(alloc.values()), PREC), 8.0)

	def test_spec_example_three_equal_batches(self):
		rows = [_row(f"B{i}", 1.0, "Regular Stock") for i in range(3)]
		alloc, _info = _allocate(rows, 1.0)
		self.assertEqual(alloc, _legacy_split(rows, 1.0))
		self.assertEqual(flt(sum(alloc.values()), PREC), 1.0)

	def test_sum_invariant_holds_across_random_layouts(self):
		rng = random.Random(11)
		invs = [
			"Regular Stock",
			"Pure Metal",
			"Customer Goods",
			"Customer Stock",
			None,
		]
		checked = 0
		for _ in range(200):
			n = rng.randint(1, 7)
			rows = [
				_row(f"B{i}", round(rng.uniform(0, 40), 3), rng.choice(invs))
				for i in range(n)
			]
			loss = round(rng.uniform(0.001, 120), 3)
			alloc, _info = _allocate(rows, loss)
			if not alloc:
				continue
			checked += 1
			self.assertEqual(flt(sum(alloc.values()), PREC), flt(loss, PREC))
			self.assertGreaterEqual(min(alloc.values()), 0)
		self.assertGreater(checked, 150)


class TestAllocateInOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_takes_in_pool_order_and_reports_shortfall(self):
		pool = [("A", 2.0), ("B", 5.0)]
		out, short = opri.allocate_in_order(pool, 4.0, PREC)
		self.assertEqual(out, [("A", 2.0), ("B", 2.0)])
		self.assertEqual(short, 0)

		out, short = opri.allocate_in_order(pool, 9.0, PREC)
		self.assertEqual(out, [("A", 2.0), ("B", 5.0)])
		self.assertEqual(flt(short, PREC), 2.0)

	def test_shared_taken_ledger_prevents_double_booking(self):
		# The two tree receive passes walk one pool; the second must not re-take
		# what the first already claimed.
		pool = [("A", 6.0), ("B", 4.0)]
		taken = {}
		first, _ = opri.allocate_in_order(pool, 5.0, PREC, taken=taken)
		second, short = opri.allocate_in_order(pool, 5.0, PREC, taken=taken)
		self.assertEqual(first, [("A", 5.0)])
		self.assertEqual(second, [("A", 1.0), ("B", 4.0)])
		self.assertEqual(short, 0)
		total = sum(q for _k, q in first) + sum(q for _k, q in second)
		self.assertEqual(flt(total, PREC), 10.0)

	def test_skips_fully_taken_entries(self):
		pool = [("A", 2.0), ("B", 3.0)]
		out, _short = opri.allocate_in_order(pool, 3.0, PREC, taken={"A": 2.0})
		self.assertEqual(out, [("B", 3.0)])


class TestBatchPriorityMap(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_empty_input_makes_no_query(self):
		with patch.object(frappe.db, "get_all") as ga:
			self.assertEqual(opri.batch_priority_map([]), {})
			self.assertEqual(opri.batch_priority_map(None), {})
			ga.assert_not_called()

	def test_single_round_trip_and_dedup(self):
		rows = [
			{
				"name": "B1",
				"creation": "2026-01-01",
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "CUST-1",
			}
		]
		with patch.object(frappe.db, "get_all", return_value=rows) as ga:
			out = opri.batch_priority_map(["B1", "B1", None, ""])
			ga.assert_called_once()
			self.assertEqual(ga.call_args.kwargs["filters"]["name"][1], ["B1"])
		self.assertEqual(out["B1"].inventory_type, "Customer Goods")
		self.assertEqual(out["B1"].customer, "CUST-1")
		self.assertFalse(out["B1"].no_wastage)

	def test_tolerates_rows_missing_name_and_creation(self):
		# Some suites patch frappe.db.get_all globally and hand back rows shaped
		# for a different query; the map must not raise on them.
		rows = [{"item_code": "M1", "batch_no": "B1", "qty": 3}, {"name": "B2"}]
		with patch.object(frappe.db, "get_all", return_value=rows):
			out = opri.batch_priority_map(["B1", "B2"])
		self.assertNotIn("B1", out)
		self.assertEqual(out["B2"].creation, "")
		self.assertIsNone(out["B2"].inventory_type)

	def test_no_wastage_resolved_in_one_extra_round_trip(self):
		batches = [
			{
				"name": "B1",
				"creation": "2026-01-01",
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "CUST-NW",
			},
			{
				"name": "B2",
				"creation": "2026-01-02",
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "CUST-OK",
			},
		]
		with patch.object(
			frappe.db, "get_all", side_effect=[batches, [{"name": "CUST-NW"}]]
		) as ga:
			out = opri.batch_priority_map(["B1", "B2"], with_no_wastage=True)
			self.assertEqual(ga.call_count, 2)
		self.assertTrue(out["B1"].no_wastage)
		self.assertFalse(out["B2"].no_wastage)


class TestSortBatches(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _ranks(self):
		return {
			"B-CG": frappe._dict(
				inventory_type="Customer Goods", creation="2026-03-01", no_wastage=False
			),
			"B-RS": frappe._dict(
				inventory_type="Regular Stock", creation="2026-01-01", no_wastage=False
			),
			"B-PM": frappe._dict(
				inventory_type="Pure Metal", creation="2026-02-01", no_wastage=False
			),
		}

	def test_consume_mode_puts_customer_first_despite_later_creation(self):
		cands = [frappe._dict(batch_no=b) for b in ("B-RS", "B-PM", "B-CG")]
		out = opri.sort_batches(cands, self._ranks(), mode="consume")
		self.assertEqual([c.batch_no for c in out], ["B-CG", "B-RS", "B-PM"])

	def test_loss_mode_puts_regular_first_and_customer_last(self):
		cands = [frappe._dict(batch_no=b) for b in ("B-CG", "B-PM", "B-RS")]
		out = opri.sort_batches(cands, self._ranks(), mode="loss")
		self.assertEqual([c.batch_no for c in out], ["B-RS", "B-PM", "B-CG"])

	def test_caller_order_survives_inside_a_tier(self):
		# The sort is stable and keys on the rank alone, so whatever order the
		# caller supplied (already FIFO from capped_auto_batch_nos) is preserved.
		ranks = {
			"B2": frappe._dict(
				inventory_type="Regular Stock", creation="2026-02-01", no_wastage=False
			),
			"B1": frappe._dict(
				inventory_type="Regular Stock", creation="2026-01-01", no_wastage=False
			),
		}
		cands = [frappe._dict(batch_no="B2"), frappe._dict(batch_no="B1")]
		out = opri.sort_batches(cands, ranks, mode="consume")
		self.assertEqual([c.batch_no for c in out], ["B2", "B1"])

	def test_unresolved_batches_keep_their_incoming_order(self):
		# Regression: a key of (rank, creation, batch_no) silently replaced FIFO
		# with alphabetical whenever ownership could not be resolved, because
		# creation fell back to "" and the batch name broke the tie. B-Z must NOT
		# be moved behind B-A.
		cands = [frappe._dict(batch_no="B-Z"), frappe._dict(batch_no="B-A")]
		out = opri.sort_batches(cands, {}, mode="consume")
		self.assertEqual([c.batch_no for c in out], ["B-Z", "B-A"])

	def test_unresolved_batch_does_not_raise(self):
		cands = [frappe._dict(batch_no="B-GHOST"), frappe._dict(batch_no="B-RS")]
		out = opri.sort_batches(cands, self._ranks(), mode="consume")
		self.assertEqual(len(out), 2)
