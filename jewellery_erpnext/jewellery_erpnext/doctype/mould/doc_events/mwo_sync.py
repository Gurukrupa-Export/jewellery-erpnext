from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	get_current_mould_no,
)


def sync_mould_no(doc, method=None):
	doc.mould_no = get_current_mould_no(doc.item_code)
