# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for Process Loss produce-row valuation.

The bug: every loss builder stamps ``set_basic_rate_manually = 1`` on the produce row and
supplies no ``basic_rate``. ERPNext's ``set_basic_rate`` skips manual rows outright, so
the scrap/loss item entered stock at rate 0 -- the consumed metal's value left the ledger
and nothing replaced it. On gk.site MAT-STE-116592 booked 0.523 g of M-G-18KT out at
5493.83 and the matching ML-G-18KT row in at 0.00, diluting the Scrap warehouse's
"Avg Rate (Balance Stock)" on every single loss booking.

Mocked/pure-logic style (see test_loss_row_ownership.py): plain-dict rows, no DB.
"""

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils.loss_valuation import (
	iter_loss_runs,
	set_process_loss_produce_rates,
)


class _FakeSE:
	"""Minimal stand-in for a Stock Entry: rows are plain dicts in `items`."""

	def __init__(self, items, stock_entry_type="Process Loss"):
		self.stock_entry_type = stock_entry_type
		self.items = list(items)

	def get(self, field):
		return getattr(self, field, None)


def _consume(item_code, qty, rate, **fields):
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"s_warehouse": "Waxing WO - GEPL",
		"t_warehouse": None,
		"basic_rate": rate,
		"basic_amount": round(qty * rate, 2),
		"inventory_type": "Regular Stock",
		"customer": None,
	}
	row.update(fields)
	return row


def _produce(item_code, qty, **fields):
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"s_warehouse": None,
		"t_warehouse": "Diamond Setting Scrap - GEPL",
		"basic_rate": 0.0,
		"basic_amount": 0.0,
		"is_finished_item": 1,
		"set_basic_rate_manually": 1,
		"inventory_type": "Regular Stock",
		"customer": None,
	}
	row.update(fields)
	return row


def _totals(se):
	"""(total_outgoing_value, total_incoming_value) the way ERPNext derives them."""
	out = sum(r["basic_amount"] for r in se.items if r["s_warehouse"])
	inc = sum(r["basic_amount"] for r in se.items if r["t_warehouse"])
	return round(out, 2), round(inc, 2)


class TestIterLossRuns(IntegrationTestCase):
	def test_alternating_pairs_yield_one_run_each(self):
		"""Employee IR's combined SE shape: consume, produce, consume, produce."""
		items = [
			_consume("M-A", 1.0, 100.0),
			_produce("ML-A", 1.0),
			_consume("M-B", 2.0, 200.0),
			_produce("ML-B", 2.0),
		]
		runs = list(iter_loss_runs(items))
		self.assertEqual(len(runs), 2)
		self.assertEqual([len(c) for c, _p in runs], [1, 1])
		self.assertEqual([p[0]["item_code"] for _c, p in runs], ["ML-A", "ML-B"])

	def test_many_consume_one_produce_is_a_single_run(self):
		"""Melting loss / Employee Loss Entry shape: N batches, one scrap row."""
		items = [
			_consume("M-A", 1.0, 100.0, batch_no="B1"),
			_consume("M-A", 2.0, 100.0, batch_no="B2"),
			_produce("ML-A", 3.0),
		]
		runs = list(iter_loss_runs(items))
		self.assertEqual(len(runs), 1)
		self.assertEqual(len(runs[0][0]), 2)
		self.assertEqual(len(runs[0][1]), 1)

	def test_trailing_consume_without_produce_is_not_yielded(self):
		items = [
			_consume("M-A", 1.0, 100.0),
			_produce("ML-A", 1.0),
			_consume("M-B", 1.0, 50.0),
		]
		self.assertEqual(len(list(iter_loss_runs(items))), 1)

	def test_transfer_row_breaks_the_run(self):
		"""A row with both warehouses is not part of a loss pair."""
		items = [
			_consume("M-A", 1.0, 100.0),
			{
				"item_code": "M-A",
				"transfer_qty": 1.0,
				"s_warehouse": "WH-1",
				"t_warehouse": "WH-2",
				"basic_amount": 100.0,
			},
			_produce("ML-A", 1.0),
		]
		self.assertEqual(list(iter_loss_runs(items)), [])

	def test_empty_items(self):
		self.assertEqual(list(iter_loss_runs(None)), [])
		self.assertEqual(list(iter_loss_runs([])), [])


class TestSetProcessLossProduceRates(IntegrationTestCase):
	def test_simple_pair_conserves_value(self):
		"""MAT-STE-116592 row 3/4: 0.523 g of M-G-18KT at 5493.83."""
		se = _FakeSE(
			[
				_consume("M-G-18KT-75.4-P", 0.523, 5493.83),
				_produce("ML-G-18KT-75.4-P", 0.523),
			]
		)
		set_process_loss_produce_rates(se)
		produce = se.items[1]
		self.assertEqual(produce["basic_amount"], 2873.27)
		# The rate comes off the consumed rate, not off the rounded amount.
		self.assertAlmostEqual(produce["basic_rate"], 5493.83, places=6)
		self.assertEqual(_totals(se), (2873.27, 2873.27))

	def test_rate_derived_from_consumed_rate_not_rounded_amount(self):
		"""MAT-STE-116356: 0.005 g at 5528.4406 rounds to basic_amount 27.64, and
		27.64 / 0.005 is 5528.00 -- a 0.44 error straight into the Stock Ledger."""
		se = _FakeSE(
			[
				_consume("M-G-18KT-75.4-Y", 0.005, 5528.4406),
				_produce("ML-G-18KT-75.4-Y", 0.005),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertAlmostEqual(se.items[1]["basic_rate"], 5528.4406, places=6)
		self.assertEqual(se.items[1]["basic_amount"], 27.64)
		self.assertEqual(_totals(se), (27.64, 27.64))

	def test_each_pair_valued_independently(self):
		"""18KT value must not smear onto the 22KT scrap row (the reason
		set_basic_rate_manually cannot simply be dropped)."""
		se = _FakeSE(
			[
				_consume("M-G-18KT", 1.0, 4000.0),
				_produce("ML-G-18KT", 1.0),
				_consume("M-G-22KT", 1.0, 6000.0),
				_produce("ML-G-22KT", 1.0),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 4000.0)
		self.assertEqual(se.items[3]["basic_rate"], 6000.0)
		self.assertEqual(_totals(se), (10000.0, 10000.0))

	def test_many_consume_rows_blend_qty_weighted(self):
		se = _FakeSE(
			[
				_consume("M-A", 1.0, 100.0, batch_no="B1"),
				_consume("M-A", 3.0, 200.0, batch_no="B2"),
				_produce("ML-A", 4.0),
			]
		)
		set_process_loss_produce_rates(se)
		# (100 + 600) / 4
		self.assertEqual(se.items[2]["basic_amount"], 700.0)
		self.assertEqual(se.items[2]["basic_rate"], 175.0)

	def test_owner_split_gives_each_produce_row_its_own_value(self):
		"""warehouse_stock_entry._produce_rows_for_run splits a produce row pro-rata by
		owner; each split must take its own owner's consumed value, not a blended share."""
		se = _FakeSE(
			[
				_consume("M-A", 1.0, 5000.0),
				_consume(
					"M-A",
					2.0,
					0.0,
					inventory_type="Customer Goods",
					customer="MHCU0012",
					basic_amount=0.0,
				),
				_produce("ML-A", 1.0),
				_produce(
					"ML-A", 2.0, inventory_type="Customer Goods", customer="MHCU0012"
				),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[2]["basic_amount"], 5000.0)
		self.assertEqual(se.items[2]["basic_rate"], 5000.0)
		# Customer Goods consumed at zero valuation stays at zero.
		self.assertEqual(se.items[3]["basic_amount"], 0.0)
		self.assertEqual(se.items[3]["basic_rate"], 0.0)
		self.assertEqual(_totals(se), (5000.0, 5000.0))

	def test_split_rounding_remainder_keeps_the_run_balanced(self):
		"""Three-way split of an odd paisa must still sum to the consumed value."""
		se = _FakeSE(
			[
				_consume("M-A", 3.0, 3.34, basic_amount=10.01),
				_produce("ML-A", 1.0, inventory_type="X"),
				_produce("ML-A", 1.0, inventory_type="Y"),
				_produce("ML-A", 1.0, inventory_type="Z"),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(_totals(se), (10.01, 10.01))

	def test_zero_valued_source_batch_stays_zero(self):
		"""Strict conservation: no fallback to the Bin/Item rate, so a 0-valued batch
		(MAT-STE-116532's GE2F063-MGL22919Y0-O5H44) produces a 0-valued loss row rather
		than inventing a Stock Adjustment gain."""
		se = _FakeSE(
			[
				_consume("M-G-22KT-91.9-Y", 0.224, 0.0),
				_produce("ML-G-22KT-91.9-Y", 0.224),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 0.0)
		self.assertEqual(se.items[1]["basic_amount"], 0.0)

	def test_other_stock_entry_type_untouched(self):
		se = _FakeSE(
			[_consume("M-A", 1.0, 100.0), _produce("ML-A", 1.0)],
			stock_entry_type="Material Transfer",
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 0.0)
		self.assertEqual(se.items[1]["basic_amount"], 0.0)

	def test_produce_row_without_a_run_is_untouched(self):
		se = _FakeSE([_produce("ML-A", 1.0)])
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[0]["basic_rate"], 0.0)

	def test_zero_transfer_qty_does_not_raise(self):
		se = _FakeSE([_consume("M-A", 1.0, 100.0), _produce("ML-A", 0.0)])
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 0.0)

	def test_no_items_is_a_noop(self):
		se = _FakeSE([])
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items, [])


class TestControllerWiring(IntegrationTestCase):
	def test_custom_stock_entry_overrides_set_basic_rate(self):
		"""The fix has to live on the controller: ERPNext re-runs
		calculate_rate_and_amount on every Repack repost
		(stock_ledger.recalculate_amounts_in_stock_entry) and reads valuation_rate back
		off the Stock Entry Detail row, so a pre-insert fix in a builder would be
		reverted to 0."""
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry import (
			CustomStockEntry,
		)

		self.assertIsNot(CustomStockEntry.set_basic_rate, StockEntry.set_basic_rate)
