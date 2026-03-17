# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)

    return columns, data


def get_columns():
    return [
        {
            "label": "Batch No",
            "fieldname": "batch_no",
            "fieldtype": "Link",
            "options": "Batch",
            "width": 280,
        },
        {
            "label": "Owner",
            "fieldname": "owner",
            "fieldtype": "Link",
            "options": "Customer",
            "width": 130,
        },
        {
            "label": "Actual Qty",
            "fieldname": "actual_qty",
            "fieldtype": "Float",
            "precision": "3",
            "width": 150,
        },
        {
            "label": "Used Same",
            "fieldname": "used_same",
            "fieldtype": "Float",
            "precision": "3",
            "width": 150,
        },
        {
            "label": "Used Other",
            "fieldname": "used_other",
            "fieldtype": "Float",
            "precision": "3",
            "width": 140,
        },
        {
            "label": "Batch Balance",
            "fieldname": "batch_balance",
            "fieldtype": "Float",
            "precision": "3",
            "width": 150,
        },
    ]


def get_data(filters):

    batch_conditions = ""
    sle_conditions = ""

    if filters.get("batch_no"):
        batch_conditions += " AND b.name = %(batch_no)s "
        sle_conditions += " AND sbe.batch_no = %(batch_no)s "

    if filters.get("customer"):
        batch_conditions += " AND b.custom_customer = %(customer)s "

    batches = frappe.db.sql(f"""
        SELECT
            b.name,
            b.custom_customer,
            b.batch_qty
        FROM `tabBatch` b
        WHERE
            b.custom_inventory_type = 'Customer Goods'
            {batch_conditions}
		ORDER BY creation DESC
		LIMIT 200
    """, filters, as_dict=1)

    if not batches:
        return []

    batch_owner_map = {b.name: b.custom_customer for b in batches}

    inward_data = frappe.db.sql(f"""
        SELECT
            sbe.batch_no,
            SUM(sle.actual_qty) AS qty
        FROM `tabStock Ledger Entry` sle
        JOIN `tabSerial and Batch Entry` sbe
            ON sle.serial_and_batch_bundle = sbe.parent
        WHERE
            sle.inventory_type = 'Customer Goods'
            AND sle.actual_qty > 0
            AND sle.is_cancelled = 0
            AND sbe.batch_no IS NOT NULL
            {sle_conditions}
        GROUP BY sbe.batch_no
    """, filters, as_dict=1)

    actual_map = {d.batch_no: d.qty for d in inward_data}

    usage_data = frappe.db.sql(f"""
        SELECT
            sbe.batch_no,
            sed.customer,
            SUM(ABS(sle.actual_qty)) AS qty
        FROM `tabStock Ledger Entry` sle
        JOIN `tabSerial and Batch Entry` sbe
            ON sle.serial_and_batch_bundle = sbe.parent
        JOIN `tabStock Entry Detail` sed
            ON sle.voucher_detail_no = sed.name
        WHERE
            sle.inventory_type = 'Customer Goods'
            AND sle.actual_qty < 0
            AND sle.is_cancelled = 0
            AND sbe.batch_no IS NOT NULL
            {sle_conditions}
        GROUP BY
            sbe.batch_no,
            sed.customer
    """, filters, as_dict=1)

    used_same_map = {}
    used_other_map = {}

    for row in usage_data:

        batch_no = row.batch_no
        qty = row.qty or 0
        customer = row.customer

        owner = batch_owner_map.get(batch_no)

        if not owner:
            continue

        if customer == owner:
            used_same_map[batch_no] = used_same_map.get(batch_no, 0) + qty
        else:
            used_other_map[batch_no] = used_other_map.get(batch_no, 0) + qty


    data = []

    for batch in batches:

        batch_no = batch.name

        actual_qty = actual_map.get(batch_no, 0)
        # actual_qty = batch.batch_qty or 0
        used_same = used_same_map.get(batch_no, 0)
        used_other = used_other_map.get(batch_no, 0)

        balance = actual_qty - used_same - used_other

        data.append({
            "batch_no": batch_no,
            "owner": batch.custom_customer,
            "actual_qty": actual_qty,
            "used_same": used_same,
            "used_other": used_other,
            "batch_balance": balance
        })

    return data


# unoptimized code


# def get_data(filters):

#     batch_conditions = ""

#     if filters.get("batch_no"):
#         batch_conditions += " AND b.name = %(batch_no)s "

#     if filters.get("customer"):
#         batch_conditions += " AND b.custom_customer = %(customer)s "

#     # Fetch batches
#     batches = frappe.db.sql(f"""
#         SELECT
#             b.name,
#             b.custom_customer,
#             b.batch_qty
#         FROM `tabBatch` b
#         WHERE
#             b.custom_inventory_type = 'Customer Goods'
#             {batch_conditions}
#         ORDER BY b.creation DESC
#         LIMIT 200
#     """, filters, as_dict=1)

#     if not batches:
#         return []

#     batch_owner_map = {b.name: b.custom_customer for b in batches}

#     # Usage data (outward movement)
#     usage_data = frappe.db.sql(f"""
#         SELECT
#             sbe.batch_no,
#             sed.customer,
#             SUM(ABS(sle.actual_qty)) AS qty
#         FROM `tabStock Ledger Entry` sle
#         JOIN `tabSerial and Batch Entry` sbe
#             ON sle.serial_and_batch_bundle = sbe.parent
#         JOIN `tabStock Entry Detail` sed
#             ON sle.voucher_detail_no = sed.name
#         WHERE
#             sle.inventory_type = 'Customer Goods'
#             AND sle.actual_qty < 0
#             AND sle.is_cancelled = 0
#             AND sbe.batch_no IS NOT NULL
#         GROUP BY
#             sbe.batch_no,
#             sed.customer
#     """, filters, as_dict=1)

#     used_same_map = {}
#     used_other_map = {}

#     for row in usage_data:

#         batch_no = row.batch_no
#         qty = row.qty or 0
#         customer = row.customer

#         owner = batch_owner_map.get(batch_no)

#         if not owner:
#             continue

#         if customer == owner:
#             used_same_map[batch_no] = used_same_map.get(batch_no, 0) + qty
#         else:
#             used_other_map[batch_no] = used_other_map.get(batch_no, 0) + qty

#     data = []

#     for batch in batches:

#         batch_no = batch.name

#         # Actual Qty from batch record
#         actual_qty = batch.batch_qty or 0

#         used_same = used_same_map.get(batch_no, 0)
#         used_other = used_other_map.get(batch_no, 0)

#         balance = actual_qty - used_same - used_other

#         data.append({
#             "batch_no": batch_no,
#             "owner": batch.custom_customer,
#             "actual_qty": actual_qty,
#             "used_same": used_same,
#             "used_other": used_other,
#             "batch_balance": balance
#         })

#     return data
