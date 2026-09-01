"""Move ``Supplier.custom_allowed_item_group`` into its own full-width section
after the ``image`` field.

The Table field currently renders at the end of the form because it is absent
from the ``field_order`` Property Setter (67 entries) that overrides the whole
layout. This patch:

1. creates a dedicated Section Break ``custom_section_allowed_item_group``
   (insert_after ``image``),
2. points the Table's ``insert_after`` at that section,
3. injects both fields into the ``field_order`` Property Setter immediately
   after ``image`` (index 11, before ``section_break_hnpah``),

keeping the exact same fieldname / fieldtype / options so existing
``Supplier Allowed Item Group`` child rows (linked by ``parent`` = Supplier
name) remain intact.

Safe to re-run: every step is guarded by existence checks and idempotent
keyed lookups. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.move_supplier_allowed_item_group.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SECTION_FIELD = "custom_section_allowed_item_group"
TABLE_FIELD = "custom_allowed_item_group"
DOCTYPE = "Supplier"


def execute():
    doctype = DOCTYPE

    # 1. Create the Section Break after `image`. create_custom_fields is
    # idempotent keyed on (dt, fieldname) — no-op if already present.
    create_custom_fields(
        {
            doctype: [
                {
                    "fieldname": SECTION_FIELD,
                    "label": "Allowed Item Group Section",
                    "fieldtype": "Section Break",
                    "insert_after": "image",
                }
            ]
        },
        update=True,
    )

    # 2. Direct DB update bypasses Frappe's custom-field chronological sort
    # bug; Property Setters do NOT re-order Custom Fields.
    frappe.db.set_value(
        "Custom Field", {"dt": doctype, "fieldname": SECTION_FIELD}, "insert_after", "image"
    )
    frappe.db.set_value(
        "Custom Field", {"dt": doctype, "fieldname": TABLE_FIELD}, "insert_after", SECTION_FIELD
    )

    # Clean up any Property Setters that would override insert_after / width.
    frappe.db.delete(
        "Property Setter",
        {"doc_type": doctype, "field_name": TABLE_FIELD, "property": "insert_after"},
    )
    frappe.db.delete(
        "Property Setter",
        {"doc_type": doctype, "field_name": SECTION_FIELD, "property": "insert_after"},
    )

    # Force the Table to 100% width.
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    make_property_setter(doctype, TABLE_FIELD, "width", "100%", "Data")

    # 3. The `field_order` Property Setter overrides the entire layout. Both our
    # fields must be injected right after `image` (before section_break_hnpah).
    field_order_str = frappe.db.get_value(
        "Property Setter", {"doc_type": doctype, "property": "field_order"}, "value"
    )
    if field_order_str:
        field_order = json.loads(field_order_str)

        # Remove both fields if present anywhere, so the injection below is
        # deterministic (no duplicates).
        for f in (SECTION_FIELD, TABLE_FIELD):
            if f in field_order:
                field_order.remove(f)

        if "image" in field_order:
            anchor_idx = field_order.index("image") + 1
            for i, f in enumerate((SECTION_FIELD, TABLE_FIELD)):
                field_order.insert(anchor_idx + i, f)

            frappe.db.set_value(
                "Property Setter",
                {"doc_type": doctype, "property": "field_order"},
                "value",
                json.dumps(field_order),
            )

    # Reset idx so the ordering is driven purely by insert_after / field_order.
    frappe.db.sql(
        "UPDATE `tabCustom Field` SET idx = 0 WHERE dt = %s", (doctype,)
    )
    frappe.db.commit()

    frappe.clear_cache(doctype=doctype)
    print(f"[move_supplier_allowed_item_group] {TABLE_FIELD} moved into {SECTION_FIELD} after image")