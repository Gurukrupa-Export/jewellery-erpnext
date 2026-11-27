"""Add Department.custom_enable_loss_booking_for_diamond_gemstone.

Ticking it shows the "Book Loss of Diamond/Gemstone" button on that department's
Manufacturing Operations (see doctype/manufacturing_operation/dg_loss_booking.py). Field only;
no value is set.

Idempotent. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_department_dg_loss_booking_flag.execute
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Department": [
				{
					"fieldname": "custom_enable_loss_booking_for_diamond_gemstone",
					"fieldtype": "Check",
					"label": "Enable Loss Booking for Diamond/Gemstone",
					"insert_after": "custom_enable_process_control",
					"description": "Shows the Book Loss of Diamond/Gemstone button on this department's Manufacturing Operations.",
				}
			]
		}
	)
