"""Create the order type / sales type / flow type fields carried from Order down to Purchase Order.

Why a patch and not ``custom_fields/*.json``: this app's ``after_migrate`` hook is commented out
(``hooks.py:14``) and ``migrate.py:after_migrate()`` is the only reader of those files, so they
never reach a real site. The ``fixtures`` hook is commented out too (``hooks.py:247``), and
``gke_customization``'s Custom Field fixture is scoped to its own modules. A patch is the only
delivery mechanism.

Manufacturing Plan is an app-owned DocType, so its three fields ship in ``manufacturing_plan.json``
and arrive with ``bench migrate`` -- they are not repeated here.

Idempotent: every field is guarded on ``frappe.db.has_column``, so a field that already exists is
left exactly as it is.

Ad-hoc: bench --site gk execute jewellery_erpnext.patches.add_order_flow_sales_type_fields.execute
"""

import frappe

FLOW_TYPE_OPTIONS = "\nMTO\nMTBI\nMTR\nFILLER\nGCC\nUS\nJWO\nPROTO\nSTT\nPCPM"

CUSTOM_FIELDS = {
    "Quotation": [
        {
            "fieldname": "custom_flow_type",
            "label": "Flow Type",
            "fieldtype": "Select",
            "options": FLOW_TYPE_OPTIONS,
            "insert_after": "custom_sales_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
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
    "Purchase Order": [
        # Data, not Select: the Sales Order's order_type option list lives only as a Property Setter
        # in the database, so a hard-coded Select here would start failing Manufacturing Plan submit
        # the day someone edits that list. Material Request.custom_order_type does the same.
        {
            "fieldname": "custom_order_type",
            "label": "Order Type",
            "fieldtype": "Data",
            "insert_after": "ref_customer",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        },
        {
            "fieldname": "custom_sales_type",
            "label": "Sales Type",
            "fieldtype": "Link",
            "options": "Sales Type",
            "insert_after": "custom_order_type",
            "read_only": 1,
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
        "add_order_flow_sales_type_fields: created "
        + ", ".join(
            f"{dt}.{f['fieldname']}" for dt, fields in pending.items() for f in fields
        )
    )
