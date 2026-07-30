# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Ownership-lane split for a single Metal Conversion.

One source qty is FIFO-allocated across whatever batches the source warehouse
holds, which may span several ownerships at once. Each distinct
``(inventory_type, customer)`` in that allocation is a **lane**: it converts to
the target purity on its own and lands in its own target batch, because a
Regular Stock target batch and a customer's target batch are different stock and
must never be merged into one.

Everything here is a pure function -- no DB access, no Document API -- so the
whole split is unit-testable without a site, which is how this suite works.

The two invariants the callers rely on:

* ``sum(lane["target_qty"]) == total target qty`` exactly, and
* ``sum(lane["alloy_qty"]) == total alloy qty`` exactly,

at the document's own precision. Rounding each lane independently does NOT give
that, so the residual is absorbed by the largest lane (the same convention the
SNC warehouse split uses) and the alloy is *derived* from the reconciled targets
rather than computed a second time from the purities.
"""

from frappe.utils import flt

REGULAR_STOCK = "Regular Stock"


def lane_key(inventory_type, customer):
	"""Canonical, None-safe ownership key.

	An empty ``custom_inventory_type`` means company stock, not a third kind of
	ownership -- normalising here is what keeps untyped batches allocatable.
	"""
	return (inventory_type or REGULAR_STOCK, customer or None)


def build_lanes(allocations, lane_map):
	"""Group FIFO allocations into ownership lanes.

	``allocations`` -- rows carrying ``qty`` and ``batch`` (i.e.
	``source_batch_details``), in FIFO order.
	``lane_map`` -- ``{batch_no: (inventory_type, customer)}`` as returned by
	``doc_events.utils.get_batch_lane_map``.

	Lanes come back ordered by each lane's **first appearance** in the FIFO
	sequence, so the Stock Entry rows are emitted in the order the metal was
	actually drawn. Each lane is::

	    {
	        "inventory_type": str,
	        "customer": str | None,
	        "source_qty": float,
	        "batches": [{"batch": str, "qty": float}, ...],
	    }
	"""
	lanes = {}
	order = []

	for row in allocations:
		get = row.get if hasattr(row, "get") else (lambda f, d=None: getattr(row, f, d))
		batch = get("batch") or get("batch_no")
		qty = flt(get("qty"))
		if not batch or qty <= 0:
			continue

		key = lane_key(*lane_map.get(batch, (REGULAR_STOCK, None)))
		if key not in lanes:
			lanes[key] = {
				"inventory_type": key[0],
				"customer": key[1],
				"source_qty": 0.0,
				"batches": [],
			}
			order.append(key)

		lane = lanes[key]
		lane["source_qty"] = flt(lane["source_qty"] + qty)
		lane["batches"].append({"batch": batch, "qty": qty})

	return [lanes[key] for key in order]


def apportion(total, weights, precision=3):
	"""Split ``total`` across ``weights`` so the parts sum EXACTLY to ``total``.

	Each part is ``total * weight / sum(weights)`` rounded at ``precision``; the
	rounding residual is then added to the part with the largest weight. Returns
	a list parallel to ``weights``.

	A zero (or negative) total splits to all zeros, and an empty/zero weight set
	returns zeros rather than dividing by zero.
	"""
	weights = [flt(w) for w in weights]
	if not weights:
		return []

	total_weight = sum(weights)
	if not total_weight:
		return [0.0 for _ in weights]

	parts = [flt(flt(total) * w / total_weight, precision) for w in weights]

	residual = flt(flt(total, precision) - sum(parts), precision)
	if residual:
		largest = weights.index(max(weights))
		parts[largest] = flt(parts[largest] + residual, precision)

	return parts


def split_conversion(lanes, target_qty, precision=3):
	"""Fill each lane's ``target_qty`` and ``alloy_qty`` in place; return ``lanes``.

	The target is apportioned by each lane's source qty -- which is exactly
	``lane_source * source_purity / target_purity``, since that factor is shared
	by every lane -- and reconciled so the parts sum to ``target_qty``.

	The alloy is then *derived* as ``lane_target - lane_source``, which makes
	``sum(lane_alloy) == target_qty - source_qty`` true by construction. Deriving
	it (rather than re-deriving from the purities and rounding again) also handles
	the case where a small lane's alloy legitimately rounds to zero while the
	whole lot's does not: that lane simply gets no alloy row.

	The alloy *sign* is necessarily uniform across lanes, because source and
	target purity are shared -- a lane can be zero, but it can never be opposite.
	"""
	targets = apportion(target_qty, [lane["source_qty"] for lane in lanes], precision)

	for lane, lane_target in zip(lanes, targets):
		lane["target_qty"] = lane_target
		lane["alloy_qty"] = flt(lane_target - lane["source_qty"], precision)

	return lanes


def split_allocations(allocations, needs, precision=3):
	"""Hand out one FIFO allocation pool across several ``needs``, in order.

	Used for the alloy: ``update_alloy_betch`` allocates the whole
	``source_alloy_qty`` as a single FIFO list, but each lane must consume its own
	share as its own Stock Entry row -- otherwise the alloy is unattributable and
	the per-lane Batch Rate blend cannot be reconstructed.

	Returns a list parallel to ``needs``; each element is the ``[{batch, qty}]``
	funding that need. A need of zero (or less) gets an empty list. Whatever the
	pool cannot cover is simply not handed out -- the caller already validated
	availability via ``get_alloy_bailance``.
	"""
	tolerance = 1.0 / (10 ** (precision + 1))
	pool = []
	for row in allocations:
		get = row.get if hasattr(row, "get") else (lambda f, d=None: getattr(row, f, d))
		batch = get("batch") or get("batch_no")
		qty = flt(get("qty"))
		if batch and qty > 0:
			pool.append([batch, qty])

	out = []
	cursor = 0
	for need in needs:
		remaining = flt(need, precision)
		rows = []
		while remaining > tolerance and cursor < len(pool):
			batch, available = pool[cursor]
			take = flt(min(remaining, available), precision)
			if take > tolerance:
				rows.append({"batch": batch, "qty": take})
				remaining = flt(remaining - take, precision)
			pool[cursor][1] = flt(available - take, precision)
			if pool[cursor][1] <= tolerance:
				cursor += 1
		out.append(rows)

	return out
