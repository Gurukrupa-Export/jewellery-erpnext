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
	hold_ledger_valuation_rates,
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
		flags=frappe._dict(),
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


def _erpnext_set_rate_for_outgoing_items(
	se, reset_outgoing_rate=True, ledger_rate=LEDGER_RATE
):
	"""``StockEntry.set_rate_for_outgoing_items`` -- the step apply_batch_metal_rate follows.

	With ``reset_outgoing_rate=False`` core does NOT re-read the ledger; it recomputes
	basic_amount from whatever basic_rate was persisted. That is the interleaving the repost
	tests turn on, and it was previously unmodelled here.
	"""
	for row in se.get("items") or []:
		if not row.get("s_warehouse"):
			continue
		if reset_outgoing_rate:
			row.basic_rate = ledger_rate
		row.basic_amount = round(row.get("transfer_qty") * row.get("basic_rate"), 2)


def _calculate_rate_and_amount(se, reset_outgoing_rate=True, ledger_rate=LEDGER_RATE):
	"""``StockEntry.calculate_rate_and_amount`` plus CustomStockEntry's two overrides.

	Faithful on the one detail every repost bug here turns on: core hands
	``reset_outgoing_rate`` to ``set_basic_rate`` (erpnext stock_entry.py:1431) but then calls
	``self.update_valuation_rate()`` with NO argument (:1434), so the flag is dropped and the
	valuation pass always arrives as True -- which is why the override cannot key off the
	caller's mode and holds ``valuation_rate`` across super() instead.
	"""
	_erpnext_set_rate_for_outgoing_items(se, reset_outgoing_rate, ledger_rate)
	apply_batch_metal_rate(se, reset_outgoing_rate)
	_custom_update_valuation_rate(se)


def _custom_update_valuation_rate(se):
	"""``CustomStockEntry.update_valuation_rate``.

	Always entered as True -- ``calculate_rate_and_amount`` drops the flag it was given
	(erpnext stock_entry.py:1434), and ``RepostItemValuation._recalculate_valuation_rate``
	calls it with no ``set_basic_rate`` pass at all.
	"""
	held = hold_ledger_valuation_rates(se)
	_erpnext_update_valuation_rate(se, True)
	pin_ledger_valuation_rate(se, held)


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
			_calculate_rate_and_amount(se)

		self.assertEqual(row.basic_rate, BATCH_RATE)
		self.assertEqual(row.valuation_rate, LEDGER_RATE)

	def test_pin_carries_additional_cost_the_way_core_does(self):
		row = _row(
			t_warehouse="Casting Dept - GE", additional_cost=250.0, transfer_qty=10.0
		)
		se = _se([row], purpose="Material Transfer")

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			_calculate_rate_and_amount(se)

		self.assertEqual(row.valuation_rate, LEDGER_RATE + 25.0)

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


class TestRepostPass(IntegrationTestCase):
	"""Every pass that recomputes ``valuation_rate`` without having just re-read the ledger:
	``stock_ledger.recalculate_amounts_in_stock_entry`` (the ``reset_outgoing_rate=False``
	pass) and ``RepostItemValuation._recalculate_valuation_rate`` (no ``set_basic_rate`` pass
	at all).

	Two things make these their own hazard. Core hands the flag to ``set_basic_rate`` but calls
	``update_valuation_rate()`` bare (erpnext stock_entry.py:1431 vs :1434), so its own "skip
	source rows" guard never fires and it happily re-derives ``valuation_rate`` from whatever
	``basic_rate`` holds -- which here is the Batch Rate. And nothing on these passes re-reads
	the ledger, so the ledger rate is no longer anywhere on the doc to pin from. So the
	override snapshots the ``valuation_rate`` pinned at submit before super() recomputes it,
	and restores it per row -- per row, so a row whose batch has no Batch Rate was never
	rewritten and keeps the value core just computed for it.

	Known limitation, deliberately not covered by a test because it is core behaviour we do
	not control: ``update_rate_on_stock_entry`` overwrites ``basic_rate`` unconditionally but
	only calls the recalc when ``dependant_sle_voucher_detail_no`` is unset. That field IS set
	for any source row carrying a ``t_warehouse`` (stock_entry.py:2128-2129), so on a Material
	Transfer a Repost Item Valuation leaves ``basic_rate`` at the ledger rate with no recalc to
	restore the Batch Rate. Valuation stays correct; only the display drifts.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_repost_does_not_leak_the_batch_rate_into_valuation_rate(self):
		# THE regression test. valuation_rate is what get_sle_for_target_warehouse and
		# SerialandBatchBundle.set_incoming_rate_for_inward_transaction read, so letting the
		# Batch Rate reach it revalues the inward leg -- the one outcome this module exists
		# to prevent. The row arrives as the repost loads it: basic_rate already the Batch
		# Rate we persisted at submit, valuation_rate the ledger rate we pinned.
		row = _row(
			t_warehouse="Casting Dept - GE",
			basic_rate=BATCH_RATE,
			basic_amount=55284.4,
			valuation_rate=LEDGER_RATE,
		)
		se = _se([row], purpose="Material Transfer")

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			_calculate_rate_and_amount(se, reset_outgoing_rate=False)

		self.assertEqual(row.valuation_rate, LEDGER_RATE)
		self.assertNotEqual(row.valuation_rate, BATCH_RATE)

	def test_repost_steady_state_leaves_the_row_unchanged(self):
		# Nothing overwrote basic_rate, so core recomputes basic_amount from the persisted
		# Batch Rate and the pass writes back the identical numbers. This is the assertion
		# the review asked for -- and it holds.
		row = _row(
			basic_rate=BATCH_RATE, basic_amount=55284.4, valuation_rate=LEDGER_RATE
		)
		se = _se([row])
		before = (row.basic_rate, row.basic_amount, row.valuation_rate)

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			_calculate_rate_and_amount(se, reset_outgoing_rate=False)

		self.assertEqual((row.basic_rate, row.basic_amount, row.valuation_rate), before)

	def test_repost_restores_the_batch_rate_after_a_ledger_overwrite(self):
		# update_rate_on_stock_entry does a raw db.set_value of basic_rate and THEN reloads
		# the doc, so this pass is the only place the Batch Rate can come back before
		# d.db_update() persists the row. Gating the pass off on repost -- the fix the review
		# proposed -- makes every Repost Item Valuation revert the column permanently.
		row = _row(
			basic_rate=LEDGER_RATE, basic_amount=50000.0, valuation_rate=LEDGER_RATE
		)
		se = _se([row])

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			_calculate_rate_and_amount(se, reset_outgoing_rate=False)

		self.assertEqual(row.basic_rate, BATCH_RATE)
		self.assertEqual(row.basic_amount, 55284.4)

	def test_repost_refreshes_a_row_whose_batch_has_no_rate(self):
		# The hold is per ROW, not per document. Row B's batch carries no Batch Rate, so
		# apply_batch_metal_rate never rewrote it -- its basic_rate is the fresh ledger rate
		# update_rate_on_stock_entry just wrote, and core's recomputed valuation_rate for it
		# is correct and must survive. A doc-wide skip would freeze it at the stale value and
		# feed that back into the target warehouse at stock_ledger.py:1241.
		rated = _row(basic_rate=BATCH_RATE, valuation_rate=LEDGER_RATE)
		unrated = _row(
			name="sed-0002",
			batch_no="BATCH-B",
			basic_rate=28000.0,
			valuation_rate=30000.0,
		)
		se = _se([rated, unrated])

		with _batch_rates({"BATCH-A": BATCH_RATE, "BATCH-B": 0}):
			_calculate_rate_and_amount(se, reset_outgoing_rate=False)

		self.assertEqual(rated.valuation_rate, LEDGER_RATE)
		self.assertEqual(unrated.valuation_rate, 28000.0)

	def test_repost_item_valuation_cannot_leak_the_batch_rate(self):
		# RepostItemValuation._recalculate_valuation_rate (repost_item_valuation.py:347-353)
		# calls update_valuation_rate() with NO set_basic_rate pass, then db_sets the result --
		# so there is no stash to pin from and nothing declares the mode. Reachable from the
		# "Create Reposting Entries" button on the Stock and Account Value Comparison report.
		row = _row(
			t_warehouse="Casting Dept - GE",
			basic_rate=BATCH_RATE,
			basic_amount=55284.4,
			valuation_rate=LEDGER_RATE,
		)
		se = _se([row], purpose="Material Transfer")

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			_custom_update_valuation_rate(se)

		self.assertEqual(row.valuation_rate, LEDGER_RATE)
		self.assertNotEqual(row.valuation_rate, BATCH_RATE)

	def test_nothing_is_held_when_a_fresh_stash_exists(self):
		# A normal pass carries the exact displaced ledger rate, so the hold must stand aside
		# and let the stash win -- otherwise a submit would pin the row's pre-save valuation.
		row = _row()
		se = _se([row])

		with _batch_rates({"BATCH-A": BATCH_RATE}):
			apply_batch_metal_rate(se)

		self.assertEqual(hold_ledger_valuation_rates(se), {})


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
