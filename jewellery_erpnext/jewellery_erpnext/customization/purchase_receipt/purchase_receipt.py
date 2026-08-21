from jewellery_erpnext.jewellery_erpnext.customization.purchase_receipt.doc_events.utils import (
	update_bundle_details,
	update_customer,
	update_inventory_type,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice import (
	sync_tax_row_rate_with_item,
)


def before_validate(self, method):
	update_customer(self)
	update_inventory_type(self)
	if self.purchase_type == "Branch Purchase":
		remove_serial_and_batch_bundle(self)


def validate(self, method=None):
	sync_tax_row_rate_with_item(self)


def on_submit(self, method):
	update_bundle_details(self)


def remove_serial_and_batch_bundle(self):
	if self.items:
		for i in self.items:
			i.serial_and_batch_bundle = ""
			i.use_serial_batch_fields = 0
			i.is_internal_supplier = 0
