# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt
#
# Stock Entry Type To Reservation: include Repack and Material Transfer (WORK ORDER)
# so ``stock_reservation_entry_for_mwo`` runs on submit for those voucher types.

import frappe
from frappe import _
from frappe.model.document import Document

# Employee IR injection submits Material Transfer (WORK ORDER) and/or Repack Stock Entries.
# Both must appear in Stock Entry Type To Reservation or ``stock_reservation_entry_for_mwo``
# skips that voucher type.
_RESERVATION_TYPES_FOR_EIR = frozenset(
	("Repack", "Material Transfer (WORK ORDER)")
)


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
