import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute():
    doctype = "Customer"

    layout = [
        (
            "custom_section_order_type_criteria_tab",
            "Order Type Criteria Section",
            "custom_order_type_criteria",
        ),
        (
            "custom_section_sales_type_tab",
            "Sales Type Section",
            "sales_type",
        ),
        (
            "custom_section_metal_criteria_tab",
            "Metal Criteria Section",
            "metal_criteria",
        ),
        (
            "custom_section_diamond_price_list_tab",
            "Diamond Price List Section",
            "custom_diamond_price_list_table",
        ),
        (
            "custom_section_item_categories_tab",
            "Item Categories Section",
            "custom_item_categories",
        ),
    ]

    previous_field = "supplier_numbers"

    for section_fieldname, section_label, table_fieldname in layout:

        # 1. Create/update the Section Break
        create_custom_fields(
            {
                doctype: [
                    {
                        "fieldname": section_fieldname,
                        "label": section_label,
                        "fieldtype": "Section Break",
                    }
                ]
            },
            update=True,
        )

        # 2. To bypass Frappe's field chronological sorting bug, we must update
        # tabCustom Field directly. Property Setters DO NOT re-order Custom Fields!
        frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": section_fieldname}, "insert_after", previous_field)
        
        if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": table_fieldname}):
            frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": table_fieldname}, "insert_after", previous_field)
            
            # Clean up old property setters that might interfere
            frappe.db.delete("Property Setter", {"doc_type": doctype, "field_name": table_fieldname, "property": "insert_after"})

        # Force table width to 100% via Property Setter (this is a display property, so it works)
        make_property_setter(doctype, table_fieldname, "width", "100%", "Data")
        
        # 3. Untangle fields directly in tabCustom Field to prevent drag-along columns
        if table_fieldname == "sales_type":
            frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": "column_break_6v9nw"}, "insert_after", "compute_making_charges_on")
        elif table_fieldname == "custom_order_type_criteria":
            # Move the offending "No Label" section break far away so it doesn't wrap sales_type
            frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": "section_break_yfp4k"}, "insert_after", "brand")
        elif table_fieldname == "metal_criteria":
            frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": "diamond_quality"}, "insert_after", "custom_gemstone_price_list_type")

        # Next section comes after current table
        previous_field = table_fieldname

    # ------------------------------------------------------------
    # Customer Representatives & Certification Column Break
    # ------------------------------------------------------------
    create_custom_fields(
        {
            doctype: [
                {
                    "fieldname": "custom_section_customer_reps_tab",
                    "label": "Customer Representatives Section",
                    "fieldtype": "Section Break",
                },
                {
                    "fieldname": "custom_column_break_certification",
                    "fieldtype": "Column Break",
                }
            ]
        },
        update=True,
    )

    frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": "custom_section_customer_reps_tab"}, "insert_after", "primary_address")
    frappe.db.set_value("Custom Field", {"dt": doctype, "fieldname": "custom_customer_representatives"}, "insert_after", "custom_section_customer_reps_tab")
    
    frappe.db.delete("Property Setter", {"doc_type": doctype, "field_name": "custom_customer_representatives", "property": "insert_after"})
    make_property_setter(doctype, "custom_customer_representatives", "width", "100%", "Data")

    # ------------------------------------------------------------
    # ULTIMATE FIX: Override the "field_order" Property Setter
    # ------------------------------------------------------------
    # If the user ever dragged/dropped fields in "Customize Form", Frappe created a
    # "field_order" Property Setter. This hardcoded JSON list OVERRIDES ALL insert_after logic.
    import json
    field_order_str = frappe.db.get_value("Property Setter", {"doc_type": doctype, "property": "field_order"}, "value")
    if field_order_str:
        field_order = json.loads(field_order_str)
        
        # Remove all our fields from their broken positions
        fields_to_reorder = [
            "custom_section_order_type_criteria_tab", "custom_order_type_criteria",
            "custom_section_sales_type_tab", "sales_type",
            "custom_section_metal_criteria_tab", "metal_criteria",
            "custom_section_diamond_price_list_tab", "custom_diamond_price_list_table",
            "custom_section_item_categories_tab", "custom_allowed_item_category_for_invoice", "custom_item_categories",
            "section_break_yfp4k", "column_break_6v9nw", "connections_tab"
        ]
        for f in fields_to_reorder:
            if f in field_order:
                field_order.remove(f)
                
        # Find a safe anchor point (custom_gemstone_price_list_type)
        anchor_idx = -1
        if "custom_gemstone_price_list_type" in field_order:
            anchor_idx = field_order.index("custom_gemstone_price_list_type")
        elif "diamond_quality" in field_order:
            # Inject custom_gemstone_price_list_type right before diamond_quality if it's missing
            idx = field_order.index("diamond_quality")
            field_order.insert(idx, "custom_gemstone_price_list_type")
            anchor_idx = idx
        elif "supplier_numbers" in field_order:
            anchor_idx = field_order.index("supplier_numbers")
            
        if anchor_idx != -1:
            perfect_sequence = [
                "custom_section_order_type_criteria_tab", "custom_order_type_criteria",
                "custom_section_sales_type_tab", "sales_type",
                "custom_section_metal_criteria_tab", "metal_criteria",
                "custom_section_diamond_price_list_tab", "custom_diamond_price_list_table",
                "custom_section_item_categories_tab", "custom_allowed_item_category_for_invoice", "custom_item_categories"
            ]
            
            # The Certification Charges block (custom_section_break_xxrxq)
            certification_fields = [
                "custom_section_break_xxrxq",
                "custom_ignore_po_creation_for_certification",
                "custom_separate_hallmarking_invoice",
                "custom_block_refining",
                "custom_no_wastage",
                "custom_column_break_certification",
                "custom_fetch_certification_charge_from_price_list",
                "custom_making_rates_based_on_custom_code",
                "custom_allow_regular_goods_instead_of_customer_goods"
            ]
            
            for cf in certification_fields:
                if cf in field_order:
                    field_order.remove(cf)
                perfect_sequence.append(cf)
                
            for i, f in enumerate(perfect_sequence):
                field_order.insert(anchor_idx + 1 + i, f)
                
        # Bury the dragged-along column/section breaks safely away from our tables
        if "compute_making_charges_on" in field_order:
            field_order.insert(field_order.index("compute_making_charges_on") + 1, "column_break_6v9nw")
        if "brand" in field_order:
            field_order.insert(field_order.index("brand") + 1, "section_break_yfp4k")
            
        # Save the master layout back
        frappe.db.set_value("Property Setter", {"doc_type": doctype, "property": "field_order"}, "value", json.dumps(field_order))

    # Reset idx for all Customer custom fields to 0
    frappe.db.sql("UPDATE `tabCustom Field` SET idx = 0 WHERE dt = %s", (doctype,))
    frappe.db.commit()

    frappe.clear_cache(doctype=doctype)