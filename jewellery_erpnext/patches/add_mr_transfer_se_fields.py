"""Add tracking fields to Material Request for the deferred "Material Transfer From
Reserve" Stock Entry.

The MR ``on_submit`` no longer creates+submits that secondary SE inside the submit
transaction (which held Series/Bin/SLE/SRE locks through a second full SE submit);
it now enqueues an idempotent ``enqueue_after_commit`` job. These fields make the
deferred work observable and reconcilable:

* ``custom_transfer_se``       — Link to the created SE (also the idempotency guard).
* ``custom_transfer_se_state`` — Pending / Done / Failed.
* ``custom_transfer_se_error`` — last failure message for a Failed run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Material Request": [
			{
				"fieldname": "custom_transfer_se",
				"fieldtype": "Link",
				"options": "Stock Entry",
				"label": "Transfer Stock Entry (Deferred)",
				"insert_after": "custom_reserve_se",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
				"description": "Material Transfer From Reserve SE, created asynchronously after submit.",
			},
			{
				"fieldname": "custom_transfer_se_state",
				"fieldtype": "Select",
				"options": "\nPending\nDone\nFailed",
				"label": "Transfer SE State",
				"insert_after": "custom_transfer_se",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_transfer_se_error",
				"fieldtype": "Small Text",
				"label": "Transfer SE Error",
				"insert_after": "custom_transfer_se_state",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info("add_mr_transfer_se_fields: custom fields created/updated on Material Request")
