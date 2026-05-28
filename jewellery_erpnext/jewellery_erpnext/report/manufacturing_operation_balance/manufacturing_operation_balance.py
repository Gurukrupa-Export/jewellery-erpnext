# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
)

_MANDATORY_FILTERS = (
	"company",
	"manufacturer",
	"manufacturing_work_order",
	"manufacturing_operation",
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate_filters(filters)
	columns = _get_columns()
	data = _get_data(filters)
	return columns, data


def _validate_filters(filters):
	for field in _MANDATORY_FILTERS:
		if not filters.get(field):
			frappe.throw(
				_(
					"Filter {0} is mandatory for Manufacturing Operation Balance report."
				).format(frappe.bold(field.replace("_", " ").title()))
			)

	# Confirm the MOP belongs to the selected MWO (prevents cross-MWO filter bypass)
	actual_mwo = frappe.db.get_value(
		"Manufacturing Operation",
		filters.manufacturing_operation,
		"manufacturing_work_order",
	)
	if actual_mwo != filters.manufacturing_work_order:
		frappe.throw(
			_(
				"Manufacturing Operation {0} does not belong to Manufacturing Work Order {1}."
			).format(
				frappe.bold(filters.manufacturing_operation),
				frappe.bold(filters.manufacturing_work_order),
			)
		)


def _get_columns():
	return [
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
			"label": _("MOP Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
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
	mop_name = filters.manufacturing_operation
	mwo_name = filters.manufacturing_work_order

	mop_meta = (
		frappe.db.get_value(
			"Manufacturing Operation",
			mop_name,
			["status", "department", "manufacturing_order"],
			as_dict=1,
		)
		or frappe._dict()
	)

	customer = None
	inventory_type = "Regular Stock"
	if mop_meta.get("manufacturing_order"):
		pmo_data = (
			frappe.db.get_value(
				"Parent Manufacturing Order",
				mop_meta.manufacturing_order,
				[
					"customer",
					"customer_gold",
					"customer_diamond",
					"customer_stone",
					"customer_good",
				],
				as_dict=1,
			)
			or frappe._dict()
		)
		customer = pmo_data.get("customer")
		if any(
			[
				cint(pmo_data.get("customer_gold")),
				cint(pmo_data.get("customer_diamond")),
				cint(pmo_data.get("customer_stone")),
				cint(pmo_data.get("customer_good")),
			]
		):
			inventory_type = "Customer Goods"

	# Item UOM cache to avoid N+1 queries
	uom_cache = {}

	balance_rows = get_current_mop_balance_rows(mop_name)
	data = []
	for row in balance_rows:
		item_code = row.get("item_code")
		qty = flt(
			row.get("qty_after_transaction_batch_based")
			or row.get("qty_after_transaction")
			or 0
		)
		pcs = cint(row.get("pcs_after_transaction_batch_based") or 0)

		if qty <= 0 and pcs <= 0:
			continue

		if item_code and item_code not in uom_cache:
			uom_cache[item_code] = (
				frappe.db.get_value("Item", item_code, "stock_uom") or ""
			)

		data.append(
			{
				"manufacturing_work_order": mwo_name,
				"manufacturing_operation": mop_name,
				"status": mop_meta.get("status"),
				"item_code": item_code,
				"qty": qty,
				"pcs": pcs,
				"uom": uom_cache.get(item_code, ""),
				"batch_no": row.get("batch_no"),
				"department": mop_meta.get("department"),
				"inventory_type": inventory_type,
				"customer": customer,
			}
		)

	return data
