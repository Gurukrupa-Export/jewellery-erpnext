# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Latest per-batch balance for one Manufacturing Operation.

Customer and Inventory Type are read off the ROW'S OWN BATCH
(``Batch.custom_customer`` / ``Batch.custom_inventory_type``), not off the Parent
Manufacturing Order. The batch is the physical truth (rule 1 in
``customization/utils/row_ownership``), and one MOP routinely holds
customer-owned and company-owned batches at the same time, so a single PMO-level
flag cannot describe a per-batch report.

This report deliberately does NOT call ``row_ownership.resolve_batch_ownership``.
That helper reads the same two columns, but (a) one ``frappe.db.get_value`` per
row, which is an N+1 here, and (b) it applies ``normalize_ownership``, whose
rules exist to keep a Stock Entry *writable* -- they blank the customer on a
Regular Stock row and downgrade "Customer Goods with no customer" to Regular
Stock. Those are exactly the anomalies an operator opens a balance report to
find, so they are shown raw.
"""

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
	if not filters.get("manufacturing_operation"):
		frappe.throw(_("Manufacturing Operation is mandatory."))


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
			"label": _("Manufacturing Operation Status"),
			"fieldname": "manufacturing_operation_status",
			"fieldtype": "Data",
			"width": 220,
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


def _preload_mop_meta_map(mop_names):
	"""Return ``{mop_name: {status, department}}`` in one round trip."""
	mop_meta_map = {}
	mop_names = [name for name in set(mop_names or []) if name]
	if not mop_names:
		return mop_meta_map

	for mop in frappe.db.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", mop_names]},
		fields=["name", "status", "department"],
	):
		mop_meta_map[mop.name] = mop

	return mop_meta_map


def _preload_batch_ownership_map(batch_nos):
	"""Return ``{batch_no: (inventory_type, customer)}`` in one round trip.

	Values are the raw ``Batch`` columns -- see the module docstring for why
	``row_ownership.normalize_ownership`` is not applied here.
	"""
	batch_map = {}
	batch_nos = [batch_no for batch_no in set(batch_nos or []) if batch_no]
	if not batch_nos:
		return batch_map

	for batch in frappe.db.get_all(
		"Batch",
		filters={"name": ["in", batch_nos]},
		fields=["name", "custom_inventory_type", "custom_customer"],
	):
		batch_map[batch.name] = (batch.custom_inventory_type, batch.custom_customer)

	return batch_map


def _preload_uom_map(item_codes):
	"""Return ``{item_code: stock_uom}`` in one round trip."""
	uom_map = {}
	item_codes = [item_code for item_code in set(item_codes or []) if item_code]
	if not item_codes:
		return uom_map

	for item in frappe.db.get_all(
		"Item",
		filters={"name": ["in", item_codes]},
		fields=["name", "stock_uom"],
	):
		uom_map[item.name] = item.stock_uom

	return uom_map


def _get_data(filters):
	log_filters = {
		"manufacturing_work_order": filters.manufacturing_work_order,
		"manufacturing_operation": filters.manufacturing_operation,
		"is_cancelled": 0,
	}
	if filters.get("item_code"):
		log_filters["item_code"] = filters.item_code

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
		],
		# `name desc` is the tiebreak: MOP Logs minted by one Stock Entry submit
		# share a `creation` timestamp, so `creation desc` alone leaves the
		# keep-first below non-deterministic.
		order_by="manufacturing_operation asc, item_code asc, creation desc, name desc",
	)

	# Keep only the latest row per (manufacturing_operation, item_code, batch_no).
	# The MOP is pinned to one value by a mandatory filter today, but it stays in
	# the key -- and the prefetch key sets below are derived from the returned
	# rows, not from the filter -- so this stays correct if that filter is ever
	# relaxed back to whole-MWO.
	seen = {}
	for log in mop_logs:
		key = (log.manufacturing_operation, log.item_code, log.batch_no)
		if key not in seen:
			seen[key] = log

	if not seen:
		return []

	mop_meta_map = _preload_mop_meta_map(
		log.manufacturing_operation for log in seen.values()
	)
	batch_map = _preload_batch_ownership_map(log.batch_no for log in seen.values())
	uom_map = _preload_uom_map(log.item_code for log in seen.values())

	data = []
	for log in sorted(
		seen.values(),
		key=lambda r: (
			r.manufacturing_operation or "",
			r.item_code or "",
			r.batch_no or "",
		),
	):
		qty = flt(log.get("qty_after_transaction_batch_based") or 0)
		pcs = cint(log.get("pcs_after_transaction_batch_based") or 0)
		# A zero balance is nothing left to report, so the row is dropped whatever
		# its Pcs says -- Pcs alone does not keep a row alive. A NEGATIVE balance is
		# kept on purpose: it is impossible in reality and the operator needs to see
		# it, same reasoning as the ownership anomalies in the module docstring.
		if not qty:
			continue

		mop_m = mop_meta_map.get(log.manufacturing_operation) or frappe._dict()
		# A blank batch -- or a batch_no string pointing at a batch that no longer
		# exists, since batch_no is plain Data with no FK -- leaves both columns
		# empty rather than defaulting to Regular Stock.
		inventory_type, customer = batch_map.get(log.batch_no) or (None, None)

		data.append(
			{
				"manufacturing_work_order": log.manufacturing_work_order,
				"manufacturing_operation": log.manufacturing_operation,
				"manufacturing_operation_status": mop_m.get("status"),
				"item_code": log.item_code,
				"qty": qty,
				"pcs": pcs,
				"uom": uom_map.get(log.item_code, ""),
				"batch_no": log.batch_no,
				"department": mop_m.get("department"),
				"inventory_type": inventory_type,
				"customer": customer,
			}
		)

	return data
