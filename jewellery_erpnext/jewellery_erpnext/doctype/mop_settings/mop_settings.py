# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
#
# Stock Entry Type To Reservation: include Repack and Material Transfer (WORK ORDER)
# so ``stock_reservation_entry_for_mwo`` runs on submit for those voucher types.

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, time_diff_in_hours

_STALE_LOCK_HOURS = 4  # auto-clear sync_running if set longer than this

# Employee IR injection submits Material Transfer (WORK ORDER) and/or Repack Stock Entries.
# Both must appear in Stock Entry Type To Reservation or ``stock_reservation_entry_for_mwo``
# skips that voucher type.
_RESERVATION_TYPES_FOR_EIR = frozenset(
	("Repack", "Material Transfer (WORK ORDER)", "Material Receive (WORK ORDER)")
)


_SYNC_TIME_ROLE = "System Manager"  # only this role may change sync_time


class MOPSettings(Document):
	def validate(self):
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
		self._validate_sync_time_change()

	def _validate_sync_time_change(self):
		"""Block non-System-Manager users from modifying sync_time; log every change."""
		old_sync_time = frappe.db.get_single_value("MOP Settings", "sync_time")
		new_sync_time = self.sync_time
		if old_sync_time == new_sync_time:
			return

		if _SYNC_TIME_ROLE not in frappe.get_roles(frappe.session.user):
			frappe.throw(
				_(
					"Only users with the role {0} are allowed to modify the Scheduled Sync Time."
				).format(frappe.bold(_SYNC_TIME_ROLE)),
				title=_("Insufficient Permission"),
			)

		frappe.log_error(
			title="MOP Settings sync_time changed",
			message=(
				f"sync_time changed by {frappe.session.user} "
				f"from '{old_sync_time}' to '{new_sync_time}' "
				f"at {frappe.utils.now()}."
			),
		)

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


def assert_sync_not_running():
	"""Raise ValidationError if EOD MOP Log Sync is currently active.

	The EOD sync sets frappe.flags.mop_sync_in_progress in its own worker thread
	so its own SE/MOP Log creates are never blocked by this guard.

	Stale locks (running for more than _STALE_LOCK_HOURS) are auto-cleared with a warning.
	Called from MOP Log before_insert and Stock Entry before_submit.
	"""
	if frappe.flags.get("mop_sync_in_progress"):
		return  # EOD sync's own creates are exempt

	sync_running = cint(frappe.db.get_single_value("MOP Settings", "sync_running") or 0)
	if not sync_running:
		return

	sync_started_at = frappe.db.get_single_value("MOP Settings", "sync_started_at")
	if sync_started_at:
		elapsed = flt(time_diff_in_hours(frappe.utils.now(), sync_started_at))
		if elapsed > _STALE_LOCK_HOURS:
			frappe.db.set_value(
				"MOP Settings",
				"MOP Settings",
				{"sync_running": 0, "sync_started_at": None},
				update_modified=False,
			)
			frappe.log_error(
				title="MOP Sync stale lock auto-cleared",
				message=(
					f"sync_running was 1 for {elapsed:.1f} hours (started {sync_started_at}). "
					"Auto-cleared. Please verify EOD sync completed correctly."
				),
			)
			return

	frappe.throw(
		_(
			"MOP Log Sync is currently running. "
			"Stock transactions and MOP Log writes are blocked until sync completes. "
			"If sync appears stuck, contact System Manager to clear the lock."
		),
		title=_("Sync In Progress"),
	)
