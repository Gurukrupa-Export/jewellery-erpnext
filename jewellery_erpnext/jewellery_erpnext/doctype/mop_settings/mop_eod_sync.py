"""
EOD MOP Log Sync — converts unsynced MOP Log entries into consolidating Stock Entries.

MOP Logs with ``is_synced = 0`` represent virtual warehouse movements recorded by
Department IR and Employee IR.  This module groups those logs by Manufacturing
Operation and creates:

1. A **Material Transfer** Stock Entry that moves items from the first warehouse
   in the log chain to the last warehouse.
2. If the Manufacturing Operation has ``loss_wt`` < 0 (process loss), a **Repack**
   Stock Entry that books the loss to the configured loss warehouse.

The sync is designed to run via the "Sync MOP Log" button on MOP Settings or via
a scheduled background job.
"""

import frappe
from frappe import _
from frappe.utils import flt


def sync_mop_logs():
	"""Main entry point.  Returns a summary dict for the UI."""
	unsynced = _get_unsynced_mop_groups()
	processed = 0
	stock_entries = []

	for mop_name, logs in unsynced.items():
		try:
			se_names = _sync_single_mop(mop_name, logs)
			stock_entries.extend(se_names)
			processed += len(logs)
		except Exception:
			frappe.log_error(
				title=f"MOP EOD Sync failed for {mop_name}",
			)

	frappe.db.commit()
	return {"processed": processed, "stock_entries": stock_entries}


def _get_unsynced_mop_groups():
	"""Return a dict of {manufacturing_operation: [log_rows]} for all unsynced logs."""
	logs = frappe.db.get_all(
		"MOP Log",
		filters={"is_synced": 0, "is_cancelled": 0},
		fields=[
			"name",
			"manufacturing_operation",
			"manufacturing_work_order",
			"item_code",
			"batch_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"from_warehouse",
			"to_warehouse",
			"flow_index",
			"voucher_type",
			"voucher_no",
		],
		order_by="manufacturing_operation, flow_index asc, creation asc",
	)

	groups = {}
	for log in logs:
		groups.setdefault(log.manufacturing_operation, []).append(log)
	return groups


def _sync_single_mop(mop_name, logs):
	"""Create consolidating Stock Entries for one Manufacturing Operation."""
	se_names = []
	if not logs:
		return se_names

	mop = frappe.db.get_value(
		"Manufacturing Operation",
		mop_name,
		[
			"company",
			"manufacturer",
			"manufacturing_work_order",
			"manufacturing_order",
			"department",
			"loss_wt",
		],
		as_dict=True,
	)
	if not mop:
		return se_names

	first_wh, last_wh = _resolve_warehouses(logs)
	if not first_wh or not last_wh:
		frappe.log_error(
			title=f"MOP EOD Sync: cannot resolve warehouses for {mop_name}",
			message=(
				"Skipping sync for this MOP — logs stay unsynced until warehouses can be "
				f"derived (first_wh={first_wh!r}, last_wh={last_wh!r})."
			),
		)
		return se_names

	last_flow = max(l.flow_index for l in logs)
	latest_logs = [l for l in logs if l.flow_index == last_flow]

	items_to_transfer = []
	for log in latest_logs:
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue
		items_to_transfer.append(
			{
				"item_code": log.item_code,
				"qty": qty,
				"batch_no": log.batch_no,
				"s_warehouse": first_wh,
				"t_warehouse": last_wh,
				"manufacturing_operation": mop_name,
				"custom_manufacturing_work_order": mop.manufacturing_work_order,
				"use_serial_batch_fields": 1,
			}
		)

	if items_to_transfer and first_wh != last_wh:
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer to Department"
		se.company = mop.company
		se.manufacturing_order = mop.manufacturing_order
		se.manufacturing_work_order = mop.manufacturing_work_order
		se.manufacturing_operation = mop_name
		se.auto_created = 1
		for item in items_to_transfer:
			se.append("items", item)
		se.flags.ignore_permissions = True
		se.save()
		se.submit()
		se_names.append(se.name)

	loss_wt = flt(mop.loss_wt, 3)
	if loss_wt < 0:
		loss_se_names = _create_loss_entries(mop, mop_name, latest_logs, last_wh, abs(loss_wt))
		se_names.extend(loss_se_names)

	_mark_synced(logs)
	return se_names


def _resolve_warehouses(logs):
	"""Determine the first source warehouse and last target warehouse from the log chain."""
	first_wh = None
	last_wh = None

	min_idx = min(l.flow_index for l in logs)
	max_idx = max(l.flow_index for l in logs)

	for log in logs:
		if log.flow_index == min_idx and log.from_warehouse and not first_wh:
			first_wh = log.from_warehouse
		if log.flow_index == max_idx and log.to_warehouse:
			last_wh = log.to_warehouse

	return first_wh, last_wh


def _create_loss_entries(mop, mop_name, latest_logs, warehouse, total_loss):
	"""Create Repack Stock Entry for process loss."""
	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	se_names = []
	metal_logs = [l for l in latest_logs if l.item_code and l.item_code[0] in ("M", "F")]
	if not metal_logs:
		return se_names

	total_metal_qty = sum(flt(l.qty_after_transaction_batch_based, 3) for l in metal_logs)
	if total_metal_qty <= 0:
		return se_names

	variant_loss_details = frappe.db.get_value(
		"Variant Loss Warehouse",
		{"parent": mop.manufacturer, "variant": "M"},
		["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
		as_dict=True,
	)

	loss_warehouse = None
	if variant_loss_details:
		if variant_loss_details.get("loss_warehouse"):
			loss_warehouse = variant_loss_details.get("loss_warehouse")
		elif variant_loss_details.get("consider_department_warehouse") and variant_loss_details.get(
			"warehouse_type"
		):
			loss_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"department": mop.department,
					"warehouse_type": variant_loss_details.get("warehouse_type"),
				},
			)

	if not loss_warehouse:
		frappe.log_error(
			title=f"MOP EOD Sync: loss warehouse not found for {mop_name}",
			message="Skipping loss entry creation — configure Variant Loss Warehouse on Manufacturer.",
		)
		return se_names

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Repack"
	se.company = mop.company
	se.manufacturing_order = mop.manufacturing_order
	se.manufacturing_work_order = mop.manufacturing_work_order
	se.manufacturing_operation = mop_name
	se.auto_created = 1

	remaining_loss = total_loss
	for log in metal_logs:
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue
		proportional_loss = flt((qty / total_metal_qty) * total_loss, 3)
		if proportional_loss <= 0:
			continue
		remaining_loss -= proportional_loss
		se.append(
			"items",
			{
				"item_code": log.item_code,
				"qty": proportional_loss,
				"s_warehouse": warehouse,
				"batch_no": log.batch_no,
				"manufacturing_operation": mop_name,
				"use_serial_batch_fields": 1,
			},
		)

	if se.items:
		loss_item = get_item_loss_item(mop.company, se.items[0].item_code, "M")
		if loss_item:
			se.append(
				"items",
				{
					"item_code": loss_item,
					"qty": total_loss,
					"t_warehouse": loss_warehouse,
					"manufacturing_operation": mop_name,
					"use_serial_batch_fields": 1,
				},
			)
			se.flags.ignore_permissions = True
			se.save()
			se.submit()
			se_names.append(se.name)

	return se_names


def _mark_synced(logs):
	"""Mark all MOP Log entries as synced."""
	log_names = [l.name for l in logs]
	if log_names:
		frappe.db.set_value(
			"MOP Log",
			{"name": ["in", log_names]},
			"is_synced",
			1,
		)
