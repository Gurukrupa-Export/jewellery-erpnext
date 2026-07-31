# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""A rate typed on a Customer Goods row must reach the Batch that row mints.

ERPNext's ``set_basic_rate`` clears ``basic_rate`` on every allow-zero-valuation
row, so ``Customer Goods Received`` batches were minted with no Batch Rate and
every Repack-Metal Conversion downstream blended 0 from them. The capture/restore
pair parks the entered rate on ``custom_metal_rate``, which both batch-stamping
readers already prefer over ``basic_rate``.

DB-free per the suite convention: ``setUpClass`` is neutralized and the logic runs
against ``SimpleNamespace`` rows.
"""

from types import SimpleNamespace

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
from jewellery_erpnext.jewellery_erpnext.customization.utils.entered_metal_rate import (
	capture_entered_metal_rates,
	restore_entered_metal_rates,
)


class _Row(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


def _row(**fields):
	defaults = {
		"item_code": "M-G-24KT-99.9-Y",
		"qty": 10.0,
		"transfer_qty": 10.0,
		"s_warehouse": None,
		"t_warehouse": "Customer Goods - GE",
		"basic_rate": 0.0,
		"basic_amount": 0.0,
		"custom_metal_rate": 0.0,
		"allow_zero_valuation_rate": 0,
		"set_basic_rate_manually": 0,
		"inventory_type": "Customer Goods",
		"customer": "TNCU0085",
	}
	defaults.update(fields)
	return _Row(**defaults)


class _SE(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


def _se(rows, purpose="Material Receipt"):
	return _SE(
		doctype="Stock Entry",
		purpose=purpose,
		stock_entry_type="Customer Goods Received",
		items=rows,
	)


def _erpnext_set_basic_rate(se):
	"""The part of ``StockEntry.set_basic_rate`` this module works around.

	Reproduces stock_entry.py's guard clauses and the allow-zero-valuation wipe so
	the tests exercise the real interleaving rather than asserting on the capture
	list in isolation.
	"""
	for row in se.items:
		if row.s_warehouse or row.set_basic_rate_manually:
			continue
		if (
			row.allow_zero_valuation_rate
			and row.basic_rate
			and se.purpose != "Receive from Customer"
		):
			row.basic_rate = 0.0
		row.basic_amount = row.transfer_qty * row.basic_rate


def _run(se):
	captured = capture_entered_metal_rates(se)
	_erpnext_set_basic_rate(se)
	restore_entered_metal_rates(captured)


class TestEnteredMetalRateSurvives(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_customer_goods_rate_is_parked_on_custom_metal_rate(self):
		# KGJPL-SE-CGR-26-00010: 10 g of the customer's 24KT, allow_zero_valuation_rate
		# forced on by inventory_type "Customer Goods". The typed 7308.63 was lost.
		row = _row(basic_rate=7308.625798077, allow_zero_valuation_rate=1)
		_run(_se([row]))

		self.assertEqual(row.custom_metal_rate, 7308.625798077)
		# The ledger stays untouched: the customer's metal is not company value.
		self.assertEqual(row.basic_rate, 0.0)
		self.assertEqual(row.basic_amount, 0.0)

	def test_parked_rate_is_what_the_batch_stamper_reads(self):
		row = _row(basic_rate=7308.625798077, allow_zero_valuation_rate=1)
		se = _se([row])
		_run(se)

		# create_parent_batches / create_child_batches stamp the batch from this.
		self.assertEqual(batch_rename._source_row_rate(se, row), 7308.625798077)

	def test_row_with_no_entered_rate_is_left_alone(self):
		row = _row(allow_zero_valuation_rate=1)
		_run(_se([row]))

		self.assertEqual(row.custom_metal_rate, 0.0)

	def test_regular_stock_row_keeps_basic_rate_and_is_not_parked(self):
		# Nothing zeroes an allow_zero_valuation_rate=0 row, so the existing
		# basic_rate fallback already reaches the right number.
		row = _row(basic_rate=6120.5, inventory_type="Regular Stock", customer=None)
		_run(_se([row]))

		self.assertEqual(row.basic_rate, 6120.5)
		self.assertEqual(row.custom_metal_rate, 0.0)

	def test_consume_row_keeps_the_source_batch_rate(self):
		# custom_metal_rate on a consume row is fetched from the batch being drawn;
		# overwriting it would replace a real batch rate with a transient one.
		row = _row(
			s_warehouse="Customer Goods - GE",
			t_warehouse=None,
			basic_rate=5900.0,
			custom_metal_rate=5900.0,
			allow_zero_valuation_rate=1,
		)
		_run(_se([row]))

		self.assertEqual(row.custom_metal_rate, 5900.0)

	def test_manual_rate_row_is_not_touched(self):
		# set_basic_rate_manually rows are skipped by ERPNext, so basic_rate survives
		# and the Process Loss valuation owns them -- capturing would be a no-op that
		# also parks a rate on a scrap row that never asked for one.
		row = _row(
			basic_rate=5528.4406,
			allow_zero_valuation_rate=1,
			set_basic_rate_manually=1,
		)
		_run(_se([row]))

		self.assertEqual(row.basic_rate, 5528.4406)
		self.assertEqual(row.custom_metal_rate, 0.0)

	def test_receive_from_customer_purpose_keeps_basic_rate(self):
		# ERPNext exempts this purpose from the wipe, so there is nothing to rescue.
		row = _row(basic_rate=7000.0, allow_zero_valuation_rate=1)
		_run(_se([row], purpose="Receive from Customer"))

		self.assertEqual(row.basic_rate, 7000.0)
		self.assertEqual(row.custom_metal_rate, 0.0)

	def test_mixed_rows_are_parked_independently(self):
		customer_row = _row(basic_rate=7308.62, allow_zero_valuation_rate=1)
		own_row = _row(
			item_code="M-G-18KT-75.4-Y",
			basic_rate=2863.598303487,
			transfer_qty=7.56,
			inventory_type="Regular Stock",
			customer=None,
		)
		_run(_se([customer_row, own_row]))

		self.assertEqual(customer_row.custom_metal_rate, 7308.62)
		self.assertEqual(own_row.custom_metal_rate, 0.0)
		self.assertEqual(own_row.basic_rate, 2863.598303487)

	def test_revalidation_does_not_clear_the_parked_rate(self):
		# calculate_rate_and_amount re-runs on every save and on every repost, by
		# which time basic_rate is already 0 and only custom_metal_rate holds the value.
		row = _row(basic_rate=7308.62, allow_zero_valuation_rate=1)
		se = _se([row])
		_run(se)
		_run(se)

		self.assertEqual(row.custom_metal_rate, 7308.62)
