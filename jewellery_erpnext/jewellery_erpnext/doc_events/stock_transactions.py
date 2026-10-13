"""Copy a receiving row's Certificate ID onto the batches that row brought in.

A certificate (lab report, assay or hallmark reference) is entered on the receiving row --
``Purchase Receipt Item.custom_certificate_id`` or, for a credit note that takes stock back,
``Sales Invoice Item.custom_certificate_id`` -- and belongs to the physical batch, not to the
voucher. So on submit, every batch in that row's own submitted INWARD Serial and Batch Bundle
gets ``Batch.custom_certificate_id`` set to the row's value.

Wired as an extra ``on_submit`` doc_event on Purchase Receipt and Sales Invoice (see
``hooks.py``), after the existing entries. The controllers themselves are not touched.

WHY THE BUNDLE IS RE-READ FROM THE DATABASE
-------------------------------------------
v16 creates most bundles during stock-ledger posting, inside the controller's own
``on_submit``, and writes the bundle name onto the row with ``db_set`` /
``frappe.db.set_value``. The in-memory row this hook receives may therefore still carry no
bundle at all. Only the row's own bundle field(s) are re-read, by row name.

WHAT IS STAMPED, AND WHAT IS NOT
--------------------------------
- Only bundles whose ``type_of_transaction`` is ``Inward`` and whose ``docstatus`` is 1. A
  Purchase Receipt return posts an Outward bundle and stamps nothing; a draft or cancelled
  bundle stamps nothing.
- Both the accepted and the rejected bundle of a Purchase Receipt row, when the child table
  has the field (Sales Invoice Item has no rejected bundle).
- A Sales Invoice only when it is a return (credit note). A regular sale moves stock out.
- Overwrite semantics: a batch that already carries a certificate gets the row's value.
- Batches are written with ``frappe.db.set_value``: no Batch hooks run, so batch naming,
  rates, inventory dimensions and lineage are untouched. ``Batch.modified`` is bumped.
- Stock Entries are not stamped.

GUARDS
------
Nothing happens unless the column exists on both ``Batch`` and the item table (it is
provisioned by ``patches/add_batch_certificate_id_fields.py``). A row with no certificate
costs nothing beyond those two cached ``has_column`` checks per submit.
"""

import frappe

CERTIFICATE_FIELD = "custom_certificate_id"

#: The row fields that can point at a bundle, in the order they are processed.
BUNDLE_FIELDS = ("serial_and_batch_bundle", "rejected_serial_and_batch_bundle")


def set_batch_certificate_id(doc, method=None):
	"""``on_submit`` doc_event: stamp each row's certificate onto its inward batches."""
	if doc.doctype == "Sales Invoice" and not doc.get("is_return"):
		return

	child_doctype = doc.meta.get_field("items").options
	if not (
		frappe.db.has_column("Batch", CERTIFICATE_FIELD)
		and frappe.db.has_column(child_doctype, CERTIFICATE_FIELD)
	):
		return

	child_meta = frappe.get_meta(child_doctype)
	bundle_fields = [field for field in BUNDLE_FIELDS if child_meta.has_field(field)]
	if not bundle_fields:
		return

	for row in doc.get("items") or []:
		certificate = row.get(CERTIFICATE_FIELD)
		if not certificate:
			continue

		bundles = (
			frappe.db.get_value(child_doctype, row.name, bundle_fields, as_dict=True)
			or {}
		)
		for bundle_field in bundle_fields:
			bundle = bundles.get(bundle_field)
			if not bundle:
				continue

			for batch_no in get_inward_bundle_batches(bundle):
				frappe.db.set_value("Batch", batch_no, CERTIFICATE_FIELD, certificate)


def get_inward_bundle_batches(bundle):
	"""Distinct batch numbers of a submitted Inward bundle, sorted; else an empty list."""
	header = frappe.db.get_value(
		"Serial and Batch Bundle",
		bundle,
		["type_of_transaction", "docstatus"],
		as_dict=True,
	)
	if not header or header.type_of_transaction != "Inward" or header.docstatus != 1:
		return []

	batch_nos = frappe.get_all(
		"Serial and Batch Entry", filters={"parent": bundle}, pluck="batch_no"
	)
	return sorted({batch_no for batch_no in batch_nos if batch_no})
