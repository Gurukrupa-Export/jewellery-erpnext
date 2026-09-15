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

The field is populated for a piece the Serial Number Creator produces -- and ONLY for
those; see ``doc_events.serial_no.set_stamping_no``. This patch backfills SNC-produced
Serial No records with sequential numbers ordered by creation date within each year.

"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
	next_stamping_sequence,
	stamping_prefix,
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

	# Backfill only SNC-PRODUCED pieces that have never been stamped, and only those.
	# A stamping number means "the Serial Number Creator produced this piece"; Job Card
	# tags, Purchase Receipt serials and anything hand-created must stay blank, the same
	# rule the live path now enforces by minting solely in update_new_serial_no.
	#
	# `Serial Number Creator.fg_serial_no` is the definitive set: it is written straight
	# after create_manufacturing_entry mints the serial, so a submitted SNC names exactly
	# the one piece it produced.
	#
	# This only matters for FRESH sites -- on production this patch has already run and
	# will not run again. create_test_data re-executes it on every test-site rebuild,
	# which is what would otherwise keep stamping non-SNC serials and contradict the rule.
	#
	# A stamping number ends up physically on the piece, so a re-run must never renumber
	# one that already carries a value -- seed each year's counter from the numbers
	# already issued instead.
	snc_serials = frappe.db.get_all(
		"Serial Number Creator",
		filters={"docstatus": 1, "fg_serial_no": ["is", "set"]},
		pluck="fg_serial_no",
	)

	unstamped = (
		frappe.db.get_all(
			"Serial No",
			filters={
				"custom_stamping_no": ["is", "not set"],
				"name": ["in", snc_serials],
			},
			fields=["name", "creation"],
			order_by="creation asc",
		)
		if snc_serials
		else []
	)

	# Seeded by CREATION year, deliberately: a legacy piece is labelled with the year it was
	# made, while the live path (stamping_prefix() with no argument) labels a new piece with
	# the year its number is issued. Each prefix owns its own counter row, so the two can
	# never collide.
	year_sequences = {}
	for serial_no_doc in unstamped:
		creation_year = serial_no_doc.creation.year
		prefix = stamping_prefix(serial_no_doc.creation)

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
		f"add_serial_no_stamping_no_field: backfilled {len(unstamped)} SNC-produced Serial No records with year-based sequential stamping numbers"
	)

	# Advance the tabSeries counters past everything just handed out. The backfill issues
	# numbers through next_stamping_sequence, which does NOT touch the counters, so without
	# this the counter trails the data. Inert on production (this patch is already logged
	# there) but load-bearing for create_test_data, which calls execute() on every rebuild.
	from jewellery_erpnext.patches.seed_serial_no_stamping_series import (
		execute as seed_stamping_series,
	)

	seed_stamping_series()
