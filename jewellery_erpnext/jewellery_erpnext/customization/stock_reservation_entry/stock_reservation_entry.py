from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
)
import frappe
from frappe import _
from frappe.utils import cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_mop_link import (
	bold,
	owner_of,
	report,
	same,
)


class CustomStockReservationEntry(StockReservationEntry):
	def validate(self) -> None:
		super().validate()
		self.validate_work_order_owns_operation()

	def is_row_of_voucher(self) -> bool:
		"""A work-order reservation names its Sales Order LINE, not its item.

		ERPNext 16.52 (frappe/erpnext#60428) refuses a Sales Order reservation unless the
		reserved item is the line's own item. A Manufacturing Work Order reserves the RAW
		MATERIAL -- metal, diamonds, findings -- that goes into the line's finished piece,
		so its item is never on the line and every work-order transfer failed with "Item ...
		is not a row of Sales Order ...". For those reservations the line itself is what has
		to exist on the Sales Order. Every other reservation keeps ERPNext's own rule.
		"""
		if self.get("manufacturing_work_order") and self.voucher_type == "Sales Order":
			return bool(
				self.voucher_detail_no
				and frappe.db.exists(
					"Sales Order Item",
					{"name": self.voucher_detail_no, "parent": self.voucher_no},
				)
			)
		return super().is_row_of_voucher()

	def validate_work_order_owns_operation(self) -> None:
		"""A work-order reservation must be keyed to the work order that OWNS its operation.

		Make Receive Entry lists reservations by ``manufacturing_work_order`` alone, so an entry keyed
		(sibling work order, this operation) hands one work order's stock to the other -- which is how two
		Make Receive Entries consumed a sibling's reservation (see ``doc_events/mwo_mop_link``). Core
		ERPNext reservations carry neither field and are untouched, and an operation that cannot be
		resolved is left alone (both fields are plain Data). Runs on insert, save and submit; warn mode
		(``mwo_mop_link_guard``) logs instead of raising.
		"""
		mop = cstr(self.get("manufacturing_operation")).strip()
		mwo = cstr(self.get("manufacturing_work_order")).strip()
		if not (mop and mwo):
			return
		owner = owner_of(mop)
		if owner and not same(owner, mwo):
			report(
				[
					_(
						"Operation {0} belongs to work order {1}, not {2}: a reservation must be keyed to "
						"the work order that owns its operation."
					).format(bold(mop), bold(owner), bold(mwo))
				],
				doc=self,
			)

	def auto_reserve_serial_and_batch(self, based_on: str | None = None) -> None:
		# MWO + MOP flow pre-populates sb_entries with the exact operator-picked
		# batch in stock_reservation_entry_for_mwo (doc_events/stock_entry.py).
		# ERPNext's auto-pick would clear and re-FIFO those rows, breaking
		# per-operation batch lineage downstream in MOP Log / EOD sync.
		if (
			self.get("manufacturing_work_order")
			and self.get("manufacturing_operation")
			and self.get("sb_entries")
			and self.get("reservation_based_on")
			and self.get("reservation_based_on") == "Serial and Batch"
		):
			return

		super().auto_reserve_serial_and_batch(based_on)
