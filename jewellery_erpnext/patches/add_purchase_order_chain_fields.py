import frappe

# Mirrors this branch's Order Form flow_type options (no STT / PCPM on this branch).
FLOW_TYPE_OPTIONS = "\nMTO\nMTBI\nMTR\nFILLER\nGCC\nUS\nJWO\nPROTO"

READ_ONLY_DOWNSTREAM = {"read_only": 1, "is_system_generated": 1, "module": "Jewellery Erpnext"}

CUSTOM_FIELDS = {
    "Purchase Order": [
        {
            "fieldname": "custom_sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "purchase_type",
            **READ_ONLY_DOWNSTREAM,
        },
        {
            "fieldname": "custom_order_type",
            "label": "Order Type",
            "fieldtype": "Data",
            "insert_after": "custom_sales_type",
            **READ_ONLY_DOWNSTREAM,
        },
        {
            "fieldname": "custom_flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "custom_order_type",
            **READ_ONLY_DOWNSTREAM,
        },
    ]
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
        "add_purchase_order_chain_fields: created "
        + ", ".join(f"{dt}.{f['fieldname']}" for dt, fields in pending.items() for f in fields)
    )
