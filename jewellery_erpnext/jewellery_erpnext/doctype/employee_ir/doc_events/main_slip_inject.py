# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
"""Main Slip-required Employee IR Receive: repack extra returned metal from
the employee/subcontractor warehouse into the MOP department warehouse so the
positive delta surfaces as a MOP Log row via the existing Stock Entry bridge.

Gate:  ``eir.is_main_slip_required = 1`` AND ``row.received_gross_wt > row.gross_wt``

Mechanism per row (with ``eir.main_slip`` set)::

    required_qty_per_colour = (received_gross_wt - gross_wt) / #colours

    for each colour -> resolved target metal item (via MWO attributes):
        walk Main Slip ``batch_details`` rows (variant_of = "M") in inventory_type
        priority order: Regular Stock -> Customer Goods -> Pure Metal

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
read **Bin** stock in the employee/subcontractor Manufacturing warehouse. If
enough alloy exists → **Material Transfer (WORK ORDER)** only; otherwise
**Repack** pure→alloy (purity conversion). Partial alloy coverage may emit both
(one MT SE and one Repack SE).

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
from frappe import _
from frappe.utils import cint, flt, nowtime, today

from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_auto_batch_nos,
)

from jewellery_erpnext.utils import get_item_from_attribute


REPACK_STOCK_ENTRY_TYPE = "Repack"
MATERIAL_TRANSFER_STOCK_ENTRY_TYPE = "Material Transfer (WORK ORDER)"

# Priority order for consuming Main Slip batch_details when injecting extra
# metal into MOP. Lower index = higher priority.
INVENTORY_TYPE_PRIORITY = {
	"Regular Stock": 0,
	"Customer Goods": 1,
	"Pure Metal": 2,
}


def _ensure_posting_datetime(se):
	if not se.get("posting_date"):
		se.posting_date = today()
	if not se.get("posting_time"):
		se.posting_time = nowtime()


def _row_to_append_dict(row):
	if isinstance(row, dict):
		d = {k: v for k, v in row.items() if k not in ("name", "idx", "owner", "creation", "modified")}
	else:
		d = row.as_dict()
		for k in ("name", "idx", "owner", "creation", "modified"):
			d.pop(k, None)
	return d


def _row_qty_val(row):
	return flt(row.get("qty") if isinstance(row, dict) else row.qty)


def _expand_source_rows_for_fifo(se, row):
	"""Split one outgoing row into FIFO batch rows. Returns list of dicts for ``append('items')``."""
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
	if not frappe.db.get_value("Item", item_code, "has_batch_no"):
		return [_row_to_append_dict(row)]

	need = _row_qty_val(row)
	kwargs = frappe._dict(
		posting_date=se.posting_date,
		posting_time=se.posting_time,
		item_code=item_code,
		warehouse=s_wh,
		qty=need,
		for_stock_levels=False,
		consider_negative_batches=False,
	)
	batches = get_auto_batch_nos(kwargs) or []
	got = sum(flt(b.qty) for b in batches)
	if round(need - got, 6) > 0:
		frappe.throw(
			_(
				"EIR injection: insufficient FIFO batch stock for {0}: need {1} g in {2} (available {3})."
			).format(item_code, need, s_wh, got)
		)

	out = []
	base = _row_to_append_dict(row)
	for b in batches:
		if flt(b.qty) <= 0:
			continue
		line = dict(base)
		line["batch_no"] = b.batch_no
		line["qty"] = flt(b.qty)
		line["transfer_qty"] = flt(b.qty)
		inv = frappe.db.get_value(
			"Batch",
			b.batch_no,
			["custom_inventory_type", "custom_customer"],
			as_dict=True,
		)
		if inv:
			if inv.get("custom_inventory_type"):
				line["inventory_type"] = inv.custom_inventory_type
			if inv.get("custom_customer"):
				line["customer"] = inv.custom_customer
		out.append(line)
	return out


def _apply_fifo_batches_to_stock_entry(se):
	"""Populate ``batch_no`` on source rows using ERPNext FIFO (``get_auto_batch_nos``)."""
	_ensure_posting_datetime(se)
	if not se.get("items"):
		return
	if se.stock_entry_type == REPACK_STOCK_ENTRY_TYPE:
		_apply_fifo_to_repack_stock_entry(se)
	else:
		_apply_fifo_to_transfer_stock_entry(se)


def _apply_fifo_to_transfer_stock_entry(se):
	new_items = []
	for row in list(se.items):
		new_items.extend(_expand_source_rows_for_fifo(se, row))
	se.set("items", [])
	for d in new_items:
		se.append("items", d)


def _apply_fifo_to_repack_stock_entry(se):
	rows = list(se.items)
	new_items = []
	i = 0
	while i < len(rows):
		cur = rows[i]
		nxt = rows[i + 1] if i + 1 < len(rows) else None
		cur_s = cur.get("s_warehouse") if isinstance(cur, dict) else cur.s_warehouse
		cur_t = cur.get("t_warehouse") if isinstance(cur, dict) else cur.t_warehouse
		nxt_t = nxt.get("t_warehouse") if nxt and isinstance(nxt, dict) else getattr(nxt, "t_warehouse", None)
		nxt_s = nxt.get("s_warehouse") if nxt and isinstance(nxt, dict) else getattr(nxt, "s_warehouse", None)

		if cur_s and not cur_t and nxt and nxt_t and not nxt_s:
			c_tot = _row_qty_val(cur)
			p_tot = _row_qty_val(nxt)
			consumes = _expand_source_rows_for_fifo(se, cur)
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


def _pure_metal_item_for_mwo(mwo_name):
	manufacturer = frappe.db.get_value("Manufacturing Work Order", mwo_name, "manufacturer")
	if not manufacturer:
		return None
	return frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": manufacturer},
		"pure_gold_item",
	)


def _get_bin_qty(item_code, warehouse):
	if not item_code or not warehouse:
		return 0.0
	return flt(
		frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
	)


def _merge_transfer_segments(raw_transfer):
	"""Combine transfer lines for the same item into one row (FIFO runs on merged qty)."""
	by_item = {}
	s_wh = None
	t_wh = None
	for seg in raw_transfer:
		if seg.get("mode") != "transfer":
			continue
		ic = seg["item_code"]
		by_item[ic] = by_item.get(ic, 0) + flt(seg["qty"])
		s_wh = seg.get("s_warehouse")
		t_wh = seg.get("t_warehouse")
	out = []
	for ic, qty in by_item.items():
		if qty <= 0:
			continue
		out.append(
			{
				"mode": "transfer",
				"item_code": ic,
				"qty": round(qty, 3),
				"s_warehouse": s_wh,
				"t_warehouse": t_wh,
			}
		)
	return out


def _resolve_fallback_inject_segments(eir, mwo_name, total_extra, dept_wh):
	"""Stock-first segments: transfer alloy when Bin has stock; else pure→alloy repack; split partial."""
	source_wh = _resolve_source_warehouse(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: source warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)

	mwo = frappe.db.get_value(
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

	per_colour_qty = round(float(total_extra) / len(colours), 3)
	raw_transfer = []
	raw_purity = []
	pure_item = _pure_metal_item_for_mwo(mwo_name)

	for colour in colours:
		alloy_item = get_item_from_attribute(
			mwo["metal_type"], mwo["metal_touch"], mwo["metal_purity"], colour
		)
		if not alloy_item:
			frappe.throw(
				_(
					"Main Slip injection: cannot resolve metal Item for "
					"{0}/{1}/{2}/{3}"
				).format(
					mwo["metal_type"],
					mwo["metal_touch"],
					mwo["metal_purity"],
					colour,
				)
			)

		required = per_colour_qty
		alloy_stock = _get_bin_qty(alloy_item, source_wh)

		if alloy_stock >= required - 1e-6:
			raw_transfer.append(
				{
					"mode": "transfer",
					"item_code": alloy_item,
					"qty": required,
					"s_warehouse": source_wh,
					"t_warehouse": dept_wh,
				}
			)
			continue

		if alloy_stock > 1e-6:
			t_take = round(min(alloy_stock, required), 3)
			remainder = round(required - t_take, 3)
			raw_transfer.append(
				{
					"mode": "transfer",
					"item_code": alloy_item,
					"qty": t_take,
					"s_warehouse": source_wh,
					"t_warehouse": dept_wh,
				}
			)
			if remainder <= 1e-6:
				continue
			required = remainder

		if not pure_item:
			frappe.throw(
				_(
					"EIR injection: required metal not available for transfer or repack. "
					"Alloy {0} is short in {1} and no pure gold item is configured for "
					"this work order's manufacturer."
				).format(alloy_item, source_wh)
			)
		source_p = _get_item_metal_purity(pure_item)
		target_p = _get_item_metal_purity(alloy_item)
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
				"s_warehouse": source_wh,
				"t_warehouse": dept_wh,
			}
		)

	transfer_segments = _merge_transfer_segments(raw_transfer)
	return transfer_segments + raw_purity


def inject_extra_metal_for_eir_receive(eir, row):
	"""Per Employee IR Operation row gain, build + submit Stock Entries that
	push the extra returned metal into the MOP via the MOP Log bridge.

	Returns a list of created Stock Entry names (empty list if skipped).
	"""
	if not cint(getattr(eir, "is_main_slip_required", 0)):
		return []

	extra = flt(row.received_gross_wt) - flt(row.gross_wt)
	if extra <= 0:
		return []

	dept_wh = _resolve_department_warehouse(eir.department)
	if not dept_wh:
		frappe.throw(
			_("Main Slip injection: MFG Warehouse not configured for department {0}").format(
				eir.department
			)
		)

	if getattr(eir, "main_slip", None):
		if _existing_injection_se(eir.name, row.name):
			return []
		target_items = _resolve_inject_metal_items(row.manufacturing_work_order, extra)
		return _inject_via_main_slip_batches(eir, row, target_items, dept_wh)

	segments = _resolve_fallback_inject_segments(eir, row.manufacturing_work_order, extra, dept_wh)
	if _fallback_injection_fully_submitted(eir.name, row.name, segments):
		return []
	return _inject_via_source_warehouse_fallback(eir, row, segments, dept_wh)


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
			"stock_entry_type": ["in", [REPACK_STOCK_ENTRY_TYPE, MATERIAL_TRANSFER_STOCK_ENTRY_TYPE]],
		},
		pluck="name",
	)
	for se_name in se_names:
		frappe.get_doc("Stock Entry", se_name).cancel()
	return se_names


# ---------------------------------------------------------------------------
# Path 1 - Main Slip batch_details walk (primary)
# ---------------------------------------------------------------------------


def _inject_via_main_slip_batches(eir, row, target_items, dept_wh):
	"""Walk Main Slip batch_details in inventory-type priority and emit one SE
	per consumed segment until each target item's required qty is satisfied."""
	source_wh = _resolve_source_warehouse(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: source warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)

	batches = list(_iter_main_slip_batches(eir.main_slip))
	se_names = []

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

			inv_type = batch.get("inventory_type")
			is_pure_subcontracting = (
				inv_type == "Pure Metal" and eir.subcontracting == "Yes"
			)

			if is_pure_subcontracting:
				# Purity conversion: how much of the pure source do we need to
				# produce `remaining` grams of the target alloy?
				source_purity = _get_item_metal_purity(batch["item_code"])
				target_purity = _get_item_metal_purity(target_item)
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
				consume_this = round(
					produce_this * target_purity / source_purity, 3
				)
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
				)
				produce_this = take
				consume_this = take

			se.flags.ignore_permissions = True
			_apply_fifo_batches_to_stock_entry(se)
			se.save()
			se.submit()
			se_names.append(se.name)

			batch["available_qty"] = available - consume_this
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

	return se_names


def _iter_main_slip_batches(main_slip):
	"""Yield Main Slip SE Details rows (``variant_of = 'M'``) in inventory_type
	priority order, only those with positive available qty."""
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
			order_by="creation asc",
		)
		or []
	)

	def _sort_key(r):
		return (
			INVENTORY_TYPE_PRIORITY.get(r.get("inventory_type"), 99),
			r.get("creation") or "",
			r.get("name") or "",
		)

	for r in sorted(rows, key=_sort_key):
		available = flt(r.get("qty", 0)) - flt(r.get("consume_qty", 0))
		if available <= 0:
			continue
		r["available_qty"] = available
		yield r


def _get_item_metal_purity(item_code):
	"""Return the numeric metal_purity attribute value for an Item, or None."""
	value = frappe.db.get_value(
		"Item Variant Attribute",
		{"parent": item_code, "attribute": "Metal Purity"},
		"attribute_value",
	)
	try:
		return flt(value) if value is not None else None
	except (TypeError, ValueError):
		return None


# ---------------------------------------------------------------------------
# Path 2 - source-warehouse fallback (no Main Slip batch details configured)
# ---------------------------------------------------------------------------


def _inject_via_source_warehouse_fallback(eir, row, segments, dept_wh):
	source_wh = _resolve_source_warehouse(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: source warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)
	transfer_segs = [s for s in segments if s.get("mode") == "transfer"]
	purity_segs = [s for s in segments if s.get("mode") == "purity"]
	_validate_fallback_segments_against_source_bin(transfer_segs, purity_segs, source_wh)

	out = []
	if transfer_segs and not _injection_se_exists(
		eir.name, row.name, MATERIAL_TRANSFER_STOCK_ENTRY_TYPE
	):
		se_mt = _build_material_transfer_from_segments(eir, row, transfer_segs, source_wh, dept_wh)
		se_mt.flags.ignore_permissions = True
		_apply_fifo_batches_to_stock_entry(se_mt)
		se_mt.save()
		se_mt.submit()
		out.append(se_mt.name)

	if purity_segs and not _injection_se_exists(eir.name, row.name, REPACK_STOCK_ENTRY_TYPE):
		se_rp = _build_repack_from_purity_segments(eir, row, purity_segs, source_wh, dept_wh)
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


def _injection_se_exists(eir_name, row_name, stock_entry_type):
	return frappe.db.exists(
		"Stock Entry",
		{
			"employee_ir": eir_name,
			"custom_eir_operation_row": row_name,
			"auto_created": 1,
			"docstatus": ["!=", 2],
			"stock_entry_type": stock_entry_type,
		},
	)


def _fallback_injection_fully_submitted(eir_name, row_name, segments):
	transfer_segs = [s for s in segments if s.get("mode") == "transfer"]
	purity_segs = [s for s in segments if s.get("mode") == "purity"]
	if transfer_segs and not _injection_se_exists(eir_name, row_name, MATERIAL_TRANSFER_STOCK_ENTRY_TYPE):
		return False
	if purity_segs and not _injection_se_exists(eir_name, row_name, REPACK_STOCK_ENTRY_TYPE):
		return False
	return True


def _validate_fallback_segments_against_source_bin(transfer_segs, purity_segs, source_wh):
	need = {}
	for seg in transfer_segs:
		ic = seg["item_code"]
		need[ic] = need.get(ic, 0) + flt(seg["qty"])
	for seg in purity_segs:
		ic = seg["source_item"]
		need[ic] = need.get(ic, 0) + flt(seg["consume_qty"])
	for item_code, qty in need.items():
		_check_source_stock(item_code, source_wh, qty)


def _resolve_inject_metal_items(mwo_name, total_extra):
	"""Return [{item_code, qty}] using MWO metal attributes.
	Multicolour MWO: even-split total_extra across allowed_colours."""
	mwo = frappe.db.get_value(
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
	if not mwo or not (mwo.get("metal_type") and mwo.get("metal_touch") and mwo.get("metal_purity")):
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
					"Main Slip injection: cannot resolve metal Item for "
					"{0}/{1}/{2}/{3}"
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
	if eir.subcontracting == "Yes":
		return frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": eir.company,
				"subcontractor": eir.subcontractor,
				"warehouse_type": "Manufacturing",
			},
		)
	return frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"employee": eir.employee,
			"warehouse_type": "Manufacturing",
		},
	)


def _resolve_department_warehouse(department):
	return frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"department": department,
			"warehouse_type": "Manufacturing",
		},
	)


def _check_source_stock(item_code, source_wh, required_qty):
	actual = flt(
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": source_wh}, "actual_qty"
		)
	)
	if actual < required_qty:
		frappe.throw(
			_(
				"Insufficient stock to Repack {0} of {1} from {2} (available {3}). "
				"Cannot complete Main Slip Receive injection."
			).format(required_qty, item_code, source_wh, actual)
		)


def _stamp_se_header(se, eir, row):
	se.company = eir.company
	se.manufacturing_order = frappe.db.get_value(
		"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_order"
	)
	se.manufacturing_work_order = row.manufacturing_work_order
	se.manufacturing_operation = row.manufacturing_operation
	se.main_slip = getattr(eir, "main_slip", None)
	se.employee_ir = eir.name
	se.custom_eir_operation_row = row.name
	se.auto_created = 1
	if eir.subcontracting == "Yes":
		se.subcontractor = eir.subcontractor
	else:
		se.employee = eir.employee


def _build_material_transfer_from_segments(eir, row, transfer_segments, source_wh, dept_wh):
	"""Fallback path: one Material Transfer (WORK ORDER) SE from merged transfer segments."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = MATERIAL_TRANSFER_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	for seg in transfer_segments:
		s_wh = seg.get("s_warehouse") or source_wh
		t_wh = seg.get("t_warehouse") or dept_wh
		se.append(
			"items",
			{
				"item_code": seg["item_code"],
				"qty": seg["qty"],
				"s_warehouse": s_wh,
				"t_warehouse": t_wh,
				"uom": "Gram",
				"manufacturing_operation": row.manufacturing_operation,
				"custom_manufacturing_work_order": row.manufacturing_work_order,
				"use_serial_batch_fields": 1,
			},
		)
	return se


def _build_repack_from_purity_segments(eir, row, purity_segments, source_wh, dept_wh):
	"""Fallback path: one Repack SE from pure→alloy segments (consume / produce pairs)."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	for seg in purity_segments:
		s_wh = seg.get("s_warehouse") or source_wh
		t_wh = seg.get("t_warehouse") or dept_wh
		se.append(
			"items",
			{
				"item_code": seg["source_item"],
				"qty": seg["consume_qty"],
				"s_warehouse": s_wh,
				"uom": "Gram",
				"use_serial_batch_fields": 1,
			},
		)
		se.append(
			"items",
			{
				"item_code": seg["target_item"],
				"qty": seg["produce_qty"],
				"t_warehouse": t_wh,
				"uom": "Gram",
				"manufacturing_operation": row.manufacturing_operation,
				"custom_manufacturing_work_order": row.manufacturing_work_order,
				"use_serial_batch_fields": 1,
			},
		)
	return se


def _build_material_transfer_se(eir, row, item_code, batch_no, qty, source_wh, dept_wh):
	"""Main Slip path: Material Transfer (WORK ORDER) - consume + produce the
	same item, from source warehouse to MOP department warehouse."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = MATERIAL_TRANSFER_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	se.append(
		"items",
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
):
	"""Main Slip subcontracting Pure-Metal path: Repack SE that consumes the
	pure metal (typically 24KT) from the source warehouse and produces the
	target alloy into the MOP department warehouse. Purity conversion applied
	by the caller: produce_qty = consume_qty * source_purity / target_purity.
	"""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)
	se.append(
		"items",
		{
			"item_code": source_item,
			"qty": consume_qty,
			"s_warehouse": source_wh,
			"uom": "Gram",
			"batch_no": source_batch,
			"use_serial_batch_fields": 1,
		},
	)
	se.append(
		"items",
		{
			"item_code": target_item,
			"qty": produce_qty,
			"t_warehouse": dept_wh,
			"uom": "Gram",
			"manufacturing_operation": row.manufacturing_operation,
			"custom_manufacturing_work_order": row.manufacturing_work_order,
			"use_serial_batch_fields": 1,
		},
	)
	return se
