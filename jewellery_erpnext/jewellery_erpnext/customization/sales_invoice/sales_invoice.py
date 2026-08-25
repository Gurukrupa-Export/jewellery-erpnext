from jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.doc_events.utils import (
	create_branch_po,
	validate_item_category_for_customer,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_transactions import set_batch_certificate_id


def before_validate(self, method):
	validate_item_category_for_customer(self)


def on_submit(self, method):
	create_branch_po(self)
	if self.is_return:
		set_batch_certificate_id(self)
