# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt


import frappe
from frappe.utils import flt

from jewellery_erpnext.utils import bulk_map

# Purposes whose produce rows are priced from the consume rows' basic_amount
# (outgoing_items_cost, re-derived from the persisted rate on every repost).
PRODUCE_PRICED_FROM_CONSUMPTION = ("Repack", "Manufacture")

# Where apply_batch_metal_rate parks the rate it displaced, for pin_ledger_valuation_rate to
# read back. A non-meta attribute: get_valid_dict drops it, so it never reaches the DB.
_LEDGER_RATE_ATTR = "_batch_metal_rate_ledger_rate"

# Every pass that recomputes valuation_rate does it as basic_rate + extras, so on any pass that
# did NOT just re-read the ledger it publishes the Batch Rate this module persisted. Three core
# entry points are that shape and none can be told apart by a flag:
#   * calculate_rate_and_amount hands reset_outgoing_rate to set_basic_rate but calls
#     self.update_valuation_rate() with NO argument (erpnext stock_entry.py:1434), so core's own
#     "if not reset_outgoing_rate and d.s_warehouse: continue" skip never fires on a repost;
#   * RepostItemValuation._recalculate_valuation_rate (repost_item_valuation.py:347-353) calls
#     update_valuation_rate() with no set_basic_rate pass at all, then db_sets the result;
#   * both feed stock_ledger.py:1241, which reads valuation_rate straight back off the row.
# So the defence lives entirely in the update_valuation_rate override and holds the previous
# value rather than trusting any caller to declare its mode.


def _amount_precision(row):
	"""Precision for ``basic_amount``; 2 for a plain namespace row (tests)."""
	precision = getattr(row, "precision", None)
	if not callable(precision):
		return 2
	return precision("basic_amount") or 2


def eligible_rows(se):
	"""Source rows whose consumed batch may carry its Batch Rate on ``basic_rate``.

	Empty for a Repack / Manufacture entry. A produce row is never eligible -- a rate typed
	on one is handled by the sibling ``entered_metal_rate`` pass, and there is no batch to
	read a rate from anyway.
	"""
	if se.get("purpose") in PRODUCE_PRICED_FROM_CONSUMPTION:
		return []

	return [
		row
		for row in se.get("items") or []
		if row.get("s_warehouse") and row.get("batch_no")
	]


def _batch_rate_map(rows):
	"""``{batch_no: rate}`` in one query, matching the prefetch convention in update_batches."""
	rates = bulk_map(
		"Batch", [row.get("batch_no") for row in rows], ["custom_metal_rate"]
	)
	return {name: flt(row.get("custom_metal_rate")) for name, row in rates.items()}


def apply_batch_metal_rate(se, reset_outgoing_rate=True):
	"""Move each consumed batch's Batch Rate onto ``basic_rate`` and blank the mirror.

	Runs LAST in ``CustomStockEntry.set_basic_rate`` -- after ``set_process_loss_produce_rates``,
	so that pass still values its produce rows from the ledger rate its strict
	value-conservation policy is built on -- and before ``distribute_additional_costs`` /
	``update_valuation_rate`` in ``calculate_rate_and_amount``.

	A batch with no Batch Rate is left alone: the entry keeps the valuation ERPNext already
	computed, because inventing a rate here would book a phantom gain (same policy as
	``loss_valuation``).
	"""
	rows = eligible_rows(se)
	if not rows:
		return

	rates = _batch_rate_map(rows)

	for row in rows:
		rate = rates.get(row.get("batch_no")) or 0.0
		if not rate:
			continue

		if reset_outgoing_rate:
			# super().set_basic_rate has just re-derived this from the ledger via
			# get_incoming_rate, so it is the number valuation_rate must end up back at.
			# On a repost it did not, and basic_rate is the Batch Rate persisted at submit --
			# useless to pin from, so no stash and hold_ledger_valuation_rates takes over.
			setattr(row, _LEDGER_RATE_ATTR, flt(row.get("basic_rate")))

		# Do not round off the rate, for the same reason ERPNext's own set_basic_rate does
		# not: at jewellery quantities the rounding dominates the amount.
		row.basic_rate = rate
		row.basic_amount = flt(
			flt(row.get("transfer_qty")) * rate, _amount_precision(row)
		)
		row.custom_metal_rate = 0


def hold_ledger_valuation_rates(se):
	"""Snapshot ``valuation_rate`` on the rows core is about to re-derive from the Batch Rate.

	Called at the TOP of ``CustomStockEntry.update_valuation_rate``, before super() runs. On a
	pass that did not just re-read the ledger there is no ledger rate left anywhere on the doc,
	so the only surviving one is the ``valuation_rate`` pinned when the entry was submitted --
	hold it here and ``pin_ledger_valuation_rate`` puts it back.

	Returns ``{}`` when a fresh ``set_basic_rate`` pass left a stash: there the displaced ledger
	rate is exact and nothing needs holding.
	"""
	rows = eligible_rows(se)
	if not rows:
		return {}

	if any(getattr(row, _LEDGER_RATE_ATTR, None) is not None for row in rows):
		return {}

	return {id(row): flt(row.get("valuation_rate")) for row in rows}


def pin_ledger_valuation_rate(se, held=None):
	"""Put ``valuation_rate`` back to the ledger figure super() just overwrote.

	Two sources, most precise first. A row stashed by ``apply_batch_metal_rate`` carries the
	exact rate core derived from the ledger moments earlier, so core's own formula is re-applied
	to it. Otherwise the pass never re-read the ledger, and ``held`` carries the pin from submit.

	The restore is per row, not per document: a row whose batch has no Batch Rate was never
	rewritten, so its ``basic_rate`` is still the ledger's and the value core just computed for
	it is correct and must stand.
	"""
	for row in se.get("items") or []:
		ledger_rate = getattr(row, _LEDGER_RATE_ATTR, None)
		if ledger_rate is None:
			continue

		transfer_qty = flt(row.get("transfer_qty"))
		if not transfer_qty:
			continue

		# core: valuation_rate = basic_rate + (additional_cost + landed_cost) / transfer_qty
		row.valuation_rate = (
			flt(ledger_rate)
			+ (
				flt(row.get("additional_cost"))
				+ flt(row.get("landed_cost_voucher_amount"))
			)
			/ transfer_qty
		)

	if not held:
		return

	rows = [row for row in eligible_rows(se) if id(row) in held]
	rates = _batch_rate_map(rows)
	for row in rows:
		if rates.get(row.get("batch_no")):
			row.valuation_rate = held[id(row)]


def reassert_batch_metal_rate(se):
	"""Re-write ``basic_rate`` after the stock ledger has run. Called from ``on_submit``.

	Covers the one path the controller cannot reach. When an outward Serial and Batch Bundle
	is auto-created on a Material Transfer / Send to Subcontractor / Material Transfer for
	Manufacture, ``SerialBatchBundle.set_serial_and_batch_bundle`` writes ``basic_rate`` AND
	``valuation_rate`` onto the Stock Entry Detail row with a raw ``frappe.db.set_value`` --
	no controller involved, so nothing above can defend the Batch Rate.

	Its ``valuation_rate`` (the bundle's ``avg_rate`` plus additional cost) is exactly the
	ledger number ``pin_ledger_valuation_rate`` is holding, so it is deliberately left alone;
	only ``basic_rate`` is re-asserted, and only on rows that actually drifted.
	"""
	rows = eligible_rows(se)
	if not rows:
		return

	rates = _batch_rate_map(rows)
	wanted = {}
	for row in rows:
		rate = rates.get(row.get("batch_no")) or 0.0
		if rate and row.get("name"):
			wanted[row.name] = rate

	if not wanted:
		return

	# One read for the whole item table; in the common case (the row already carried a
	# bundle, so none was auto-created) nothing drifted and this writes nothing.
	for stored in frappe.get_all(
		"Stock Entry Detail",
		filters={"name": ["in", list(wanted)]},
		fields=["name", "basic_rate"],
		order_by=None,
	):
		rate = wanted[stored.name]
		if flt(stored.basic_rate) == flt(rate):
			continue

		frappe.db.set_value(
			"Stock Entry Detail",
			stored.name,
			"basic_rate",
			rate,
			update_modified=False,
		)
