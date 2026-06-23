"""
Add audit custom fields to Stock Entry for EOD sync tracking.

These are audit-only markers. The actual EOD sync bypass is controlled by
frappe.flags.in_eod_mop_sync (server-side only). These fields exist for
visibility and reporting inside the MOP EOD Sync Log.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "custom_is_eod_sync_stock_entry",
				"fieldtype": "Check",
				"label": "Is EOD Sync Stock Entry",
				"insert_after": "auto_created",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
				"description": "Set to 1 when this Stock Entry was created by the MOP EOD Sync process.",
			},
			{
				"fieldname": "custom_eod_sync_log",
				"fieldtype": "Link",
				"options": "MOP EOD Sync Log",
				"label": "EOD Sync Log",
				"insert_after": "custom_is_eod_sync_stock_entry",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
				"description": "Link to the MOP EOD Sync Log that created this Stock Entry.",
			},
			{
				"fieldname": "custom_eod_sync_source",
				"fieldtype": "Data",
				"label": "EOD Sync Source",
				"insert_after": "custom_eod_sync_log",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
				"description": "Source identifier for EOD-created Stock Entries (e.g., 'MOP EOD Sync').",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_eod_se_custom_fields: custom fields created/updated on Stock Entry"
	)
