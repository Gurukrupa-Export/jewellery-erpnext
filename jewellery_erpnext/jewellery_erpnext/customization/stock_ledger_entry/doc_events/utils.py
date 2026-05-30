import frappe

from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events.utils import (
	CustomSerialBatchBundle,
)


def custom_on_submit(self):
	self.set_posting_datetime()
	self.check_stock_frozen_date()

	# Added to handle few test cases where serial_and_batch_bundles are not required
	if frappe.flags.in_test and frappe.flags.ignore_serial_batch_bundle_validation:
		return

	if not self.get("via_landed_cost_voucher"):
		CustomSerialBatchBundle(
			sle=self,
			item_code=self.item_code,
			warehouse=self.warehouse,
			company=self.company,
		)

	# Bypass validation for Product Certification flow to avoid batch/serial errors
	if self.voucher_type == "Stock Entry" and self.voucher_no:
		if frappe.db.get_value("Stock Entry", self.voucher_no, "product_certification"):
			return

	self.validate_serial_batch_no_bundle()
