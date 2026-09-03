"""Provision ``Serial No.custom_stamping_no`` — year-based sequential stamping number.

This field stores a unique sequential stamping number for each Serial No based on
creation year. The format is "2" + year code + 4-digit sequence number.

Year code is derived from year (A=2021, B=2022, C=2023, D=2024, E=2025, F=2026, G=2027, etc.)
The sequence resets for each new year.

Examples:
- 2026: 2F0001, 2F0002, 2F0003, ...
- 2027: 2G0001, 2G0002, 2G0003, ...
- 2028: 2H0001, 2H0002, ...

This allows quick lookup and identification during stamping processes, with year visibility.

The field is automatically populated when a new Serial No is created, and this
patch populates the field for all existing Serial No records with sequential numbers
ordered by creation date within each year.

"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
	next_stamping_sequence,
	stamping_year_code,
)


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

	# Backfill only pieces that have never been stamped. A stamping number ends up
	# physically on the piece, so a re-run (create_test_data calls this to provision
	# test_site) must never renumber one that already carries a value -- seed each
	# year's counter from the numbers already issued instead.
	unstamped = frappe.db.get_all(
		"Serial No",
		filters={"custom_stamping_no": ["is", "not set"]},
		fields=["name", "creation"],
		order_by="creation asc",
	)

	year_sequences = {}
	for serial_no_doc in unstamped:
		creation_year = serial_no_doc.creation.year
		prefix = f"2{stamping_year_code(creation_year)}"

		if creation_year not in year_sequences:
			year_sequences[creation_year] = next_stamping_sequence(prefix)
		else:
			year_sequences[creation_year] += 1

		frappe.db.set_value(
			"Serial No",
			serial_no_doc.name,
			"custom_stamping_no",
			f"{prefix}{year_sequences[creation_year]:04d}",
			update_modified=False,
		)

	frappe.logger().info(
		f"add_serial_no_stamping_no_field: backfilled {len(unstamped)} Serial No records with year-based sequential stamping numbers"
	)
