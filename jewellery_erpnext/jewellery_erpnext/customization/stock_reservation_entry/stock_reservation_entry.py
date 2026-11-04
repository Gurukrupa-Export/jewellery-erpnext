import frappe
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
)


class CustomStockReservationEntry(StockReservationEntry):
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
