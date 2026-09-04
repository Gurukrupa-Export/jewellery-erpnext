import frappe
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
	PurchaseReceipt as ERPNextPurchaseReceipt,
)

from jewellery_erpnext.jewellery_erpnext.customization.utils.internal_transfer import (
	fill_purchase_invoice_references,
)


class CustomPurchaseReceipt(ERPNextPurchaseReceipt):
	def validate(self):
		pass


@frappe.whitelist()
def make_purchase_invoice(source_name, target_doc=None, args=None):
	"""Override of erpnext...purchase_receipt.make_purchase_invoice (registered in
	hooks.py's override_whitelisted_methods) -- the Purchase Receipt's own "Create ->
	Purchase Invoice" button uses a different core function than the Purchase Order's
	(erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice, not
	erpnext.buying...purchase_order.make_purchase_invoice), so it needs its own override
	even though the fix is identical. Core's PR -> Purchase Invoice mapper carries
	`purchase_order_item` forward as `po_detail` on each row, which is exactly what
	fill_purchase_invoice_references() (shared with the Purchase Order override in
	doc_events/purchase_order.py) needs to resolve the matching Sales Invoice Item.
	"""
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		make_purchase_invoice as _core_make_purchase_invoice,
	)

	doc = _core_make_purchase_invoice(source_name, target_doc, args)

	if doc.get("is_return"):
		return doc
	if not (doc.get("is_internal_supplier") and doc.represents_company == doc.company):
		return doc

	fill_purchase_invoice_references(doc)
	return doc
