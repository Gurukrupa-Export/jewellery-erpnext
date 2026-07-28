# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Valuation for the produce rows of a "Process Loss" (Repack) Stock Entry.

A Process Loss SE consumes metal out of a WIP/MSL warehouse and produces the mapped
loss/scrap variant (``ML-*`` / ``FL-*``) into a Scrap warehouse. Every builder stamps
``set_basic_rate_manually = 1`` on the produce row -- and none of them supplies a
``basic_rate``. ERPNext's ``StockEntry.set_basic_rate`` opens with::

    if d.s_warehouse or d.set_basic_rate_manually:
        continue

so a manual-rate row skips BOTH the Repack rate calculation and the trailing
``d.basic_amount = transfer_qty * basic_rate``. The row reaches ``update_valuation_rate``
with ``basic_rate = basic_amount = 0``, and that zero is what
``get_sle_for_target_warehouse`` writes as the Stock Ledger Entry's ``incoming_rate``.
The consumed metal's value leaves the ledger and nothing replaces it: the scrap item's
"Avg Rate (Balance Stock)" is diluted on every loss booking, and an item whose only
receipts are Process Loss sits at valuation_rate 0 outright.

The flag itself cannot be dropped. ``validate_repack_entry`` throws when a Repack has
multiple distinct finished-good items that are not all manual-rate, and the Employee IR
engine deliberately emits ONE combined SE covering every loss row. Dropping the flag
would also route the rows through ``get_basic_rate_for_repacked_items``, which pools all
outgoing cost across all FG rows by total FG qty -- smearing 18KT rates onto 22KT scrap.
So the rate has to be computed per consume/produce pair and assigned explicitly, which
is what this module does.

Policy: **strict value conservation**. The produce row takes exactly the value its
consume rows gave up, so the SE is value-neutral (``value_difference == 0``) and posts no
Stock Adjustment write-off. There is deliberately NO fallback to the Bin or Item rate: a
source batch that is itself 0-valued (batch-wise valuation with no value history) yields
a 0-valued loss row, because inventing value here would book a phantom gain. Customer
Goods rows (``allow_zero_valuation_rate = 1``) legitimately conserve 0 the same way.

Called from ``CustomStockEntry.set_basic_rate`` -- see
``customization/stock_entry/stock_entry.py`` -- rather than from each builder, because
ERPNext re-derives Repack rates on every repost: an incoming SLE on a Repack voucher
forces ``get_dynamic_incoming_outgoing_rate`` -> ``recalculate_amounts_in_stock_entry``,
which re-runs ``calculate_rate_and_amount`` on a freshly loaded doc and reads
``valuation_rate`` straight back off the Stock Entry Detail row. A fix applied before
``insert()`` in a builder would be silently reverted to 0 there. Hooking the controller
also covers all six producers at once (Employee IR, Employee Loss Entry, Metal
Conversions melting loss, Tree Number, Warehouse loss, Main Slip).
"""

from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	PROCESS_LOSS_SE_TYPE,
)


def _get(row, fieldname):
	"""Read ``fieldname`` off a row that may be a dict or a Document/namespace."""
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _set(row, fieldname, value):
	"""Write ``fieldname`` on a row that may be a dict or a Document/namespace."""
	if isinstance(row, dict):
		row[fieldname] = value
	else:
		setattr(row, fieldname, value)


def _amount_precision(row):
	"""Precision for ``basic_amount``; 2 for a plain dict row (tests / pre-insert dicts)."""
	precision = getattr(row, "precision", None)
	if not callable(precision):
		return 2
	return precision("basic_amount") or 2


def _is_consume(row):
	return bool(_get(row, "s_warehouse")) and not _get(row, "t_warehouse")


def _is_produce(row):
	return bool(_get(row, "t_warehouse")) and not _get(row, "s_warehouse")


def _owner_key(row):
	"""``(inventory_type, customer)`` -- the split key ``_produce_rows_for_run`` uses."""
	return (_get(row, "inventory_type") or None, _get(row, "customer") or None)


def iter_loss_runs(items):
	"""Yield ``(consume_rows, produce_rows)`` pairs from a Process Loss item table.

	A "run" is consecutive consume rows (source only) followed by consecutive produce
	rows (target only). Every builder emits that shape: Employee IR alternates
	consume/produce per loss row, melting loss emits N consume rows then one produce row,
	and the tree / warehouse builders emit ``[consume..., produce]`` groups. A row that is
	neither (both warehouses set, or neither) breaks the run and is skipped -- the same
	recognition ``row_ownership.validate_loss_ownership_carried`` and
	``warehouse_stock_entry._stamp_loss_produce_rows`` already rely on.

	A trailing run with no produce row is not yielded: there is nothing to value.
	"""
	consumed = []
	produced = []

	for row in items or []:
		if _is_consume(row):
			if produced:
				# A new consume row closes the previous run.
				yield consumed, produced
				consumed, produced = [], []
			consumed.append(row)
		elif _is_produce(row):
			produced.append(row)
		else:
			if consumed and produced:
				yield consumed, produced
			consumed, produced = [], []

	if consumed and produced:
		yield consumed, produced


def _row_value(row):
	"""The UNROUNDED value a consume row gives up.

	``basic_rate * transfer_qty``, not the row's ``basic_amount``: ERPNext has already
	rounded ``basic_amount`` to currency precision, and at jewellery quantities that
	rounding dominates the rate we derive from it. A 0.005 g loss booked at 5528.4406
	carries ``basic_amount`` 27.64, and 27.64 / 0.005 is 5528.00 -- a 0.44 error in the
	rate that lands straight in the Stock Ledger (MAT-STE-116356). Falls back to
	``basic_amount`` for the degenerate case of an amount with no rate.
	"""
	rate = flt(_get(row, "basic_rate"))
	if rate:
		return rate * flt(_get(row, "transfer_qty"))
	return flt(_get(row, "basic_amount"))


def _spread(shares, indexes, produced, value):
	"""Add ``value`` across ``indexes`` of ``shares``, pro-rata by ``transfer_qty``.

	The last index takes ``value`` minus what the others got, so the split sums back to
	``value`` exactly rather than to a float-drifted approximation of it.
	"""
	qtys = [flt(_get(produced[i], "transfer_qty")) for i in indexes]
	total_qty = sum(qtys)
	running = 0.0
	for n, idx in enumerate(indexes):
		if n == len(indexes) - 1 or not total_qty:
			share = value - running
		else:
			share = value * (qtys[n] / total_qty)
		shares[idx] += share
		running += share


def _allocate(consumed, produced):
	"""Return the unrounded value each produce row should carry.

	One produce row takes the run's whole value -- the shape every builder but one emits.
	Several produce rows only arise from ``warehouse_stock_entry._produce_rows_for_run``,
	which splits a run's produce row pro-rata BY OWNER, so match on
	``(inventory_type, customer)`` and hand each owner group its own consumed value
	(pro-rata by qty if that owner somehow has more than one produce row). Value belonging
	to a consume owner that no produce row claims is spread across the rows that matched
	nothing, or across every produce row when they all matched -- either way the run stays
	balanced.
	"""
	total = sum(_row_value(c) for c in consumed)

	if len(produced) == 1:
		return [total]

	consumed_by_owner = {}
	for c in consumed:
		key = _owner_key(c)
		consumed_by_owner[key] = consumed_by_owner.get(key, 0.0) + _row_value(c)

	produced_by_owner = {}
	for idx, p in enumerate(produced):
		produced_by_owner.setdefault(_owner_key(p), []).append(idx)

	shares = [0.0] * len(produced)
	unmatched = []
	for key, indexes in produced_by_owner.items():
		if key in consumed_by_owner:
			_spread(shares, indexes, produced, consumed_by_owner.pop(key))
		else:
			unmatched.extend(indexes)

	leftover = total - sum(shares)
	if leftover:
		_spread(
			shares, sorted(unmatched) or list(range(len(produced))), produced, leftover
		)

	return shares


def set_process_loss_produce_rates(se):
	"""Value the produce rows of a Process Loss SE from the rows they consumed.

	No-op for any other stock entry type. Runs AFTER ERPNext's ``set_basic_rate``, so the
	consume rows already carry their ``basic_amount`` -- on a fresh submit from
	``set_rate_for_outgoing_items`` -> ``get_incoming_rate``, and on a repost
	(``reset_outgoing_rate=False``) from the persisted rate. ``update_valuation_rate`` and
	``set_total_incoming_outgoing_value`` then run next in ``calculate_rate_and_amount``,
	so ``valuation_rate``, ``amount`` and the header totals follow automatically.
	"""
	if _get(se, "stock_entry_type") != PROCESS_LOSS_SE_TYPE:
		return

	for consumed, produced in iter_loss_runs(_get(se, "items")):
		precision = _amount_precision(produced[0])
		valued = []

		for row, share in zip(produced, _allocate(consumed, produced)):
			qty = flt(_get(row, "transfer_qty"))
			if qty <= 0:
				# set_transfer_qty() guarantees a positive transfer_qty on a submittable
				# row; guard anyway so a malformed row cannot raise ZeroDivisionError and
				# take down the whole submit.
				continue
			# Do not round off basic rate to avoid precision loss (same rationale as
			# ERPNext's own set_basic_rate). The Stock Ledger's incoming_rate comes from
			# this rate via update_valuation_rate -> get_sle_for_target_warehouse, so it
			# is the number that actually has to be right.
			_set(row, "basic_rate", share / qty)
			_set(row, "basic_amount", flt(share, precision))
			valued.append(row)

		if not valued:
			continue

		# Keep the SE header exactly balanced. total_outgoing_value sums the consume rows'
		# already-rounded basic_amount, so independently rounding the produce shares can
		# drift a paisa and leave a stray Stock Adjustment posting on a Repack that is
		# meant to be value-neutral. Park the difference on the largest row.
		booked = flt(sum(flt(_get(c, "basic_amount")) for c in consumed), precision)
		delta = flt(
			booked - sum(flt(_get(r, "basic_amount")) for r in valued), precision
		)
		if delta:
			biggest = max(valued, key=lambda r: abs(flt(_get(r, "basic_amount"))))
			_set(
				biggest,
				"basic_amount",
				flt(flt(_get(biggest, "basic_amount")) + delta, precision),
			)
