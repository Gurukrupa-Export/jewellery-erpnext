from erpnext.stock.doctype.stock_ledger_entry.stock_ledger_entry import StockLedgerEntry

from jewellery_erpnext.jewellery_erpnext.customization.stock_ledger_entry.doc_events.utils import (
	custom_on_submit,
)


class CustomStockLedgerEntry(StockLedgerEntry):
	def on_submit(self):
		custom_on_submit(self)
