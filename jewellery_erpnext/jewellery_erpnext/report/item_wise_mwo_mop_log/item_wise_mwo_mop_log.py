# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate_filters(filters)
	columns = _get_columns()
	data = _get_data(filters)
	return columns, data


def _validate_filters(filters):
	if not filters.get("company"):
		frappe.throw(_("Company is mandatory."))
	if not filters.get("manufacturing_work_order"):
		frappe.throw(_("Manufacturing Work Order is mandatory."))


def _get_columns():
	return [
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"label": _("Manufacturing Work Order"),
			"fieldname": "manufacturing_work_order",
			"fieldtype": "Link",
			"options": "Manufacturing Work Order",
			"width": 200,
		},
		{
			"label": _("Manufacturing Operation"),
			"fieldname": "manufacturing_operation",
			"fieldtype": "Link",
			"options": "Manufacturing Operation",
			"width": 200,
		},
		{
			"label": _("Qty"),
			"fieldname": "qty",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("Pcs"),
			"fieldname": "pcs",
			"fieldtype": "Int",
			"width": 80,
		},
		{
			"label": _("UOM"),
			"fieldname": "uom",
			"fieldtype": "Link",
			"options": "UOM",
			"width": 80,
		},
		{
			"label": _("Batch"),
			"fieldname": "batch_no",
			"fieldtype": "Link",
			"options": "Batch",
			"width": 140,
		},
		{
			"label": _("Department"),
			"fieldname": "department",
			"fieldtype": "Link",
			"options": "Department",
			"width": 160,
		},
		{
			"label": _("Inventory Type"),
			"fieldname": "inventory_type",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 160,
		},
	]


def _get_data(filters):
	mwo_name = filters.manufacturing_work_order
	item_code_filter = filters.get("item_code")

	# Fetch all non-cancelled MOP Logs for this MWO
	log_filters = {
		"manufacturing_work_order": mwo_name,
		"is_cancelled": 0,
	}
	if item_code_filter:
		log_filters["item_code"] = item_code_filter

	mop_logs = frappe.db.get_all(
		"MOP Log",
		filters=log_filters,
		fields=[
			"item_code",
			"manufacturing_work_order",
			"manufacturing_operation",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"batch_no",
			"creation",
		],
		order_by="manufacturing_operation desc, creation desc",
	)

	# Get the last created manufacturing operation
	if not mop_logs:
		return []

	last_mop = mop_logs[0].manufacturing_operation
	mop_logs = [log for log in mop_logs if log.manufacturing_operation == last_mop]

	# Keep only the latest row per (manufacturing_operation, item_code, batch_no)
	seen = {}
	for log in mop_logs:
		key = (log.manufacturing_operation, log.item_code, log.batch_no)
		if key not in seen:
			seen[key] = log

	if not seen:
		return []

	# Bulk fetch MOP metadata: status, department
	mop_names = list(
		{v.manufacturing_operation for v in seen.values() if v.manufacturing_operation}
	)
	mop_meta_map = {}
	if mop_names:
		for mop in frappe.db.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", mop_names]},
			fields=["name", "status", "department", "manufacturing_order"],
		):
			mop_meta_map[mop.name] = mop

	# Bulk fetch customer and inventory type from PMO
	pmo_names = list(
		{
			m.manufacturing_order
			for m in mop_meta_map.values()
			if m.get("manufacturing_order")
		}
	)
	pmo_map = {}
	if pmo_names:
		for pmo in frappe.db.get_all(
			"Parent Manufacturing Order",
			filters={"name": ["in", pmo_names]},
			fields=[
				"name",
				"customer",
				"customer_gold",
				"customer_diamond",
				"customer_stone",
				"customer_good",
			],
		):
			pmo_map[pmo.name] = pmo

	# Bulk fetch UOM from Item
	item_codes = list({v.item_code for v in seen.values() if v.item_code})
	uom_map = {}
	if item_codes:
		for item in frappe.db.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "stock_uom"],
		):
			uom_map[item.name] = item.stock_uom

	data = []
	for log in sorted(
		seen.values(),
		key=lambda r: (r.manufacturing_operation or "", r.item_code or ""),
	):
		qty = flt(log.get("qty_after_transaction_batch_based") or 0)
		pcs = cint(log.get("pcs_after_transaction_batch_based") or 0)
		if qty <= 0 and pcs <= 0:
			continue

		mop_m = mop_meta_map.get(log.manufacturing_operation) or frappe._dict()
		pmo_m = pmo_map.get(mop_m.get("manufacturing_order")) or frappe._dict()

		inventory_type = "Regular Stock"
		if any(
			[
				cint(pmo_m.get("customer_gold")),
				cint(pmo_m.get("customer_diamond")),
				cint(pmo_m.get("customer_stone")),
				cint(pmo_m.get("customer_good")),
			]
		):
			inventory_type = "Customer Goods"

		data.append(
			{
				"item_code": log.item_code,
				"manufacturing_work_order": log.manufacturing_work_order,
				"manufacturing_operation": log.manufacturing_operation,
				"qty": qty,
				"pcs": pcs,
				"uom": uom_map.get(log.item_code, ""),
				"batch_no": log.batch_no,
				"department": mop_m.get("department"),
				"inventory_type": inventory_type,
				"customer": pmo_m.get("customer"),
			}
		)

	return data
