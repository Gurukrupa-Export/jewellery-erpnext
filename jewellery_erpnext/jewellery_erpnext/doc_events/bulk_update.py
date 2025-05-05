import frappe
from frappe import _
from frappe.desk.doctype.bulk_update.bulk_update import _bulk_action



@frappe.whitelist()
def custom_submit_cancel_or_update_docs(doctype, docnames, action="submit", data=None, task_id=None):
	if isinstance(docnames, str):
		docnames = frappe.parse_json(docnames)

	if len(docnames) < 50:
		return _bulk_action(doctype, docnames, action, data, task_id)
	elif len(docnames) <= 2500:
		frappe.msgprint(_("Bulk operation is enqueued in background."), alert=True)
		frappe.enqueue(
			_bulk_action,
			doctype=doctype,
			docnames=docnames,
			action=action,
			data=data,
			task_id=task_id,
			queue="long",
			timeout=4000,
		)
	else:
		frappe.throw(_("Bulk operations only support up to 2500 documents."), title=_("Too Many Documents"))