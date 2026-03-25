# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe


def execute(filters=None):
	if not filters:
		filters = {}

	conditions = get_conditions(filters)

	cgr_data = get_cgr_data(filters, conditions)
	transfer_data = get_transfer_data(filters, conditions)
	repack_data = get_repack_data(filters, conditions)

	report = {}

	for r in cgr_data:
		report[r.batch_no] = {
			"owner": r.customer,
			"item": r.item_code,
			"opening": r.qty,
			"used_same": 0,
			"used_other": 0,
			"repack_to_other": 0,
		}

	for r in transfer_data:
		if r.batch_no not in report:
			continue

		owner = report[r.batch_no]["owner"]
		target = get_customer_from_work_order(r.manufacturing_work_order)

		if owner == target:
			report[r.batch_no]["used_same"] += r.qty
		else:
			report[r.batch_no]["used_other"] += r.qty

	for r in repack_data:
		parent_batch = get_parent_batch(r.batch_no)

		if parent_batch not in report:
			continue

		if r.is_finished_item:
			report[parent_batch]["repack_to_other"] += r.qty

	data = []

	for batch, d in report.items():
		balance = d["opening"] - d["used_same"] - d["used_other"] + d["repack_to_other"]
		balance_from_other = d["used_other"] - d["repack_to_other"]

		data.append(
			[
				batch,
				d["owner"],
				d["item"],
				d["opening"],
				d["used_same"],
				d["used_other"],
				d["repack_to_other"],
				balance,
				balance_from_other,
			]
		)

	columns = get_columns()

	item_filter = filters.get("item_code") or filters.get("item")
	if item_filter and data:
		total_opening = sum(frappe.utils.flt(r[3]) for r in data)
		total_used_same = sum(frappe.utils.flt(r[4]) for r in data)
		total_used_other = sum(frappe.utils.flt(r[5]) for r in data)
		total_repack_to_other = sum(frappe.utils.flt(r[6]) for r in data)

		total_balance = (
			total_opening - total_used_same - total_used_other + total_repack_to_other
		)
		total_balance_from_other = total_used_other - total_repack_to_other

		data.append(
			[
				"Total",
				"",
				item_filter,
				total_opening,
				total_used_same,
				total_used_other,
				total_repack_to_other,
				total_balance,
				total_balance_from_other,
			]
		)

	return columns, data


def get_conditions(filters):
	conditions = ""

	if filters.get("customer"):
		conditions += """
            AND (
                sed.customer = %(customer)s
                OR sed.batch_no LIKE %(customer_prefix)s
            )
        """
		filters["customer_prefix"] = filters["customer"] + "%"

	if filters.get("batch"):
		conditions += " AND sed.batch_no = %(batch)s"

	if filters.get("item_code"):
		conditions += " AND sed.item_code = %(item_code)s"

	return conditions


def get_cgr_data(filters, conditions):
	return frappe.db.sql(
		f"""
        SELECT
            sed.batch_no,
            sed.qty,
            sed.customer,
            sed.item_code
        FROM `tabStock Entry Detail` sed
        JOIN `tabStock Entry` se ON se.name = sed.parent
        WHERE se.stock_entry_type = 'Customer Goods Received'
        {conditions}
    """,
		filters,
		as_dict=1,
	)


def get_transfer_data(filters, conditions):
	return frappe.db.sql(
		f"""
        SELECT
            sed.batch_no,
            sed.qty,
            sed.item_code,
            se.manufacturing_work_order
        FROM `tabStock Entry Detail` sed
        JOIN `tabStock Entry` se ON se.name = sed.parent
        WHERE se.stock_entry_type = 'Material Transfer (WORK ORDER)'
        {conditions}
    """,
		filters,
		as_dict=1,
	)


def get_repack_data(filters, conditions):
	return frappe.db.sql(
		f"""
        SELECT
            sed.batch_no,
            sed.qty,
            sed.item_code,
            sed.is_finished_item
        FROM `tabStock Entry Detail` sed
        JOIN `tabStock Entry` se ON se.name = sed.parent
        WHERE se.stock_entry_type = 'Subcontracting Repack'
        {conditions}
    """,
		filters,
		as_dict=1,
	)


def get_parent_batch(batch):
	if "-A" in batch:
		return batch.split("-A")[0]
	return batch


def get_customer_from_work_order(wo):
	if not wo:
		return None
	meta = frappe.get_meta("Work Order")

	if meta.get_field("customer"):
		return frappe.db.get_value("Work Order", wo, "customer")

	if meta.get_field("sales_order"):
		sales_order = frappe.db.get_value("Work Order", wo, "sales_order")
		if sales_order:
			return frappe.db.get_value("Sales Order", sales_order, "customer")

	if meta.get_field("custom_customer"):
		return frappe.db.get_value("Work Order", wo, "custom_customer")

	return None


def get_columns():
	return [
		"Batch No:Link/Batch:220",
		"Owner:Link/Customer:150",
		"Item:Link/Item:180",
		"Opening Qty:Float:120",
		"Used Same:Float:120",
		"Used Other:Float:120",
		"Repack to Other:Float:150",
		"Balance:Float:120",
		"Balance from other:Float:150",
	]
