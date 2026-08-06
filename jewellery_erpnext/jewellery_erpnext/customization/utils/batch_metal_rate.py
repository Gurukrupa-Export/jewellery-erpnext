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
			# Skipped when False: on the repost path the stored basic_rate is already OUR
			# Batch Rate, and update_valuation_rate ignores source rows in that mode anyway.
			setattr(row, _LEDGER_RATE_ATTR, flt(row.get("basic_rate")))

		# Do not round off the rate, for the same reason ERPNext's own set_basic_rate does
		# not: at jewellery quantities the rounding dominates the amount.
		row.basic_rate = rate
		row.basic_amount = flt(
			flt(row.get("transfer_qty")) * rate, _amount_precision(row)
		)
		row.custom_metal_rate = 0


def pin_ledger_valuation_rate(se, reset_outgoing_rate=True):
	"""Restore ``valuation_rate`` to the number ERPNext computed, on every row rewritten above.

	Called from ``CustomStockEntry.update_valuation_rate`` after super(), which has just
	derived ``valuation_rate`` from the Batch Rate we substituted. Re-applies core's own
	formula with the displaced ledger rate put back.

	No-op when ``reset_outgoing_rate`` is False, because core's ``update_valuation_rate``
	``continue``s on every source row in that mode -- the persisted value is authoritative
	there and must not be overwritten from a stale stash.
	"""
	if not reset_outgoing_rate:
		return

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
