
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    fieldname = "custom_serial_print_with_hallmarking"

    if not frappe.db.exists(
        "Custom Field",
        {"dt": "Customer", "fieldname": fieldname}
    ):
        custom_fields = {
            "Customer": [
                {
                    "fieldname": fieldname,
                    "label": "Serial Print With Hallmarking",
                    "fieldtype": "Check",
                    "insert_after": "prospect_name",
                    "is_system_generated": 0,
                    "module": "Jewellery Erpnext",
                }
            ]
        }

        create_custom_fields(custom_fields, ignore_validate=True)

        frappe.logger().info(
            f"Created Custom Field {fieldname} on Customer"
        )
    else:
        frappe.logger().info(
            f"Custom Field {fieldname} already exists on Customer; skipped"
        )
