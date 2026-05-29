# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
	get_scrap_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)

_TOLERANCE = 1e-9
PROCESS_LOSS_STOCK_ENTRY_TYPE = "Process Loss"


def _ensure_process_loss_stock_entry_type_exists():
	if not frappe.db.exists("Stock Entry Type", PROCESS_LOSS_STOCK_ENTRY_TYPE):
		frappe.throw(
			_(
				"Stock Entry Type {0} is missing. "
				"Run bench migrate to create it before processing Employee IR loss."
			).format(frappe.bold(PROCESS_LOSS_STOCK_ENTRY_TYPE))
		)


def get_all_employee_loss_rows(doc):
	"""Combine manually_book_loss_details and employee_loss_details into one list.

	manually_book_loss_details (explicit user entries) are processed first.
	Both tables have identical field structure.
	"""
	rows = []
	for row in doc.manually_book_loss_details or []:
		rows.append(row)
	for row in doc.employee_loss_details or []:
		rows.append(row)
	return rows


def should_handle_employee_receive_loss(doc):
	all_rows = get_all_employee_loss_rows(doc)
	return (
		doc.type == "Receive"
		and bool(all_rows)
		and any(flt(r.proportionally_loss) > _TOLERANCE for r in all_rows)
	)


def handle_employee_receive_loss(doc):
	"""Create a Process Loss Stock Entry and update SREs for Employee IR Receive loss rows.

	Called from Employee IR on_submit_receive after existing MOP Log logic.
	"""
	if not should_handle_employee_receive_loss(doc):
		return

	# Idempotency: skip if Process Loss SE already exists for this Employee IR
	existing_se = frappe.db.get_value(
		"Stock Entry",
		{
			"employee_ir": doc.name,
			"stock_entry_type": "Process Loss",
			"docstatus": ["!=", 2],
		},
	)
	if existing_se:
		frappe.msgprint(
			_(
				"Process Loss Stock Entry {0} already created for Employee IR {1}. Skipping."
			).format(existing_se, doc.name)
		)
		return

	# Validate all rows upfront — no SRE cancellation happens until all pass
	validated_items = _validate_and_prepare_loss_items(doc)
	if not validated_items:
		return

	_create_process_loss_se(doc, validated_items)


# ---------------------------------------------------------------------------
# SRE lookup helpers
# ---------------------------------------------------------------------------


def _get_sre_candidates_for_loss_row(row, sre_cols, require_mop):
	"""Return a list of active SRE dicts matching item + MWO + optional MOP + batch.

	When batch_no is set, enforces batch matching via a JOIN to Serial and Batch Entry
	so an SRE for the wrong batch can never be selected. The JOIN result includes
	matched_batch_no, batch_qty, and batch_delivered_qty for downstream qty validation.

	When batch_no is absent the SRE uses reservation_based_on = "Qty"; a standard
	frappe.db.get_all without JOIN is sufficient.
	"""
	item_code = row.item_code
	mwo_name = row.manufacturing_work_order
	mop_name = row.get("manufacturing_operation")
	batch_no = row.get("batch_no")
	has_mwo_col = "manufacturing_work_order" in sre_cols
	has_mop_col = "manufacturing_operation" in sre_cols

	if batch_no:
		# Employee loss rows carry the new receive-side MOP while the active SRE was
		# created against the source/issue-side MOP (set from the Stock Entry item row
		# by stock_reservation_entry_for_mwo). After exact MOP lookup fails we fall back
		# to item + MWO + batch so the reservation remains batch-safe.
		sql = """
			SELECT
				sre.name,
				sre.item_code,
				sre.warehouse,
				sre.reserved_qty,
				sre.delivered_qty,
				sre.reservation_based_on,
				sre.manufacturing_work_order,
				sre.manufacturing_operation,
				sre.voucher_type,
				sre.voucher_no,
				sre.voucher_detail_no,
				sre.voucher_qty,
				sre.company,
				sre.stock_uom,
				sb.batch_no AS matched_batch_no,
				sb.qty AS batch_qty,
				sb.delivered_qty AS batch_delivered_qty
			FROM `tabStock Reservation Entry` sre
			INNER JOIN `tabSerial and Batch Entry` sb ON sb.parent = sre.name
			WHERE
				sre.docstatus = 1
				AND sre.item_code = %s
				AND sb.batch_no = %s
		"""
		params = [item_code, batch_no]
		if has_mwo_col:
			sql += " AND sre.manufacturing_work_order = %s"
			params.append(mwo_name)
		if require_mop and mop_name and has_mop_col:
			sql += " AND sre.manufacturing_operation = %s"
			params.append(mop_name)
		return frappe.db.sql(sql, params, as_dict=True)

	# No batch: SRE uses reservation_based_on = "Qty"; no child table JOIN needed
	sre_filters = {"docstatus": 1, "item_code": item_code}
	if has_mwo_col:
		sre_filters["manufacturing_work_order"] = mwo_name
	if require_mop and mop_name and has_mop_col:
		sre_filters["manufacturing_operation"] = mop_name
	return frappe.db.get_all(
		"Stock Reservation Entry",
		filters=sre_filters,
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"reservation_based_on",
			"manufacturing_work_order",
			"manufacturing_operation",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"voucher_qty",
			"company",
			"stock_uom",
		],
	)


def _find_active_sre_for_loss_row(row, sre_cols):
	"""Find exactly one active SRE for a loss row using a two-step lookup.

	Step 1 — exact match: item + MWO + loss-row MOP + batch.
	Step 2 — fallback:   item + MWO + batch (MOP relaxed).

	The fallback exists because on_submit_receive creates a new receive-side MOP
	via create_operation_for_next_op and stores it on the loss detail row, while
	the SRE was created against the source/issue-side MOP. The fallback is safe
	only when the result is unambiguous; multiple matches raise an error.
	"""
	exact = _get_sre_candidates_for_loss_row(row, sre_cols, require_mop=True)
	if len(exact) == 1:
		return exact[0]
	if len(exact) > 1:
		_throw_ambiguous_sre(row, exact, mode="exact")

	fallback = _get_sre_candidates_for_loss_row(row, sre_cols, require_mop=False)
	if len(fallback) == 1:
		return fallback[0]
	if len(fallback) > 1:
		_throw_ambiguous_sre(row, fallback, mode="fallback")

	_throw_no_sre_found(row)


def _validate_loss_qty_against_sre(row, candidate, loss_qty):
	"""Raise if loss_qty exceeds parent remaining qty or batch remaining qty."""
	sre_remaining = flt(candidate.get("reserved_qty")) - flt(
		candidate.get("delivered_qty")
	)

	# Batch-path candidates include batch_qty / batch_delivered_qty from the JOIN
	batch_qty = candidate.get("batch_qty")
	if batch_qty is not None:
		batch_remaining = flt(batch_qty) - flt(
			candidate.get("batch_delivered_qty") or 0
		)
		if loss_qty > batch_remaining + _TOLERANCE:
			frappe.throw(
				_(
					"Row {0}: Loss qty {1} exceeds batch remaining qty {2} for Item {3} / MWO {4} / "
					"Batch {5} / SRE {6}. Cannot create Process Loss entry."
				).format(
					row.idx,
					frappe.bold(loss_qty),
					frappe.bold(batch_remaining),
					row.item_code,
					row.manufacturing_work_order,
					candidate.get("matched_batch_no") or row.get("batch_no"),
					candidate.get("name"),
				)
			)

	if loss_qty > sre_remaining + _TOLERANCE:
		frappe.throw(
			_(
				"Row {0}: Loss qty {1} exceeds SRE remaining qty {2} for Item {3} / MWO {4} / "
				"SRE {5}. Cannot create Process Loss entry."
			).format(
				row.idx,
				frappe.bold(loss_qty),
				frappe.bold(sre_remaining),
				row.item_code,
				row.manufacturing_work_order,
				candidate.get("name"),
			)
		)


def _throw_no_sre_found(row):
	"""Throw a diagnostic error when no active SRE is found for a loss row."""
	frappe.throw(
		_(
			"Row {0}: No active Stock Reservation Entry found for Employee IR loss row.\n\n"
			"Item: {1}\n"
			"MWO: {2}\n"
			"Loss row MOP: {3}\n"
			"Batch: {4}\n\n"
			"Tried:\n"
			"1. Active SRE by item + MWO + MOP + batch.\n"
			"2. Active SRE by item + MWO + batch (MOP relaxed).\n\n"
			"No matching active SRE was found. "
			"Verify with: SELECT sre.name, sre.manufacturing_operation, sb.batch_no "
			"FROM `tabStock Reservation Entry` sre "
			"LEFT JOIN `tabSerial and Batch Entry` sb ON sb.parent = sre.name "
			"WHERE sre.docstatus = 1 AND sre.item_code = '{5}' "
			"AND sre.manufacturing_work_order = '{6}'"
		).format(
			row.idx,
			row.item_code,
			row.manufacturing_work_order,
			row.get("manufacturing_operation") or "(none)",
			row.get("batch_no") or "(none)",
			row.item_code,
			row.manufacturing_work_order,
		)
	)


def _throw_ambiguous_sre(row, candidates, mode):
	"""Throw a diagnostic error when multiple SREs match a loss row."""
	lines = []
	for c in candidates:
		batch_remaining = ""
		if c.get("batch_qty") is not None:
			br = flt(c.get("batch_qty")) - flt(c.get("batch_delivered_qty") or 0)
			batch_remaining = f", Batch Remaining: {br}"
		sre_remaining = flt(c.get("reserved_qty")) - flt(c.get("delivered_qty"))
		lines.append(
			f"  - {c['name']}: SRE MOP {c.get('manufacturing_operation') or '(none)'}, "
			f"Warehouse {c.get('warehouse')}, Remaining: {sre_remaining}{batch_remaining}"
		)
	candidates_text = "\n".join(lines)

	frappe.throw(
		_(
			"Row {0}: Multiple active Stock Reservation Entries match this loss row ({1} match).\n\n"
			"Item: {2} / MWO: {3} / Batch: {4} / Loss row MOP: {5}\n\n"
			"Candidates:\n{6}\n\n"
			"Cannot choose automatically. "
			"Resolve by cancelling or delivering the duplicate SREs before retrying."
		).format(
			row.idx,
			mode,
			row.item_code,
			row.manufacturing_work_order,
			row.get("batch_no") or "(none)",
			row.get("manufacturing_operation") or "(none)",
			candidates_text,
		)
	)


# ---------------------------------------------------------------------------
# Loss item and warehouse helpers
# ---------------------------------------------------------------------------


def _get_loss_target_warehouse(doc):
	"""Return the target warehouse for Process Loss SE output items.

	When is_main_slip_required the loss goes back to the employee's (or
	subcontractor's) raw material warehouse so it can be reused. Otherwise it
	goes to the department scrap warehouse.
	"""
	if cint(doc.is_main_slip_required):
		dynamic_filter = (
			{"subcontractor": doc.subcontractor}
			if doc.get("subcontractor")
			else {"employee": doc.employee}
		)
		warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": doc.company,
				"warehouse_type": "Raw Material",
				**dynamic_filter,
			},
		)
		if warehouse:
			return warehouse
	return get_scrap_warehouse(doc.department)


# ---------------------------------------------------------------------------
# Validation and preparation
# ---------------------------------------------------------------------------


def _validate_and_prepare_loss_items(doc):
	"""Validate each loss row and return enriched dicts for SE creation.

	Fetches sre_cols once before the loop. Uses two-step SRE lookup (exact MOP
	then MOP-relaxed fallback) and validates both parent and batch remaining qty.
	No SRE is cancelled here — cancellation happens in _create_process_loss_se
	only after all rows pass validation.

	Each returned dict includes loss_item_code (looked up from Variant Loss Table
	using variant + loss_type) and t_warehouse (main slip raw material warehouse
	when is_main_slip_required, else department scrap warehouse).
	"""
	sre_cols = frappe.db.get_table_columns("Stock Reservation Entry")
	t_warehouse = _get_loss_target_warehouse(doc)
	prepared = []

	for row in get_all_employee_loss_rows(doc):
		loss_qty = flt(row.proportionally_loss)
		if loss_qty <= _TOLERANCE:
			continue

		item_code = row.item_code
		mwo_name = row.manufacturing_work_order
		mop_name = row.get("manufacturing_operation")
		batch_no = row.get("batch_no")
		loss_type = row.get("loss_type")

		if not mwo_name:
			frappe.throw(
				_(
					"Row {0}: Manufacturing Work Order is required in Employee Loss Details"
				).format(row.idx)
			)
		if not item_code:
			frappe.throw(
				_("Row {0}: Item Code is required in Employee Loss Details").format(
					row.idx
				)
			)

		variant_of = item_code[0]
		loss_item_code = get_item_loss_item(
			doc.company, item_code, variant_of, loss_type
		)
		if not loss_item_code:
			frappe.throw(
				_(
					"Row {0}: Could not find or create loss item for variant {1} / loss type {2}. "
					"Check Variant Loss Table configuration."
				).format(row.idx, variant_of, loss_type or "(none)")
			)

		candidate = _find_active_sre_for_loss_row(row, sre_cols)
		_validate_loss_qty_against_sre(row, candidate, loss_qty)

		sre_remaining = flt(candidate["reserved_qty"]) - flt(candidate["delivered_qty"])
		is_pcs_item = item_code[0] in ("D", "G")

		prepared.append(
			{
				"item_code": item_code,
				"loss_item_code": loss_item_code,
				"mwo_name": mwo_name,
				"mop_name": mop_name,
				"batch_no": batch_no,
				"loss_qty": loss_qty,
				"loss_pcs": row.pcs if is_pcs_item else None,
				"s_warehouse": candidate["warehouse"],
				"t_warehouse": t_warehouse,
				"sre_name": candidate["name"],
				"sre_row": candidate,
				"reserved_qty": sre_remaining,
				"is_pcs_item": is_pcs_item,
				"inventory_type": row.get("inventory_type"),
			}
		)

	return prepared


# ---------------------------------------------------------------------------
# Process Loss SE creation and SRE recreation
# ---------------------------------------------------------------------------


def _create_process_loss_se(doc, loss_items):
	"""Cancel relevant SREs, create and submit Process Loss SE, recreate SREs with reduced qty."""
	_ensure_process_loss_stock_entry_type_exists()
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	# Phase 1: Cancel SREs — all validation passed, safe to proceed
	cancelled_sres = []
	for item in loss_items:
		sre_doc = frappe.get_doc("Stock Reservation Entry", item["sre_name"])
		if sre_doc.docstatus != 1:
			frappe.throw(
				_(
					"Stock Reservation Entry {0} is not in submitted state. Cannot cancel."
				).format(sre_doc.name)
			)
		sre_doc.ignore_permissions = True
		sre_doc.cancel()
		cancelled_sres.append(
			{
				"original": sre_doc,
				"loss_qty": item["loss_qty"],
				"target_batch_no": item["batch_no"],
			}
		)

	# Phase 2: Build and submit Process Loss Stock Entry (Repack)
	# Each loss row becomes two SE items: source item out (s_warehouse) + loss variant in (t_warehouse)
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Process Loss"
	se_doc.purpose = "Repack"
	se_doc.company = doc.company
	se_doc.employee_ir = doc.name
	se_doc.department = doc.department
	se_doc.to_department = doc.department
	se_doc.auto_created = 1

	for item in loss_items:
		if not se_doc.manufacturing_work_order and item["mwo_name"]:
			se_doc.manufacturing_work_order = item["mwo_name"]

		source_row = {
			"item_code": item["item_code"],
			"qty": item["loss_qty"],
			"s_warehouse": item["s_warehouse"],
			"t_warehouse": None,
			"batch_no": item["batch_no"],
			"use_serial_batch_fields": True,
			"manufacturing_operation": item["mop_name"],
			"department": doc.department,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
		}
		if item["is_pcs_item"] and item["loss_pcs"] is not None:
			source_row["pcs"] = item["loss_pcs"]
		if item.get("inventory_type"):
			source_row["inventory_type"] = item["inventory_type"]
		se_doc.append("items", source_row)

		target_row = {
			"item_code": item["loss_item_code"],
			"qty": item["loss_qty"],
			"s_warehouse": None,
			"t_warehouse": item["t_warehouse"],
			"use_serial_batch_fields": True,
			"manufacturing_operation": item["mop_name"],
			"department": doc.department,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
		}
		if item.get("inventory_type"):
			target_row["inventory_type"] = item["inventory_type"]
		se_doc.append("items", target_row)

	se_doc.flags.ignore_permissions = True
	se_doc.save()
	se_doc.submit()

	# Phase 3: Recreate SREs with qty reduced by loss
	for update in cancelled_sres:
		original_sre = update["original"]
		loss_qty = update["loss_qty"]
		target_batch_no = update["target_batch_no"]
		reserved_qty = flt(original_sre.reserved_qty) - flt(original_sre.delivered_qty)
		remaining_qty = reserved_qty - loss_qty

		if remaining_qty <= _TOLERANCE:
			continue  # fully consumed — no replacement SRE needed

		has_batch_no, has_serial_no = frappe.get_cached_value(
			"Item", original_sre.item_code, ["has_batch_no", "has_serial_no"]
		)

		# Rebuild batch rows, subtracting loss only from the batch matching target_batch_no
		sb_entries = []
		if cint(has_batch_no):
			old_sb = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": original_sre.name},
				fields=["batch_no", "qty", "delivered_qty"],
			)
			for sb in old_sb:
				sb_remaining = flt(sb.qty) - flt(sb.delivered_qty)
				if target_batch_no and sb.batch_no == target_batch_no:
					sb_remaining -= loss_qty
				if sb_remaining > _TOLERANCE:
					sb_entries.append({"batch_no": sb.batch_no, "qty": sb_remaining})
			if not sb_entries:
				continue

		if sb_entries and cint(has_batch_no):
			available_qty = get_available_qty_to_reserve(
				original_sre.item_code,
				original_sre.warehouse,
				batch_no=sb_entries[0]["batch_no"],
			)
		else:
			available_qty = get_available_qty_to_reserve(
				original_sre.item_code, original_sre.warehouse
			)

		new_sre = frappe.new_doc("Stock Reservation Entry")
		new_sre.voucher_type = original_sre.voucher_type
		new_sre.voucher_no = original_sre.voucher_no
		new_sre.voucher_detail_no = original_sre.voucher_detail_no
		new_sre.voucher_qty = original_sre.voucher_qty
		new_sre.item_code = original_sre.item_code
		new_sre.warehouse = original_sre.warehouse  # same warehouse, only qty reduced
		new_sre.reserved_qty = remaining_qty
		new_sre.company = original_sre.company
		new_sre.stock_uom = original_sre.stock_uom
		new_sre.reservation_based_on = original_sre.reservation_based_on
		new_sre.has_batch_no = cint(has_batch_no)
		new_sre.has_serial_no = cint(has_serial_no)
		new_sre.available_qty = max(flt(available_qty), remaining_qty)
		new_sre.manufacturing_work_order = original_sre.manufacturing_work_order
		new_sre.manufacturing_operation = original_sre.manufacturing_operation

		for sb in sb_entries:
			new_sre.append("sb_entries", {"batch_no": sb["batch_no"], "qty": sb["qty"]})

		new_sre.flags.ignore_permissions = True
		new_sre.insert(ignore_links=1)
		new_sre.submit()
