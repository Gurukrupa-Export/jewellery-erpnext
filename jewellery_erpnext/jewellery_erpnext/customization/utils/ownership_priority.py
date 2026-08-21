# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Ownership-aware ordering and tiered allocation for metal consumption and loss.

Companion to :mod:`row_ownership`, which decides *what* a row's
``(inventory_type, customer)`` is. This module decides *which* rows get picked
first, and how a loss budget is spread across them.

Two directions, deliberately inverted:

* **Consume / add metal** -- ``CONSUME_PRIORITY``: Customer Goods first, then
  Regular Stock, then Pure Metal. A job draws down the customer's own metal
  before the company's.
* **Book loss** -- ``LOSS_PRIORITY``: Regular Stock first, then Pure Metal, then
  Customer Goods. Wastage lands on company metal; a customer's gold is written
  off only when nothing else has capacity.

``tiered_allocate`` is the waterfall that implements the loss direction: it fills
each tier proportionally, caps a tier at its own balance, and carries the
remainder to the next tier. A single-tier input is byte-identical to a flat
proportional split, so a site with no customer-owned batches sees no change.

**Deliberate exemptions -- do not "fix" these to use this module:**

* Metal Conversions melting loss (``metal_conversions/doc_events/utils.py``)
  books the ML row to the company by policy, on an operator-declared
  per-document ownership. ``row_ownership.validate_loss_ownership_carried``
  documents that this is legitimate, not a builder bug.
* The EOD sync (``mop_settings/mop_eod_sync.py``) is a pass-through: it moves the
  exact batch its MOP Log names, in full. A priority branch there would desync
  ``qty_after_transaction_batch_based``.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
	DEFAULT_INVENTORY_TYPE,
	normalize_ownership,
)

# Lower rank = picked first.
CONSUME_PRIORITY = {
	"Customer Goods": 0,
	"Customer Stock": 0,
	"Regular Stock": 1,
	"Pure Metal": 2,
}
LOSS_PRIORITY = {
	"Regular Stock": 0,
	"Pure Metal": 1,
	"Customer Goods": 2,
	"Customer Stock": 2,
}

# Any loss allocation landing at or beyond this rank means a customer absorbed
# wastage -- the trigger for the operator warning. NOT "the waterfall overflowed":
# the ordinary business case (regular stock exhausted, remainder on customer gold)
# leaves no overflow at all and must still warn.
CUSTOMER_LOSS_RANK = 2

# A `Customer.custom_no_wastage` batch sorts behind every ordinary customer, so the
# loss waterfall reaches it only when nothing else on the operation has capacity.
# That is exactly where validate_process_loss' existing hard throw is the right
# answer -- the operator must return the full weight. Ranking it merely "last among
# customers" would let a routine spill turn a warning into a blocked submit.
NO_WASTAGE_RANK = 8

# Consume direction only: an inventory type we do not know about must never be
# preferred over the customer metal we are trying to drain first.
UNKNOWN_RANK = 9


def consume_rank(inventory_type):
	"""Pick order for consuming/adding metal. Unknown types sort last."""
	return CONSUME_PRIORITY.get(inventory_type, UNKNOWN_RANK)


def loss_rank(inventory_type, no_wastage=False):
	"""Pick order for booking loss.

	A blank or unresolvable ``inventory_type`` ranks as Regular Stock, not last.
	That is not leniency: ``MOP Log.batch_no`` is a ``Data`` field with no
	referential integrity, so a loss row whose batch cannot be resolved is
	routine, and every other default in the app (``row_ownership``'s
	``DEFAULT_INVENTORY_TYPE``, ``doc_events/stock_entry.before_validate``'s
	blanket fill) treats a blank owner as the company's. Sorting it last would
	quietly push unattributable loss onto customer gold.
	"""
	if no_wastage:
		return NO_WASTAGE_RANK
	if not inventory_type:
		return LOSS_PRIORITY[DEFAULT_INVENTORY_TYPE]
	return LOSS_PRIORITY.get(inventory_type, LOSS_PRIORITY[DEFAULT_INVENTORY_TYPE])


def is_customer_rank(rank):
	"""True when ``rank`` denotes customer-owned metal (incl. no-wastage)."""
	return rank >= CUSTOMER_LOSS_RANK


def batch_priority_map(batch_nos, with_no_wastage=False):
	"""``{batch_no: frappe._dict(inventory_type, customer, creation, no_wastage)}``.

	ONE round-trip for a whole allocation. ``creation`` is fetched because it is
	the FIFO tie-break *within* a tier and no caller can get it elsewhere:
	``capped_auto_batch_nos`` rebuilds its rows with only batch_no/qty/warehouse,
	which is why the callers used to re-query ``Batch`` just for it.

	Tolerates rows without ``name``/``creation`` -- tests stub ``frappe.db.get_all``
	globally and hand back rows shaped for a different query.

	``with_no_wastage`` adds one further round-trip over the distinct customers to
	resolve ``Customer.custom_no_wastage``; skipped by default because most callers
	do not rank on it.
	"""
	batch_nos = sorted({b for b in (batch_nos or []) if b})
	if not batch_nos:
		return {}

	rows = (
		frappe.db.get_all(
			"Batch",
			filters={"name": ["in", batch_nos]},
			fields=["name", "creation", "custom_inventory_type", "custom_customer"],
		)
		or []
	)

	out = {}
	for r in rows:
		name = r.get("name") if hasattr(r, "get") else getattr(r, "name", None)
		if not name:
			continue
		get = r.get if hasattr(r, "get") else lambda k: getattr(r, k, None)
		out[name] = frappe._dict(
			inventory_type=get("custom_inventory_type"),
			customer=get("custom_customer"),
			creation=get("creation") or "",
			no_wastage=False,
		)

	if with_no_wastage:
		customers = sorted({m.customer for m in out.values() if m.customer})
		if customers:
			flagged = {
				c["name"]
				for c in (
					frappe.db.get_all(
						"Customer",
						filters={"name": ["in", customers], "custom_no_wastage": 1},
						fields=["name"],
					)
					or []
				)
				if c.get("name")
			}
			for meta in out.values():
				if meta.customer in flagged:
					meta.no_wastage = True

	return out


def batch_sort_key(batch_no, meta, mode="consume"):
	"""The ownership rank of ``batch_no`` -- and ONLY the rank.

	``meta`` is a ``batch_priority_map`` entry, or ``None`` for a batch that could
	not be resolved.

	Deliberately not ``(rank, creation, batch_no)``. Every pool this ranks is
	already in FIFO order when it arrives -- ``capped_auto_batch_nos`` returns
	``Batch.creation`` order, and ``_tree_owed_batches`` preserves it -- so a
	rank-only key plus Python's **stable** sort re-buckets by ownership while
	leaving FIFO untouched inside each bucket. Adding ``creation`` as an inner term
	looks harmless but is not: a batch whose metadata cannot be resolved has no
	creation, falls back to the empty string, and the batch name then decides the
	order, silently replacing FIFO with alphabetical. Callers that need an explicit
	FIFO guarantee sort by creation FIRST and by rank second -- two stable sorts
	compose exactly the right way round.
	"""
	meta = meta or frappe._dict(inventory_type=None, no_wastage=False)
	if mode == "loss":
		return loss_rank(meta.get("inventory_type"), meta.get("no_wastage"))
	return consume_rank(meta.get("inventory_type"))


def sort_batches(candidates, ranks, batch_of=None, mode="consume"):
	"""Re-bucket ``candidates`` by ownership tier, preserving their order within a tier.

	``ranks`` is a ``batch_priority_map``. ``batch_of`` extracts the batch_no from a
	candidate (defaults to ``candidate.batch_no``). Returns a new list; the input is
	not mutated. Stable, so whatever order the caller supplied survives inside each
	tier -- see ``batch_sort_key``.
	"""
	if batch_of is None:

		def batch_of(c):
			return (
				c.get("batch_no") if hasattr(c, "get") else getattr(c, "batch_no", None)
			)

	return sorted(
		candidates,
		key=lambda c: batch_sort_key(batch_of(c), ranks.get(batch_of(c)), mode),
	)


def allocate_in_order(pool, need, precision, qty_of=None, taken=None):
	"""Greedily take ``need`` from an already-ordered ``pool``.

	``pool`` is ``[(key, available)]`` or a list of objects plus ``qty_of``.
	``taken`` is an optional ``{key: qty}`` ledger of quantities a previous pass
	already claimed, so two passes can share one pool without double-booking.

	Returns ``([(key, qty)], shortfall)``. The caller decides whether a shortfall
	is fatal -- some paths throw, some fall back.
	"""
	eps = _eps(precision)
	taken = taken if taken is not None else {}
	out = []
	remaining = flt(need, precision)
	for entry in pool:
		if remaining <= eps:
			break
		if qty_of is None:
			key, avail = entry
		else:
			key, avail = entry, qty_of(entry)
		free = flt(flt(avail) - flt(taken.get(key)), precision)
		if free <= eps:
			continue
		take = flt(min(free, remaining), precision)
		if take <= eps:
			continue
		out.append((key, take))
		taken[key] = flt(taken.get(key)) + take
		remaining = flt(remaining - take, precision)
	return out, max(remaining, 0.0)


def _eps(precision):
	"""Half a unit at ``precision`` -- the same float-dust guard the SE builders use."""
	return (10 ** -int(precision)) / 2


def tiered_allocate(rows, total, rank_of, qty_of, precision=3, key_of=None):
	"""Spread ``total`` across ``rows`` tier by tier, proportionally within a tier.

	Walks ranks ascending. Each tier absorbs at most its own capacity
	(``take = min(remaining, tier_qty)``) and splits that share proportionally by
	each row's balance; whatever is left flows to the next tier.

	Returns ``(allocations, info)`` where ``allocations`` is ``{key: qty}`` and
	``info`` carries ``ranks_touched`` (a sorted list) and ``overflow`` (qty that
	no tier could cover).

	Guarantees, all load-bearing:

	* ``sum(allocations.values()) == flt(total, precision)`` **exactly**.
	  ``validate_loss_tables_required`` asserts the booked total matches the
	  ``gross_wt - received_gross_wt`` baseline within 0.0005, so the residual
	  reconciliation below is mandatory, not cosmetic.
	* A single funded tier reproduces the legacy flat proportional split **row for
	  row**, not merely in total. That needs three things to match: the per-tier cap
	  is bypassed (the legacy split let a row exceed its own balance when the loss
	  did), each row is rounded independently rather than making the last row absorb
	  the tier remainder, and the residual anchors on the FIRST maximal row in input
	  order -- which is what ``max()`` returned in the legacy code.
	* Row order within a tier is the caller's input order, which is already
	  deterministic (the MOP balance is read ``order_by="creation asc"``). Preserving
	  it is what makes cancel + resubmit reproduce the same allocation *and* keeps
	  the legacy parity above.
	* Overflow (``total`` exceeds every tier's capacity combined) anchors on the
	  **first** funded tier, never the last. Under ``LOSS_PRIORITY`` the last tier is
	  the customer; anchoring there would drive a customer row's
	  ``received_gross_weight`` negative and mint a customer-owned scrap batch for
	  metal that never existed. The company absorbs what cannot be attributed.
	"""
	precision = int(precision or 3)
	eps = _eps(precision)
	target = flt(total, precision)
	info = frappe._dict(ranks_touched=[], overflow=0.0)
	if not rows or target <= eps:
		return {}, info

	if key_of is None:

		def key_of(row):
			return row.get("key") if hasattr(row, "get") else getattr(row, "key", None)

	tiers = {}
	for row in rows:
		qty = flt(qty_of(row))
		if qty <= 0:
			continue
		tiers.setdefault(rank_of(row), []).append((key_of(row), qty))

	if not tiers:
		return {}, info

	ordered_ranks = sorted(tiers)
	# A lone funded tier must behave exactly like the pre-waterfall flat split,
	# which never capped a row at its own balance.
	single_tier = len(ordered_ranks) == 1

	allocations = {}
	funded_ranks = []
	remaining = target
	for rank in ordered_ranks:
		if remaining <= eps:
			break
		tier = tiers[rank]
		tier_qty = flt(sum(q for _k, q in tier), precision)
		if tier_qty <= eps:
			continue
		take = remaining if single_tier else flt(min(remaining, tier_qty), precision)
		if take <= eps:
			continue
		_split_tier_proportionally(allocations, tier, tier_qty, take, precision)
		funded_ranks.append(rank)
		remaining = flt(remaining - take, precision)

	if not funded_ranks:
		return {}, info

	info.ranks_touched = sorted(funded_ranks)
	if remaining > eps:
		info.overflow = remaining

	# Anchor every residual -- per-tier rounding crumbs and any genuine overflow --
	# on the first funded tier, so the sum lands on target without pushing
	# unattributable weight onto customer metal. `max` over the tier in input order
	# returns the FIRST maximal row, matching the legacy reconciliation exactly.
	distributed = flt(sum(allocations.values()), precision)
	residual = flt(target - distributed, precision)
	if residual:
		anchor_keys = [k for k, _q in tiers[funded_ranks[0]]]
		anchor = max(anchor_keys, key=lambda k: flt(allocations.get(k)))
		allocations[anchor] = flt(flt(allocations.get(anchor)) + residual, precision)

	return {k: v for k, v in allocations.items() if flt(v, precision) > 0}, info


def _split_tier_proportionally(allocations, tier, tier_qty, take, precision):
	"""Split ``take`` across one tier by each row's share of ``tier_qty``.

	Each row is rounded independently -- deliberately NOT "last row absorbs the
	remainder". The legacy flat split rounded independently and let the global
	anchor mop up, and single-tier parity requires reproducing that row for row.
	"""
	for key, qty in tier:
		share = flt(take * (qty / tier_qty), precision)
		if share <= 0:
			continue
		allocations[key] = flt(flt(allocations.get(key)) + share, precision)


# ---------------------------------------------------------------------------
# Produce-row ownership
# ---------------------------------------------------------------------------


def _row_to_dict(row):
	"""Copy a row (dict or child Document) into a plain append-able dict."""
	drop = ("name", "idx", "owner", "creation", "modified")
	if isinstance(row, dict):
		return {k: v for k, v in row.items() if k not in drop}
	d = row.as_dict()
	for k in drop:
		d.pop(k, None)
	return d


def _row_set(row, field, value):
	"""Write a field on a row that may be a plain dict or already a child Document."""
	if isinstance(row, dict):
		row[field] = value
	else:
		setattr(row, field, value)


def produce_rows_for_run(produce_row, run, precision=3, row_to_dict=None):
	"""Stamp ``produce_row`` from the consume ``run`` that feeds it, splitting on mixed owners.

	Returns the row(s) that should replace ``produce_row``. The single-owner case
	-- the norm -- stamps in place and returns ``[produce_row]``, so the caller can
	skip rebuilding the item table entirely.

	A row carries exactly one owner, so a produce row fed by batches belonging to
	different owners is split pro-rata into one row per owner; the last split
	absorbs the rounding remainder so the consume/produce pair stays balanced.
	"""
	row_to_dict = row_to_dict or _row_to_dict

	# Group the run's consumed qty by owner, preserving FIFO order.
	by_owner = {}
	for c in run:
		key = (c.get("inventory_type") or None, c.get("customer") or None)
		by_owner[key] = by_owner.get(key, 0.0) + flt(c.get("qty"))

	total = sum(by_owner.values())
	if not total:
		return [produce_row]

	owners = list(by_owner.items())

	if len(owners) == 1:
		(inv, cust), _qty = owners[0]
		inv, cust = normalize_ownership(
			inv, cust, item_code=produce_row.get("item_code")
		)
		_row_set(produce_row, "inventory_type", inv)
		_row_set(produce_row, "customer", cust)
		return [produce_row]

	# Resolved lazily: only a mixed-owner split needs it, and the single-owner path
	# (the norm) must not pay for a meta lookup it never uses.
	if precision is None:
		precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3

	produce_qty = flt(produce_row.get("qty"))
	out = []
	remaining = produce_qty
	for i, ((inv, cust), consumed) in enumerate(owners):
		inv, cust = normalize_ownership(
			inv, cust, item_code=produce_row.get("item_code")
		)
		if i == len(owners) - 1:
			qty = flt(remaining, precision)
		else:
			qty = flt(produce_qty * (consumed / total), precision)
			remaining -= qty
		if qty <= 0:
			continue
		d = row_to_dict(produce_row)
		d["qty"] = qty
		d["transfer_qty"] = qty
		d["inventory_type"] = inv
		d["customer"] = cust
		out.append(frappe._dict(d))
	return out or [produce_row]


def stamp_produce_rows_from_consumes(se, precision=3, row_to_dict=None):
	"""Carry each consumed batch's ownership onto the produce row it feeds.

	The bug this exists to prevent: a builder resolves batches on its CONSUME rows
	(so those carry the right owner) but leaves the PRODUCE row bare, because the
	FIFO expander short-circuits any row without an ``s_warehouse``. A bare produce
	row is then blanket-defaulted to ``Regular Stock`` by
	``doc_events/stock_entry.before_validate``, and the batch minted from it -- which
	reads ``inventory_type`` / ``customer`` straight off that row via
	``Batch.custom_voucher_detail_no`` -- silently stops being the customer's.

	Applies to any consume/produce builder: ``Process Loss`` write-offs and purity
	``Repack`` conversions alike. Rows are walked as ``[consume..., produce]`` runs.

	Returns ``True`` when the item table had to be rebuilt (a mixed-owner split),
	``False`` when every produce row was stamped in place.
	"""
	row_to_dict = row_to_dict or _row_to_dict
	rebuilt = []
	run = []
	split_needed = False

	for row in list(se.items):
		s_wh, t_wh = row.get("s_warehouse"), row.get("t_warehouse")
		if s_wh and not t_wh:
			run.append(row)
			rebuilt.append(row)
			continue
		if t_wh and not s_wh and run:
			produced = produce_rows_for_run(row, run, precision, row_to_dict)
			split_needed = split_needed or len(produced) > 1
			rebuilt.extend(produced)
			run = []
			continue
		rebuilt.append(row)
		run = []

	if not split_needed:
		# Single owner per produce row: produce_rows_for_run already stamped it in
		# place, so the item table is untouched and needs no rebuild.
		return False

	se.set("items", [])
	for d in rebuilt:
		se.append("items", row_to_dict(d))
	return True


def describe_customer_spill(spill_rows, precision=3):
	"""Human-readable ``customer / item / batch / qty`` lines for the spill warning.

	``spill_rows`` is an iterable of dicts carrying ``customer``, ``item_code``,
	``batch_no`` and ``qty``.
	"""
	lines = []
	for row in spill_rows:
		get = row.get if hasattr(row, "get") else lambda k: getattr(row, k, None)
		lines.append(
			"{0} &mdash; {1} / {2}: {3}".format(
				frappe.bold(get("customer") or _unknown()),
				get("item_code") or "",
				get("batch_no") or "",
				flt(get("qty"), int(precision or 3)),
			)
		)
	return lines


def _unknown():
	from frappe import _

	return _("Unknown Customer")


def is_customer_owned(inventory_type):
	"""True when ``inventory_type`` is one of the customer-owned values."""
	return inventory_type in CUSTOMER_INVENTORY_TYPES
