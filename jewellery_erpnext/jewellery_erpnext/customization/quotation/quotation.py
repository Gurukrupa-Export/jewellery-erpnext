from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.utils import (
	validate_po,
)


def before_validate(self, method):
	validate_po(self)
