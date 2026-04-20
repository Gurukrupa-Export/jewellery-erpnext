# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
"""Main Slip-required Employee IR Receive: repack extra returned metal from
the employee/subcontractor warehouse into the MOP department warehouse so the
positive delta surfaces as a MOP Log row via the existing Stock Entry bridge.

Gate:  ``eir.is_main_slip_required = 1`` AND ``row.received_gross_wt > row.gross_wt``

Mechanism per row:
    Emit a single Repack SE per target item that consumes the manufacturer's 
    pure_gold_item and produces the target alloy, regardless of matching-alloy stock.

    consume_pure_qty = target_qty * target_purity / pure_purity

    Source priorities:
    1. Main Slip batches (eir.main_slip set): inventory_type='Pure Metal' AND item_code=pure_gold_item
       (walked in creation asc order).
    2. Fallback: remainder from employee/subcontractor Manufacturing warehouse with 
       `se.flags.throw_batch_error = True` to trigger FIFO update_batches.

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
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import get_purity_percentage


REPACK_STOCK_ENTRY_TYPE = "Repack"


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

	source_wh = _resolve_source_warehouse(eir)
	if not source_wh:
		frappe.throw(
			_("Main Slip injection: source warehouse not configured for {0}").format(
				eir.subcontractor if eir.subcontracting == "Yes" else eir.employee
			)
		)

	pure_item = _resolve_pure_gold_item(eir, row)
	if not pure_item:
		frappe.throw(
			_("Main Slip injection: pure_gold_item not configured in Manufacturing Setting")
		)

	pure_purity = get_purity_percentage(pure_item)
	if not pure_purity:
		frappe.throw(
			_("Main Slip injection: cannot resolve metal_purity for pure_gold_item {0}").format(pure_item)
		)

	target_items = _resolve_inject_metal_items(row.manufacturing_work_order, extra)

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	_stamp_se_header(se, eir, row)

	for target in target_items:
		target_item = target["item_code"]
		target_purity = get_purity_percentage(target_item)
		if not target_purity:
			frappe.throw(
				_("Main Slip injection: cannot resolve metal_purity for target {0}").format(target_item)
			)

		consume_qty = round(target["qty"] * target_purity / pure_purity, 3)
		segments = list(_walk_main_slip_pure_batches(eir.main_slip, pure_item, consume_qty)) if getattr(eir, "main_slip", None) else []

		consumed_from_ms = sum(s["qty"] for s in segments)
		remainder = round(consume_qty - consumed_from_ms, 3)

		for seg in segments:
			se.append("items", _consume_row(pure_item, seg["qty"], source_wh, seg["batch_no"]))

		if remainder > 0:
			# Validate fallback stock before delegating to let update_batches
			actual = flt(
				frappe.db.get_value(
					"Bin", {"item_code": pure_item, "warehouse": source_wh}, "actual_qty"
				)
			)
			if actual < remainder:
				frappe.throw(
					_(
						"Insufficient pure stock to Repack {0} gram(s) of {1}. Required {2}g of {3}. "
						"Produced {4}g from Main Slip {5}, remaining {6}g needed but only {7}g available "
						"in source warehouse {8}."
					).format(
						target["qty"], target_item,
						consume_qty, pure_item,
						consumed_from_ms, getattr(eir, "main_slip", "None") or "None",
						remainder, actual, source_wh
					)
				)

			# Let update_batches() FIFO-resolve this remainder row at before_validate time.
			se.append("items", _consume_row(pure_item, remainder, source_wh, batch_no=None))

		se.append("items", _produce_row(target_item, target["qty"], dept_wh, row.manufacturing_operation))

	se.flags.throw_batch_error = True
	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return [se.name]


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
			"stock_entry_type": REPACK_STOCK_ENTRY_TYPE,
		},
		pluck="name",
	)
	for se_name in se_names:
		frappe.get_doc("Stock Entry", se_name).cancel()
	return se_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_pure_gold_item(eir, row):
	"""Reads manufacturer from eir.manufacturer, falls back to PMO.manufacturer
	via row.manufacturing_work_order -> PMO; looks up Manufacturing Setting.{manufacturer}.pure_gold_item.
	Returns None if unresolved."""
	manufacturer = getattr(eir, "manufacturer", None)
	if not manufacturer:
		manufacturer = frappe.db.get_value(
			"Parent Manufacturing Order", 
			frappe.db.get_value("Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_order"), 
			"manufacturer"
		)
	if not manufacturer:
		manufacturer = frappe.defaults.get_user_default("manufacturer")
		
	if manufacturer:
		return frappe.db.get_value("Manufacturing Setting", {"manufacturer": manufacturer}, "pure_gold_item")
	return None


def _walk_main_slip_pure_batches(main_slip, pure_item, required_qty):
	"""Queries `Main Slip SE Details` filtered on parent=main_slip, parentfield="batch_details",
	inventory_type="Pure Metal", item_code=pure_item, ordered by creation asc; yields `{batch_no, qty}` segments
	until `required_qty` satisfied."""
	if not main_slip:
		return

	rows = (
		frappe.db.get_all(
			"Main Slip SE Details",
			filters={
				"parent": main_slip,
				"parentfield": "batch_details",
				"variant_of": "M",
				"inventory_type": "Pure Metal",
				"item_code": pure_item,
			},
			fields=[
				"name",
				"batch_no",
				"qty",
				"consume_qty",
				"creation",
			],
			order_by="creation asc",
		)
		or []
	)

	remaining = flt(required_qty)
	for r in rows:
		if remaining <= 0:
			break
		available = flt(r.get("qty", 0)) - flt(r.get("consume_qty", 0))
		if available <= 0:
			continue
			
		take = min(available, remaining)
		yield {"batch_no": r.get("batch_no"), "qty": take}
		remaining = round(remaining - take, 3)


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


def _consume_row(item_code, qty, s_warehouse, batch_no):
	row = {
		"item_code": item_code,
		"qty": qty,
		"s_warehouse": s_warehouse,
		"uom": "Gram",
		"use_serial_batch_fields": 1,
	}
	if batch_no:
		row["batch_no"] = batch_no
	return row


def _produce_row(item_code, qty, t_warehouse, mop):
	return {
		"item_code": item_code,
		"qty": qty,
		"t_warehouse": t_warehouse,
		"manufacturing_operation": mop,
		"uom": "Gram",
		"use_serial_batch_fields": 1,
	}


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
