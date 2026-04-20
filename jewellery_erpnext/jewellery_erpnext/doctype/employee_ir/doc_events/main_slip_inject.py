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

Fallback (no ``eir.main_slip`` on record): a single Repack SE from the
employee/subcontractor Manufacturing warehouse into the MOP department
warehouse with the resolved target metal item (pre-extension behaviour).

Insufficient stock: explicit ``frappe.throw`` with available vs required
numbers; never silent-skip.

Idempotency: keyed on ``(Stock Entry.employee_ir, custom_eir_operation_row)``
with ``auto_created = 1``; re-submission short-circuits.

Cancel: ``cancel_injections_for_eir`` iterates auto-created SEs for the EIR
and calls ``.cancel()``; their ``on_cancel`` flips MOP Log rows to
``is_cancelled = 1`` via the existing bridge.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

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

	if _existing_injection_se(eir.name, row.name):
		return []

	dept_wh = _resolve_department_warehouse(eir.department)
	if not dept_wh:
		frappe.throw(
			_("Main Slip injection: MFG Warehouse not configured for department {0}").format(
				eir.department
			)
		)

	target_items = _resolve_inject_metal_items(row.manufacturing_work_order, extra)

	if getattr(eir, "main_slip", None):
		return _inject_via_main_slip_batches(eir, row, target_items, dept_wh)
	return _inject_via_source_warehouse_fallback(eir, row, target_items, dept_wh)


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


def _inject_via_source_warehouse_fallback(eir, row, target_items, dept_wh):
	source_wh = _resolve_source_warehouse(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: source warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)
	for t in target_items:
		_check_source_stock(t["item_code"], source_wh, t["qty"])

	se = _build_repack_se(eir, row, target_items, source_wh, dept_wh)
	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return [se.name]


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


def _build_repack_se(eir, row, items, source_wh, dept_wh):
	"""Fallback path: single Repack SE with consume + produce rows for each
	target item, all from the employee/subcontractor Manufacturing warehouse."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)

	for it in items:
		se.append(
			"items",
			{
				"item_code": it["item_code"],
				"qty": it["qty"],
				"s_warehouse": source_wh,
				"uom": "Gram",
				"batch_no": it.get("batch_no"),
				"use_serial_batch_fields": 1,
			},
		)
		se.append(
			"items",
			{
				"item_code": it["item_code"],
				"qty": it["qty"],
				"t_warehouse": dept_wh,
				"uom": "Gram",
				"manufacturing_operation": row.manufacturing_operation,
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
			"use_serial_batch_fields": 1,
		},
	)
	return se
