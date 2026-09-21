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
	METAL_CONVERSION_SE_TYPE,
	PROCESS_LOSS_SE_TYPE,
	REPACK_SE_TYPE,
)

# The types whose produce rows this module may value. They are NOT treated alike -- see
# ``_owns_produce_row`` for why a plain Repack only gets the rows nothing else prices, and
# why a Metal Conversion has its rate REPLACED rather than merely filled.
VALUED_SE_TYPES = (PROCESS_LOSS_SE_TYPE, REPACK_SE_TYPE, METAL_CONVERSION_SE_TYPE)


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


def _owns_produce_row(se, se_type, row):
	"""Whether this module may write the rate on one produce row, by stock entry type.

	**Process Loss: all of them, unconditionally.** Every loss builder leaves the rate
	entirely to this module, and a repost has to re-derive it exactly as the original submit
	did -- so this branch must stay free of any "only if still zero" condition. 18,253 live
	batches were valued through it.

	**Repack: only an auto-created entry's rows carrying ``set_basic_rate_manually`` with no
	``basic_rate``.** That combination is what nothing else in the stack fills in: ERPNext's
	``set_basic_rate`` skips a manual row before reaching both the ``purpose == "Repack"``
	branch and the ``get_row_valuation_rate`` fallback, and posts it at 0 without complaint.
	The flag itself cannot be dropped -- ``validate_repack_entry`` throws when a Repack has
	several distinct finished goods that are not all manual-rate, which is exactly what a
	two-finding ``finding_repack`` entry is.

	All three terms are load-bearing:

	* ``auto_created`` confines this to entries the app built. ``set_basic_rate_manually`` is
	  NOT an internal marker -- it is a plain user-visible checkbox on Stock Entry Detail
	  (``depends_on: eval:parent.purpose==="Repack" && doc.t_warehouse``), shown on every
	  Repack produce row. Without this term, an operator hand-building a Repack who ticks it
	  and deliberately leaves the rate at 0 (a zero-value by-product) would silently be handed
	  the whole consumed value, with nothing in the document to show why.
	* the manual flag: a row without it belongs to ERPNext's
	  ``get_basic_rate_for_repacked_items`` pooling and must not be stolen from it.
	* no ``basic_rate``: a Repack produce row that already has one was priced by its builder on
	  purpose (``_convert_received_scrap_to_scrap_batch`` sets ``basic_rate = val_rate``), and a
	  repost would otherwise silently reprice entries already posted.

	So on a Repack this only ever fills a zero on a voucher the app itself made; it never
	rewrites a real number and never touches a user's own document.
	"""
	if se_type == PROCESS_LOSS_SE_TYPE:
		return True

	if se_type == METAL_CONVERSION_SE_TYPE:
		# **Metal Conversion: every produce row of an app-built entry, REPLACING the rate
		# ERPNext already wrote.** This is the one branch that overwrites a non-zero number,
		# and it has to: ``get_basic_rate_for_repacked_items`` pools the whole voucher's
		# outgoing cost across every finished row by total finished qty, and a conversion is
		# explicitly multi-lane -- ``metal_conversions`` tags each row with
		# ``custom_conversion_lane`` precisely because one entry carries several owners at
		# once. Pooling therefore blends one customer's gold rate into another lane's batch.
		#
		# Measured on MAT-STE-17964: 4 g of a customer's 24KT at 159,000 plus 1.5 g of
		# company 24KT at 15,487.41 produced two rows that BOTH took 109,968.61, the
		# voucher-wide average. The company's 1.635 g absorbed 179,798.67 of value against
		# 23,239.48 of its own inputs -- 156,559.19 of the customer's gold -- while the
		# customer's own row was left at 0 and 479,463.13 went to Stock Adjustment.
		#
		# So "only if still zero" is exactly wrong here: the wrong number is already there.
		# ``auto_created`` still confines this to entries the app itself built, for the same
		# reason it does on a plain Repack.
		return bool(_get(se, "auto_created"))

	return (
		bool(_get(se, "auto_created"))
		and bool(_get(row, "set_basic_rate_manually"))
		and not flt(_get(row, "basic_rate"))
	)


def set_process_loss_produce_rates(se):
	"""Value the produce rows of a Process Loss (or Repack) SE from the rows they consumed.

	No-op for any other stock entry type. Runs AFTER ERPNext's ``set_basic_rate``, so the
	consume rows already carry their ``basic_amount`` -- on a fresh submit from
	``set_rate_for_outgoing_items`` -> ``get_incoming_rate``, and on a repost
	(``reset_outgoing_rate=False``) from the persisted rate. ``update_valuation_rate`` and
	``set_total_incoming_outgoing_value`` then run next in ``calculate_rate_and_amount``,
	so ``valuation_rate``, ``amount`` and the header totals follow automatically.

	``Repack`` is included for the ``finding_repack`` engine, which consumes casting-tree
	metal and produces finding items under the plain ``Repack`` type.

	``Repack-Metal Conversion`` is included because a conversion is multi-lane by design and
	ERPNext prices it single-lane. ``iter_loss_runs`` already splits the item table into
	consume/produce runs and ``_allocate`` already apportions by ``(inventory_type,
	customer)`` -- which is exactly what ``custom_conversion_lane`` encodes -- so each lane
	ends up carrying the value its own rows gave up, and the voucher stops smearing one
	customer's gold across another owner's batch. See ``_owns_produce_row`` for how the
	three types differ.

	Nothing here branches on the Customer Gold valuation policy, and it does not need to.
	Under **Nominal** a customer's consume row carries the booked rate, so the lane carries
	it forward. Under **Zero Value** that row carries 0, so a lane fed only by the customer's
	own metal allocates 0 and its produce row stays 0, as today.

	A Zero-Value lane that ALSO consumes company alloy is the one case worth naming: it comes
	out at the alloy's value alone, not 0. A 4.36 g lane taking 0.36 g of alloy at 62.00
	produces 22.32 over 4.36 g = 5.1193. That is the point of value conservation rather than
	a policy switch -- the company really did put 22.32 into that batch, and the alternative
	is writing it off to Stock Adjustment.
	"""
	se_type = _get(se, "stock_entry_type")
	if se_type not in VALUED_SE_TYPES:
		return

	for consumed, produced in iter_loss_runs(_get(se, "items")):
		precision = _amount_precision(produced[0])
		valued = []
		unowned = 0

		# Allocation is computed across the WHOLE run, so each produce row still gets the
		# share its own owner group consumed; rows this module does not own are then simply
		# left unwritten. Filtering before _allocate would hand their share to the others
		# and over-value them.
		for row, share in zip(produced, _allocate(consumed, produced)):
			if not _owns_produce_row(se, se_type, row):
				unowned += 1
				continue
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

		if unowned:
			# Part of this run is priced by someone else, so the consumed total is NOT the
			# total of the rows written here and the balancing below would park another
			# writer's share onto ours. Leave the rounding residue rather than mis-assign it.
			# ``unowned`` is always 0 for Process Loss (every produce row is owned) and for
			# the finding repack (every produce row is manual and unrated), so this keeps
			# both of those paths on the balancing branch exactly as before; it guards only
			# the mixed Repack run that becomes possible now the type is in scope.
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
