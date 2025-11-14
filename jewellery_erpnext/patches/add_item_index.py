import frappe

def execute():

    item_code_doctypes = [
        "MOP Balance Table",
        "Main Slip SE Details",
        "Manufacturing Work Order",
        "Product Details",
        "SC Source Table",
        "Jewellery System Item",
        "Operation Loss Details",
    ]

    for doctype in item_code_doctypes:
        try:
            frappe.db.add_index(doctype, ["item_code"])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message=f"Failed to add index on item_code for {doctype}: {e}"
            )
    
    item_doctypes = [
        "BOM Diamond Detail",
        "BOM Metal Detail",
        "BOM Finding Detail",
    ]

    for doctype in item_doctypes:
        try:
            frappe.db.add_index(doctype, ["item"])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message=f"Failed to add index on item for {doctype}: {e}"
            )

    variant_doctypes = [
        "Variant Loss Table",
        "Variant Loss Warehouse",
    ]

    for doctype in variant_doctypes:
        try:
            frappe.db.add_index(doctype, ["variant"])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message=f"Failed to add index on variant for {doctype}: {e}"
            )


    extra_indexes = {
        "Department Operation": "service_item",
        "Variant Loss Table": "loss_variant",
        "Product Certification Details": "purchase_item",
        "Variant Item Group": "item_variant",
        "Manufacturing Setting": "pure_gold_item",
    }

    for doctype, field in extra_indexes.items():
        try:
            frappe.db.add_index(doctype, [field])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message=f"Failed to add index on {field} for {doctype}: {e}"
            )

    print("Indexes for item field added successfully.")
