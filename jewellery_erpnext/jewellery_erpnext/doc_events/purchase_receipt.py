import frappe
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt as ERPNextPurchaseReceipt
from frappe.utils import cint

class CustomPurchaseReceipt(ERPNextPurchaseReceipt):
    def validate(self):
        pass
