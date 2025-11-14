import frappe

def execute():
    department_doctypes = [
        "Ignore Department For MOP",
        "Manufacturing Setting",
        "Variant based Warehouse",
        "Department Operation",
        "Manufacturing Work Order",
        "Manufacturing Operation",
    ]

    for doctype in department_doctypes:
        try:
            frappe.db.add_index(doctype, ["department"])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message=f"Failed to add index on department for {doctype}: {e}"
            )

    try:
        frappe.db.add_index("Manufacturing Setting", ["default_fg_department"])
    except Exception as e:
        frappe.log_error(
            title="Index Creation Failed",
            message=f"Failed to add index on default_fg_department: {e}"
        )

    try:
        frappe.db.add_index("Manufacturing Setting", ["default_department"])
    except Exception as e:
        frappe.log_error(
            title="Index Creation Failed",
            message=f"Failed to add index on default_department: {e}"
        )
        
    print("Index for department field added sucessfully.")
