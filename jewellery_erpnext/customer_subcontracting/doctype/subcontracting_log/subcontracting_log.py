# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage import (
	classify_gold_usage,
	find_pending_settlements,
	get_inventory_data,
	get_order_customer,
	get_sales_order,
	update_pending_settlement,
)


class SubcontractingLog(Document):
	pass


ENTRY_TYPE = {
	"Customer Goods Received": {
		"transaction_type": "Customer Goods Received",
		"is_repack": 0,
	},
	"Customer Goods Transfer": {
		"transaction_type": "Customer Goods Transfer",
		"is_repack": 0,
	},
	"Material Transfer (DEPARTMENT)": {
		"transaction_type": "Material Tranfer (DEPARTMENT)",
		"is_repack": 0,
	},
	"Material Transfer to Department": {
		"transaction_type": "Manufacturing Tranfer to Department",
		"is_repack": 0,
	},
	"Material Transfer (WORK ORDER)": {
		"transaction_type": "Material Transfer (WORK ORDER)",
		"is_repack": 0,
	},
	"Subcontracting Repack": {
		"transaction_type": "Subcontracting Repack",
		"is_repack": 1,
	},
}


def create_subcontracting_log(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	if doc.docstatus != 1:
		return

	config = ENTRY_TYPE.get(doc.stock_entry_type)

	if not config:
		return

	pending_rows = []

	for item in doc.items:
		if not item.batch_no:
			continue

		if doc.stock_entry_type in [
			"Customer Goods Received",
			"Customer Goods Transfer",
			"Material Transfer (DEPARTMENT)",
		]:
			log_data = get_inventory_data(doc, item, config)

		elif doc.stock_entry_type in [
			"Material Transfer to Department",
			"Material Transfer (WORK ORDER)",
		]:
			customer = get_order_customer(doc)
			ownership = (
				"Customer Gold"
				if item.inventory_type == "Customer Goods"
				else "Company Gold"
			)
			usage_data = classify_gold_usage(doc, item)
			sales_order = get_sales_order(doc)
			log_data = {
				"doctype": "Subcontracting Log",
				"customer": customer,
				"batch": item.batch_no,
				"item": item.item_code,
				"quantity": item.qty,
				"pure_qty": item.custom_pure_qty or 0,
				"source_warehouse": item.s_warehouse,
				"target_warehouse": item.t_warehouse,
				"inventory_type": item.inventory_type,
				"ownership": ownership,
				"reference_doctype": doc.doctype,
				"reference_docname": doc.name,
				"transaction_type": config["transaction_type"],
				"sales_order_name": sales_order,
				"mwo_type": usage_data.get("mwo_type"),
				"parent_manufacturing_order_name": doc.manufacturing_order,
				"manufacturing_work_order_name": item.custom_manufacturing_work_order
				or doc.manufacturing_work_order,
				"manufacturing_operation_name": item.manufacturing_operation,
				"usage_batch": item.batch_no,
				"batch_item": item.item_code,
				"usage_type": usage_data.get("usage_type"),
				"used_as_fallback": usage_data.get("used_as_fallback", 0),
				"settlement_required": usage_data.get("settlement_required", 0),
				"settlement_status": usage_data.get("settlement_status"),
				"settlement_type": usage_data.get("settlement_type"),
				"pending_pure_qty": item.custom_pure_qty
				if usage_data.get("settlement_required")
				else 0,
				"settled_pure_qty": 0,
				"balance_pure_qty": item.custom_pure_qty
				if usage_data.get("settlement_required")
				else 0,
				"settlement_customer": usage_data.get("settlement_customer"),
				"settlement_priority": 1,
				"is_repack": config["is_repack"],
				"repack_entry": doc.name if config["is_repack"] else None,
			}

		elif doc.stock_entry_type == "Subcontracting Repack":
			# only finished item
			if not item.is_finished_item:
				continue

			ownership = (
				"Customer Gold"
				if item.inventory_type == "Customer Goods"
				else "Company Gold"
			)

			# ALWAYS create repack inventory log
			base_log_data = {
				"doctype": "Subcontracting Log",
				"reference_doctype": doc.doctype,
				"reference_docname": doc.name,
				"transaction_type": config["transaction_type"],
				"is_repack": 1,
				"repack_entry": doc.name,
				"customer": item.customer,
				"batch": item.batch_no,
				"item": item.item_code,
				"quantity": item.qty,
				"pure_qty": item.custom_pure_qty or 0,
				"inventory_type": item.inventory_type,
				"ownership": ownership,
				"source_warehouse": item.s_warehouse,
				"target_warehouse": item.t_warehouse,
			}

			repack_log = frappe.get_doc(base_log_data)
			repack_log.insert(ignore_permissions=True)

			pending_logs = find_pending_settlements(item)

			if not pending_logs:
				continue

			remaining_qty = item.custom_pure_qty or 0

			for pending_log in pending_logs:
				if remaining_qty <= 0:
					break

				settle_qty = min(remaining_qty, pending_log.balance_pure_qty)

				settlement_log = frappe.get_doc(
					{
						"doctype": "Subcontracting Log",
						"reference_doctype": doc.doctype,
						"reference_docname": doc.name,
						"transaction_type": "Settlement",
						"is_repack": 1,
						"repack_entry": doc.name,
						"customer": pending_log.customer,
						"batch": item.batch_no,
						"item": item.item_code,
						"settled_pure_qty": settle_qty,
						"settled_against_log": pending_log.name,
						"settled_by_repack": doc.name,
						"settlement_batch": item.batch_no,
						"sales_order_name": pending_log.sales_order_name,
						"parent_manufacturing_order_name": pending_log.parent_manufacturing_order_name,
						"manufacturing_work_order_name": pending_log.manufacturing_work_order_name,
						"manufacturing_operation_name": pending_log.manufacturing_operation_name,
					}
				)

				settlement_log.insert(ignore_permissions=True)

				update_pending_settlement(
					pending_log.name,
					settle_qty,
					doc.name,
					item.batch_no,
				)

				remaining_qty -= settle_qty

			continue

		pending_rows.append(log_data)

	_insert_logs_in_bulk(pending_rows)


# Framework columns bulk_insert must be given explicitly -- it runs no Document lifecycle.
_LOG_FRAMEWORK_COLUMNS = (
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
)


def _insert_logs_in_bulk(rows, chunk_size=500):
	"""Insert many Subcontracting Log rows with one multi-row INSERT per chunk.

	A consolidated EOD Stock Entry has one row per batched item -- 26,489 on one real run --
	and this ran a full ``frappe.get_doc(...).insert()`` for every one of them. Splitting the
	Stock Entry does NOT reduce that count (the rows are the same rows), so batching here is
	the only lever.

	Safe to bulk-insert because ``SubcontractingLog`` is a plain ``Document`` with **no**
	controller methods, there are no ``doc_events`` registered for it in any app, and it has
	no ``autoname``/naming series -- frappe names it with a 10-char hash, which is supplied
	here. If any of those three facts ever changes, this must go back to ``doc.insert()``:
	``bulk_insert`` fires no hooks at all.

	Falls back to per-row inserts if the batch fails, so one bad value costs its own row
	rather than the whole Stock Entry's audit trail.
	"""
	if not rows:
		return

	stamp = now_datetime()
	user = frappe.session.user or "Administrator"
	# Branches build different key sets, and bulk_insert needs one fixed column list.
	fields = sorted({k for row in rows for k in row if k != "doctype"})
	existing = set(frappe.db.get_table_columns("Subcontracting Log") or [])
	if existing:
		fields = [f for f in fields if f in existing]
	columns = list(_LOG_FRAMEWORK_COLUMNS) + fields

	values = []
	for idx, row in enumerate(rows, 1):
		base = (
			frappe.generate_hash(length=10),
			user,
			stamp,
			stamp,
			user,
			0,
			idx,
		)
		values.append(base + tuple(row.get(f) for f in fields))

	try:
		frappe.db.bulk_insert(
			"Subcontracting Log", fields=columns, values=values, chunk_size=chunk_size
		)
		return
	except Exception:
		frappe.logger().exception(
			"Subcontracting Log: bulk insert of %s row(s) failed; retrying per row",
			len(rows),
		)

	for row in rows:
		try:
			frappe.get_doc(dict(row, doctype="Subcontracting Log")).insert(
				ignore_permissions=True
			)
		except Exception:
			frappe.logger().exception(
				"Subcontracting Log: could not write row for %s / %s",
				row.get("reference_docname"),
				row.get("batch"),
			)
