"""Provision the ``Purchase Receipt.custom_refining_entry`` back-link.

External Refinery's receiving entry books the refined metal back in as a Purchase
Receipt against the sending entry's service Purchase Order
(``RefiningEntry.receive_external_refined_metal``). This link ties that receipt back to
its Refining Entry, mirroring ``Purchase Order.refining_entry``
(``add_po_refining_entry_field``), so the entry can find/cancel it.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, a fixture-only column would never reach the DB. Wired
in two idempotent places: this ``post_model_sync`` patch and ``create_test_data.setup_data``.
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_purchase_receipt_refining_entry_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Purchase Receipt": [
			{
				"fieldname": "custom_refining_entry",
				"fieldtype": "Link",
				"label": "Refining Entry",
				"options": "Refining Entry",
				"insert_after": "company",
				"read_only": 1,
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			}
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_purchase_receipt_refining_entry_field: ensured Purchase Receipt.custom_refining_entry"
	)
