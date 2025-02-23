from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.utils import (
	validate_po,
	update_si,
)
from jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.doc_events.utils import (
	validate_item_category_for_customer,
)


def before_validate(self, method):
	validate_po(self)
	validate_item_category_for_customer(self)
	# update_si(self)
