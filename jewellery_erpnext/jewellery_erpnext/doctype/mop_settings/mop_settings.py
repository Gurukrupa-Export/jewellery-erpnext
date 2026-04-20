# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MOPSettings(Document):
	@frappe.whitelist()
	def sync_mop_log(self):
		"""Enqueue EOD MOP Log sync as a background job."""
		frappe.enqueue(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.sync_mop_logs",
			queue="long",
			timeout=3600,
		)
		frappe.msgprint(
			"MOP Log sync has been queued. You will be notified when it completes.",
			alert=True,
		)
