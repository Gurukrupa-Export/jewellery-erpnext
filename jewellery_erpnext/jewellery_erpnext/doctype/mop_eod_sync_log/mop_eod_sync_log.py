# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MOPEODSyncLog(Document):
	pass


@frappe.whitelist()
def get_latest_eod_sync_progress(sync_log_name=None):
	"""Return progress dict for the given (or latest) MOP EOD Sync Log.

	Does NOT expose technical tracebacks — those stay inside child rows and Error Log.
	"""
	frappe.has_permission("MOP EOD Sync Log", "read", throw=True)
	if not sync_log_name:
		sync_log_name = frappe.db.get_value(
			"MOP EOD Sync Log",
			filters={},
			fieldname="name",
			order_by="creation desc",
		)
	if not sync_log_name:
		return {}

	row = frappe.db.get_value(
		"MOP EOD Sync Log",
		sync_log_name,
		[
			"status",
			"total_qty",
			"eligible_qty",
			"synced_qty",
			"unsynced_qty",
			"failed_qty",
			"skipped_qty",
			"excluded_qty",
			"progress_percent",
			"progress_message",
			"total_mwos",
			"processed_mwos",
			"synced_mwos",
			"failed_mwos",
			"total_items",
			"synced_items",
			"failed_items",
			"excluded_items",
			"submitted_stock_entries",
			"draft_stock_entries",
			"started_on",
			"completed_on",
			"trigger_type",
		],
		as_dict=True,
	)
	if not row:
		return {}

	row["sync_log"] = sync_log_name
	return row
