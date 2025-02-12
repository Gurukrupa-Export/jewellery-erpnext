import frappe

from jewellery_erpnext.jewellery_erpnext.customization.purchase_receipt.doc_events.utils import (
	update_bundle_details,
	update_customer,
	update_inventory_type,
)


def before_validate(self, method):
	update_customer(self)
	update_inventory_type(self)


def on_submit(self, method):
	update_bundle_details(self)
