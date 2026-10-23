"""Customer Goods Return — the "Settle" button.

A customer-goods-return Material Request transfers the customer's metal out of
``set_from_warehouse``. When that warehouse does not hold enough of the customer's
material at the required purity, the "Settle" button sources the shortfall INTO it,
exactly the way the ``Create SNC`` flow settles borrowed gold:

  * **Case 1 — already there.** ``set_from_warehouse`` holds enough. Nothing to do.
  * **Case 2 — same purity elsewhere.** Transfer the customer's gold in from the other
    warehouse, then transfer an equal quantity of REGULAR (company) gold back to it, so
    that warehouse ends whole in quantity — it lent the customer's gold and got company
    gold in its place.
  * **Case 3 — only a different purity elsewhere.** Convert that purity to the required
    one (customer gold) and, mirrored with regular gold, keep every purity/warehouse
    physically balanced, then the Case 2 transfer + reverse.

Batch scope. When the Material Request row names a batch -- the incoming batch of the
receipt being returned -- Settle works on that batch alone. "Already there" counts only
that batch in ``set_from_warehouse`` (the request's transfer is batch-strict), and the
shortfall is sourced only from the batch itself or metal produced from it
(``customer_gold_trace.discover_scope``). Another receipt's batch of the same customer
never counts and is never used. A row without a batch keeps the customer-wide behaviour.
The request's own target warehouse is never a source. When no single holding of the batch
covers a row -- part of it converted, part left as it was -- Settle combines its holdings,
each part settled the way a single source is (_plan_parts).

Part of the batch's metal may have been issued to a work order. When the Raw Material
warehouses fall short and that order is a company-gold one whose operation has not
started, Settle takes the metal back first -- the same borrow it makes from any other
warehouse: the customer's batch comes out and an equal weight of Regular Stock goes in, so
the work order ends whole -- and settles from Raw Material as usual. That holds while the
work order's IR moves still wait for the end-of-day sync: the pull is written into them, so
the sync moves the regular batch on instead. When the lineage still
cannot cover a batch row, the refusal lists where the batch's metal is and how much the row
can be settled for.

Nothing is posted when a conversion would produce customer metal of an item without the
Customer Goods flag: the Batch guard would refuse that batch inside the first submit.

Scope is deliberately narrow (confirmed with the user): Settle ONLY tops up
``set_from_warehouse``. It does not submit the Material Request and does not create the
In-Transit / End-Transit entries — the user drives those with the existing buttons.

Like ``create_snc``, this runs synchronously on the button click and goes through the
canonical lock-order helpers. It reuses the SNC owner-batch finder (with its per-run
allocation guard) and the pre-submit consumable-balance assertion.
"""

import math

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, flt, now_datetime

from jewellery_erpnext.customer_subcontracting.customer_goods_eligibility import (
	CUSTOMER_GOODS_FLAG_LABEL,
	can_be_customer_goods,
)
from jewellery_erpnext.customer_subcontracting.sub_utils.snc import (
	PURITY_PRIORITY,
	_append_item,
	_consumable_batch_qty_map,
	_get_gold_items_for_purity,
	_get_item_purity,
	_get_purity_label,
	_get_raw_material_warehouses,
	_mwo_needs_settlement,
	_submit_consuming_stock_entry,
	find_owner_rm_warehouse,
)

CUSTOMER_GOODS = "Customer Goods"
REGULAR_STOCK = "Regular Stock"

# Warehouse-to-warehouse metal moves within the company. ``auto_created = 1`` makes
# CustomStockEntry.update_batches a no-op (no FIFO refetch, no department gate), so the
# batches we pre-fill move as-is; _submit_consuming_stock_entry still asserts the
# consumable balance up front.
#
# The transfer must be a DIRECT source -> target move, created and submitted in one
# entry -- NOT routed through a transit warehouse. On this site "Material Transfer
# (DEPARTMENT)" and "Customer Goods Transfer" both carry add_to_transit = 1 on the Stock
# Entry Type, which the Stock Entry field fetches (fetch_if_empty, default 0) so it can't
# be held at 0 while that type is used. The plain base "Material Transfer" type is
# add_to_transit = 0, giving the direct one-shot transfer.
TRANSFER_SE_TYPE = "Material Transfer"
REPACK_SE_TYPE = "Repack-Metal Conversion"

TOLERANCE = 0.001

# Sourcing outcomes recorded per row (for the response / logging).
LEVEL_AVAILABLE = "Available"
LEVEL_CONVERT_IN_PLACE = "ConvertInPlace"
LEVEL_SAME_PURITY = "SamePurity"
LEVEL_CONVERT = "Convert"
# The batch's metal sits in a work order: take it back first, then settle as above.
LEVEL_WORK_ORDER = "WorkOrder"

# The one operation state Settle takes metal back from: issued to the operation, nobody
# working on it yet, so the metal still sits in the department's work-order warehouse.
OPERATION_NOT_STARTED = "Not Started"


@frappe.whitelist()
def settle_material_request(mr_name):
	"""Top up ``set_from_warehouse`` for every short row of a return Material Request."""
	mr = frappe.get_doc("Material Request", mr_name)
	mr.check_permission("write")
	_validate_settleable(mr)

	plan = _build_plan(mr)

	actionable = [p for p in plan if p["level"] != LEVEL_AVAILABLE]
	if not actionable:
		return {
			"settled": [],
			"message": _("Source warehouse already holds enough — nothing to settle."),
		}

	_check_customer_goods_flag(actionable)
	# A row settled in parts appears once per part.
	settled = list(dict.fromkeys(p["row"] for p in actionable))

	pulled = []
	if any(p["level"] == LEVEL_WORK_ORDER for p in actionable):
		# Take the batch's metal back from the work orders first, then plan again on the
		# ledger as it now stands: the rows settle exactly as if the metal had been in Raw
		# Material all along. The second plan never takes from a work order again.
		pulled = _pull_from_work_orders(mr, actionable)
		plan = _build_plan(mr, from_work_orders=False)
		actionable = [p for p in plan if p["level"] != LEVEL_AVAILABLE]
		_check_customer_goods_flag(actionable)

	if actionable:
		_execute(mr, actionable)

	frappe.logger().info(
		f"cg_settle: settled {len(settled)} row(s) on {mr.name} "
		f"({', '.join(str(row) for row in settled)}), "
		f"{len(pulled)} holding(s) taken back from work orders"
	)
	if pulled:
		frappe.msgprint(_pull_summary(pulled), title=_("Taken Back from Work Order"))
	return {
		"settled": settled,
		"pulled": [
			{"work_order": p.work_order, "batch_no": p.batch_no, "qty": p.qty}
			for p in pulled
		],
		"message": _("Settled {0} row(s).").format(len(settled)),
	}


def _validate_settleable(mr):
	# Settle tops up the source warehouse with independent Stock Entries; it never
	# saves the Material Request, so it is valid on a draft OR a submitted MR — only a
	# cancelled one is rejected.
	if mr.docstatus == 2:
		frappe.throw(_("Settle is not available on a cancelled Material Request."))
	if mr.material_request_type != "Material Transfer":
		frappe.throw(_("Settle is only available for a Material Transfer."))
	if not mr.get("items"):
		frappe.throw(_("Material Request has no items to settle."))


# ---------------------------------------------------------------------------
# Planning (read-only): decide, per row, whether/where to source the shortfall.
# ---------------------------------------------------------------------------


def _build_plan(mr, from_work_orders=True):
	"""``from_work_orders`` -- whether a batch row the Raw Material warehouses cannot cover
	may be planned to take its metal back from a work order (LEVEL_WORK_ORDER)."""
	company = mr.company
	all_rm = _get_raw_material_warehouses(company)

	# Batch-level reservations across every sourcing find in this run, so two rows can
	# never be handed the same source batch. Kept separate: how much of the PRE-EXISTING
	# target stock earlier rows have already earmarked (material sourced in for a row is
	# earmarked for that row, so it must not count as free for a later row).
	allocated = {}
	consumed_target = {}
	# {batch_no: lineage} -- rows naming the same batch share one discover_scope walk.
	lineage_cache = {}

	plan = []
	for row in mr.items:
		planned = _plan_row(
			row,
			mr,
			company,
			all_rm,
			allocated,
			consumed_target,
			lineage_cache,
			from_work_orders,
		)
		# A row covered from several of its batch's holdings comes back as one entry per
		# part, each a single-source settle _execute runs as it is.
		plan.extend(planned if isinstance(planned, list) else [planned])
	return plan


def _plan_row(
	row,
	mr,
	company,
	all_rm,
	allocated,
	consumed_target,
	lineage_cache,
	from_work_orders=True,
):
	target = row.get("from_warehouse") or mr.get("set_from_warehouse")
	if not target:
		frappe.throw(
			_(
				"Row {0}: a source warehouse (Set From Warehouse) is required to Settle."
			).format(row.idx)
		)

	customer = row.get("customer") or mr.get("customer") or mr.get("_customer")
	if not customer:
		frappe.throw(
			_("Row {0}: a customer is required to settle customer goods.").format(
				row.idx
			)
		)

	item = row.item_code
	required = flt(row.qty, 3)
	mr_batch = row.get("batch_no")
	# The request's own destination is never a source: metal pulled from there into the
	# source warehouse would only be moved straight back by the request's transfer, leaving
	# the balancing regular gold displaced.
	dest = row.get("warehouse") or mr.get("set_warehouse")

	if mr_batch:
		# Batch scope: the request's transfer moves THIS batch, so only this batch counts
		# as already there, and only this batch's own metal may make up the shortfall.
		_validate_row_batch(row, mr_batch, item, customer)
		lineage = _batch_lineage(mr_batch, customer, lineage_cache)
		have = _batch_qty_in_warehouse(mr_batch, item, target, consumed_target)
	else:
		lineage = None
		have = _owner_qty_in_warehouse(customer, item, target, consumed_target)
	shortfall = flt(required - have, 3)

	base = {
		"row": row.idx,
		"item": item,
		"customer": customer,
		"target": target,
		"required": required,
		"shortfall": shortfall,
		# The batch the MR references. The MR's own transfer is batch-strict (the
		# MR -> Stock Entry mapper copies this onto the transfer row), so the sourced
		# material must land in THIS batch, not a fresh child batch. None => let the
		# child-batch minter name it (fallback).
		"mr_batch": mr_batch,
	}

	if shortfall <= TOLERANCE:
		# Case 1 — enough of the customer's material already in the target warehouse.
		_claim_have(
			consumed_target, customer, item, target, mr_batch, min(required, have)
		)
		return dict(base, level=LEVEL_AVAILABLE)

	# Whatever the target already holds is earmarked for this row.
	_claim_have(consumed_target, customer, item, target, mr_batch, have)

	others = [w for w in all_rm if w not in (target, dest)]
	pure_short = _pure_weight(row, item, shortfall, required)

	# Convert-in-place — the customer's OWN convertible purity is already sitting IN the
	# target warehouse (the common return case: their 24KT was converted to 18KT for
	# manufacturing and never left Central). Convert it back in place, with no borrowing
	# from another warehouse; a regular-gold mirror repack (below) keeps each purity
	# physically balanced. Preferred over borrowing from elsewhere. Aggregates across
	# batches, since the converted metal is usually spread over several.
	in_target = _gather_convertible(
		customer, item, pure_short, target, company, allocated, batch_nos=lineage
	)
	if in_target:
		# The customer conversion is systematic only (no physical melt), so mirror it with
		# regular gold (item -> item2 in the target) to keep each purity's system total
		# matching physical — the same balancing the cross-warehouse case does.
		reg = _reserve_regular(item, shortfall, company, target, allocated, row.idx)
		return dict(
			base,
			level=LEVEL_CONVERT_IN_PLACE,
			src=in_target,
			regular=reg,
			pure_short=pure_short,
		)

	# Case 2 — same purity available in another warehouse.
	src = find_owner_rm_warehouse(
		customer,
		item,
		shortfall,
		company=company,
		warehouses=others,
		allocated=allocated,
		batch_nos=lineage,
	)
	if src:
		reg = _reserve_regular(item, shortfall, company, target, allocated, row.idx)
		return dict(base, level=LEVEL_SAME_PURITY, src=src, regular=reg)

	# Case 3 — only a different purity available, in another warehouse; convert + balance.
	conv = find_owner_rm_warehouse(
		customer,
		item,
		pure_short,
		search_different_purity=True,
		company=company,
		warehouses=others,
		allocated=allocated,
		batch_nos=lineage,
	)
	if conv:
		# Regular gold of the REQUIRED purity in the target, to mirror the conversion so
		# each purity stays physically balanced (see _execute).
		reg = _reserve_regular(item, shortfall, company, target, allocated, row.idx)
		return dict(
			base, level=LEVEL_CONVERT, src=conv, regular=reg, pure_short=pure_short
		)

	if mr_batch:
		holdings = _fetch_lineage_holdings(lineage)
		# No single holding covers the row: combine the batch's holdings in Raw Material,
		# each part settled the way a single source is.
		parts = _plan_parts(
			row, base, item, shortfall, target, others, company, allocated, holdings
		)
		if parts:
			return parts
		# The batch's own metal is short in Raw Material. Part of it may sit in a work
		# order: take that back -- the work order gets Regular Stock in its place -- the
		# same borrow Settle makes from any other warehouse (see _pull_from_work_order).
		work_orders = (
			_judge_work_order_holdings(lineage, company, holdings, all_rm, dest)
			if from_work_orders
			else []
		)
		pulls = _plan_work_order_pull(
			row,
			mr_batch,
			item,
			shortfall,
			target,
			dest,
			all_rm,
			company,
			allocated,
			holdings,
			work_orders,
		)
		if pulls:
			return dict(base, level=LEVEL_WORK_ORDER, pulls=pulls)
		_throw_batch_unavailable(
			row,
			mr_batch,
			item,
			shortfall,
			target,
			dest,
			holdings,
			all_rm,
			work_orders,
		)
	frappe.throw(
		_(
			"Row {0}: need {1} of {2} for customer {3}, but it is not available in the "
			"source warehouse, in any other warehouse at this purity, or in a convertible "
			"purity."
		).format(row.idx, shortfall, item, customer)
	)


def _pure_weight(row, item, shortfall, required):
	pure_row = flt(row.get("custom_pure_qty") or 0, 3)
	if pure_row and required:
		# Scale the row's pure qty down to the shortfall proportion.
		return flt(pure_row * shortfall / required, 3)
	return flt(shortfall * flt(_get_item_purity(item)) / 100, 3)


def _pure_per_unit(row, item):
	"""Pure weight per unit of the row's item, unrounded: _pure_weight's basis."""
	required = flt(row.qty, 3)
	pure_row = flt(row.get("custom_pure_qty") or 0, 3)
	if pure_row and required:
		return pure_row / required
	return flt(_get_item_purity(item)) / 100


def _reserve_regular(item, qty, company, target, allocated, row_idx):
	"""Reserve regular (company) gold of ``item`` in the target warehouse for a reverse
	leg. Missing regular gold fails the whole Settle rather than leaving a source
	warehouse short after the reverse cannot be booked."""
	reg = find_owner_rm_warehouse(
		None, item, qty, company=company, warehouses=[target], allocated=allocated
	)
	if not reg:
		frappe.throw(
			_(
				"Row {0}: {1} of Regular Stock {2} is needed in {3} to balance the "
				"borrowed gold, but is not available. Stock regular gold there and retry."
			).format(row_idx, qty, item, target)
		)
	return reg


def _plan_parts(
	row, base, item, shortfall, target, others, company, allocated, holdings
):
	"""Cover ``shortfall`` from several of the batch's holdings in Raw Material when no single
	one covers it: one plan entry per part, or None.

	Each part is one of Settle's single-source settles -- _execute runs it as it is, and it
	lands in the request's batch. In Settle's order of preference, largest holding first so
	the parts are few:

	1. other purities already in the source warehouse, converted in place (one part per
	   item, across its batches);
	2. the same item in another Raw Material warehouse (one part per batch and warehouse);
	3. another purity in another Raw Material warehouse, converted there (likewise).

	Only what is free is taken: the ledger, net of reservations (get_batch_qty) and of what
	this run has already claimed. The claims, and the Regular Stock each part reserves, are
	made on a copy of ``allocated`` and kept only when the parts cover the whole shortfall.
	"""
	pure_per_unit = _pure_per_unit(row, item)
	if not pure_per_unit:
		return None
	trial = dict(allocated)
	remaining = flt(shortfall, 3)
	parts = []

	def free(holding):
		key = (holding.batch_no, holding.warehouse)
		qty = min(
			flt(holding.qty, 3),
			flt(
				get_batch_qty(
					batch_no=holding.batch_no,
					warehouse=holding.warehouse,
					item_code=holding.item_code,
				),
				3,
			),
		)
		return max(flt(qty - flt(trial.get(key, 0), 3), 3), 0.0)

	def claim(batch_no, warehouse, qty):
		trial[(batch_no, warehouse)] = flt(trial.get((batch_no, warehouse), 0) + qty, 3)

	def convertible(holding):
		return holding.item_code != item and _converts_into(holding.item_code, item)

	# 1. Other purities in the source warehouse, converted in place, one item at a time.
	in_source = {}
	for h in holdings:
		if h.warehouse == target and convertible(h):
			in_source.setdefault(h.item_code, []).append(h)
	for item_code, group in sorted(
		in_source.items(), key=lambda kv: sum(flt(h.qty) for h in kv[1]), reverse=True
	):
		purity = flt(_get_item_purity(item_code))
		if remaining <= TOLERANCE or not purity:
			continue
		frees = [
			(h, free(h)) for h in sorted(group, key=lambda h: flt(h.qty), reverse=True)
		]
		take, need = _conversion_take(
			sum(q for _h, q in frees), remaining, purity, pure_per_unit
		)
		if take <= TOLERANCE:
			continue
		rows = []
		for h, q in frees:
			use = flt(min(q, need - sum(r["qty"] for r in rows)), 3)
			if use > TOLERANCE:
				rows.append({"batch_no": h.batch_no, "qty": use})
				claim(h.batch_no, target, use)
		parts.append(
			dict(
				base,
				level=LEVEL_CONVERT_IN_PLACE,
				src={
					"item_code": item_code,
					"purity": purity,
					"warehouse": target,
					"rows": rows,
				},
				shortfall=take,
				pure_short=flt(take * pure_per_unit, 3),
			)
		)
		remaining = flt(remaining - take, 3)

	# 2. The same item in another Raw Material warehouse.
	for h in sorted(
		(h for h in holdings if h.item_code == item and h.warehouse in others),
		key=lambda h: flt(h.qty),
		reverse=True,
	):
		if remaining <= TOLERANCE:
			break
		available = free(h)
		take = flt(min(available, remaining), 3)
		if take <= TOLERANCE:
			continue
		claim(h.batch_no, h.warehouse, take)
		parts.append(
			dict(
				base,
				level=LEVEL_SAME_PURITY,
				src={
					"batch_no": h.batch_no,
					"item_code": item,
					"warehouse": h.warehouse,
					"qty": take,
					"available_qty": available,
				},
				shortfall=take,
			)
		)
		remaining = flt(remaining - take, 3)

	# 3. Another purity in another Raw Material warehouse, converted there.
	for h in sorted(
		(h for h in holdings if h.warehouse in others and convertible(h)),
		key=lambda h: _equivalent(h, item, pure_per_unit),
		reverse=True,
	):
		purity = flt(_get_item_purity(h.item_code))
		if remaining <= TOLERANCE:
			break
		if not purity:
			continue
		take, need = _conversion_take(free(h), remaining, purity, pure_per_unit)
		if take <= TOLERANCE:
			continue
		claim(h.batch_no, h.warehouse, need)
		parts.append(
			dict(
				base,
				level=LEVEL_CONVERT,
				src={
					"batch_no": h.batch_no,
					"item_code": h.item_code,
					"warehouse": h.warehouse,
					"qty": need,
				},
				shortfall=take,
				pure_short=flt(take * pure_per_unit, 3),
			)
		)
		remaining = flt(remaining - take, 3)

	if not parts or remaining > TOLERANCE:
		return None
	# Covered: each part's balancing Regular Stock, then keep the claims.
	for part in parts:
		part["regular"] = _reserve_regular(
			item, part["shortfall"], company, target, trial, row.idx
		)
	allocated.update(trial)
	return parts


def _conversion_take(free_qty, remaining, purity, pure_per_unit):
	"""How much of the request's item ``free_qty`` of a ``purity`` metal makes towards
	``remaining``, and the source it consumes: ``(take, need)``. ``take`` is rounded down, so
	``need`` -- the source rounded to 3 decimals, like ``free_qty`` -- never exceeds it."""
	take = _round_down(
		min(flt(remaining), flt(free_qty) * purity / 100 / pure_per_unit)
	)
	return take, flt(take * pure_per_unit / (purity / 100), 3)


def _round_down(qty):
	"""``qty`` rounded down to 3 decimals, so what is promised can always be sourced."""
	return math.floor(flt(qty) * 1000 + 1e-6) / 1000


def _owner_qty_in_warehouse(customer, item, warehouse, consumed_target):
	"""Consumable qty of ``customer``'s ``item`` in ``warehouse``, net of what earlier
	rows of this run already earmarked."""
	batches = frappe.get_all(
		"Batch",
		filters={"item": item, "custom_customer": customer, "disabled": 0},
		pluck="name",
	)
	total = 0.0
	if batches:
		cmap = _consumable_batch_qty_map(batches, item, [warehouse])
		total = sum(flt(q, 3) for q in cmap.values())
	claimed = flt(consumed_target.get((customer, item, warehouse), 0), 3)
	return max(flt(total, 3) - claimed, 0.0)


def _claim_target(consumed_target, customer, item, warehouse, qty):
	if qty <= 0:
		return
	key = (customer, item, warehouse)
	consumed_target[key] = flt(consumed_target.get(key, 0), 3) + flt(qty, 3)


def _claim_have(consumed_target, customer, item, warehouse, batch_no, qty):
	"""Earmark ``qty`` of the target's existing stock for this row.

	A batch row claims its batch AND the customer-wide pool, so a later row without a
	batch -- which counts every batch of the customer -- cannot count the same grams."""
	_claim_target(consumed_target, customer, item, warehouse, qty)
	if batch_no and qty > 0:
		key = ("batch", batch_no, warehouse)
		consumed_target[key] = flt(consumed_target.get(key, 0), 3) + flt(qty, 3)


def _validate_row_batch(row, batch_no, item, customer):
	"""The row's batch must be ``customer``'s enabled batch of ``item``: Settle tops up
	exactly that batch, so a wrong one would land the customer's metal under it."""
	batch = frappe.db.get_value(
		"Batch", batch_no, ["item", "custom_customer", "disabled"], as_dict=True
	)
	if not batch or batch.disabled:
		frappe.throw(
			_("Row {0}: batch {1} does not exist or is disabled.").format(
				row.idx, frappe.bold(batch_no)
			)
		)
	if batch.item != item:
		frappe.throw(
			_("Row {0}: batch {1} is not a batch of {2}.").format(
				row.idx, frappe.bold(batch_no), frappe.bold(item)
			)
		)
	if batch.custom_customer != customer:
		frappe.throw(
			_(
				"Row {0}: batch {1} is not held for customer {2}, so Settle cannot top it up."
			).format(row.idx, frappe.bold(batch_no), frappe.bold(customer))
		)


def _batch_qty_in_warehouse(batch_no, item, warehouse, consumed_target):
	"""Consumable qty of ``batch_no`` in ``warehouse`` -- the balance the request's
	batch-strict transfer is checked against -- net of earlier rows' claims on it."""
	total = flt(
		_consumable_batch_qty_map([batch_no], item, [warehouse]).get(
			(batch_no, warehouse), 0
		),
		3,
	)
	claimed = flt(consumed_target.get(("batch", batch_no, warehouse), 0), 3)
	return max(flt(total - claimed, 3), 0.0)


def _batch_lineage(batch_no, customer, cache):
	"""``batch_no`` plus every batch produced from it (any depth, same ownership lane),
	limited to ``customer``'s enabled batches: the only metal that may settle this batch.

	Same lineage rule the receipt-linked return applies (``_validate_descendant``). It
	reads the stock ledger only, so it does not depend on the Customer Gold flow being on.
	"""
	if batch_no in cache:
		return cache[batch_no]

	from jewellery_erpnext.customer_subcontracting.customer_gold_trace import (
		discover_scope,
	)

	scope = discover_scope({batch_no})
	lineage = set(
		frappe.get_all(
			"Batch",
			filters={
				"name": ["in", list(scope)],
				"custom_customer": customer,
				"disabled": 0,
			},
			pluck="name",
		)
	)
	# Already validated as this customer's enabled batch.
	lineage.add(batch_no)
	cache[batch_no] = lineage
	return lineage


def _throw_batch_unavailable(
	row, batch_no, item, shortfall, target, dest, holdings, sources, work_orders=()
):
	"""Nothing in the batch's own lineage covers the shortfall in one go: say what there is,
	where, and how much this row can be settled for, instead of leaving the user to meet it
	later as negative stock on the request's own transfer.

	``holdings`` are the lineage's -- the batch and every batch made from it -- in every
	warehouse (_fetch_lineage_holdings): free in ``sources`` (the company's Raw Material
	warehouses), outside them (a work order's Manufacturing warehouse, transit...), and the
	batch already in the request's target. Another purity is also shown in the request item's
	units. ``work_orders`` (_judge_work_order_holdings) name the work order behind each
	holding in one and say whether Settle may take it back; the ceiling counts what it may.
	"""
	sources = set(sources)
	pure_per_unit = _pure_per_unit(row, item)
	summary = _summarise_holdings(
		holdings, batch_no, item, target, dest, pure_per_unit, sources
	)
	takeable = [h for h in work_orders if not h.reason]
	ceiling = summary.coverable
	if takeable:
		ceiling = _summarise_holdings(
			_moved(holdings, takeable),
			batch_no,
			item,
			target,
			dest,
			pure_per_unit,
			sources,
		).coverable
	# The request's own batch taken back into the source warehouse is on hand there.
	landed = sum(
		flt(h.qty) for h in takeable if h.batch_no == batch_no and h.landing == target
	)
	lines = [
		_(
			"Row {0}: batch {1} needs {2} more of {3} in {4}. Settle uses only this batch's "
			"own metal -- the batch and metal made from it; the customer's other batches are "
			"left alone."
		).format(
			row.idx,
			frappe.bold(batch_no),
			shortfall,
			frappe.bold(item),
			frappe.bold(target),
		)
	]
	if summary.free:
		lines.append(
			_("Free in Raw Material warehouses:") + _holding_list(summary.free, item)
		)
	if summary.outside:
		lines.append(
			_(
				"Outside Raw Material warehouses -- Settle takes metal back only from a "
				"company-gold work order whose operation has not started:"
			)
			+ _holding_list(
				summary.outside,
				item,
				with_type=True,
				notes=_work_order_notes(work_orders),
			)
		)
	if summary.at_dest:
		lines.append(
			_(
				"This much is already in {0}, this request's target warehouse, so it does "
				"not need to be transferred:"
			).format(frappe.bold(dest))
			+ _holding_list(summary.at_dest, item)
		)
	if not (summary.free or summary.outside or summary.at_dest):
		lines.append(_("None of this batch's metal is left in any warehouse."))

	# What the source warehouse already holds of the batch, plus what Settle can bring in.
	reach = flt(flt(row.qty, 3) - shortfall + ceiling + landed, 3)
	if ceiling + landed >= shortfall - TOLERANCE:
		# The ledger shows enough, but the finder saw less: Stock Reservation Entries
		# (get_batch_qty) or an earlier row of this run took part of it.
		lines.append(
			_(
				"Settle could not use it: part of it is reserved, or earlier rows of this "
				"request already take it."
			)
		)
	elif reach > TOLERANCE and summary.outside:
		lines.append(
			_(
				"This row can be settled for at most {0} of {1} now: reduce its quantity to "
				"that, or bring the metal outside Raw Material warehouses back to one first."
			).format(reach, frappe.bold(item))
		)
	elif reach > TOLERANCE:
		lines.append(
			_(
				"This row can be settled for at most {0} of {1} now: reduce its quantity to "
				"that."
			).format(reach, frappe.bold(item))
		)
	frappe.throw("<br>".join(lines), title=_("Batch Not Available"))


def _fetch_lineage_holdings(batch_nos):
	"""Every positive holding of ``batch_nos`` in any warehouse, with the warehouse's type:
	one row per (batch, item, warehouse). The same balance _consumable_batch_qty_map reads --
	submitted bundle rows up to now, no future-dated ones."""
	if not batch_nos:
		return []
	return frappe.db.sql(
		"""
		select sbe.batch_no, sbe.item_code, sbe.warehouse, wh.warehouse_type,
			sum(sbe.qty) as qty
		from `tabSerial and Batch Entry` sbe
		left join `tabWarehouse` wh on wh.name = sbe.warehouse
		where sbe.batch_no in %(batch_nos)s and sbe.docstatus = 1
			and sbe.type_of_transaction in ('Inward', 'Outward')
			and sbe.posting_datetime <= %(now)s
		group by sbe.batch_no, sbe.item_code, sbe.warehouse, wh.warehouse_type
		having sum(sbe.qty) > %(tolerance)s
		""",
		{
			"batch_nos": tuple(batch_nos),
			"now": now_datetime(),
			"tolerance": TOLERANCE,
		},
		as_dict=True,
	)


def _summarise_holdings(rows, batch_no, item, target, dest, pure_per_unit, sources):
	"""Sort a lineage's holdings by what Settle may do with them.

	* ``at_dest`` -- the request's batch already in the request's target: nothing to move.
	* ``free`` -- ``sources``, the company's enabled Raw Material warehouses: Settle's only
	  sources (_build_plan's ``all_rm``). A holding Settle still cannot use carries a
	  ``note`` saying why.
	* ``outside`` -- every other warehouse (Manufacturing, Transit, Scrap...): Settle never
	  takes metal from these.

	``coverable`` is the most one Settle run can bring in: every holding it may use adds up
	(_plan_parts combines them) -- other purities in the source warehouse converted in place,
	the same item or another purity elsewhere. Each is rounded down, so a request for exactly
	that much is sourced in full.
	"""
	free, at_dest, outside = [], [], []
	coverable = 0.0
	for row in rows:
		if row.batch_no == batch_no and row.warehouse == target:
			# The request's own batch in the source warehouse is already counted as on hand.
			continue
		holding = frappe._dict(row)
		holding.equivalent = _equivalent(row, item, pure_per_unit)
		if row.batch_no == batch_no and row.warehouse == dest:
			at_dest.append(holding)
			continue
		if row.warehouse not in sources:
			outside.append(holding)
			continue
		free.append(holding)
		holding.note = _unusable_reason(row, item, target, dest)
		if not holding.note:
			coverable += _round_down(holding.equivalent)
	return frappe._dict(
		free=free,
		at_dest=at_dest,
		outside=outside,
		coverable=flt(coverable, 3),
	)


def _unusable_reason(holding, item, target, dest):
	"""Why Settle cannot use a free holding of the lineage, or None when it can."""
	if holding.warehouse == dest:
		return _("Settle never takes metal from the request's target warehouse")
	if holding.item_code == item:
		if holding.warehouse == target:
			return _(
				"Settle does not relabel another batch inside the source warehouse"
			)
		return None
	if not _converts_into(holding.item_code, item):
		return _("Settle does not convert this item into {0}").format(item)
	return None


def _converts_into(source_item, item):
	"""Whether Settle converts ``source_item`` into ``item``: a gold item of another purity
	in PURITY_PRIORITY and the same colour -- the candidates _get_gold_items_for_purity
	offers both conversion paths."""
	label = _get_purity_label(source_item)
	return (
		source_item.startswith("M-G-")
		and label in PURITY_PRIORITY
		and label != _get_purity_label(item)
		and source_item.split("-")[-1] == (item or "").split("-")[-1]
	)


def _equivalent(holding, item, pure_per_unit):
	"""``holding``'s quantity in the request item's units, unrounded: the same item 1:1, a
	purity Settle converts by pure weight on the row's own pure basis (_pure_weight's), and
	anything else 0."""
	qty = flt(holding.qty, 3)
	if holding.item_code == item:
		return qty
	if not pure_per_unit or not _converts_into(holding.item_code, item):
		return 0.0
	return qty * flt(_get_item_purity(holding.item_code)) / 100 / pure_per_unit


def _holding_list(holdings, item, with_type=False, notes=None):
	"""The holdings as an HTML list for the refusal. ``notes`` -- {(batch, warehouse):
	[text]} -- adds what is known about a holding there (_work_order_notes)."""
	lines = []
	for h in holdings:
		line = _("{0} of {1} (batch {2}) in {3}").format(
			flt(h.qty, 3),
			frappe.bold(h.item_code),
			h.batch_no,
			frappe.bold(h.warehouse),
		)
		if with_type and h.warehouse_type:
			line += f" ({_(h.warehouse_type)})"
		if h.item_code != item and h.equivalent:
			line += " = " + _("{0} of {1}").format(flt(h.equivalent, 3), item)
		for note in [h.get("note"), *(notes or {}).get((h.batch_no, h.warehouse), [])]:
			if note:
				line += f" -- {note}"
		lines.append(f"<li>{line}</li>")
	return "<ul>" + "".join(lines) + "</ul>"


def _work_order_notes(work_orders):
	"""{(batch, warehouse): [text]}: the work order holding the batch there, and whether
	Settle may take it back or why not."""
	notes = {}
	for h in work_orders:
		if h.reason:
			text = _("work order {0}: not taken back, {1}").format(
				frappe.bold(h.work_order), h.reason
			)
		else:
			text = _("work order {0}: Settle can take it back").format(
				frappe.bold(h.work_order)
			)
		notes.setdefault((h.batch_no, h.warehouse), []).append(text)
	return notes


def _check_customer_goods_flag(actionable):
	"""Refuse before anything is posted when a planned conversion would land customer metal
	in a batch the Batch guard refuses (update_inventory_dimentions): a Customer Goods batch
	of an item without the Customer Goods flag.

	ConvertInPlace, Convert, and a SamePurity that relabels another batch into the request's
	all produce customer metal of the request's item, into the request's batch or a new
	one. update_parent_batch_id re-saves that batch on submit and the guard throws there,
	deep inside the first Stock Entry. Asking the guard's own questions here names the Item
	up front instead.
	"""
	for entry in actionable:
		if not _repacks_customer_metal(entry):
			continue
		if can_be_customer_goods(entry["item"]):
			continue
		mr_batch = entry.get("mr_batch")
		if mr_batch and _guard_spares(mr_batch):
			continue
		frappe.throw(
			_(
				"Row {0}: Settle would produce the customer's metal into {1} of {2}, but "
				"Item {2} does not have '{3}' ticked. Tick it on the Item, then Settle again."
			).format(
				entry["row"],
				_("batch {0}").format(frappe.bold(mr_batch))
				if mr_batch
				else _("a new customer batch"),
				frappe.bold(entry["item"]),
				_(CUSTOMER_GOODS_FLAG_LABEL),
			),
			title=_("Item Not Allowed as Customer Goods"),
		)


def _repacks_customer_metal(entry):
	"""Whether the planned settle of ``entry`` produces customer metal in a Repack (see
	_check_customer_goods_flag); a plain transfer of the request's own batch does not."""
	if entry["level"] in (LEVEL_CONVERT_IN_PLACE, LEVEL_CONVERT):
		return True
	if entry["level"] == LEVEL_SAME_PURITY:
		mr_batch = entry.get("mr_batch")
		return bool(mr_batch) and entry["src"]["batch_no"] != mr_batch
	if entry["level"] == LEVEL_WORK_ORDER:
		# Once taken back, the row settles from the pulled batch: a conversion or a relabel
		# into the request's batch, unless the pulled batch is the request's own.
		return any(pull.batch_no != entry.get("mr_batch") for pull in entry["pulls"])
	return False


def _guard_spares(batch_no):
	"""Whether the Batch guard re-saves the existing ``batch_no`` without the Item's flag.

	The guard judges a batch by the entry that minted it, not by the Repack re-saving it:
	it skips a batch that is not customer inventory, and one minted by a flow it exempts
	(subcontracting repack, process loss, repair unpack, an SNC settlement)."""
	from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events.utils import (
		is_process_loss_repack,
		is_repair_unpack,
		is_snc_settlement_conversion,
		is_subcontracting_gold_repack,
	)

	batch = frappe.get_doc("Batch", batch_no)
	if batch.get("custom_inventory_type") not in ("Customer Goods", "Customer Stock"):
		return True
	return any(
		exempt(batch)
		for exempt in (
			is_subcontracting_gold_repack,
			is_snc_settlement_conversion,
			is_process_loss_repack,
			is_repair_unpack,
		)
	)


def _gather_convertible(
	customer, item, pure_needed, warehouse, company, allocated, batch_nos=None
):
	"""Gather enough of the customer's convertible purity in ONE warehouse to cover
	``pure_needed`` (pure weight), possibly across SEVERAL batches.

	The customer's converted metal is normally spread over multiple batches, so unlike
	the single-batch finder this accumulates batches of one candidate purity until the
	pure requirement is met. Returns
	``{"item_code", "purity", "warehouse", "rows": [{"batch_no", "qty"}]}`` (source qty in
	candidate-item units) and reserves each consumed batch in ``allocated``; ``None`` if no
	single candidate purity can cover it. Higher purities are tried first (PURITY_PRIORITY).
	``batch_nos`` (a Material Request batch's lineage) restricts the candidate batches.
	"""
	pure_needed = flt(pure_needed, 3)
	if pure_needed <= TOLERANCE:
		return None

	required_label = _get_purity_label(item)
	for purity in PURITY_PRIORITY:
		if purity == required_label:
			continue
		for candidate in _get_gold_items_for_purity(purity, item):
			cand_purity = flt(_get_item_purity(candidate))
			if not cand_purity:
				continue
			rows, acc_pure = _accumulate_batches(
				customer,
				candidate,
				cand_purity,
				pure_needed,
				warehouse,
				allocated,
				batch_nos=batch_nos,
			)
			if acc_pure >= pure_needed - TOLERANCE and rows:
				for r in rows:
					key = (r["batch_no"], warehouse)
					allocated[key] = flt(allocated.get(key, 0), 3) + r["qty"]
				return {
					"item_code": candidate,
					"purity": cand_purity,
					"warehouse": warehouse,
					"rows": rows,
				}
	return None


def _accumulate_batches(
	customer, candidate, cand_purity, pure_needed, warehouse, allocated, batch_nos=None
):
	"""Take the customer's ``candidate`` batches in ``warehouse`` (oldest first) until the
	pure requirement is met. Returns (rows, accumulated_pure) without reserving.
	``batch_nos``, when given, limits the candidates to those batches."""
	filters = {"item": candidate, "custom_customer": customer, "disabled": 0}
	if batch_nos is not None:
		if not batch_nos:
			return [], 0.0
		filters["name"] = ["in", list(batch_nos)]
	batches = frappe.get_all(
		"Batch",
		filters=filters,
		order_by="creation asc",
		pluck="name",
	)
	if not batches:
		return [], 0.0

	cmap = _consumable_batch_qty_map(batches, candidate, [warehouse])
	rows = []
	acc_pure = 0.0
	for batch in batches:
		consumable = flt(cmap.get((batch, warehouse), 0), 3)
		if consumable <= TOLERANCE:
			continue
		# Net reservations (get_batch_qty) and this run's earlier claims, so we never
		# promise stock that is reserved or already earmarked.
		netted = min(
			consumable,
			flt(
				get_batch_qty(batch_no=batch, warehouse=warehouse, item_code=candidate),
				3,
			),
		)
		avail = flt(netted - flt(allocated.get((batch, warehouse), 0), 3), 3)
		if avail <= TOLERANCE:
			continue
		need_units = flt((pure_needed - acc_pure) / (cand_purity / 100), 3)
		take = flt(min(avail, need_units), 3)
		if take <= TOLERANCE:
			continue
		rows.append({"batch_no": batch, "qty": take})
		acc_pure = flt(acc_pure + take * cand_purity / 100, 3)
		if acc_pure >= pure_needed - TOLERANCE:
			break
	return rows, acc_pure


# ---------------------------------------------------------------------------
# Planning, work orders (read-only): the batch's metal issued to a work order.
#
# The batch scope keeps Settle to the batch's own metal, and part of it can sit in a work
# order the customer's gold was issued to. Settle takes that back the way it borrows from
# any other warehouse: the customer's batch comes out, an equal weight of Regular Stock
# goes in, so the work order ends whole -- only from a company-gold order, where the
# customer's metal does not belong, and only from an operation nobody has started.
# ---------------------------------------------------------------------------


def _judge_work_order_holdings(lineage, company, holdings, sources, dest):
	"""Every work order holding the lineage's metal, with the Raw Material warehouse it
	would go back to (``landing``) and, when Settle may not take it, why (``reason``)."""
	sources = set(sources)
	judged = []
	for holding in _fetch_work_order_holdings(lineage, company):
		holding = frappe._dict(holding)
		holding.landing = _landing_warehouse(holding, holdings, sources, dest)
		holding.reason = _why_not_taken(holding)
		judged.append(holding)
	return judged


def _fetch_work_order_holdings(lineage, company):
	"""The lineage's metal reserved in work-order warehouses: one row per reservation row,
	from the Stock Reservation Entries a work-order issue makes (``batch_no`` is indexed),
	with everything _why_not_taken reads -- the order's ownership, the work order's current
	operation and its MOP Log balance for the batch, pending end-of-day syncs, draft IRs,
	the batch's stock in that warehouse -- and the department's Raw Material warehouse."""
	if not lineage:
		return []
	rows = frappe.db.sql(
		"""
		select sre.name as reservation, sb.batch_no, sre.item_code, sre.warehouse,
			sb.qty - sb.delivered_qty as qty, sre.manufacturing_work_order as work_order,
			mwo.docstatus as work_order_docstatus, mwo.manufacturing_operation as operation,
			mop.status as operation_status, mop.department_ir_status, mop.department,
			ifnull(pmo.is_customer_gold, 0) as customer_gold
		from `tabSerial and Batch Entry` sb
		join `tabStock Reservation Entry` sre on sre.name = sb.parent
		join `tabManufacturing Work Order` mwo on mwo.name = sre.manufacturing_work_order
		left join `tabManufacturing Operation` mop
			on mop.name = mwo.manufacturing_operation
		left join `tabParent Manufacturing Order` pmo on pmo.name = mwo.manufacturing_order
		where sb.batch_no in %(lineage)s and sb.parenttype = 'Stock Reservation Entry'
			and sre.docstatus = 1 and sre.status not in ('Cancelled', 'Delivered')
			and sre.company = %(company)s and sb.qty - sb.delivered_qty > %(tolerance)s
		""",
		{"lineage": tuple(lineage), "company": company, "tolerance": TOLERANCE},
		as_dict=True,
	)
	for row in rows:
		row.qty = flt(row.qty, 3)
		# Moves an IR logged that the end-of-day sync has not posted yet.
		pending = frappe.get_all(
			"MOP Log",
			filters={
				"manufacturing_work_order": row.work_order,
				"is_cancelled": 0,
				"is_synced": 0,
			},
			fields=[
				"manufacturing_operation",
				"to_warehouse",
				"flow_index",
				"creation",
			],
		)
		row.pending_elsewhere = _pending_on_another_open_operation(
			pending, row.operation
		)
		row.eod_target = (
			_eod_target(pending, row.operation, row.department) if pending else None
		)
		row.draft_ir = any(
			frappe.db.exists(
				doctype, {"manufacturing_work_order": row.work_order, "docstatus": 0}
			)
			for doctype in ("Employee IR Operation", "Department IR Operation")
		)
		row.operation_balance = _operation_batch_balance(
			row.operation, row.item_code, row.batch_no
		)
		row.in_warehouse = flt(
			_consumable_batch_qty_map(
				[row.batch_no], row.item_code, [row.warehouse]
			).get((row.batch_no, row.warehouse), 0),
			3,
		)
		row.department_rm = row.department and frappe.db.get_value(
			"Warehouse",
			{
				"warehouse_type": "Raw Material",
				"department": row.department,
				"company": company,
				"disabled": 0,
			},
			"name",
		)
	return rows


def _pending_on_another_open_operation(pending, operation):
	"""Whether a pending log sits on an open operation other than the work order's current
	one: the end-of-day sync holds such a work order (_open_non_pointer_operations), so its
	metal cannot be followed. Pending logs on a finished predecessor are the normal IR
	audit copies."""
	from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
		is_open_operation,
	)

	others = {
		log.manufacturing_operation
		for log in pending
		if log.manufacturing_operation and log.manufacturing_operation != operation
	}
	if not others:
		return False
	return any(
		is_open_operation(op.status, op.department_ir_status)
		for op in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", sorted(others)]},
			fields=["status", "department_ir_status"],
		)
	)


def _eod_target(pending, operation, department):
	"""Where the end-of-day sync will move the work order's metal: the latest pending
	``to_warehouse`` on the current operation, else on the work order, else the
	department's Manufacturing warehouse -- the sync's own rule (_plan_mwo_group)."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
		_get_t_warehouse_from_logs,
		_resolve_department_warehouse,
	)

	return (
		_get_t_warehouse_from_logs(
			[log for log in pending if log.manufacturing_operation == operation]
		)
		or _get_t_warehouse_from_logs(pending)
		or _resolve_department_warehouse({"department": department})
	)


def _operation_batch_balance(operation, item_code, batch_no):
	"""The operation's MOP Log balance for (item, batch): its latest row's batch figure."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	if not operation:
		return 0.0
	for log in get_current_mop_balance_rows(operation, keys=[(item_code, batch_no)]):
		if log.get("item_code") == item_code and log.get("batch_no") == batch_no:
			return flt(log.get("qty_after_transaction_batch_based"), 3)
	return 0.0


def _why_not_taken(holding):
	"""Why Settle may not take ``holding`` back from its work order, or None when it may.

	Only from a company-gold order, whose operation is the work order's current one and not
	started, with no draft IR about to carry the batch forward. IR moves still waiting for the
	end-of-day sync are followed (_log_on_operation writes the pull into them), unless they
	sit on another open operation, which the sync holds. The reservation and the ledger must
	hold what is taken, and the operation's MOP Log exactly that, so the batch ends at 0.
	"""
	if holding.customer_gold:
		return _("it is a customer-gold order")
	if holding.work_order_docstatus != 1:
		return _("the work order is not submitted")
	if holding.operation_status != OPERATION_NOT_STARTED:
		return _("its operation {0} is {1}").format(
			holding.operation or "-", holding.operation_status or _("missing")
		)
	if holding.department_ir_status == "In-Transit":
		return _("its operation is In-Transit")
	if holding.pending_elsewhere:
		return _(
			"its MOP Logs waiting for the end-of-day sync sit on another open operation"
		)
	if holding.draft_ir:
		return _("a draft Employee IR or Department IR holds it")
	if (
		abs(flt(holding.operation_balance) - flt(holding.qty)) > TOLERANCE
		or flt(holding.in_warehouse) < flt(holding.qty) - TOLERANCE
	):
		return _(
			"its operation's MOP Log shows {0} and {1} holds {2}, not the {3} reserved"
		).format(
			flt(holding.operation_balance, 3),
			holding.warehouse,
			flt(holding.in_warehouse, 3),
			holding.qty,
		)
	if not holding.landing:
		return _("no Raw Material warehouse to take it back to")
	return None


def _landing_warehouse(holding, holdings, sources, dest):
	"""Where ``holding`` goes back to: the Raw Material warehouse already holding the most of
	the same batch, so the two add up for one conversion; else the operation's department
	Raw Material warehouse. Never the request's destination, which Settle never draws from."""
	same = [
		h
		for h in holdings
		if h.batch_no == holding.batch_no
		and h.warehouse in sources
		and h.warehouse != dest
	]
	if same:
		return max(same, key=lambda h: flt(h.qty)).warehouse
	if holding.department_rm in sources and holding.department_rm != dest:
		return holding.department_rm
	return None


def _plan_work_order_pull(
	row,
	mr_batch,
	item,
	shortfall,
	target,
	dest,
	sources,
	company,
	allocated,
	holdings,
	work_orders,
):
	"""The work-order holdings to take back so the batch's own metal covers the row, or None.

	Largest first, stopping as soon as the lineage -- each pulled holding counted at its
	landing -- covers the shortfall by Settle's own sourcing rules (_summarise_holdings).
	Each pull reserves the Regular Stock that goes back to its work order; nothing stays
	reserved when the pulls cannot cover the row.
	"""
	eligible = sorted(
		(h for h in work_orders if not h.reason),
		key=lambda h: flt(h.qty),
		reverse=True,
	)
	if not eligible:
		return None

	sources = set(sources)
	pure_per_unit = _pure_per_unit(row, item)
	trial = dict(allocated)
	pulls = []
	for holding in eligible:
		regular = _regular_to_put_back(holding, company, trial)
		if not regular:
			holding.reason = _(
				"no free Regular Stock of {0} in {1} to put back"
			).format(holding.item_code, holding.landing)
			continue
		pulls.append(frappe._dict(holding, regular=regular))
		after = _summarise_holdings(
			_moved(holdings, pulls),
			mr_batch,
			item,
			target,
			dest,
			pure_per_unit,
			sources,
		)
		landed = sum(
			flt(p.qty) for p in pulls if p.batch_no == mr_batch and p.landing == target
		)
		if after.coverable + landed >= shortfall - TOLERANCE:
			allocated.update(trial)
			return pulls
	return None


def _regular_to_put_back(holding, company, trial):
	"""The Regular Stock batch that goes into the work order in place of the customer's,
	claimed in ``trial``.

	With moves waiting for the end-of-day sync, never a batch the sync's target already holds
	enough of: the sync would take it as already there and leave it where Settle put it.
	"""
	probe = dict(trial)
	for _attempt in range(5):
		regular = find_owner_rm_warehouse(
			None,
			holding.item_code,
			holding.qty,
			company=company,
			warehouses=[holding.landing],
			allocated=probe,
			reserve=False,
		)
		if not regular:
			return None
		key = (regular["batch_no"], holding.landing)
		already_there = (
			holding.eod_target
			and flt(
				_consumable_batch_qty_map(
					[regular["batch_no"]], holding.item_code, [holding.eod_target]
				).get((regular["batch_no"], holding.eod_target), 0)
			)
			>= flt(holding.qty) - TOLERANCE
		)
		if not already_there:
			trial[key] = flt(trial.get(key, 0) + holding.qty, 3)
			return regular
		# Counted as fully taken, so the finder offers the next batch.
		probe[key] = float("inf")
	return None


def _moved(holdings, pulls):
	"""``holdings`` as they stand once ``pulls`` are taken back: each pulled holding leaves
	its work-order warehouse for its landing, merged with the batch's metal already there."""
	totals = {}
	types = {}
	for h in holdings:
		key = (h.batch_no, h.item_code, h.warehouse)
		totals[key] = totals.get(key, 0.0) + flt(h.qty)
		types[h.warehouse] = h.warehouse_type
	for p in pulls:
		out_key = (p.batch_no, p.item_code, p.warehouse)
		totals[out_key] = totals.get(out_key, 0.0) - flt(p.qty)
		in_key = (p.batch_no, p.item_code, p.landing)
		totals[in_key] = totals.get(in_key, 0.0) + flt(p.qty)
	return [
		frappe._dict(
			batch_no=batch_no,
			item_code=item_code,
			warehouse=warehouse,
			warehouse_type=types.get(warehouse),
			qty=flt(qty, 3),
		)
		for (batch_no, item_code, warehouse), qty in totals.items()
		if qty > TOLERANCE
	]


# ---------------------------------------------------------------------------
# Execution (writes): mint + submit the Stock Entries the plan calls for.
# ---------------------------------------------------------------------------


def _execute(mr, actionable):
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		series_stubs,
	)

	# RULE B — pin the naming counters and every Bin this run touches, up front, in
	# canonical order, so a concurrent submit cannot interleave Series <-> Bin.
	preallocate_series_for_docs(
		*series_stubs(mr.company, TRANSFER_SE_TYPE, REPACK_SE_TYPE)
	)
	lock_bins(_bin_pairs(actionable))

	for entry in actionable:
		if entry["level"] == LEVEL_CONVERT_IN_PLACE:
			_settle_convert_in_place(mr, entry)
		elif entry["level"] == LEVEL_SAME_PURITY:
			_settle_same_purity(mr, entry)
		elif entry["level"] == LEVEL_CONVERT:
			_settle_convert(mr, entry)


def _bin_pairs(actionable):
	pairs = []
	for e in actionable:
		item, target, src = e["item"], e["target"], e["src"]
		pairs.append((item, target))
		pairs.append((item, src["warehouse"]))
		if e["level"] in (LEVEL_CONVERT, LEVEL_CONVERT_IN_PLACE):
			# the different-purity source item also moves / is consumed
			pairs.append((src["item_code"], src["warehouse"]))
			pairs.append((src["item_code"], target))
	return pairs


def _settle_convert_in_place(mr, entry):
	"""The customer's convertible purity is already in the target: convert it back there,
	then mirror the conversion with regular gold to keep physical stock balanced.

	Two repacks, both in the target, no transfers:
	  1. customer:  item2 -> item   (systematic; produced into the MR batch)
	  2. regular:   item  -> item2  (company gold; restores each purity's physical total,
	     since the customer conversion did not physically melt anything)
	"""
	conv = entry["src"]  # {item_code, warehouse: target, rows: [{batch_no, qty}]}
	item = entry["item"]
	item2 = conv["item_code"]
	target = entry["target"]
	shortfall = entry["shortfall"]
	# Total source consumed by the customer conversion; the regular mirror produces it
	# back so the item2 physical total is unchanged.
	source_total = flt(sum(flt(r["qty"], 3) for r in conv["rows"]), 3)

	# 1. Customer conversion: item2 (gathered batches) -> item, into the MR batch.
	_convert_multi(
		mr,
		source_item=item2,
		source_rows=conv["rows"],
		target_item=item,
		target_qty=shortfall,
		warehouse=target,
		customer=entry["customer"],
		target_batch=entry.get("mr_batch"),
	)
	# 2. Regular mirror: item (regular) -> item2 (regular), same quantities reversed.
	reg = entry["regular"]
	_convert(
		mr,
		source_item=item,
		source_qty=shortfall,
		source_batch=reg["batch_no"],
		target_item=item2,
		target_qty=source_total,
		warehouse=target,
		customer=None,
	)


def _settle_same_purity(mr, entry):
	item = entry["item"]
	qty = entry["shortfall"]
	target = entry["target"]
	src = entry["src"]
	reg = entry["regular"]
	mr_batch = entry.get("mr_batch")
	found_batch = src["batch_no"]

	# The MR transfer is batch-strict, so the customer's gold must arrive in the MR's
	# batch. A transfer can't relabel, so when the found batch differs, relabel it in the
	# source warehouse first (same-item repack: consume found -> produce mr_batch) and
	# move mr_batch in; otherwise transfer the found batch (already the MR batch) as-is.
	transfer_batch = found_batch
	if mr_batch and found_batch != mr_batch:
		transfer_batch = _convert(
			mr,
			source_item=item,
			source_qty=qty,
			source_batch=found_batch,
			target_item=item,
			target_qty=qty,
			warehouse=src["warehouse"],
			customer=entry["customer"],
			target_batch=mr_batch,
		)

	# Bring the customer's gold in from the lending warehouse.
	_transfer(
		mr, item, qty, transfer_batch, src["warehouse"], target, entry["customer"]
	)
	# Return an equal quantity of regular gold, so the lender ends whole.
	_transfer(mr, item, qty, reg["batch_no"], target, src["warehouse"], None)


def _settle_convert(mr, entry):
	"""Different-purity settle, balanced with regular gold.

	Net effect per the user's Case 3, achieved with four Stock Entries:

	  1. customer conversion in W:  item2 -> item        (customer's holding, pure-equiv)
	  2. regular conversion in T:   item  -> item2       (company gold)
	  3. customer transfer:         item   W -> T        (customer gets the required item)
	  4. regular transfer:          item2  T -> W        (W restored, now regular)

	Result: W's item2 quantity is unchanged (customer -> regular), T gains the customer's
	required-purity item in place of company gold. Provisional choreography anchored on
	create_snc — confirm against a real voucher on the cloud site (see plan open items).
	"""
	item = entry["item"]
	qty = entry["shortfall"]
	target = entry["target"]
	conv = entry["src"]  # {item_code: item2, batch_no, warehouse: W, qty: source_qty}
	reg = entry["regular"]  # regular `item` in target
	item2 = conv["item_code"]
	source_qty = flt(conv["qty"], 3)
	W = conv["warehouse"]

	# 1. Customer conversion in W: item2 -> item, produced INTO the MR's batch so the
	#    batch-strict MR transfer later finds it (falls back to an auto batch if the MR
	#    row carries none).
	produced_item_batch = _convert(
		mr,
		source_item=item2,
		source_qty=source_qty,
		source_batch=conv["batch_no"],
		target_item=item,
		target_qty=qty,
		warehouse=W,
		customer=entry["customer"],
		target_batch=entry.get("mr_batch"),
	)
	# 2. Regular conversion in target: item -> item2 (company gold).
	produced_item2_batch = _convert(
		mr,
		source_item=item,
		source_qty=qty,
		source_batch=reg["batch_no"],
		target_item=item2,
		target_qty=source_qty,
		warehouse=target,
		customer=None,
	)
	# 3. Customer transfer: item W -> target.
	_transfer(mr, item, qty, produced_item_batch, W, target, entry["customer"])
	# 4. Regular transfer: item2 target -> W.
	_transfer(mr, item2, source_qty, produced_item2_batch, target, W, None)


def _pull_from_work_orders(mr, actionable):
	"""Take back every holding the plan pulls, once each however many rows planned it.

	Pins the naming series and every Bin the run touches first, in canonical order (RULE B),
	as _execute does: the pulls, and the settle that follows them on the same items and
	warehouses (the pulled metal is converted or moved from its landing to the target).
	"""
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		series_stubs,
	)

	pulls = {}
	pairs = _bin_pairs([e for e in actionable if e["level"] != LEVEL_WORK_ORDER])
	for entry in actionable:
		if entry["level"] != LEVEL_WORK_ORDER:
			continue
		for pull in entry["pulls"]:
			pulls.setdefault(
				(pull.reservation, pull.batch_no),
				frappe._dict(pull, customer=entry["customer"]),
			)
			for item_code in (entry["item"], pull.item_code):
				for warehouse in (entry["target"], pull.landing, pull.warehouse):
					pairs.append((item_code, warehouse))

	preallocate_series_for_docs(
		*series_stubs(mr.company, TRANSFER_SE_TYPE, REPACK_SE_TYPE)
	)
	lock_bins(pairs)
	return [_pull_from_work_order(mr, pull) for pull in pulls.values()]


def _pull_from_work_order(mr, pull):
	"""Take ``pull`` -- the batch's holding in a company-gold work order -- back to Raw
	Material, the same borrow _settle_same_purity makes from any other warehouse: the
	customer's batch comes out and an equal weight of Regular Stock goes in, so the work
	order ends whole.

	The work order's own bookkeeping moves with the metal, or the moves are refused or
	break it later:

	* its Stock Reservation Entry is re-pointed at the regular batch -- ERPNext will not
	  move a reserved batch out (validate_reserved_batches, "Reserved Batch Conflict");
	* its operation's MOP Log gets +qty for the regular batch and -qty for the customer's
	  -- otherwise the next IR copies the stale batch forward and the end-of-day sync,
	  unable to find it, holds the work order;
	* its SNC requirement is stamped again from what it now holds -- stamp_snc_requirement
	  only runs for WORK ORDER transfers, and a stale "Need" blocks its finished-goods
	  submit (validate_snc_before_submit) with no borrowed gold left to settle.
	"""
	qty = flt(pull.qty, 3)
	regular_batch = pull.regular["batch_no"]
	# Regular Stock in first: the re-pointed reservation must find the batch there.
	regular_entry = _transfer(
		mr, pull.item_code, qty, regular_batch, pull.landing, pull.warehouse, None
	)
	reservation = _move_reservation(pull.reservation, pull.batch_no, regular_batch)
	customer_entry = _transfer(
		mr,
		pull.item_code,
		qty,
		pull.batch_no,
		pull.warehouse,
		pull.landing,
		pull.customer,
	)
	# Both rows at the operation's highest flow index, so readers ordering by it -- the
	# end-of-day sync, the finished-goods receive -- take them as the latest.
	flow_index = _highest_flow_index(pull.operation)
	_log_on_operation(regular_entry, pull, outward=False, flow_index=flow_index)
	_log_on_operation(customer_entry, pull, outward=True, flow_index=flow_index)
	_restamp_snc_requirement(pull.work_order)
	return frappe._dict(
		pull,
		regular_entry=regular_entry,
		customer_entry=customer_entry,
		new_reservation=reservation,
	)


def _move_reservation(reservation, from_batch, to_batch):
	"""Re-point a work order's Stock Reservation Entry from ``from_batch`` to ``to_batch``:
	the same sales order line, quantity and warehouse, one batch swapped. Cancelled and
	rebuilt, the way Make Receive replaces a reservation (_build_replacement_sre)."""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		_build_replacement_sre,
	)

	original = frappe.get_doc("Stock Reservation Entry", reservation)
	remaining = {}
	for sb in frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": original.name, "parenttype": "Stock Reservation Entry"},
		fields=["batch_no", "qty", "delivered_qty"],
		order_by="idx asc",
	):
		left = flt(sb.qty) - flt(sb.delivered_qty)
		if left <= TOLERANCE:
			continue
		batch_no = to_batch if sb.batch_no == from_batch else sb.batch_no
		remaining[batch_no] = flt(remaining.get(batch_no, 0) + left, 3)
	original.flags.ignore_permissions = True
	original.cancel()
	return _build_replacement_sre(
		original,
		flt(sum(remaining.values()), 3),
		sb_remaining=[{"batch_no": b, "qty": q} for b, q in remaining.items()],
	)


def _highest_flow_index(operation):
	"""The highest flow index on the operation's live MOP Log rows (0 when none)."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_last_mop_index,
	)

	return cint(get_last_mop_index(operation))


def _log_on_operation(entry_name, pull, outward, flow_index):
	"""Record one leg of a pull on the work order's operation, as a WORK ORDER entry's own
	submit would (create_mop_log_for_stock_transfer_to_mo). Settle's legs are plain
	transfers, so the direction is passed, not read off the entry type.

	With IR moves still waiting for the end-of-day sync (``pull.eod_target``), the rows join
	them: unsynced, bound for the sync's target. The sync then moves the regular batch -- the
	customer's ends at 0 and is skipped -- and marks them all synced. Written synced, the
	pending chain would still move the customer's batch, which is gone: "batch short", and
	the work order held every night.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		create_mop_log_for_stock_transfer_to_mo,
	)

	entry = frappe.get_doc("Stock Entry", entry_name)
	row = entry.items[0]
	pending = bool(pull.get("eod_target"))
	create_mop_log_for_stock_transfer_to_mo(
		frappe._dict(
			doctype=entry.doctype,
			name=entry.name,
			stock_entry_type=entry.stock_entry_type,
			manufacturing_work_order=pull.work_order,
		),
		frappe._dict(
			item_code=row.item_code,
			qty=row.qty,
			# Gold is weighed, not counted: no pieces move with it.
			pcs=0,
			batch_no=row.batch_no,
			s_warehouse=row.s_warehouse,
			t_warehouse=pull.eod_target if pending else row.t_warehouse,
			manufacturing_operation=pull.operation,
			name=row.name,
			serial_and_batch_bundle=row.serial_and_batch_bundle,
		),
		is_synced=not pending,
		outward=outward,
		flow_index=flow_index,
	)


def _restamp_snc_requirement(work_order):
	"""Stamp the work order's SNC requirement from what it holds now, as
	stamp_snc_requirement does after a WORK ORDER transfer."""
	needs = _mwo_needs_settlement(work_order)
	values = {"snc_requirement": "Need" if needs else "Not Need"}
	if needs:
		values["snc_done"] = 0
	frappe.db.set_value("Manufacturing Work Order", work_order, values)


def _pull_summary(pulled):
	"""What was taken back from which work order, and what replaced it there."""
	return "<br>".join(
		_(
			"Took {0} of {1} (batch {2}) back from work order {3} into {4}, and gave the "
			"work order {0} of Regular Stock (batch {5}) in its place: {6}, {7}."
		).format(
			p.qty,
			frappe.bold(p.item_code),
			p.batch_no,
			frappe.bold(p.work_order),
			frappe.bold(p.landing),
			p.regular["batch_no"],
			p.regular_entry,
			p.customer_entry,
		)
		for p in pulled
	)


# ---------------------------------------------------------------------------
# Stock Entry builders
# ---------------------------------------------------------------------------


def _new_se(mr, se_type, purpose, from_wh, to_wh, customer):
	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": se_type,
			"purpose": purpose,
			"company": mr.company,
			"branch": mr.get("branch")
			or frappe.db.get_value("Warehouse", from_wh or to_wh, "custom_branch"),
			"from_warehouse": from_wh,
			"to_warehouse": to_wh,
			# Direct one-shot transfer, never routed through a transit warehouse.
			"add_to_transit": 0,
			"inventory_type": CUSTOMER_GOODS if customer else REGULAR_STOCK,
			"_customer": customer,
			"auto_created": 1,
		}
	)
	return se


def _transfer(mr, item, qty, batch, from_wh, to_wh, customer):
	inv = CUSTOMER_GOODS if customer else REGULAR_STOCK
	se = _new_se(mr, TRANSFER_SE_TYPE, "Material Transfer", from_wh, to_wh, customer)
	_append_item(
		se,
		{
			"item_code": item,
			"qty": flt(qty, 3),
			"batch_no": batch,
			"s_warehouse": from_wh,
			"t_warehouse": to_wh,
			"inventory_type": inv,
			"customer": customer,
		},
	)
	se.insert(ignore_permissions=True)
	_submit_consuming_stock_entry(se)
	return se.name


def _convert(
	mr,
	source_item,
	source_qty,
	source_batch,
	target_item,
	target_qty,
	warehouse,
	customer,
	target_batch=None,
):
	"""Repack one purity into another inside a single warehouse; return the target batch.

	``target_batch`` -- when given, the produced (inward) row is stamped with it, so
	``create_child_batches`` skips minting a fresh child batch (it only mints for
	produced rows with no batch) and the metal is produced into that existing batch. Used
	to land the sourced material in the batch the Material Request references.
	"""
	return _convert_multi(
		mr,
		source_item=source_item,
		source_rows=[{"batch_no": source_batch, "qty": flt(source_qty, 3)}],
		target_item=target_item,
		target_qty=target_qty,
		warehouse=warehouse,
		customer=customer,
		target_batch=target_batch,
	)


def _convert_multi(
	mr,
	source_item,
	source_rows,
	target_item,
	target_qty,
	warehouse,
	customer,
	target_batch=None,
):
	"""Repack one purity into another inside a single warehouse, consuming one or more
	source batches of ``source_item``; return the target batch.

	See ``_convert`` for the ``target_batch`` semantics. Multiple source rows let a
	conversion draw the requirement from several batches when it is spread across them.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
		lane_tag,
	)

	inv = CUSTOMER_GOODS if customer else REGULAR_STOCK
	# The ownership lane, stamped exactly as Metal Conversions stamps it. Without it the
	# customer-gold dispatcher could not tell this Repack was a conversion, wrote no Conversion
	# events, and a receipt batch refilled here read as holding nothing returnable. Every row of
	# one Settle conversion is one owner, so provenance scoping is unchanged.
	lane = lane_tag(inv, customer)
	se = _new_se(mr, REPACK_SE_TYPE, "Repack", warehouse, warehouse, customer)
	for sr in source_rows:
		_append_item(
			se,
			{
				"item_code": source_item,
				"qty": flt(sr["qty"], 3),
				"batch_no": sr["batch_no"],
				"s_warehouse": warehouse,
				"inventory_type": inv,
				"customer": customer,
				"custom_conversion_lane": lane,
			},
		)
	produced_row = {
		"item_code": target_item,
		"qty": flt(target_qty, 3),
		"t_warehouse": warehouse,
		"inventory_type": inv,
		"customer": customer,
		"custom_conversion_lane": lane,
	}
	if target_batch:
		produced_row["batch_no"] = target_batch
	_append_item(se, produced_row)
	se.insert(ignore_permissions=True)
	_submit_consuming_stock_entry(se)

	if target_batch:
		return target_batch

	produced = frappe.db.get_value(
		"Stock Entry Detail",
		{"parent": se.name, "item_code": target_item, "t_warehouse": warehouse},
		"batch_no",
	)
	if not produced:
		frappe.throw(
			_("{0} {1} did not create a target batch.").format(REPACK_SE_TYPE, se.name)
		)
	return produced
