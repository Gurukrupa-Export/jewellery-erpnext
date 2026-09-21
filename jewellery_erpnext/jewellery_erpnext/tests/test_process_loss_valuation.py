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

	def __init__(self, items, stock_entry_type="Process Loss", auto_created=1):
		self.stock_entry_type = stock_entry_type
		# Every in-app builder stamps auto_created; the Repack branch requires it so a
		# hand-made Repack from the UI can never be repriced. Defaulted on so the Process
		# Loss cases below read the same as before.
		self.auto_created = auto_created
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


class TestRepackProduceRates(IntegrationTestCase):
	"""The `Repack` branch, added for the finding repack engine.

	`finding_repack` consumes casting-tree metal and produces finding items under the PLAIN
	`Repack` type, with `set_basic_rate_manually = 1` on the produce rows (mandatory --
	`validate_repack_entry` throws for a multi-finished-good Repack otherwise) and no
	`basic_rate`. ERPNext skips manual rows entirely, so MAT-STE-28117 posted 21,352.34 of gold
	out and 0.00 in, writing the whole lot off as a Stock Adjustment.

	The branch is deliberately narrower than the Process Loss one: it fills a zero and never
	rewrites a rate someone else set.
	"""

	def _repack(self, items):
		return _FakeSE(items, stock_entry_type="Repack")

	def test_manual_unrated_produce_row_takes_the_consumed_value(self):
		se = self._repack(
			[_consume("M-A", 1.23, 14624.891247705), _produce("F-A", 1.23)]
		)
		set_process_loss_produce_rates(se)
		self.assertAlmostEqual(se.items[1]["basic_rate"], 14624.891247705, places=6)
		out, inc = _totals(se)
		self.assertEqual(out, inc)

	def test_two_findings_from_one_run_each_take_their_own_share(self):
		"""The real MAT-STE-28117 shape: consume/produce pairs, 1.23 g then 0.23 g."""
		se = self._repack(
			[
				_consume("M-A", 1.23, 14624.891247705),
				_produce("F-PO", 1.23),
				_consume("M-A", 0.23, 14624.891247705),
				_produce("F-SW", 0.23),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertAlmostEqual(se.items[1]["basic_amount"], 17988.62, places=2)
		self.assertAlmostEqual(se.items[3]["basic_amount"], 3363.72, places=2)
		out, inc = _totals(se)
		self.assertEqual(out, inc)

	def test_mixed_ownership_split_values_each_owner_from_its_own_consumes(self):
		"""_append_finding_rows emits one produce row per ownership tier; each must take the
		value ITS consume rows gave up, not a pro-rata slice of the run."""
		se = self._repack(
			[
				_consume(
					"M-A", 1.0, 100.0, inventory_type="Customer Goods", customer="C1"
				),
				_consume("M-A", 2.0, 200.0),
				_produce("F-A", 1.0, inventory_type="Customer Goods", customer="C1"),
				_produce("F-A", 2.0),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertAlmostEqual(se.items[2]["basic_amount"], 100.0, places=2)
		self.assertAlmostEqual(se.items[3]["basic_amount"], 400.0, places=2)

	def test_row_that_already_has_a_rate_is_left_alone(self):
		"""_convert_received_scrap_to_scrap_batch prices both its legs deliberately; recomputing
		would reprice entries already posted whenever ERPNext reposts them."""
		se = self._repack(
			[
				_consume("M-A", 1.0, 100.0),
				_produce("ML-A", 1.0, basic_rate=77.0, basic_amount=77.0),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 77.0)
		self.assertEqual(se.items[1]["basic_amount"], 77.0)

	def test_row_without_the_manual_flag_is_left_to_erpnext(self):
		"""No manual flag means ERPNext's get_basic_rate_for_repacked_items prices it; this
		module must not steal the row (main_slip and the purity repack rely on that)."""
		se = self._repack(
			[
				_consume("M-A", 1.0, 100.0),
				_produce("ML-A", 1.0, set_basic_rate_manually=0),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 0.0)

	def test_mixed_run_skips_the_balancing_rather_than_misparking(self):
		"""One owned row and one priced elsewhere: the owned row still takes its own share, but
		the rounding residue must NOT be parked onto it -- that residue is the other row's."""
		se = self._repack(
			[
				_consume("M-A", 1.0, 100.0),
				_consume("M-A", 2.0, 200.0),
				_produce("F-A", 1.0),
				_produce("F-B", 2.0, basic_rate=50.0, basic_amount=100.0),
			]
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[3]["basic_amount"], 100.0)
		self.assertGreater(se.items[2]["basic_amount"], 0.0)

	def test_process_loss_is_untouched_by_the_widened_gate(self):
		"""Regression guard: 18,253 live batches were valued through the Process Loss branch,
		which stays unconditional -- a Process Loss row is valued even if it already has a rate."""
		se = _FakeSE(
			[_consume("M-A", 1.0, 100.0), _produce("ML-A", 1.0, basic_rate=5.0)]
		)
		set_process_loss_produce_rates(se)
		self.assertAlmostEqual(se.items[1]["basic_rate"], 100.0, places=6)

	def test_hand_made_repack_is_never_repriced(self):
		"""`set_basic_rate_manually` is a plain user-visible checkbox on Stock Entry Detail
		(depends_on parent.purpose==="Repack" && doc.t_warehouse), not an internal marker.
		An operator who ticks it on their own Repack and leaves the rate at 0 on purpose --
		a zero-value by-product -- must keep that 0; silently handing them the whole consumed
		value would be invisible in the document."""
		se = _FakeSE(
			[_consume("M-A", 1.0, 100.0), _produce("F-A", 1.0)],
			stock_entry_type="Repack",
			auto_created=0,
		)
		set_process_loss_produce_rates(se)
		self.assertEqual(se.items[1]["basic_rate"], 0.0)
		self.assertEqual(se.items[1]["basic_amount"], 0.0)

	def test_hand_made_process_loss_is_still_valued(self):
		"""The auto_created term is Repack-only: Process Loss stays unconditional."""
		se = _FakeSE(
			[_consume("M-A", 1.0, 100.0), _produce("ML-A", 1.0)], auto_created=0
		)
		set_process_loss_produce_rates(se)
		self.assertAlmostEqual(se.items[1]["basic_rate"], 100.0, places=6)


def _cv_produce(item_code, qty, pooled_rate, **fields):
	"""A Metal Conversion produce row as ERPNext leaves it: priced, and priced WRONG.

	Unlike the loss and finding rows above this one carries no ``set_basic_rate_manually``
	and a non-zero ``basic_rate`` -- the voucher-wide pooled average from
	``get_basic_rate_for_repacked_items``. The conversion branch has to replace it.
	"""
	row = _produce(item_code, qty, **fields)
	row["set_basic_rate_manually"] = 0
	row["basic_rate"] = pooled_rate
	row["basic_amount"] = round(qty * pooled_rate, 2)
	return row


class TestMetalConversionLaneRates(IntegrationTestCase):
	"""``Repack-Metal Conversion``: each ownership lane keeps its own value.

	A conversion is multi-lane by design -- ``metal_conversions`` tags every row with
	``custom_conversion_lane`` because one entry carries several owners at once. ERPNext
	prices it single-lane: ``get_basic_rate_for_repacked_items`` pools the whole voucher's
	outgoing cost over total finished qty, so every produced row takes the same blended
	number regardless of whose metal it came from.

	Measured on MAT-STE-17964: 4 g of a customer's 24KT at 159,000 and 1.5 g of company 24KT
	at 15,487.41 both produced rows valued at 109,968.61. The company's 1.635 g absorbed
	179,798.67 against 23,239.48 of its own inputs -- 156,559.19 of the customer's gold --
	while the customer's own row sat at 0 and 479,463.13 went to Stock Adjustment.
	"""

	POOLED = 109968.607172644

	def _conversion(self, items, auto_created=1):
		return _FakeSE(
			items, stock_entry_type="Repack-Metal Conversion", auto_created=auto_created
		)

	def _two_lane_rows(self):
		"""The exact MAT-STE-17964 shape: customer lane, then company lane."""
		return [
			_consume(
				"M-G-24KT-99.9-Y",
				4,
				159000.0,
				inventory_type="Customer Goods",
				customer="GJCU0009",
			),
			_consume("M-Genia-221", 0.36, 62.0),
			_cv_produce(
				"M-G-22KT-91.75-Y",
				4.36,
				self.POOLED,
				inventory_type="Customer Goods",
				customer="GJCU0009",
			),
			_consume("M-G-24KT-99.9-Y", 1.5, 15487.405405405),
			_consume("M-Genia-221", 0.135, 62.0),
			_cv_produce("M-G-22KT-91.75-Y", 1.635, self.POOLED),
		]

	def test_each_lane_takes_only_its_own_consumed_value(self):
		"""The defect, reproduced and fixed: two rows, two rates, not one blended one."""
		se = self._conversion(self._two_lane_rows())
		set_process_loss_produce_rates(se)

		# customer lane: (4 x 159000 + 0.36 x 62) / 4.36
		self.assertAlmostEqual(se.items[2]["basic_rate"], 145876.678899083, places=6)
		# company lane: (1.5 x 15487.405405405 + 0.135 x 62) / 1.635
		self.assertAlmostEqual(se.items[5]["basic_rate"], 14213.748078353, places=6)

	def test_the_voucher_is_value_neutral(self):
		"""No Stock Adjustment write-off: what goes out equals what comes in."""
		se = self._conversion(self._two_lane_rows())
		set_process_loss_produce_rates(se)

		out, inc = _totals(se)
		self.assertEqual(out, inc)
		self.assertEqual(out, 659261.80)

	def test_the_company_lane_stops_absorbing_the_customers_gold(self):
		"""156,559.19 of customer metal used to land in company stock. It no longer does."""
		se = self._conversion(self._two_lane_rows())
		set_process_loss_produce_rates(se)

		self.assertAlmostEqual(se.items[5]["basic_amount"], 23239.48, places=2)
		self.assertAlmostEqual(se.items[2]["basic_amount"], 636022.32, places=2)

	def test_the_pooled_rate_is_replaced_not_merely_filled(self):
		"""Every other branch only fills a zero. This one must overwrite a real number --
		the wrong number is already there."""
		se = self._conversion(self._two_lane_rows())
		before = se.items[5]["basic_rate"]
		set_process_loss_produce_rates(se)

		self.assertEqual(before, self.POOLED)
		self.assertNotAlmostEqual(se.items[5]["basic_rate"], self.POOLED, places=2)

	def test_a_single_lane_conversion_still_conserves_value(self):
		"""The MAT-STE-17890 shape, which was already value-neutral, must stay so."""
		se = self._conversion(
			[
				_consume(
					"M-G-24KT-99.9-Y",
					5,
					159000.0,
					inventory_type="Customer Goods",
					customer="GJCU0009",
				),
				_consume("M-Genia-221", 0.45, 62.0),
				_cv_produce(
					"M-G-22KT-91.75-Y",
					5.45,
					self.POOLED,
					inventory_type="Customer Goods",
					customer="GJCU0009",
				),
			]
		)
		set_process_loss_produce_rates(se)

		self.assertAlmostEqual(se.items[2]["basic_rate"], 145876.678899083, places=6)
		out, inc = _totals(se)
		self.assertEqual(out, inc)

	def test_a_hand_built_conversion_is_never_repriced(self):
		"""``auto_created`` confines this to vouchers the app made, as on a plain Repack."""
		se = self._conversion(self._two_lane_rows(), auto_created=0)
		set_process_loss_produce_rates(se)

		self.assertEqual(se.items[2]["basic_rate"], self.POOLED)
		self.assertEqual(se.items[5]["basic_rate"], self.POOLED)

	def test_zero_value_policy_a_customer_only_lane_stays_at_zero(self):
		"""No policy branch exists, and none is needed: a 0-valued consume row allocates 0."""
		rows = self._two_lane_rows()
		rows[0]["basic_rate"] = 0.0
		rows[0]["basic_amount"] = 0.0
		rows[1]["inventory_type"] = "Customer Goods"
		rows[1]["customer"] = "GJCU0009"
		rows[1]["basic_rate"] = 0.0
		rows[1]["basic_amount"] = 0.0
		se = self._conversion(rows)
		set_process_loss_produce_rates(se)

		self.assertEqual(se.items[2]["basic_rate"], 0.0)

	def test_zero_value_policy_company_alloy_in_the_lane_is_still_conserved(self):
		"""The one case worth naming: the alloy really cost 22.32, so it is carried, not
		written off. 22.32 over 4.36 g is 5.1193 -- small, and deliberately not zero."""
		rows = self._two_lane_rows()
		rows[0]["basic_rate"] = 0.0
		rows[0]["basic_amount"] = 0.0
		se = self._conversion(rows)
		set_process_loss_produce_rates(se)

		self.assertAlmostEqual(se.items[2]["basic_rate"], 5.119266055, places=6)
		self.assertAlmostEqual(se.items[2]["basic_amount"], 22.32, places=2)

	def test_other_stock_entry_types_are_untouched(self):
		for se_type in ("Material Transfer", "Manufacture", "Material Receipt"):
			with self.subTest(se_type=se_type):
				se = _FakeSE(
					self._two_lane_rows(), stock_entry_type=se_type, auto_created=1
				)
				set_process_loss_produce_rates(se)
				self.assertEqual(se.items[2]["basic_rate"], self.POOLED)

	# ------------------------------------------------------------------ released alloy

	def _released_alloy_rows(self, alloy_inventory_type, alloy_customer=None):
		"""A purity-RAISE lane: customer metal in, customer metal + freed alloy out.

		``metal_conversions`` emits that second produce row with the SAME lane tag and
		(for the company's share) a DIFFERENT ownership -- see the C09 carve-out at
		metal_conversions.py:617-644.
		"""
		return [
			_consume(
				"M-G-22KT-91.75-Y",
				4.36,
				145876.678899083,
				inventory_type="Customer Goods",
				customer="GJCU0009",
			),
			_cv_produce(
				"M-G-24KT-99.9-Y",
				4.0,
				self.POOLED,
				inventory_type="Customer Goods",
				customer="GJCU0009",
			),
			_cv_produce(
				"M-Genia-221",
				0.36,
				self.POOLED,
				inventory_type=alloy_inventory_type,
				customer=alloy_customer,
			),
		]

	def test_a_lane_that_releases_company_alloy_is_left_to_erpnext(self):
		"""The regression this guard exists to stop.

		Owner-matched allocation gives the customer row the whole consumed value, finds no
		consumed owner for the Regular Stock alloy row, and computes ``leftover`` 0 -- so the
		alloy would come back at 0 with the company's value left inside the customer's metal.
		The voucher balances either way, which is why the value-neutrality tests cannot see it.
		"""
		se = self._conversion(self._released_alloy_rows("Regular Stock"))
		set_process_loss_produce_rates(se)

		self.assertEqual(se.items[1]["basic_rate"], self.POOLED)
		self.assertEqual(se.items[2]["basic_rate"], self.POOLED)
		self.assertNotEqual(se.items[2]["basic_rate"], 0.0)

	def test_a_released_company_alloy_lane_is_left_alone_under_zero_value_too(self):
		"""Zero Value makes it starker: the customer's gold is worth 0 and the alloy is not,
		so zeroing the alloy row would move the company's only value into customer stock."""
		rows = self._released_alloy_rows("Regular Stock")
		rows[0]["basic_rate"] = 0.0
		rows[0]["basic_amount"] = 0.0
		se = self._conversion(rows)
		set_process_loss_produce_rates(se)

		self.assertEqual(se.items[2]["basic_rate"], self.POOLED)

	def test_a_lane_releasing_customer_alloy_is_also_left_alone(self):
		"""The guard keys on ROW COUNT, not owner diversity, and this is why.

		Here both produce rows share an owner, so there is no mismatch to notice -- and
		``_allocate`` would spread the lane value pro-rata by qty, valuing 0.36 g of alloy
		like 0.36 g of gold. Wrong for the same reason, with nothing to signal it.
		"""
		se = self._conversion(
			self._released_alloy_rows("Customer Goods", alloy_customer="GJCU0009")
		)
		set_process_loss_produce_rates(se)

		self.assertEqual(se.items[1]["basic_rate"], self.POOLED)
		self.assertEqual(se.items[2]["basic_rate"], self.POOLED)

	def test_a_single_produce_lane_is_still_owned(self):
		"""The narrowing must not switch the reported fix off. One row in, one row out."""
		se = self._conversion(self._two_lane_rows())
		set_process_loss_produce_rates(se)

		self.assertAlmostEqual(se.items[2]["basic_rate"], 145876.678899083, places=6)
		self.assertAlmostEqual(se.items[5]["basic_rate"], 14213.748078353, places=6)
