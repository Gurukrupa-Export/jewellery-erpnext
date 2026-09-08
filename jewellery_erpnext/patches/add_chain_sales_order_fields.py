"""Create the sales type / order type / flow type fields on the manufacturing chain docs.

These carry the three dimension fields from Order Form -> Manufacturing Plan down to the
finished Serial No. Manufacturing Plan already ships its three fields in
``manufacturing_plan.json`` (``custom_sales_type`` / ``custom_order_type`` /
``custom_flow_type``) and they are stamped there from the Sales Order, so this patch only adds
the downstream columns: PMO, MWO, SNC (Serial Number Creator) and the finished Serial No.

Why a patch and not ``custom_fields/*.json``: the same reason as
``add_order_flow_sales_type_fields`` -- this app's ``after_migrate`` hook and ``fixtures`` hook
are commented out, so a patch is the only delivery mechanism that reaches a real site.

Idempotent: every field is guarded on ``frappe.db.has_column``, so a field that already exists
is left exactly as it is. No defaults are invented -- empty values stay empty.

Ad-hoc: bench --site gk15 execute jewellery_erpnext.patches.add_chain_sales_order_fields.execute
"""

import frappe

FLOW_TYPE_OPTIONS = "\nMTO\nMTBI\nMTR\nFILLER\nGCC\nUS\nJWO\nPROTO\nSTT\nPCPM"

# order_type already exists as a Data field on PMO / MWO / SNC (each fetch_from its immediate
# source), so it is deliberately not repeated here -- see add_order_flow_sales_type_fields for
# why Data rather than a hard-coded Select.
CUSTOM_FIELDS = {
    "Parent Manufacturing Order": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
    ],
    "Manufacturing Work Order": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
    ],
    "Serial Number Creator": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
    ],
    "Serial No": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "custom_bom_no",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "order_type",
            "label": "Order Type",
            "fieldtype": "Data",
            "insert_after": "sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "order_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
    ],
}


def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    pending = {}
    for doctype, fields in CUSTOM_FIELDS.items():
        missing = [
            f for f in fields if not frappe.db.has_column(doctype, f["fieldname"])
        ]
        if missing:
            pending[doctype] = missing

    if not pending:
        return

    create_custom_fields(pending, ignore_validate=True)
    frappe.db.commit()
    frappe.logger().info(
        "add_chain_sales_order_fields: created "
        + ", ".join(
            f"{dt}.{f['fieldname']}" for dt, fields in pending.items() for f in fields
        )
    )
