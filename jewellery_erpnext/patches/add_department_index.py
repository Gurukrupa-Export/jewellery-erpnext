import frappe

def execute():
    department_doctypes = [
        "Ignore Department For MOP",
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

    employee_doctypes = [
        "Employee Metal Loss",
        "Manufacturing Operation",
    ]
    for doctype in employee_doctypes:
        try:
            frappe.db.add_index(doctype,["employee"])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message=f"Failed to add index on employee:{e}"
            )

    customer_doctypes = [
        "MOP Balance Table",
        "Main Slip SE Details",
        "Parent Manufacturing Order",
        "Customer Payment Terms"
    ]

    for doctype in customer_doctypes:
        try:
            frappe.db.add_index(doctype,["customer"])
        except Exception as e:
            frappe.log_error(
                title="Index Creation Failed",
                message = f"Failed to add index on customer:{e}"
            )
    try:
        frappe.db.add_index("Customer Product Tolerance Master", ["customer_name"])
    except Exception as e:
         frappe.log_error(
                title="Index Creation Failed",
                message = f"Failed to add index on customer_name:{e}"
            )

    print("Index added sucessfully.")
