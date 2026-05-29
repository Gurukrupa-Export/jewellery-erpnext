# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
	get_scrap_warehouse,
)

_TOLERANCE = 1e-9


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

	scrap_warehouse = get_scrap_warehouse(doc.department)  # throws if not configured

	# Validate all rows upfront — no SRE cancellation happens until all pass
	validated_items = _validate_and_prepare_loss_items(doc, scrap_warehouse)
	if not validated_items:
		return

	_create_process_loss_se(doc, validated_items)


def _validate_and_prepare_loss_items(doc, scrap_warehouse):
	"""Validate each loss row and return enriched dicts for SE creation."""
	sre_cols = frappe.db.get_table_columns("Stock Reservation Entry")
	prepared = []

	for row in get_all_employee_loss_rows(doc):
		loss_qty = flt(row.proportionally_loss)
		if loss_qty <= _TOLERANCE:
			continue

		item_code = row.item_code
		mwo_name = row.manufacturing_work_order
		mop_name = row.manufacturing_operation
		batch_no = row.batch_no

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

		# Find the active SRE for this item / MWO / MOP
		sre_filters = {"docstatus": 1, "item_code": item_code}
		if "manufacturing_work_order" in sre_cols:
			sre_filters["manufacturing_work_order"] = mwo_name
		if "manufacturing_operation" in sre_cols and mop_name:
			sre_filters["manufacturing_operation"] = mop_name

		sre_list = frappe.db.get_all(
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

		if not sre_list:
			frappe.throw(
				_(
					"Row {0}: No active Stock Reservation Entry found for Item {1} / MWO {2}. Cannot process loss."
				).format(row.idx, item_code, mwo_name)
			)

		sre_row = sre_list[0]
		reserved_qty = flt(sre_row["reserved_qty"]) - flt(sre_row["delivered_qty"])

		if loss_qty > reserved_qty + _TOLERANCE:
			frappe.throw(
				_(
					"Row {0}: Loss qty {1} exceeds reserved qty {2} for Item {3} / MWO {4}. Cannot create Process Loss entry."
				).format(
					row.idx,
					frappe.bold(loss_qty),
					frappe.bold(reserved_qty),
					item_code,
					mwo_name,
				)
			)

		is_pcs_item = bool(item_code) and item_code[0] in ("D", "G")

		prepared.append(
			{
				"item_code": item_code,
				"mwo_name": mwo_name,
				"mop_name": mop_name,
				"batch_no": batch_no,
				"loss_qty": loss_qty,
				"loss_pcs": row.pcs if is_pcs_item else None,
				"s_warehouse": sre_row["warehouse"],
				"t_warehouse": scrap_warehouse,
				"sre_name": sre_row["name"],
				"sre_row": sre_row,
				"reserved_qty": reserved_qty,
				"is_pcs_item": is_pcs_item,
			}
		)

	return prepared


def _create_process_loss_se(doc, loss_items):
	"""Cancel relevant SREs, create and submit Process Loss SE, recreate SREs with reduced qty."""
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

	# Phase 2: Build and submit Process Loss Stock Entry
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Process Loss"
	se_doc.company = doc.company
	se_doc.employee_ir = doc.name
	se_doc.auto_created = 1

	for item in loss_items:
		item_dict = {
			"item_code": item["item_code"],
			"qty": item["loss_qty"],
			"s_warehouse": item["s_warehouse"],
			"t_warehouse": item["t_warehouse"],
			"batch_no": item["batch_no"],
			"use_serial_batch_fields": True,
			"manufacturing_operation": item["mop_name"],
		}
		if item["is_pcs_item"] and item["loss_pcs"] is not None:
			item_dict["pcs"] = item["loss_pcs"]

		# Set MWO on SE header once (all loss rows share the same MWO in most cases)
		if not se_doc.manufacturing_work_order and item["mwo_name"]:
			se_doc.manufacturing_work_order = item["mwo_name"]

		se_doc.append("items", item_dict)

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
