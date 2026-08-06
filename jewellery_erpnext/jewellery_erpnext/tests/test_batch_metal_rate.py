# Copyright (c) 2026, Nirali and contributors
# See license.txt


from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils import batch_metal_rate
from jewellery_erpnext.jewellery_erpnext.customization.utils.batch_metal_rate import (
	apply_batch_metal_rate,
	eligible_rows,
	pin_ledger_valuation_rate,
	reassert_batch_metal_rate,
)

# The ledger rate get_incoming_rate would return for the batch below -- what
# valuation_rate must still equal after the swap.
LEDGER_RATE = 5000.0
BATCH_RATE = 5528.44


class _Row(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


def _row(**fields):
	defaults = {
		"name": "sed-0001",
		"item_code": "M-G-22KT-91.6-Y",
		"qty": 10.0,
		"transfer_qty": 10.0,
		"s_warehouse": "Casting WIP - GE",
		"t_warehouse": None,
		"batch_no": "BATCH-A",
		"basic_rate": LEDGER_RATE,
		"basic_amount": 50000.0,
		"valuation_rate": LEDGER_RATE,
		"additional_cost": 0.0,
		"landed_cost_voucher_amount": 0.0,
		"custom_metal_rate": 0.0,
		"inventory_type": "Customer Goods",
	}
	defaults.update(fields)
	return _Row(**defaults)


class _SE(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


def _se(rows, purpose="Material Issue"):
	return _SE(
		doctype="Stock Entry",
		name="MAT-STE-2026-00001",
		purpose=purpose,
		stock_entry_type="Material Issue",
		items=rows,
	)


def _batch_rates(mapping):
	"""Patch the module's ``bulk_map`` with a fixed ``{batch_no: rate}``."""
	return patch.object(
		batch_metal_rate,
		"bulk_map",
		lambda doctype, names, fields: {
			name: {"custom_metal_rate": mapping[name]}
			for name in names
			if name in mapping
		},
	)


def _erpnext_update_valuation_rate(se, reset_outgoing_rate=True):
	"""``StockEntry.update_valuation_rate``, the part the pin has to undo."""
	for row in se.get("items") or []:
		if not reset_outgoing_rate and row.get("s_warehouse"):
			continue
		if row.get("transfer_qty"):
			extra = row.get("additional_cost") + row.get("landed_cost_voucher_amount")
			row.amount = row.get("basic_amount") + extra
			row.valuation_rate = row.get("basic_rate") + extra / row.get("transfer_qty")


class TestBatchMetalRateMovesToBasicRate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	# --- happy path ---------------------------------------------------------------

	def test_batch_rate_lands_on_basic_rate_and_blanks_the_mirror(self):
		row = _row(custom_metal_rate=BATCH_RATE)
		se = _se([row])

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, BATCH_RATE)
		self.assertEqual(row.basic_amount, 55284.4)
		self.assertEqual(row.custom_metal_rate, 0)

	def test_batch_master_wins_over_a_stale_fetched_mirror(self):
		# get_fifo_batches deep-copies the pre-split row, so every split row inherits the
		# ORIGINAL row's custom_metal_rate under a DIFFERENT batch_no -- and the fetch does
		# not re-run on submit at all. The Batch master is the only reliable source.
		row = _row(batch_no="BATCH-B", custom_metal_rate=1.0)
		se = _se([row])

		with _batch_rates({"BATCH-B": 6120.5}):
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, 6120.5)
		self.assertEqual(row.custom_metal_rate, 0)

	def test_running_twice_is_a_no_op(self):
		row = _row()
		se = _se([row])

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, BATCH_RATE)
		self.assertEqual(row.basic_amount, 55284.4)

	# --- the valuation pin ---------------------------------------------------------

	def test_transfer_row_keeps_the_ledger_valuation_rate(self):
		# A row with both warehouses publishes valuation_rate as the target warehouse's
		# incoming_rate. basic_rate may show the Batch Rate; valuation_rate may not.
		row = _row(t_warehouse="Casting Dept - GE")
		se = _se([row], purpose="Material Transfer")

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)
		_erpnext_update_valuation_rate(se)
		pin_ledger_valuation_rate(se)

		self.assertEqual(row.basic_rate, BATCH_RATE)
		self.assertEqual(row.valuation_rate, LEDGER_RATE)

	def test_pin_carries_additional_cost_the_way_core_does(self):
		row = _row(
			t_warehouse="Casting Dept - GE", additional_cost=250.0, transfer_qty=10.0
		)
		se = _se([row], purpose="Material Transfer")

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)
		_erpnext_update_valuation_rate(se)
		pin_ledger_valuation_rate(se)

		self.assertEqual(row.valuation_rate, LEDGER_RATE + 25.0)

	def test_repost_pass_neither_stashes_nor_pins(self):
		# reset_outgoing_rate=False: core keeps the stored basic_rate (already OUR Batch
		# Rate, not a ledger rate) and skips source rows in update_valuation_rate, so the
		# persisted valuation_rate is authoritative and must survive untouched.
		row = _row(t_warehouse="Casting Dept - GE", valuation_rate=4321.0)
		se = _se([row], purpose="Material Transfer")

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se, reset_outgoing_rate=False)
		_erpnext_update_valuation_rate(se, reset_outgoing_rate=False)
		pin_ledger_valuation_rate(se, reset_outgoing_rate=False)

		self.assertEqual(row.basic_rate, BATCH_RATE)
		self.assertEqual(row.valuation_rate, 4321.0)

	def test_pin_leaves_untouched_rows_alone(self):
		# A produce row carries no stash, so the pin must not invent a valuation_rate.
		row = _row(s_warehouse=None, t_warehouse="Scrap - GE", valuation_rate=777.0)
		se = _se([row])

		pin_ledger_valuation_rate(se)

		self.assertEqual(row.valuation_rate, 777.0)

	# --- scope ---------------------------------------------------------------------

	def test_repack_entry_is_out_of_scope(self):
		# The consume rows' basic_amount IS outgoing_items_cost, which prices the finished
		# rows on every recalculate_amounts_in_stock_entry.
		row = _row()
		se = _se([row], purpose="Repack")

		self.assertEqual(eligible_rows(se), [])

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, LEDGER_RATE)

	def test_manufacture_entry_is_out_of_scope(self):
		se = _se([_row()], purpose="Manufacture")

		self.assertEqual(eligible_rows(se), [])

	def test_produce_row_is_left_alone(self):
		row = _row(s_warehouse=None, t_warehouse="Scrap - GE")
		se = _se([row])

		self.assertEqual(eligible_rows(se), [])

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, LEDGER_RATE)

	# --- degenerate data -----------------------------------------------------------

	def test_batch_without_a_rate_keeps_the_ledger_valuation(self):
		row = _row()
		se = _se([row])

		with _batch_rates({"BATCH-A": 0}):
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, LEDGER_RATE)
		self.assertEqual(row.basic_amount, 50000.0)

	def test_row_without_a_batch_is_not_queried(self):
		row = _row(batch_no=None)
		se = _se([row])

		self.assertEqual(eligible_rows(se), [])

		def _boom(*args, **kwargs):
			raise AssertionError("bulk_map must not be called for a batch-less table")

		with patch.object(batch_metal_rate, "bulk_map", _boom):
			apply_batch_metal_rate(se)

		self.assertEqual(row.basic_rate, LEDGER_RATE)


class TestReassertBatchMetalRate(IntegrationTestCase):
	"""The post-submit defence against SerialBatchBundle's raw db.set_value."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_a_row_the_bundle_overwrote_is_restored(self):
		row = _row(t_warehouse="Casting Dept - GE", basic_rate=BATCH_RATE)
		se = _se([row], purpose="Material Transfer")
		writes = []

		with (
			_batch_rates({"BATCH-A": BATCH_RATE}),
			patch.object(
				batch_metal_rate.frappe,
				"get_all",
				lambda *a, **kw: [
					frappe._dict(name="sed-0001", basic_rate=LEDGER_RATE)
				],
			),
			patch.object(
				batch_metal_rate.frappe.db,
				"set_value",
				lambda dt, dn, field, val, **kw: writes.append((dn, field, val)),
			),
		):
			reassert_batch_metal_rate(se)

		self.assertEqual(writes, [("sed-0001", "basic_rate", BATCH_RATE)])

	def test_an_undrifted_row_is_not_written(self):
		row = _row(t_warehouse="Casting Dept - GE", basic_rate=BATCH_RATE)
		se = _se([row], purpose="Material Transfer")
		writes = []

		with (
			_batch_rates({"BATCH-A": BATCH_RATE}),
			patch.object(
				batch_metal_rate.frappe,
				"get_all",
				lambda *a, **kw: [frappe._dict(name="sed-0001", basic_rate=BATCH_RATE)],
			),
			patch.object(
				batch_metal_rate.frappe.db,
				"set_value",
				lambda *a, **kw: writes.append(a),
			),
		):
			reassert_batch_metal_rate(se)

		self.assertEqual(writes, [])

	def test_repack_entry_is_never_touched(self):
		se = _se([_row()], purpose="Repack")

		def _boom(*args, **kwargs):
			raise AssertionError("a Repack entry must not reach the DB here")

		with patch.object(batch_metal_rate, "bulk_map", _boom):
			reassert_batch_metal_rate(se)


class TestControllerWiring(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_custom_stock_entry_overrides_update_valuation_rate(self):
		"""Without this override the pin never runs and every Material Transfer would
		revalue its inward leg at the Batch Rate -- capitalizing Customer Goods, which
		the ledger carries at 0. The override is what makes the swap safe, so it is
		pinned separately from set_basic_rate."""
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry import (
			CustomStockEntry,
		)

		self.assertIsNot(
			CustomStockEntry.update_valuation_rate, StockEntry.update_valuation_rate
		)
