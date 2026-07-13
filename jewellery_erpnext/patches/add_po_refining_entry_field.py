"""Provision the ``Purchase Order.refining_entry`` back-link.

The External Refinery Flow auto-creates a "Service" Purchase Order when an external
Refining Entry is submitted (``RefiningEntry.create_refining_po``). This link ties
that PO back to its Refining Entry, mirroring the existing ``product_certification``
back-link on Purchase Order, so the entry can find/cancel its PO.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach
the DB. Per the app convention this is wired in two idempotent places: this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_po_refining_entry_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Purchase Order": [
			{
				"fieldname": "refining_entry",
				"fieldtype": "Link",
				"label": "Refining Entry",
				"options": "Refining Entry",
				"insert_after": "company",
				"read_only": 1,
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			}
		],
		# Line-level back-link to the priced band, used to make the entry's PO line
		# billing idempotent (one line per Refinery Price List row / stage).
		"Purchase Order Item": [
			{
				"fieldname": "custom_refining_price_list",
				"fieldtype": "Link",
				"label": "Refinery Price List",
				"options": "Refinery Price List",
				"insert_after": "item_code",
				"read_only": 1,
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_is_refined_metal_line",
				"fieldtype": "Check",
				"label": "Is Refined Metal Line",
				"description": (
					"Marks the rate-0 accountability line for the metal itself on an "
					"External Refinery service PO (as opposed to the service charge "
					"line), so the receiving entry can find it."
				),
				"insert_after": "custom_refining_price_list",
				"read_only": 1,
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			},
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_po_refining_entry_field: ensured Purchase Order.refining_entry + "
		"Purchase Order Item.custom_refining_price_list / custom_is_refined_metal_line"
	)
