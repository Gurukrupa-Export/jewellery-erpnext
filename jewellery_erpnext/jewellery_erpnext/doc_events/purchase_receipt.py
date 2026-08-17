from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
	PurchaseReceipt as ERPNextPurchaseReceipt,
)


class CustomPurchaseReceipt(ERPNextPurchaseReceipt):
	def validate(self):
		pass
