"""Create the sales type / order type / flow type fields that carry the order dimensions
from Order Form -> Quotation -> Sales Order -> Manufacturing Plan -> PMO -> MWO -> SNC ->
finished Serial No.

Why a patch and not ``custom_fields/*.json``: this app's ``after_migrate`` hook and
``fixtures`` hook are commented out, so a patch is the only delivery mechanism that reaches a
real site (same reason as ``add_order_flow_sales_type_fields`` on newer branches).

Every downstream field is read-only and non-required with no default -- values are stamped by
the chain itself, so empty stays empty and no existing record is touched.

The Quotation gains ``custom_sales_type`` (editable entry point) and ``custom_flow_type``
(read-only, fetched from the Order via ``make_quotation``).  The Sales Order gains
``custom_flow_type`` (read-only, fetched from the Quotation on every validate).

Manufacturing Plan is stamped on ``validate`` (``ManufacturingPlan.set_order_dimensions``)
from its linked Sales Order's ``sales_type`` / ``order_type`` / ``custom_flow_type``; the MP
fields are read-only so values are always set by the chain.

Idempotent: every field is guarded on ``frappe.db.has_column``, so a field that already exists
is left exactly as it is.

Ad-hoc: bench --site <site> execute jewellery_erpnext.patches.add_chain_sales_order_fields.execute
"""

import frappe

# Mirrors this branch's Order Form flow_type options (no STT / PCPM on this branch).
FLOW_TYPE_OPTIONS = "\nMTO\nMTBI\nMTR\nFILLER\nGCC\nUS\nJWO\nPROTO"

READ_ONLY_DOWNSTREAM = {"read_only": 1, "is_system_generated": 1, "module": "Jewellery Erpnext"}

CUSTOM_FIELDS = {
    "Quotation": [
        {
            "fieldname": "custom_sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "custom_flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "custom_sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
    ],
    "Sales Order": [
        {
            "fieldname": "custom_flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
    ],
    "Manufacturing Plan": [
        {
            "fieldname": "custom_sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "sales_order",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "custom_order_type",
            "label": "Order Type",
            "fieldtype": "Data",
            "insert_after": "custom_sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "custom_flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "custom_order_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
    ],
    "Parent Manufacturing Order": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            **READ_ONLY_DOWNSTREAM,
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            **READ_ONLY_DOWNSTREAM,
        },
    ],
    "Manufacturing Work Order": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            **READ_ONLY_DOWNSTREAM,
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            **READ_ONLY_DOWNSTREAM,
        },
    ],
    "Serial Number Creator": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "order_type",
            **READ_ONLY_DOWNSTREAM,
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "sales_type",
            **READ_ONLY_DOWNSTREAM,
        },
    ],
    "Serial No": [
        {
            "fieldname": "sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "custom_bom_no",
            **READ_ONLY_DOWNSTREAM,
        },
        # Data, not Select: order type option lists live on the source doctypes as Property
        # Setters / local Selects, so a hard-coded Select here would rot. Same rationale as the
        # other branches' order_type Data fields.
        {
            "fieldname": "order_type",
            "label": "Order Type",
            "fieldtype": "Data",
            "insert_after": "sales_type",
            **READ_ONLY_DOWNSTREAM,
        },
        {
            "fieldname": "flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "order_type",
            **READ_ONLY_DOWNSTREAM,
        },
    ],
}


def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    pending = {}
    for doctype, fields in CUSTOM_FIELDS.items():
        missing = [f for f in fields if not frappe.db.has_column(doctype, f["fieldname"])]
        if missing:
            pending[doctype] = missing

    if not pending:
        return

    create_custom_fields(pending, ignore_validate=True)
    frappe.db.commit()
    frappe.logger().info(
        "add_chain_sales_order_fields: created "
        + ", ".join(f"{dt}.{f['fieldname']}" for dt, fields in pending.items() for f in fields)
    )