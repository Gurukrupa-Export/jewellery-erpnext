# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
#
# Stock Entry Type To Reservation: include Repack and Material Transfer (WORK ORDER)
# so ``stock_reservation_entry_for_mwo`` runs on submit for those voucher types.

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime, nowdate

# Employee IR injection submits Material Transfer (WORK ORDER) and/or Repack Stock Entries.
# Both must appear in Stock Entry Type To Reservation or ``stock_reservation_entry_for_mwo``
# skips that voucher type.
_RESERVATION_TYPES_FOR_EIR = frozenset(
	("Material Transfer (WORK ORDER)", "Material Receive (WORK ORDER)")
)


class MOPSettings(Document):
	def validate(self):
		self._validate_reservation_types()
		self._validate_eod_sync_time_permission()
		self._validate_eod_sync_window()

	def _validate_reservation_types(self):
		rows = self.get("stock_entry_type_to_reservation") or []
		configured = {
			r.stock_entry_type_to_reservation
			for r in rows
			if getattr(r, "stock_entry_type_to_reservation", None)
		}
		if configured and not _RESERVATION_TYPES_FOR_EIR.issubset(configured):
			missing = sorted(_RESERVATION_TYPES_FOR_EIR - configured)
			frappe.msgprint(
				_(
					"Stock Entry Type To Reservation is missing: {0}. "
					"Employee IR extra-metal reservation runs on submit only for types listed here; "
					"add Repack and Material Transfer (WORK ORDER) so both MT and Repack vouchers reserve."
				).format(", ".join(missing)),
				title=_("Reservation coverage"),
				indicator="orange",
			)

	def _validate_eod_sync_time_permission(self):
		old = self.get_doc_before_save()
		if not old:
			return
		old_time = getattr(old, "eod_sync_time", None)
		new_time = self.eod_sync_time
		if old_time == new_time:
			return
		if "System Manager" not in frappe.get_roles():
			frappe.throw(_("Only System Manager can change EOD Sync Time."))
		self.add_comment(
			"Edit",
			_("EOD Sync Time changed from {0} to {1}.").format(
				frappe.bold(old_time or "(none)"), frappe.bold(new_time or "(none)")
			),
		)

	def _validate_eod_sync_window(self):
		"""Ensure the manual EOD Sync From/To window is ordered when both are set."""
		from_dt = self.eod_sync_from_datetime
		to_dt = self.eod_sync_to_datetime
		if from_dt and to_dt and get_datetime(from_dt) >= get_datetime(to_dt):
			frappe.throw(
				_("EOD Sync From ({0}) must be earlier than EOD Sync To ({1}).").format(
					frappe.bold(from_dt), frappe.bold(to_dt)
				)
			)

	@frappe.whitelist()
	def sync_mop_log(self):
		"""Enqueue EOD MOP Log sync as a background job (System Manager only)."""
		from .eod_lock import is_eod_sync_locked, set_eod_sync_queued

		if "System Manager" not in frappe.get_roles():
			frappe.throw(_("Only System Manager can manually start EOD Sync."))

		if is_eod_sync_locked():
			frappe.throw(
				_(
					"EOD sync is in progress. You cannot proceed to make any transactions. "
					"Please contact your administrator or try again after 2 hours after the specified time."
				),
				title=_("EOD Sync In Progress"),
			)

		# Resolve the From/To window for this manual run. Blank fields fall back to
		# today's start/end; the scheduler never sets these, so only manual runs can
		# scan a custom window.
		today = nowdate()
		from_datetime = self.eod_sync_from_datetime or f"{today} 00:00:00"
		to_datetime = self.eod_sync_to_datetime or f"{today} 23:59:59"

		# Create a MOP EOD Sync Log to track this run
		sync_log = frappe.new_doc("MOP EOD Sync Log")
		sync_log.status = "Queued"
		sync_log.trigger_type = "Manual"
		sync_log.started_by = frappe.session.user
		sync_log.posting_date = nowdate()
		sync_log.eod_sync_time = self.eod_sync_time
		sync_log.mop_settings = "MOP Settings"
		sync_log.flags.ignore_permissions = True
		sync_log.insert()
		sync_log_name = sync_log.name

		# Update MOP Settings last sync log link
		frappe.db.set_value(
			"MOP Settings",
			"MOP Settings",
			"eod_sync_last_sync_log",
			sync_log_name,
		)

		set_eod_sync_queued(sync_log_name=sync_log_name)
		frappe.enqueue(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.sync_mop_logs",
			queue="long",
			timeout=7200,
			enqueue_after_commit=True,
			sync_log_name=sync_log_name,
			from_datetime=from_datetime,
			to_datetime=to_datetime,
		)
		frappe.msgprint(
			_(
				"EOD MOP Log Sync has been queued. Open {0} to track progress. "
				"Transactions will be blocked while the sync is running."
			).format(frappe.utils.get_link_to_form("MOP EOD Sync Log", sync_log_name)),
			alert=True,
		)
