# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""F3 — a customer-gold Manufacture must not lose its finished-good valuation.

Every test drives ERPNext's REAL ``StockEntry.set_basic_rate`` through the app's
``CustomStockEntry`` override. Only ``set_rate_for_outgoing_items`` is replaced, because it reads
stock balances; the consumed rows keep the rate each test gives them, so the outgoing cost is exact.
Nothing here models ERPNext's branches -- the brief's §11 is explicit that a model of the code is how
the last false-green happened.

A "validate pass" is the part of ``validate`` that decides valuation: the app's ``before_validate``
stamp (``allow_zero_valuation``) followed by ``set_basic_rate``. ``save()`` then ``submit()`` is two
passes; a repost is a pass WITHOUT the stamp, because it re-runs ``calculate_rate_and_amount`` on a
loaded document and never calls ``before_validate``.

The production numbers are MAT-STE-18661 (serial KLHGX62F1119): 5.440 g of customer 22KT at
1,44,642.733944954 plus 0.396 ct of company diamond at 37,370 -> 8,01,654.99. At submit the finished
row went 8,01,654.99 -> 0.0 and the whole input was expensed to Stock Adjustment.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	allow_zero_valuation,
)

CUSTOMER_GOODS = "Customer Goods"
REGULAR_STOCK = "Regular Stock"

# MAT-STE-18661
GOLD_QTY, GOLD_RATE = 5.44, 144642.733944954
DIAMOND_QTY, DIAMOND_RATE = 0.396, 37370.0
ALLOY_QTY, ALLOY_RATE = 0.449, 62.0
KLHGX62F1119_FG_VALUE = 801654.99  # 786,856.47 gold + 14,798.52 diamond


def _row(item_code, qty, **fields):
	return {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"conversion_factor": 1,
		"uom": "Nos",
		"stock_uom": "Nos",
		**fields,
	}


def _consumed(item_code, qty, rate, inventory_type, **fields):
	return _row(
		item_code,
		qty,
		s_warehouse="WIP",
		basic_rate=rate,
		inventory_type=inventory_type,
		**fields,
	)


def _finished(item_code="FG-PIECE", qty=1, inventory_type=CUSTOMER_GOODS, **fields):
	return _row(
		item_code,
		qty,
		t_warehouse="FG",
		is_finished_item=1,
		inventory_type=inventory_type,
		**fields,
	)


def _entry(purpose, rows):
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"purpose": purpose,
			"stock_entry_type": purpose,
			"items": [],
		}
	)
	for row in rows:
		se.append("items", row)
	return se


def _outgoing(se):
	"""Stand-in for ``set_rate_for_outgoing_items``: consumed rows keep the rate the test gave them."""
	total = 0.0
	for row in se.items:
		if row.s_warehouse:
			row.basic_amount = flt(
				flt(row.transfer_qty) * flt(row.basic_rate),
				row.precision("basic_amount"),
			)
			total += row.basic_amount
	return total


def _pass(se, stamp=True):
	"""One validate pass as far as valuation goes. ``stamp=False`` is a repost."""
	if stamp:
		allow_zero_valuation(se)
	with patch.object(
		type(se), "set_rate_for_outgoing_items", lambda self, *a, **k: _outgoing(self)
	):
		se.set_basic_rate()


def _fg(se):
	return next(row for row in se.items if row.is_finished_item)


def _klhgx62f1119():
	return _entry(
		"Manufacture",
		[
			_consumed("D-NT-RO-6B-+6.5-7", DIAMOND_QTY, DIAMOND_RATE, REGULAR_STOCK),
			_consumed(
				"M-G-22KT-91.75-Y",
				GOLD_QTY,
				GOLD_RATE,
				CUSTOMER_GOODS,
				customer="GJCU0009",
			),
			_finished("EA02652-001"),
		],
	)


class TestCustomerGoldFinishedGoodValuation(IntegrationTestCase):
	"""No document is saved: these run on an in-memory Stock Entry, so they write nothing."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_klhgx62f1119_pattern_is_valued_on_the_first_pass(self):
		"""CG-F3-003 / F3-06: customer gold + company diamond, the production numbers."""
		se = _klhgx62f1119()
		_pass(se)
		self.assertAlmostEqual(flt(_fg(se).basic_rate), KLHGX62F1119_FG_VALUE, places=2)

	def test_save_then_submit_keeps_the_finished_good_value(self):
		"""CG-F3-002 / F3-02 — the production defect: the second pass used to zero the finished good."""
		se = _klhgx62f1119()
		_pass(se)  # save()
		_pass(se)  # submit()
		self.assertAlmostEqual(
			flt(_fg(se).basic_rate),
			KLHGX62F1119_FG_VALUE,
			places=2,
			msg="the finished good was zeroed on the second validation pass",
		)

	def test_repeated_validation_is_idempotent(self):
		"""F3-03: the valuation policy must give the same answer however many times it runs."""
		se = _klhgx62f1119()
		values = []
		for _ in range(5):
			_pass(se)
			values.append(flt(_fg(se).basic_rate, 2))
		self.assertEqual(
			values, [KLHGX62F1119_FG_VALUE] * 5, msg=f"valuation oscillated: {values}"
		)

	def test_repost_does_not_zero_a_valued_finished_good(self):
		"""CG-F3-004 / §20: a repost re-derives without the before_validate stamp.

		A builder that flags every row it appends (``customer_subcontracting/sub_utils/snc.py``) or a
		row persisted with the flag reaches a repost already flagged and carrying its rate. Repost it
		twice: no zeroing, no oscillation.
		"""
		se = _klhgx62f1119()
		_fg(se).allow_zero_valuation_rate = 1
		_pass(se, stamp=False)
		_pass(se, stamp=False)
		self.assertAlmostEqual(
			flt(_fg(se).basic_rate),
			KLHGX62F1119_FG_VALUE,
			places=2,
			msg="a repost wiped the finished good",
		)

	def test_customer_gold_company_alloy_and_diamond_all_reach_the_finished_good(self):
		"""F3-01: FG = consumed gold + alloy + diamond."""
		se = _entry(
			"Manufacture",
			[
				_consumed(
					"M-G-22KT-91.75-Y",
					GOLD_QTY,
					GOLD_RATE,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_consumed("M-Genia-221", ALLOY_QTY, ALLOY_RATE, REGULAR_STOCK),
				_consumed(
					"D-NT-RO-6B-+6.5-7", DIAMOND_QTY, DIAMOND_RATE, REGULAR_STOCK
				),
				_finished(),
			],
		)
		_pass(se)
		_pass(se)
		expected = sum(flt(r.basic_amount) for r in se.items if r.s_warehouse)
		self.assertAlmostEqual(flt(_fg(se).basic_rate), expected, places=2)
		self.assertGreater(flt(_fg(se).basic_rate), 0)

	def test_company_only_manufacture_is_unchanged(self):
		"""CG-REG-001 / F3-04: company stock is not the subject of this fix."""
		se = _entry(
			"Manufacture",
			[
				_consumed("M-G-22KT-91.75-Y", GOLD_QTY, GOLD_RATE, REGULAR_STOCK),
				_consumed(
					"D-NT-RO-6B-+6.5-7", DIAMOND_QTY, DIAMOND_RATE, REGULAR_STOCK
				),
				_finished(inventory_type=REGULAR_STOCK),
			],
		)
		_pass(se)
		_pass(se)
		self.assertAlmostEqual(flt(_fg(se).basic_rate), KLHGX62F1119_FG_VALUE, places=2)
		self.assertFalse(
			[r.item_code for r in se.items if r.allow_zero_valuation_rate],
			msg="a company row was given the zero-valuation flag",
		)

	def test_customer_gold_alone_is_valued(self):
		"""F3-05: the simplest customer-gold Manufacture."""
		se = _entry(
			"Manufacture",
			[
				_consumed(
					"M-G-22KT-91.75-Y",
					GOLD_QTY,
					GOLD_RATE,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_finished(),
			],
		)
		_pass(se)
		_pass(se)
		self.assertAlmostEqual(
			flt(_fg(se).basic_rate), flt(GOLD_QTY * GOLD_RATE, 2), places=2
		)

	def test_zero_value_customer_only_output_stays_zero_and_stays_flagged(self):
		"""Under Zero Value the customer's metal carries 0, so the finished good legitimately derives 0.

		It must stay 0 -- ERPNext must not reach for the item's valuation rate -- and it must keep the
		flag, which is what lets the ledger accept a zero incoming rate.
		"""
		se = _entry(
			"Manufacture",
			[
				_consumed(
					"M-G-22KT-91.75-Y",
					GOLD_QTY,
					0.0,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_finished(),
			],
		)
		with patch(
			"erpnext.stock.doctype.stock_entry.stock_entry.StockEntry.get_row_valuation_rate",
			side_effect=AssertionError(
				"fell back to the item's valuation rate for a derived zero"
			),
		):
			_pass(se)
			_pass(se)
		self.assertEqual(flt(_fg(se).basic_rate), 0.0)
		self.assertEqual(_fg(se).allow_zero_valuation_rate, 1)

	def test_a_customer_secondary_row_keeps_its_old_behaviour(self):
		"""Secondary / scrap rows are deliberately NOT governed.

		ERPNext prices them from the target warehouse, not from the inputs. Refining returns a
		customer's own stones as Customer Goods scrap that must land at zero value; releasing the flag
		there valued them at a company rate, or threw "Valuation Rate Missing" when the warehouse had
		none. So a customer scrap row keeps the flag and ERPNext's existing cycle, exactly like company
		scrap below.
		"""
		se = _entry(
			"Manufacture",
			[
				_consumed(
					"M-G-22KT-91.75-Y",
					GOLD_QTY,
					GOLD_RATE,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_finished(),
				_row(
					"ML-G-22KT-91.75-Y",
					0.01,
					t_warehouse="SCRAP",
					secondary_item_type="Scrap",
					inventory_type=CUSTOMER_GOODS,
				),
			],
		)
		scrap = se.items[2]
		values = []
		with patch(
			"erpnext.stock.doctype.stock_entry.stock_entry.StockEntry.get_row_valuation_rate",
			return_value=GOLD_RATE,
		):
			for _ in range(2):
				_pass(se)
				values.append(flt(scrap.basic_rate, 6))
		self.assertEqual(
			scrap.allow_zero_valuation_rate, 1, "customer scrap lost its flag"
		)
		self.assertEqual(values, [flt(GOLD_RATE, 6), 0.0])

	def test_a_negative_derivation_is_held_at_zero(self):
		"""Customer refining under Zero Value: inputs cost 0, returned scrap is priced above that.

		ERPNext derives (0 - scrap) / qty, a negative rate, and would accept it. The old flag zeroed it
		on the second pass; the governed row is held at 0 and flagged on every pass instead.
		"""
		se = _entry(
			"Manufacture",
			[
				_consumed(
					"M-G-22KT-91.75-Y",
					GOLD_QTY,
					0.0,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_finished(),
				_row(
					"D-NT-RO-6B-+6.5-7",
					DIAMOND_QTY,
					t_warehouse="SCRAP",
					secondary_item_type="Scrap",
					inventory_type=CUSTOMER_GOODS,
				),
			],
		)
		rates = []
		with patch(
			"erpnext.stock.doctype.stock_entry.stock_entry.StockEntry.get_row_valuation_rate",
			return_value=DIAMOND_RATE,
		):
			for _ in range(3):
				_pass(se)
				rates.append(flt(_fg(se).basic_rate, 6))
		self.assertEqual(
			rates, [0.0, 0.0, 0.0], f"a negative finished-good rate: {rates}"
		)
		self.assertEqual(_fg(se).allow_zero_valuation_rate, 1)
		self.assertEqual(flt(_fg(se).basic_amount), 0.0)

	def test_the_flag_follows_the_rate_the_lane_pricer_leaves(self):
		"""A conversion lane is re-priced after ERPNext; the flag must be decided on the final rate.

		Deciding it first left a pooled rate's "unflagged" on a row the pricer then set to 0, and the
		ledger substitutes a fallback rate for an unflagged zero.
		"""
		se = _entry(
			"Repack",
			[
				_consumed(
					"M-G-24KT-99.9-Y",
					10.0,
					159000.0,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_finished("M-G-22KT-91.75-Y", 10.899),
			],
		)

		def price_lane_to_zero(entry):
			_fg(entry).basic_rate = 0.0
			_fg(entry).basic_amount = 0.0

		with patch(
			"jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry.set_process_loss_produce_rates",
			side_effect=price_lane_to_zero,
		):
			_pass(se)
		self.assertEqual(flt(_fg(se).basic_rate), 0.0)
		self.assertEqual(_fg(se).allow_zero_valuation_rate, 1)

	def test_customer_stock_rows_are_not_newly_stamped(self):
		"""The old stamp covered "Customer Goods" only, and still does."""
		se = _entry(
			"Manufacture",
			[
				_consumed(
					"M-G-22KT-91.75-Y",
					GOLD_QTY,
					GOLD_RATE,
					"Customer Stock",
					customer="C",
				),
				_finished(),
			],
		)
		allow_zero_valuation(se)
		self.assertFalse(se.items[0].allow_zero_valuation_rate)

	def test_company_scrap_rows_behave_exactly_as_before(self):
		"""F3-04: ``doc_events/stock_entry.py`` flags company scrap unconditionally; that stays untouched.

		The row gets a NON-ZERO derivable rate on purpose. With a zero rate, a rule that wrongly
		governed company rows would still re-flag it and pass by coincidence -- this test was once
		exactly that weak. Here, governing it would clear the flag and keep the value on every pass;
		the real, untouched behaviour keeps the flag and follows ERPNext's existing cycle.
		"""
		se = _entry(
			"Manufacture",
			[
				_consumed("M-G-22KT-91.75-Y", GOLD_QTY, GOLD_RATE, REGULAR_STOCK),
				_finished(inventory_type=REGULAR_STOCK),
				_row(
					"ML-G-22KT-91.75-Y",
					0.01,
					t_warehouse="SCRAP",
					secondary_item_type="Scrap",
					allow_zero_valuation_rate=1,
					inventory_type=REGULAR_STOCK,
				),
			],
		)
		scrap = se.items[2]
		values = []
		with patch(
			"erpnext.stock.doctype.stock_entry.stock_entry.StockEntry.get_row_valuation_rate",
			return_value=GOLD_RATE,
		):
			for _ in range(2):
				_pass(se)
				values.append(flt(scrap.basic_rate, 6))
		self.assertEqual(
			scrap.allow_zero_valuation_rate,
			1,
			msg="a company scrap row lost its existing flag",
		)
		self.assertEqual(
			values,
			[flt(GOLD_RATE, 6), 0.0],
			msg=f"company scrap no longer behaves as before: {values}",
		)

	def test_a_manually_priced_customer_output_is_left_alone(self):
		"""``loss_valuation`` prices its own produce rows and relies on the flag to permit a zero."""
		se = _entry(
			"Repack",
			[
				_consumed(
					"M-G-24KT-99.9-Y", 5.0, 0.0, CUSTOMER_GOODS, customer="GJCU0009"
				),
				_row(
					"M-G-22KT-91.75-Y",
					5.45,
					t_warehouse="WIP",
					is_finished_item=1,
					set_basic_rate_manually=1,
					basic_rate=0.0,
					allow_zero_valuation_rate=1,
					inventory_type=CUSTOMER_GOODS,
				),
			],
		)
		_pass(se)
		self.assertEqual(se.items[1].allow_zero_valuation_rate, 1)
		self.assertEqual(se.items[1].set_basic_rate_manually, 1)

	def test_customer_input_rows_keep_the_flag(self):
		"""The flag is right on the rows that bring customer material in."""
		se = _klhgx62f1119()
		_pass(se)
		gold_in = se.items[1]
		self.assertEqual(gold_in.allow_zero_valuation_rate, 1)

	def test_a_derived_rate_is_not_parked_on_custom_metal_rate(self):
		"""F8 at source: the wiped rate used to be parked as the FG's 'metal rate' (8,01,654.99)."""
		se = _klhgx62f1119()
		_pass(se)
		_pass(se)
		self.assertFalse(
			flt(_fg(se).custom_metal_rate),
			msg="a derived finished-good rate was parked on custom_metal_rate",
		)

	def test_a_repack_customer_output_left_to_erpnext_survives_the_second_pass(self):
		"""A Repack produce row that no pricer claims falls back to ERPNext's derivation, not to 0.

		Before this fix a customer produce row in such a lane was valued on the first pass and zeroed on
		the second -- the conversion half of the same defect (MAT-STE-17967).
		"""
		se = _entry(
			"Repack",
			[
				_consumed(
					"M-G-24KT-99.9-Y",
					10.0,
					159000.0,
					CUSTOMER_GOODS,
					customer="GJCU0009",
				),
				_finished("M-G-22KT-91.75-Y", 10.899),
			],
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry.set_process_loss_produce_rates"
		):
			_pass(se)
			_pass(se)
		self.assertAlmostEqual(
			flt(_fg(se).basic_rate), flt(10.0 * 159000.0 / 10.899), places=2
		)

	def test_an_entry_with_no_consumed_row_keeps_the_old_stamping(self):
		"""Where nothing is consumed, ERPNext's item-valuation fallback is not suppressed.

		Releasing the flag there could let ERPNext invent a value for a zero output, so such an entry
		keeps exactly the behaviour it had: a customer finished good is stamped as before.
		"""
		se = _entry("Manufacture", [_finished()])
		allow_zero_valuation(se)
		self.assertEqual(_fg(se).allow_zero_valuation_rate, 1)
