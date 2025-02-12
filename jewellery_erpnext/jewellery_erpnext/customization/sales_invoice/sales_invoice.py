from jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.doc_events.utils import (
	create_branch_po,
	validate_item_category_for_customer,
)


def before_validate(self, method):
	validate_item_category_for_customer(self)


def on_submit(self, method):
	create_branch_po(self)
