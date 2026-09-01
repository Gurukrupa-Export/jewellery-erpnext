"""Provision ``Serial No.stamping_no`` — last 6 digits for stamping purposes.

This field stores the last 6 digits of the serial number and is used for stamping
purposes on jewellery items. For example, if the serial number is "KLHGX62F0297",
the stamping_no will be "2F0297".

The field is automatically populated when a new Serial No is created, and this
patch populates the field for all existing Serial No records.

"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	# Create the custom field
	custom_fields = {
		"Serial No": [
			{
				"fieldname": "custom_stamping_no",
				"fieldtype": "Data",
				"label": "Stamping No",
				"insert_after": "description",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"length": 10,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_serial_no_stamping_no_field: ensured Serial No.custom_stamping_no (Data)"
	)

	# Backfill existing Serial No records with custom_stamping_no
	serial_nos = frappe.db.get_all("Serial No", fields=["name", "serial_no"])

	for serial_no_doc in serial_nos:
		if serial_no_doc.serial_no:
			custom_stamping_no = serial_no_doc.serial_no[-6:].upper()
			frappe.db.set_value(
				"Serial No",
				serial_no_doc.name,
				"custom_stamping_no",
				custom_stamping_no,
				update_modified=False,
			)

	frappe.logger().info(
		f"add_serial_no_stamping_no_field: backfilled {len(serial_nos)} Serial No records with custom_stamping_no"
	)
