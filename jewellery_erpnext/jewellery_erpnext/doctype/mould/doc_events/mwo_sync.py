from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	get_current_mould_id,
)


def sync_mould_id(doc, method=None):
	doc.mould_id = get_current_mould_id(doc.item_code)
