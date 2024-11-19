from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events.utils import (
	update_inventory_dimentions,
	update_pure_qty,
)


def validate(self, method):
	update_pure_qty(self)
	update_inventory_dimentions(self)
