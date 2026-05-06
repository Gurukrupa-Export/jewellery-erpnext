from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
)


class CustomStockReservationEntry(StockReservationEntry):
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
