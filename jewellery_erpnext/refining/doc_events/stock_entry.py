"""Stock Entry hooks for the Refining Entry's draft hand-back to Central.

Scrap and Unused/Loose Material refining hand the refined material back as a DRAFT
transfer into Central Transit (``RefiningEntry._create_central_transit_draft``), linked as
the entry's ``transfer_se``. The stock moves only when Central submits that draft, so the
entry's status follows the draft: Transferred on submit, back to Completed on cancel. If
Central deletes the draft instead, the link is cleared so the entry can create it again.
"""

import frappe


def _handed_back_entry(doc):
	"""The Refining Entry whose ``transfer_se`` is ``doc``, or None."""
	refining_entry = doc.get("custom_refining_entry")
	if not refining_entry:
		return None
	if frappe.db.get_value("Refining Entry", refining_entry, "transfer_se") != doc.name:
		return None
	return refining_entry


def on_submit(doc, method=None):
	refining_entry = _handed_back_entry(doc)
	if refining_entry:
		frappe.db.set_value("Refining Entry", refining_entry, "status", "Transferred")


def on_cancel(doc, method=None):
	refining_entry = _handed_back_entry(doc)
	if not refining_entry:
		return
	# A submitted (external) Refining Entry links this entry as its transfer_se, which would
	# otherwise block Central from cancelling it. Set here, not before_cancel: ERPNext's
	# StockEntry.on_cancel assigns ignore_linked_doctypes outright, and the back-link check
	# reads it only after every on_cancel hook (see release_material_request_links).
	doc.ignore_linked_doctypes = (
		*(doc.get("ignore_linked_doctypes") or ()),
		"Refining Entry",
	)
	# Not while the Refining Entry is itself being cancelled (its on_cancel cancels this
	# entry, by which point frappe has already stored docstatus 2 on it).
	state = frappe.db.get_value(
		"Refining Entry", refining_entry, ["docstatus", "status"], as_dict=True
	)
	if state and state.docstatus != 2 and state.status == "Transferred":
		# Back to Completed with the link released, so the entry can hand back again.
		frappe.db.set_value(
			"Refining Entry",
			refining_entry,
			{"status": "Completed", "transfer_se": None},
		)


def on_trash(doc, method=None):
	if doc.docstatus == 0 and _handed_back_entry(doc):
		frappe.db.set_value(
			"Refining Entry", doc.custom_refining_entry, "transfer_se", None
		)
