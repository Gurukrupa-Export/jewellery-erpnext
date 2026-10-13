"""Provision ``custom_certificate_id`` on Batch, Purchase Receipt Item and Sales Invoice Item.

``doc_events/stock_transactions.set_batch_certificate_id`` copies a receiving row's
Certificate ID onto the batches of that row's inward bundle when a Purchase Receipt or a
credit note is submitted. It needs the field on the item table (where the user enters it)
and on Batch (where it is kept).

CREATE ONLY, NEVER UPDATE
-------------------------
Some sites already carry these fields, defined by hand in the database with their own label,
position and flags. Those definitions are left exactly as they are. A field is created only
where the site has neither the column nor a Custom Field row for it, and
``create_custom_fields`` is called with ``update=False`` as a second line of defence.

An ORPHANED column -- the column exists but neither a Custom Field nor a DocField defines it,
which is what deleting a Custom Field leaves behind -- is logged and left alone. The hook
still reads and writes such a column, but the form shows no field for it; whether to
re-create the field over the existing data is a decision for whoever owns that site.

Because the after_migrate hook that would sync ``custom_fields/*.json`` is disabled and
``install-app`` marks patches complete WITHOUT running them on fresh / CI sites, this is
wired in the same two idempotent places as its siblings: this ``post_model_sync`` patch and
``create_test_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_batch_certificate_id_fields.execute

Idempotent: a re-run finds nothing missing and writes nothing.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELDNAME = "custom_certificate_id"

_CERTIFICATE_FIELD = {
	"fieldname": FIELDNAME,
	"fieldtype": "Data",
	"label": "Certificate ID",
	"module": "Jewellery Erpnext",
	# Deliberately NOT no_copy: a credit note made from an invoice carries the certificate.
}

CUSTOM_FIELDS = {
	"Batch": [dict(_CERTIFICATE_FIELD, insert_after="reference_name")],
	"Purchase Receipt Item": [dict(_CERTIFICATE_FIELD, insert_after="batch_no")],
	"Sales Invoice Item": [dict(_CERTIFICATE_FIELD, insert_after="batch_no")],
}


def ensure_certificate_id_fields():
	"""Create the fields that are missing everywhere; return the (doctype, fieldname) created."""
	missing = {}
	orphaned = []

	for doctype, fields in CUSTOM_FIELDS.items():
		for df in fields:
			fieldname = df["fieldname"]
			if frappe.db.exists(
				"Custom Field", {"dt": doctype, "fieldname": fieldname}
			):
				continue

			if frappe.db.has_column(doctype, fieldname):
				if not frappe.db.exists(
					"DocField", {"parent": doctype, "fieldname": fieldname}
				):
					orphaned.append(f"{doctype}.{fieldname}")
				continue

			missing.setdefault(doctype, []).append(dict(df))

	if orphaned:
		message = (
			"add_batch_certificate_id_fields: column exists with no Custom Field or "
			"DocField, left as is (no field created): " + ", ".join(orphaned)
		)
		# print is what shows in migrate / create_test_data output; ERROR is the lowest
		# level Frappe's file logger keeps outside the dev server.
		frappe.logger().error(message)
		print(message)

	if not missing:
		return []

	create_custom_fields(missing, ignore_validate=True, update=False)

	created = [
		(doctype, df["fieldname"])
		for doctype, fields in missing.items()
		for df in fields
	]
	print(
		"add_batch_certificate_id_fields: created "
		+ ", ".join(f"{doctype}.{fieldname}" for doctype, fieldname in created)
	)
	return created


def execute():
	ensure_certificate_id_fields()
	frappe.db.commit()
