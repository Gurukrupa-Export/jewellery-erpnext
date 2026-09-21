"""Create the design type field carried from Order down to the manufacturing chain.

These carry `Order.design_type` from the Quotation down to the Manufacturing Work Order, alongside
the order type / sales type / flow type fields added by ``add_order_flow_sales_type_fields`` and
``add_chain_sales_order_fields``. Manufacturing Plan is an app-owned DocType, so its field ships in
``manufacturing_plan.json`` and arrives with ``bench migrate`` -- it is not repeated here.

The fieldname is deliberately ``custom_design_type`` on the stock selling/buying doctypes and bare
``design_type`` on PMO / MWO. Matching the name on both sides of every hop is what lets
``get_mapped_doc`` carry the value for free -- Quotation -> Sales Order, and PMO -> MWO across all
five MWO creation paths. ``no_copy`` is left unset for the same reason: setting it would silently
kill both hops.

Link rather than Data, because ``Order.design_type`` is itself a Link to ``Attribute Value``
(values come from the Item Attribute record named "Design Type"). This differs from the sibling
``custom_order_type``, which is Data only because the Sales Order's option list lives in a
database Property Setter -- that reasoning does not apply here.

Why a patch and not ``custom_fields/*.json``: the same reason as the two patches above -- this
app's ``after_migrate`` hook (``hooks.py:12``) and ``fixtures`` hook (``hooks.py:247``) are both
commented out, and ``migrate.py:after_migrate()`` is the only reader of those files, so a patch is
the only delivery mechanism that reaches a real site.

Every ``insert_after`` anchor is created by ``add_chain_sales_order_fields``, which runs earlier in
``patches.txt`` -- keep it ordered that way.

Idempotent: every field is guarded on ``frappe.db.has_column``, so a field that already exists is
left exactly as it is. No defaults are invented -- empty values stay empty.

Ad-hoc: bench --site gk15 execute jewellery_erpnext.patches.add_design_type_chain_fields.execute
"""

import frappe

CUSTOM_FIELDS = {
    "Quotation": [
        {
            "fieldname": "custom_design_type",
            "label": "Design Type",
            "fieldtype": "Link",
            "options": "Attribute Value",
            "insert_after": "custom_flow_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
    ],
    "Sales Order": [
        {
            "fieldname": "custom_design_type",
            "label": "Design Type",
            "fieldtype": "Link",
            "options": "Attribute Value",
            "insert_after": "custom_flow_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
    ],
    "Purchase Order": [
        {
            "fieldname": "custom_design_type",
            "label": "Design Type",
            "fieldtype": "Link",
            "options": "Attribute Value",
            "insert_after": "custom_flow_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
    ],
    "Parent Manufacturing Order": [
        {
            "fieldname": "design_type",
            "label": "Design Type",
            "fieldtype": "Link",
            "options": "Attribute Value",
            "insert_after": "flow_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
    ],
    "Manufacturing Work Order": [
        {
            "fieldname": "design_type",
            "label": "Design Type",
            "fieldtype": "Link",
            "options": "Attribute Value",
            "insert_after": "flow_type",
            "read_only": 1,
            "is_system_generated": 1,
            "module": "Jewellery Erpnext",
        }
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
        "add_design_type_chain_fields: created "
        + ", ".join(
            f"{dt}.{f['fieldname']}" for dt, fields in pending.items() for f in fields
        )
    )
