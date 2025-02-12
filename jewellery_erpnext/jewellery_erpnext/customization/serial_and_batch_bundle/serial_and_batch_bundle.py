from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events.utils import (
	update_parent_batch_id,
)


def after_insert(self, method):
	update_parent_batch_id(self)
