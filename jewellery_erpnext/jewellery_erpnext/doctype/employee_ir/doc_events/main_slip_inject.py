# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
"""Main Slip-required Employee IR Receive: repack extra returned metal from
the employee/subcontractor warehouse into the MOP department warehouse so the
positive delta surfaces as a MOP Log row via the existing Stock Entry bridge.

Gate:  ``eir.is_raw_material = 1`` AND ``row.received_gross_wt > row.gross_wt``

Mechanism per row (with ``eir.main_slip`` set)::

    required_qty_per_colour = (received_gross_wt - gross_wt) / #colours

    for each colour -> resolved target metal item (via MWO attributes):
        walk Main Slip ``batch_details`` rows (variant_of = "M") in inventory_type
        priority order: Customer Goods / Customer Stock -> Regular Stock -> Pure Metal
        (``ownership_priority.CONSUME_PRIORITY`` -- a job draws down the customer's
        own metal before the company's)

        for each available batch segment:
            consume_qty = min(batch.available, remaining_required)
            if inventory_type == "Pure Metal" AND eir.subcontracting == "Yes":
                -> build a Repack SE (purity conversion 24KT -> target alloy)
                   produce_qty = consume_qty * source_purity / target_purity
            else:
                -> build a Material Transfer (WORK ORDER) SE
                   produce_qty == consume_qty
            submit; the existing sync_mop_log_for_stock_entry bridge writes
            the positive MOP Log row against the MOP

Fallback (no ``eir.main_slip`` on record): resolve target alloy per MWO colour,
read **Bin** stock in the employee/subcontractor **MSL (Raw Material) warehouse**.
If enough alloy exists → **Material Transfer (WORK ORDER)** only; otherwise
**Repack** pure→alloy (purity conversion). Partial alloy coverage may emit both
(one MT SE and one Repack SE). The MSL warehouse is the single source for all
injected metal — Manufacturing/WIP warehouses are never consumed here.

Insufficient stock: explicit ``frappe.throw`` with available vs required
numbers; never silent-skip.

Idempotency: Main Slip path uses ``(employee_ir, custom_eir_operation_row)``
with ``auto_created = 1``. Fallback path checks **per** ``stock_entry_type``
(MT vs Repack) so a completed transfer does not block a pending repack on retry.

Cancel: ``cancel_injections_for_eir`` iterates auto-created SEs for the EIR
and calls ``.cancel()``; their ``on_cancel`` flips MOP Log rows to
``is_cancelled = 1`` via the existing bridge.

Stock Reservation: ``stock_reservation_entry_for_mwo`` (Stock Entry ``on_submit``)
runs for SE types listed in **MOP Settings → Stock Entry Type To Reservation**
(ensure **Repack** and **Material Transfer (WORK ORDER)** are included where needed).
"""

from __future__ import annotations

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, flt, nowtime, today

from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
	capped_auto_batch_nos,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.ownership_priority import (
	allocate_in_order,
	batch_priority_map,
	batch_sort_key,
	consume_rank,
	stamp_produce_rows_from_consumes,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)
from jewellery_erpnext.utils import get_item_from_attribute

REPACK_STOCK_ENTRY_TYPE = "Repack"
MATERIAL_TRANSFER_STOCK_ENTRY_TYPE = "Material Transfer (WORK ORDER)"
PROCESS_LOSS_STOCK_ENTRY_TYPE = "Process Loss"


def _ensure_posting_datetime(se):
	if not se.get("posting_date"):
		se.posting_date = today()
	if not se.get("posting_time"):
		se.posting_time = nowtime()


def _row_to_append_dict(row):
	if isinstance(row, dict):
		d = {
			k: v
			for k, v in row.items()
			if k not in ("name", "idx", "owner", "creation", "modified")
		}
	else:
		d = row.as_dict()
		for k in ("name", "idx", "owner", "creation", "modified"):
			d.pop(k, None)
	return d


def _row_qty_val(row):
	return flt(row.get("qty") if isinstance(row, dict) else row.qty)


def _expand_source_rows_for_fifo(se, row, mode=None):
	"""Split one outgoing row into per-batch rows, ownership tier first then FIFO.

	``mode`` is ``"consume"`` (customer metal drawn first) or ``"loss"`` (company
	metal written off first); resolved by ``_apply_fifo_batches_to_stock_entry``
	from the SE type when not passed.

	Five early exits, in order -- a row that takes any of them is returned
	untouched, so ownership ordering does NOT reach it: no ``s_warehouse``
	(produce rows), a serial, a ``batch_no`` already picked by the caller, a
	non-batched item, and finally "no positive batch", which drops the row.
	"""
	item_code = row.get("item_code") if isinstance(row, dict) else row.item_code
	s_wh = row.get("s_warehouse") if isinstance(row, dict) else row.s_warehouse
	if not s_wh:
		return [_row_to_append_dict(row)]
	serial = row.get("serial_no") if isinstance(row, dict) else row.serial_no
	if serial:
		return [_row_to_append_dict(row)]
	batch_no = row.get("batch_no") if isinstance(row, dict) else row.batch_no
	if batch_no:
		return [_row_to_append_dict(row)]
	if not frappe.get_cached_value("Item", item_code, "has_batch_no"):
		return [_row_to_append_dict(row)]

	need = _row_qty_val(row)
	# `qty` is deliberately NOT passed. capped_auto_batch_nos re-applies ERPNext's
	# own qty-based FIFO truncation (get_qty_based_available_batches) as its LAST
	# step, which walks the pool in Batch.creation order and breaks -- discarding
	# every later batch. Asking for `need` therefore picks the batches before we
	# ever see them, and any ownership sort afterwards would be a no-op. Fetch the
	# whole (already balance-capped) pool, rank it, then allocate ourselves. Same
	# reasoning as tree_stock_entry._tree_owed_batches.
	kwargs = frappe._dict(
		posting_date=se.posting_date,
		posting_time=se.posting_time,
		item_code=item_code,
		warehouse=s_wh,
		for_stock_levels=False,
		consider_negative_batches=False,
	)
	batches = capped_auto_batch_nos(kwargs) or []

	# Transfer rows land in a MOP department warehouse where the EIR injection then
	# creates a Stock Reservation Entry. ERPNext's SRE before_submit independently
	# re-checks batch availability, so a plain FIFO pick can choose a batch that is
	# already over-reserved at the destination and blow up with "Stock not available
	# to reserve ... against Batch ...". Prefer source batches that are reservable at
	# the destination; fall back to the plain FIFO result if none can cover `need`.
	t_wh = row.get("t_warehouse") if isinstance(row, dict) else row.t_warehouse
	if t_wh:
		reservable = _select_fifo_batches_reservable_at_dest(
			se, item_code, s_wh, t_wh, need, mode=mode
		)
		if reservable is not None:
			batches = reservable

	pool = [b for b in batches if flt(b.qty) > 0]
	if not pool:
		if round(need, 6) > 0:
			_throw_insufficient(item_code, need, s_wh, 0.0)
		return []

	ranks = batch_priority_map([b.batch_no for b in pool])
	pool.sort(
		key=lambda b: batch_sort_key(
			b.batch_no, ranks.get(b.batch_no), mode or "consume"
		)
	)

	available = flt(sum(flt(b.qty) for b in pool))
	allocation, shortfall = allocate_in_order(
		[(b.batch_no, flt(b.qty)) for b in pool], need, _fifo_precision()
	)
	if round(shortfall, 6) > 0:
		_throw_insufficient(item_code, need, s_wh, available)
	if not allocation:
		return []

	out = []
	base = _row_to_append_dict(row)
	for picked_batch, qty_val in allocation:
		line = dict(base)
		line["batch_no"] = picked_batch
		line["qty"] = qty_val
		line["transfer_qty"] = qty_val
		meta = ranks.get(picked_batch)
		if meta:
			inv, cust = normalize_ownership(
				meta.get("inventory_type"),
				meta.get("customer"),
				batch_no=picked_batch,
				item_code=item_code,
			)
			if inv:
				line["inventory_type"] = inv
			if cust:
				line["customer"] = cust
		out.append(line)
	return out


def _fifo_precision():
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def _throw_insufficient(item_code, need, s_wh, got):
	frappe.throw(
		_(
			"EIR injection: insufficient FIFO batch stock for {0}: need {1} g in {2} (available {3})."
		).format(item_code, need, s_wh, got)
	)


def _select_fifo_batches_reservable_at_dest(se, item_code, s_wh, t_wh, need, mode=None):
	"""Pick source batches that can also be *reserved* at the destination.

	Walks the source warehouse batches in ownership-tier order (then FIFO within a
	tier), skips any batch that is already over-reserved at ``t_wh`` (existing Stock
	Reservation Entries exceed the batch's actual qty there), and allocates ``need``
	across the rest. Returns a list of ``frappe._dict(batch_no, qty)`` that covers
	``need``, or ``None`` when the reservable batches cannot cover it (caller falls
	back to the plain pool, which is ranked the same way).
	"""
	all_batches = (
		capped_auto_batch_nos(
			frappe._dict(
				posting_date=se.posting_date,
				posting_time=se.posting_time,
				item_code=item_code,
				warehouse=s_wh,
				for_stock_levels=False,
				consider_negative_batches=False,
			)
		)
		or []
	)
	candidates = [b for b in all_batches if flt(b.get("qty")) > 0]
	if not candidates:
		return None

	# Deterministic FIFO by Batch creation and name, THEN re-bucketed by ownership
	# tier. Two stable sorts in that order leave FIFO intact inside each tier. One
	# round-trip now covers both the creation tie-break and the rank; this used to
	# be a creation-only query here plus a second ownership query further down.
	ranks = batch_priority_map([c.batch_no for c in candidates])
	candidates.sort(
		key=lambda c: ((ranks.get(c.batch_no) or {}).get("creation") or "", c.batch_no)
	)
	candidates.sort(
		key=lambda c: batch_sort_key(
			c.batch_no, ranks.get(c.batch_no), mode or "consume"
		)
	)

	# Destination actual qty (ignoring reservations) and active reserved qty,
	# per batch — one round-trip each.
	dest_actual = {
		d["batch_no"]: flt(d.get("qty"))
		for d in (
			get_batch_qty(
				item_code=item_code, warehouse=t_wh, ignore_reserved_stock=True
			)
			or []
		)
	}
	dest_reserved = {
		r["batch_no"]: flt(r["qty"])
		for r in frappe.db.sql(
			"""
			SELECT sbe.batch_no AS batch_no,
			       SUM(sbe.qty - IFNULL(sbe.delivered_qty, 0)) AS qty
			FROM `tabSerial and Batch Entry` sbe
			JOIN `tabStock Reservation Entry` sre ON sbe.parent = sre.name
			WHERE sre.docstatus = 1
			  AND sre.item_code = %s
			  AND sbe.warehouse = %s
			GROUP BY sbe.batch_no
			""",
			(item_code, t_wh),
			as_dict=True,
		)
	}

	eps = 1e-6
	allocation = []
	remaining = need
	for b in candidates:
		if remaining <= eps:
			break
		# Over-reserved at the destination: even after the transfer adds qty, the
		# batch's available-to-reserve would stay <= 0, so ERPNext would reject it.
		if (
			flt(dest_actual.get(b.batch_no, 0.0))
			- flt(dest_reserved.get(b.batch_no, 0.0))
			< -eps
		):
			continue
		take = min(flt(b.qty), remaining)
		if take <= eps:
			continue
		allocation.append(frappe._dict(batch_no=b.batch_no, qty=round(take, 6)))
		remaining = round(remaining - take, 6)

	if remaining > eps:
		return None
	return allocation


def _se_priority_mode(se):
	"""``"loss"`` for a Process Loss SE, else ``"consume"``.

	The two ownership rules are opposites: metal is CONSUMED customer-first, but
	written off company-first. A Process Loss SE is the only builder whose source
	rows are a write-off, so it is the only one that flips.

	Note this keys on ``stock_entry_type``, not ``purpose``: a Process Loss SE
	carries ``purpose = "Repack"`` but ``stock_entry_type = "Process Loss"``, so it
	takes the *transfer* branch of the dispatcher below while still ranking as loss.
	"""
	return (
		"loss"
		if se.get("stock_entry_type") == PROCESS_LOSS_STOCK_ENTRY_TYPE
		else "consume"
	)


def _apply_fifo_batches_to_stock_entry(se):
	"""Populate ``batch_no`` on source rows, ownership tier first then FIFO."""
	_ensure_posting_datetime(se)
	if not se.get("items"):
		return
	mode = _se_priority_mode(se)
	if se.stock_entry_type == REPACK_STOCK_ENTRY_TYPE:
		_apply_fifo_to_repack_stock_entry(se, mode)
	else:
		_apply_fifo_to_transfer_stock_entry(se, mode)


def _apply_fifo_to_transfer_stock_entry(se, mode=None):
	new_items = []
	for row in list(se.items):
		new_items.extend(_expand_source_rows_for_fifo(se, row, mode))
	se.set("items", [])
	for d in new_items:
		se.append("items", d)


def _apply_fifo_to_repack_stock_entry(se, mode=None):
	rows = list(se.items)
	new_items = []
	i = 0
	while i < len(rows):
		cur = rows[i]
		nxt = rows[i + 1] if i + 1 < len(rows) else None
		cur_s = cur.get("s_warehouse") if isinstance(cur, dict) else cur.s_warehouse
		cur_t = cur.get("t_warehouse") if isinstance(cur, dict) else cur.t_warehouse
		nxt_t = (
			nxt.get("t_warehouse")
			if nxt and isinstance(nxt, dict)
			else getattr(nxt, "t_warehouse", None)
		)
		nxt_s = (
			nxt.get("s_warehouse")
			if nxt and isinstance(nxt, dict)
			else getattr(nxt, "s_warehouse", None)
		)

		if cur_s and not cur_t and nxt and nxt_t and not nxt_s:
			c_tot = _row_qty_val(cur)
			p_tot = _row_qty_val(nxt)
			consumes = _expand_source_rows_for_fifo(se, cur, mode)
			rem_p = p_tot
			for j, c in enumerate(consumes):
				cq = flt(c.get("qty"))
				if j == len(consumes) - 1:
					pq = round(rem_p, 3)
				else:
					pq = round(p_tot * (cq / c_tot), 3) if c_tot else 0
					rem_p -= pq
				new_items.append(c)
				prod = _row_to_append_dict(nxt)
				prod["qty"] = pq
				prod["transfer_qty"] = pq
				new_items.append(prod)
			i += 2
		else:
			new_items.append(_row_to_append_dict(cur))
			i += 1
	se.set("items", [])
	for d in new_items:
		se.append("items", d)

	# The consume rows above were resolved (and ownership-stamped) by
	# _expand_source_rows_for_fifo, but each produce row is a plain copy of the
	# original with only qty changed -- it carries NO ownership. Left bare,
	# doc_events/stock_entry.before_validate blanket-defaults it to "Regular Stock"
	# and the alloy batch minted from that row inherits company ownership, so a
	# purity Repack of a customer's pure metal silently launders it into ours.
	# precision left to the helper: it is only needed for a mixed-owner split, and
	# resolving it eagerly would cost a meta lookup on every repack.
	stamp_produce_rows_from_consumes(se, row_to_dict=_row_to_append_dict)


def _pure_metal_item_for_mwo(mwo_name):
	manufacturer = frappe.get_cached_value(
		"Manufacturing Work Order", mwo_name, "manufacturer"
	)
	if not manufacturer:
		return None
	return frappe.get_cached_value(
		"Manufacturing Setting",
		{"manufacturer": manufacturer},
		"pure_gold_item",
	)


def _get_bin_qty(item_code, warehouse):
	if not item_code or not warehouse:
		return 0.0
	return flt(
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
		)
	)


def _merge_transfer_segments(raw_transfer):
	"""Combine transfer lines for the same item into one row.

	All transfer segments share a single source (MSL) and target (department)
	warehouse, so the merge key is item-only.
	"""
	by_item = {}
	for seg in raw_transfer:
		if seg.get("mode") != "transfer":
			continue
		by_item[seg["item_code"]] = by_item.get(seg["item_code"], 0) + flt(seg["qty"])
	return [
		{"mode": "transfer", "item_code": ic, "qty": round(qty, 3)}
		for ic, qty in by_item.items()
		if qty > 0
	]


def _resolve_fallback_inject_segments(eir, mwo_name, total_extra, dept_wh):
	"""Stock-first segments sourced exclusively from the MSL (Raw Material) warehouse.

	Per colour, in order:
	1. Alloy from MSL warehouse (Material Transfer (WORK ORDER)).
	2. Pure-metal repack from MSL warehouse (purity conversion to target alloy).
	3. ``frappe.throw`` with a precise shortage message if still short.
	"""
	msl_wh = _resolve_source_warehouse_raw_material(eir)
	if not msl_wh:
		frappe.throw(
			_("Main Slip injection: MSL warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)

	mwo = frappe.get_cached_value(
		"Manufacturing Work Order",
		mwo_name,
		[
			"metal_type",
			"metal_touch",
			"metal_purity",
			"metal_colour",
			"multicolour",
			"allowed_colours",
		],
		as_dict=True,
	)
	if not mwo or not (
		mwo.get("metal_type") and mwo.get("metal_touch") and mwo.get("metal_purity")
	):
		frappe.throw(
			_(
				"Main Slip injection: Manufacturing Work Order {0} is missing "
				"metal_type / metal_touch / metal_purity"
			).format(mwo_name)
		)

	colours = [mwo.get("metal_colour") or None]

	per_colour_qty = round(float(total_extra) / len(colours), 3)
	raw_transfer = []
	raw_purity = []
	pure_item = _pure_metal_item_for_mwo(mwo_name)
	purity_cache = {}

	for colour in colours:
		alloy_item = get_item_from_attribute(
			mwo["metal_type"], mwo["metal_touch"], mwo["metal_purity"], colour
		)
		if not alloy_item:
			frappe.throw(
				_(
					"Main Slip injection: cannot resolve metal Item for {0}/{1}/{2}/{3}"
				).format(
					mwo["metal_type"],
					mwo["metal_touch"],
					mwo["metal_purity"],
					colour,
				)
			)

		required = per_colour_qty

		# --- Priority 1: alloy from MSL warehouse ---
		msl_alloy = _get_bin_qty(alloy_item, msl_wh)
		if msl_alloy > 1e-6:
			take = round(min(msl_alloy, required), 3)
			raw_transfer.append(
				{"mode": "transfer", "item_code": alloy_item, "qty": take}
			)
			required = round(required - take, 3)

		if required <= 1e-6:
			continue

		# --- Priority 2: pure-metal repack from MSL warehouse ---
		if not pure_item:
			frappe.throw(
				_(
					"EIR injection: required metal not available for transfer or repack. "
					"Alloy {0} is short in MSL warehouse {1}; no pure gold item is "
					"configured for this work order's manufacturer."
				).format(alloy_item, msl_wh)
			)
		source_p = _purity_get(pure_item, purity_cache)
		target_p = _purity_get(alloy_item, purity_cache)
		if not source_p or not target_p:
			frappe.throw(
				_("EIR injection: cannot resolve metal_purity for {0} or {1}.").format(
					pure_item, alloy_item
				)
			)
		produce_qty = required
		consume_qty = round(produce_qty * target_p / source_p, 3)
		raw_purity.append(
			{
				"mode": "purity",
				"source_item": pure_item,
				"target_item": alloy_item,
				"consume_qty": consume_qty,
				"produce_qty": produce_qty,
			}
		)

	transfer_segments = _merge_transfer_segments(raw_transfer)
	return transfer_segments + raw_purity


def inject_extra_metal_for_eir_receive(eir, row):
	"""Per Employee IR Operation row gain, build + submit Stock Entries that
	push the extra returned metal into the MOP via the MOP Log bridge.

	Returns a list of created Stock Entry names (empty list if skipped).
	"""
	if not cint(getattr(eir, "is_raw_material", 0)):
		return []

	extra = flt(row.received_gross_wt) - flt(row.gross_wt)
	if extra <= 0:
		return []

	dept_wh = _resolve_department_warehouse(eir.department)
	if not dept_wh:
		frappe.throw(
			_(
				"Main Slip injection: MFG Warehouse not configured for department {0}"
			).format(eir.department)
		)

	if getattr(eir, "main_slip", None):
		if _existing_injection_se(eir.name, row.name):
			return []
		target_items = _resolve_inject_metal_items(row.manufacturing_work_order, extra)
		return _inject_via_main_slip_batches(eir, row, target_items, dept_wh)

	segments = _resolve_fallback_inject_segments(
		eir, row.manufacturing_work_order, extra, dept_wh
	)
	existing_types = _existing_injection_se_types(eir.name, row.name)
	if _fallback_injection_fully_submitted(segments, existing_types):
		return []
	return _inject_via_source_warehouse_fallback(
		eir, row, segments, dept_wh, existing_types
	)


def cancel_injections_for_eir(eir_name):
	"""Cancel every auto-created Main Slip injection SE owned by this EIR.

	The Stock Entry ``on_cancel`` hook flips the matching MOP Log rows to
	``is_cancelled = 1`` via the existing ``sync_mop_log_for_stock_entry``
	bridge, so the MOP balance reverses automatically.
	"""
	se_names = frappe.db.get_all(
		"Stock Entry",
		filters={
			"employee_ir": eir_name,
			"auto_created": 1,
			"docstatus": 1,
			"stock_entry_type": [
				"in",
				[REPACK_STOCK_ENTRY_TYPE, MATERIAL_TRANSFER_STOCK_ENTRY_TYPE],
			],
		},
		pluck="name",
	)
	for se_name in se_names:
		frappe.get_doc("Stock Entry", se_name).cancel()
	return se_names


# ---------------------------------------------------------------------------
# Path 1 - Main Slip batch_details walk (primary)
# ---------------------------------------------------------------------------


def _main_slip_pool(eir):
	"""The Main Slip batch pool for this Employee IR, resolved ONCE and shared.

	``inject_extra_metal_for_eir_receive`` runs once per ``employee_ir_operations``
	row, i.e. once per work order. Re-reading the pool per row meant every work
	order saw the batch at its full undepleted quantity: depletion was written only
	to the local list (``available_qty``), never back to the Main Slip, so two work
	orders on one Employee IR could each mint 6 g out of the same 10 g batch and the
	shortfall guard could never fire.

	Caching on ``eir.flags`` makes every work order allocate from one ledger, which
	is what "check all the work orders" requires. ``_persist_main_slip_consumption``
	then writes the total back so a *second* Employee IR cannot re-spend it either.
	"""
	pool = eir.flags.get("main_slip_pool")
	if pool is None:
		pool = list(_iter_main_slip_batches(eir.main_slip))
		eir.flags.main_slip_pool = pool
	return pool


def _persist_main_slip_consumption(pool):
	"""Write consumed qty back onto the Main Slip rows this EIR actually drew from.

	Without this the depletion is per-document: a second Employee IR against the
	same Main Slip re-reads the original ``consume_qty`` and spends the batch again.

	``frappe.db.set_value`` on the individual child rows, deliberately -- re-saving
	the Main Slip would trigger ``update_batch_details``, a full recompute that
	rebuilds ``consume_qty`` from ``stock_details`` (a table nothing populates any
	more) and would immediately undo this. ``batch_details`` is ``allow_on_submit``,
	so writing to a submitted slip is permitted.
	"""
	for batch in pool:
		drawn = flt(batch.get("_drawn"))
		if drawn <= 0:
			continue
		frappe.db.set_value(
			"Main Slip SE Details",
			batch["name"],
			"consume_qty",
			flt(flt(batch.get("consume_qty")) + drawn, _fifo_precision()),
			update_modified=False,
		)
		batch["consume_qty"] = flt(batch.get("consume_qty")) + drawn
		batch["_drawn"] = 0.0


def _inject_via_main_slip_batches(eir, row, target_items, dept_wh):
	"""Walk Main Slip batch_details in inventory-type priority and emit one SE
	per consumed segment until each target item's required qty is satisfied.

	The single source warehouse is the employee/subcontractor MSL (Raw Material)
	warehouse — Manufacturing/WIP warehouses are never consumed here.
	"""
	source_wh = _resolve_source_warehouse_raw_material(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: MSL warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)

	# Shared across every work order on this Employee IR — see _main_slip_pool.
	batches = _main_slip_pool(eir)
	se_names = []
	purity_cache = {}

	for target in target_items:
		remaining = flt(target["qty"])
		target_item = target["item_code"]
		produced = 0.0
		for batch in batches:
			if remaining <= 0:
				break
			available = flt(batch.get("available_qty"))
			if available <= 0:
				continue

			# Resolved from the BATCH by _iter_main_slip_batches, not from the row.
			# This decides Repack-vs-Material-Transfer, so it has to move together
			# with the ranking: a batch ranked Pure Metal but branched as Regular
			# takes the transfer path, gets skipped by the item_code guard below,
			# and silently under-produces until the shortfall throw.
			inv_type = (
				batch.get("_owner_meta") or (batch.get("inventory_type"), None)
			)[0]
			is_pure_subcontracting = (
				inv_type == "Pure Metal" and eir.subcontracting == "Yes"
			)

			if is_pure_subcontracting:
				# Purity conversion: how much of the pure source do we need to
				# produce `remaining` grams of the target alloy?
				source_purity = _purity_get(batch["item_code"], purity_cache)
				target_purity = _purity_get(target_item, purity_cache)
				if not source_purity or not target_purity:
					frappe.throw(
						_(
							"Main Slip injection: cannot resolve metal_purity for "
							"{0} or {1}."
						).format(batch["item_code"], target_item)
					)
				# We prefer to use up this batch before moving on; compute the
				# produced yield if we consume all of 'available', capped at
				# what remaining demand requires.
				max_produce_from_batch = round(
					available * source_purity / target_purity, 3
				)
				produce_this = min(max_produce_from_batch, remaining)
				consume_this = round(produce_this * target_purity / source_purity, 3)
				se = _build_purity_repack_se(
					eir,
					row,
					source_item=batch["item_code"],
					source_batch=batch.get("batch_no"),
					consume_qty=consume_this,
					target_item=target_item,
					produce_qty=produce_this,
					source_wh=source_wh,
					dept_wh=dept_wh,
					owner=batch.get("_owner_meta"),
				)
			else:
				# Direct Material Transfer; the batch item must match the
				# target alloy item (Main Slip must hold the correct grade).
				if batch.get("item_code") != target_item:
					continue  # not the right alloy; skip this batch
				take = min(available, remaining)
				se = _build_material_transfer_se(
					eir,
					row,
					item_code=target_item,
					batch_no=batch.get("batch_no"),
					qty=take,
					source_wh=source_wh,
					dept_wh=dept_wh,
					owner=batch.get("_owner_meta"),
				)
				produce_this = take
				consume_this = take

			se.flags.ignore_permissions = True
			_apply_fifo_batches_to_stock_entry(se)
			se.save()
			se.submit()
			se_names.append(se.name)

			batch["available_qty"] = available - consume_this
			# Track the draw so _persist_main_slip_consumption can write it back
			# to the Main Slip once this work order's SEs are all submitted.
			batch["_drawn"] = flt(batch.get("_drawn")) + consume_this
			remaining = round(remaining - produce_this, 3)
			produced += produce_this

		if remaining > 0:
			frappe.throw(
				_(
					"Main Slip injection: insufficient stock on Main Slip {0} "
					"batch_details to repack {1} gram(s) of {2}. Produced {3}, "
					"short by {4}."
				).format(
					eir.main_slip,
					target["qty"],
					target_item,
					round(produced, 3),
					round(remaining, 3),
				)
			)

	# Only after every target for this work order is satisfied — a shortfall throws
	# above and rolls the transaction back, so a partial draw is never persisted.
	_persist_main_slip_consumption(batches)
	return se_names


def _iter_main_slip_batches(main_slip):
	"""Yield Main Slip ``batch_details`` rows (``variant_of = 'M'``) in ownership
	priority order, only those with positive available qty.

	Order is ``CONSUME_PRIORITY``: Customer Goods / Customer Stock, then Regular
	Stock, then Pure Metal -- a job draws down the customer's own metal before the
	company's. This is the gain path (it mints metal because the receive came back
	heavier than it went out), so consuming customer-owned batches here draws a
	customer down without a backing issue. That is the accepted business rule.

	**Ranked on the BATCH, not on the row.** ``Main Slip SE Details.inventory_type``
	is ``fetch_from = se_item.inventory_type`` with ``fetch_if_empty: 1`` -- a
	write-once snapshot of whatever a Stock Entry Detail row said, never refreshed,
	and never sourced from ``Batch``. It drifts for at least four reasons: the batch
	is re-resolved on every ``Batch`` save; ``customer_subcontracting.batch_rename``
	hard-sets batches to Customer Goods after the fact; a blank SE row is
	blanket-defaulted to Regular Stock by ``doc_events/stock_entry``; and
	``batch_details`` rows carry no ``se_item``, so the fetch never fires on them at
	all. Meanwhile ``_stamp_row_ownership`` already BOOKS the SE off the batch --
	so ranking on the row meant ranking and booking could name different owners.

	The rank is taken **after** ``normalize_ownership``, not off the raw batch
	value. A Customer Goods batch with no customer normalizes to Regular Stock when
	stamped; ranking it 0 would reproduce the same contradiction from the other side.

	Each yielded row gets ``_owner_meta`` -- the resolved ``(inventory_type,
	customer)`` -- so downstream builders reuse it instead of re-querying per SE.
	"""
	rows = (
		frappe.db.get_all(
			"Main Slip SE Details",
			filters={
				"parent": main_slip,
				"parentfield": "batch_details",
				"variant_of": "M",
			},
			fields=[
				"name",
				"batch_no",
				"item_code",
				"qty",
				"consume_qty",
				"inventory_type",
				"customer",
				"variant_of",
				"creation",
			],
		)
		or []
	)
	if not rows:
		return

	# One round-trip for every batch on the slip.
	ranks = batch_priority_map([r.get("batch_no") for r in rows])
	for r in rows:
		r["_owner_meta"] = _resolve_row_owner(r, ranks)

	# Two stable sorts: the rows arrive unordered (no SQL ORDER BY), so
	# (creation, name) is doing real work, and re-sorting by rank afterwards keeps
	# that order inside each tier. See batch_sort_key's docstring for why the two
	# must not be folded into a single composite key.
	rows.sort(key=lambda r: (r.get("creation") or "", r.get("name") or ""))
	rows.sort(key=lambda r: consume_rank(r["_owner_meta"][0]))

	for r in rows:
		# Nothing validates consume_qty <= qty on the Main Slip (that check is
		# commented out in update_batch_details), so this can legitimately go
		# negative; the skip is the only guard.
		available = flt(r.get("qty", 0)) - flt(r.get("consume_qty", 0))
		if available <= 0:
			continue
		r["available_qty"] = available
		yield r


def _resolve_row_owner(row, ranks):
	"""``(inventory_type, customer)`` for a Main Slip batch row -- batch first.

	Falls back to the row's own fields only when the batch cannot be resolved
	(``batch_no`` is not ``reqd`` on this child table, and both Main Slip tables are
	``allow_on_submit``, so hand-entered rows with no batch are reachable). An
	unresolvable row keeps whatever the row claimed, which ``consume_rank`` then
	sorts last if it is unknown -- deliberately, so a row nobody can vouch for never
	outranks the customer metal being drained first.
	"""
	meta = ranks.get(row.get("batch_no"))
	if meta:
		return normalize_ownership(
			meta.get("inventory_type"),
			meta.get("customer"),
			batch_no=row.get("batch_no"),
			item_code=row.get("item_code"),
		)
	return normalize_ownership(
		row.get("inventory_type"),
		row.get("customer"),
		batch_no=row.get("batch_no"),
		item_code=row.get("item_code"),
	)


def _get_item_metal_purity(item_code):
	"""Return the numeric metal_purity attribute value for an Item, or None."""
	value = frappe.get_cached_value(
		"Item Variant Attribute",
		{"parent": item_code, "attribute": "Metal Purity"},
		"attribute_value",
	)
	try:
		return flt(value) if value is not None else None
	except (TypeError, ValueError):
		return None


def _purity_get(item_code, cache):
	"""Per-call memoized purity lookup; one DB round-trip per unique item."""
	if item_code in cache:
		return cache[item_code]
	val = _get_item_metal_purity(item_code)
	cache[item_code] = val
	return val


# ---------------------------------------------------------------------------
# Path 2 - source-warehouse fallback (no Main Slip batch details configured)
# ---------------------------------------------------------------------------


def _inject_via_source_warehouse_fallback(eir, row, segments, dept_wh, existing_types):
	"""Submit Stock Entries for the resolved segments.

	The single source is the employee/subcontractor MSL (Raw Material) warehouse;
	all transfer and repack rows post against this one warehouse. ``existing_types``
	is the set of stock_entry_types already auto-created for this (eir, row), so
	this function never re-queries Stock Entry to check idempotency.
	"""
	source_wh = _resolve_source_warehouse_raw_material(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: MSL warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)
	transfer_segs = [s for s in segments if s.get("mode") == "transfer"]
	purity_segs = [s for s in segments if s.get("mode") == "purity"]

	_validate_fallback_segments_against_source_bin(
		transfer_segs, purity_segs, source_wh
	)

	out = []
	if transfer_segs and MATERIAL_TRANSFER_STOCK_ENTRY_TYPE not in existing_types:
		se_mt = _build_material_transfer_from_segments(
			eir, row, transfer_segs, source_wh, dept_wh
		)
		se_mt.flags.ignore_permissions = True
		_apply_fifo_batches_to_stock_entry(se_mt)
		se_mt.save()
		se_mt.submit()
		out.append(se_mt.name)

	if purity_segs and REPACK_STOCK_ENTRY_TYPE not in existing_types:
		se_rp = _build_repack_from_purity_segments(
			eir, row, purity_segs, source_wh, dept_wh
		)
		se_rp.flags.ignore_permissions = True
		_apply_fifo_batches_to_stock_entry(se_rp)
		se_rp.save()
		se_rp.submit()
		out.append(se_rp.name)

	return out


# ---------------------------------------------------------------------------
# Helpers shared by both paths
# ---------------------------------------------------------------------------


def _existing_injection_se(eir_name, row_name):
	return frappe.db.exists(
		"Stock Entry",
		{
			"employee_ir": eir_name,
			"custom_eir_operation_row": row_name,
			"auto_created": 1,
			"docstatus": ["!=", 2],
		},
	)


def _existing_injection_se_types(eir_name, row_name):
	"""Return the set of auto-created Stock Entry types already present for this
	(eir, row). One SQL round-trip; consumers do O(1) membership tests."""
	return set(
		frappe.db.get_all(
			"Stock Entry",
			filters={
				"employee_ir": eir_name,
				"custom_eir_operation_row": row_name,
				"auto_created": 1,
				"docstatus": ["!=", 2],
			},
			pluck="stock_entry_type",
			distinct=True,
		)
	)


def _fallback_injection_fully_submitted(segments, existing_types):
	"""True when every entry-type implied by ``segments`` already exists in
	``existing_types``. Pure in-memory check — no DB calls."""
	needed = set()
	for s in segments:
		mode = s.get("mode")
		if mode == "transfer":
			needed.add(MATERIAL_TRANSFER_STOCK_ENTRY_TYPE)
		elif mode == "purity":
			needed.add(REPACK_STOCK_ENTRY_TYPE)
	return needed.issubset(existing_types)


def _validate_fallback_segments_against_source_bin(
	transfer_segs, purity_segs, source_wh
):
	"""Validate every required item has sufficient Bin stock in the MSL warehouse.

	Uses a single ``Bin`` query for all required items, so the cost is O(1) DB
	round-trips regardless of segment count.
	"""
	need = {}  # item_code -> required_qty
	for seg in transfer_segs:
		need[seg["item_code"]] = need.get(seg["item_code"], 0) + flt(seg["qty"])
	for seg in purity_segs:
		need[seg["source_item"]] = need.get(seg["source_item"], 0) + flt(
			seg["consume_qty"]
		)
	if not need:
		return

	on_hand = {
		b["item_code"]: flt(b["actual_qty"])
		for b in frappe.db.get_all(
			"Bin",
			filters={
				"item_code": ["in", list(need.keys())],
				"warehouse": source_wh,
			},
			fields=["item_code", "actual_qty"],
		)
	}
	shortages = [
		(ic, qty, on_hand.get(ic, 0.0))
		for ic, qty in need.items()
		if on_hand.get(ic, 0.0) < qty
	]
	if shortages:
		lines = [
			_(
				"Insufficient stock to Repack {0} of {1} from {2} (available {3})."
			).format(qty, ic, source_wh, actual)
			for ic, qty, actual in shortages
		]
		lines.append(_("Cannot complete Main Slip Receive injection."))
		frappe.throw("\n".join(lines))


def _resolve_inject_metal_items(mwo_name, total_extra):
	"""Return [{item_code, qty}] using MWO metal attributes.
	Multicolour MWO: even-split total_extra across allowed_colours."""
	mwo = frappe.get_cached_value(
		"Manufacturing Work Order",
		mwo_name,
		[
			"metal_type",
			"metal_touch",
			"metal_purity",
			"metal_colour",
			"multicolour",
			"allowed_colours",
		],
		as_dict=True,
	)
	if not mwo or not (
		mwo.get("metal_type") and mwo.get("metal_touch") and mwo.get("metal_purity")
	):
		frappe.throw(
			_(
				"Main Slip injection: Manufacturing Work Order {0} is missing "
				"metal_type / metal_touch / metal_purity"
			).format(mwo_name)
		)

	colours = []
	if cint(mwo.get("multicolour")) and mwo.get("allowed_colours"):
		colours = [c.strip() for c in mwo["allowed_colours"].split(",") if c.strip()]
	if not colours:
		colours = [mwo.get("metal_colour") or None]

	# Use plain round() so this helper does not read System Settings via
	# frappe.utils.flt - keeps the function test-friendly and deterministic.
	per_colour_qty = round(float(total_extra) / len(colours), 3)
	items = []
	for colour in colours:
		item_code = get_item_from_attribute(
			mwo["metal_type"], mwo["metal_touch"], mwo["metal_purity"], colour
		)
		if not item_code:
			frappe.throw(
				_(
					"Main Slip injection: cannot resolve metal Item for {0}/{1}/{2}/{3}"
				).format(
					mwo["metal_type"],
					mwo["metal_touch"],
					mwo["metal_purity"],
					colour,
				)
			)
		items.append({"item_code": item_code, "qty": per_colour_qty, "batch_no": None})
	return items


def _resolve_source_warehouse(eir):
	return _resolve_source_warehouse_raw_material(eir)


def _resolve_source_warehouse_raw_material(eir):
	if eir.subcontracting == "Yes":
		return frappe.get_cached_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": eir.company,
				"subcontractor": eir.subcontractor,
				"warehouse_type": "Raw Material",
			},
		)
	return frappe.get_cached_value(
		"Warehouse",
		{
			"disabled": 0,
			"employee": eir.employee,
			"warehouse_type": "Raw Material",
		},
	)


def _resolve_department_warehouse(department):
	return frappe.get_cached_value(
		"Warehouse",
		{
			"disabled": 0,
			"department": department,
			"warehouse_type": "Manufacturing",
		},
	)


def _stamp_se_header(se, eir, row):
	se.company = eir.company
	se.manufacturing_order = frappe.get_cached_value(
		"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_order"
	)
	se.manufacturing_work_order = row.manufacturing_work_order
	se.manufacturing_operation = row.manufacturing_operation
	se.employee_ir = eir.name
	se.custom_eir_operation_row = row.name
	se.auto_created = 1
	if eir.subcontracting == "Yes":
		se.subcontractor = eir.subcontractor
	else:
		se.employee = eir.employee


def _build_material_transfer_from_segments(
	eir, row, transfer_segments, source_wh, dept_wh
):
	"""Fallback path: one Material Transfer (WORK ORDER) SE from merged transfer segments.

	All rows post from the single MSL ``source_wh`` to the MOP department warehouse.
	"""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = MATERIAL_TRANSFER_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	for seg in transfer_segments:
		se.append(
			"items",
			{
				"item_code": seg["item_code"],
				"qty": seg["qty"],
				"s_warehouse": source_wh,
				"t_warehouse": dept_wh,
				"uom": "Gram",
				"manufacturing_operation": row.manufacturing_operation,
				"custom_manufacturing_work_order": row.manufacturing_work_order,
				"use_serial_batch_fields": 1,
			},
		)
	return se


def _build_repack_from_purity_segments(eir, row, purity_segments, source_wh, dept_wh):
	"""Fallback path: one Repack SE from pure→alloy segments (consume / produce pairs).

	Pure metal is consumed from the single MSL ``source_wh``; the produced alloy
	lands in the MOP department warehouse.
	"""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	for seg in purity_segments:
		se.append(
			"items",
			{
				"item_code": seg["source_item"],
				"qty": seg["consume_qty"],
				"s_warehouse": source_wh,
				"uom": "Gram",
				"use_serial_batch_fields": 1,
			},
		)
		se.append(
			"items",
			{
				"item_code": seg["target_item"],
				"qty": seg["produce_qty"],
				"t_warehouse": dept_wh,
				"uom": "Gram",
				"manufacturing_operation": row.manufacturing_operation,
				"custom_manufacturing_work_order": row.manufacturing_work_order,
				"use_serial_batch_fields": 1,
			},
		)
	return se


def _stamp_row_ownership(item, batch_no, item_code=None, owner=None):
	"""Carry ``batch_no``'s ownership onto an SE row dict, in place.

	Every row this module builds already knows its batch, so it takes
	``_expand_source_rows_for_fifo``'s "batch_no already set" early exit and is
	never stamped by the FIFO helper. Left bare, ``doc_events/stock_entry``'s
	``before_validate`` blanket-defaults ``inventory_type`` to Regular Stock, and
	the batch minted from that row (``Batch.update_inventory_dimentions`` reads it
	back off ``custom_voucher_detail_no``) inherits the wrong owner -- a customer's
	metal silently becomes company stock.

	This was dormant while Regular Stock ranked first; with customer-first
	consumption it is the default path, so the stamp is mandatory.

	``owner`` is a pre-resolved ``(inventory_type, customer)`` pair from
	``_iter_main_slip_batches``; passing it avoids one Batch query per Stock Entry
	built. Without it the batch is resolved here.
	"""
	if owner is not None:
		# Already resolved in bulk by _iter_main_slip_batches; no second query.
		inv, cust = owner
	else:
		if not batch_no:
			return item
		meta = batch_priority_map([batch_no]).get(batch_no)
		if not meta:
			return item
		inv, cust = normalize_ownership(
			meta.get("inventory_type"),
			meta.get("customer"),
			batch_no=batch_no,
			item_code=item_code or item.get("item_code"),
		)
	if inv:
		item["inventory_type"] = inv
	if cust:
		item["customer"] = cust
	return item


def _build_material_transfer_se(
	eir, row, item_code, batch_no, qty, source_wh, dept_wh, owner=None
):
	"""Main Slip path: Material Transfer (WORK ORDER) - consume + produce the
	same item, from source warehouse to MOP department warehouse."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = MATERIAL_TRANSFER_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	se.append(
		"items",
		_stamp_row_ownership(
			{
				"item_code": item_code,
				"qty": qty,
				"s_warehouse": source_wh,
				"t_warehouse": dept_wh,
				"uom": "Gram",
				"batch_no": batch_no,
				"manufacturing_operation": row.manufacturing_operation,
				"custom_manufacturing_work_order": row.manufacturing_work_order,
				"use_serial_batch_fields": 1,
			},
			batch_no,
			item_code,
			owner=owner,
		),
	)
	return se


def _build_purity_repack_se(
	eir,
	row,
	source_item,
	source_batch,
	consume_qty,
	target_item,
	produce_qty,
	source_wh,
	dept_wh,
	owner=None,
):
	"""Main Slip subcontracting Pure-Metal path: Repack SE that consumes the
	pure metal (typically 24KT) from the source warehouse and produces the
	target alloy into the MOP department warehouse. Purity conversion applied
	by the caller: produce_qty = consume_qty * source_purity / target_purity.
	"""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	consume = _stamp_row_ownership(
		{
			"item_code": source_item,
			"qty": consume_qty,
			"s_warehouse": source_wh,
			"uom": "Gram",
			"batch_no": source_batch,
			"use_serial_batch_fields": 1,
		},
		source_batch,
		source_item,
		owner=owner,
	)
	se.append("items", consume)
	# The produce row mints a NEW batch of the target alloy, and that batch reads
	# its owner off this row. Carry the consumed batch's ownership across, or a
	# repack of customer metal produces company stock.
	produce = {
		"item_code": target_item,
		"qty": produce_qty,
		"t_warehouse": dept_wh,
		"uom": "Gram",
		"manufacturing_operation": row.manufacturing_operation,
		"custom_manufacturing_work_order": row.manufacturing_work_order,
		"use_serial_batch_fields": 1,
	}
	inv, cust = normalize_ownership(
		consume.get("inventory_type"),
		consume.get("customer"),
		batch_no=source_batch,
		item_code=target_item,
	)
	if inv:
		produce["inventory_type"] = inv
	if cust:
		produce["customer"] = cust
	se.append("items", produce)
	return se
