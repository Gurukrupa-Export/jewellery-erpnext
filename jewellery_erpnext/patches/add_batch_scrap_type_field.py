"""Provision the ``Batch.custom_batch_type`` marker used by Scrap Refining.

Manufacturing scrap is received back into the department (the "Receive Scrap Item"
Manufacturing Operation action) under the SAME item code but a freshly created
batch tagged ``custom_batch_type = "Scrap"`` — there is no dedicated scrap item.
Scrap Refining then fetches ONLY Scrap-typed batches
(``RefiningEntry.get_scrap_items_balance``), so ordinary department stock in the
same warehouse is never pulled into a refining entry.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach
the DB and ``batch.custom_batch_type = "Scrap"`` would raise ``1054 Unknown
column``. Per the app convention this is wired in two idempotent places: this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_batch_scrap_type_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Batch": [
			{
				"fieldname": "custom_batch_type",
				"fieldtype": "Select",
				# Blank (ordinary stock) or "Scrap" (manufacturing scrap awaiting refining).
				"options": "\nScrap",
				"label": "Batch Type",
				"insert_after": "item_name",
				"read_only": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info("add_batch_scrap_type_field: ensured Batch.custom_batch_type")
