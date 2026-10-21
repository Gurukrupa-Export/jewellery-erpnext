"""Clear Customer Voucher Type values that are not one of the three legal options.

WHAT WENT WRONG
---------------
``Batch.custom_customer_voucher_type`` is a Select with exactly three options (see
``custom_fields/batch.json``). A UAT site was carrying the literal two-character value
``''`` in that chain, and every Serial Number Creator and Metal Conversion submit died
with::

    Customer Voucher Type cannot be "''". It should be one of "", "Customer Sample
    Goods", "Customer Subcontracting", "Customer Repair"

raised from ``Batch._validate_selects`` -- NOT from the document the operator was
submitting. The path is indirect, which is why the message points nowhere useful:

1. ``serial_and_batch_bundle.after_insert`` -> ``update_parent_batch_id`` re-saves the
   PRODUCED batch on every Manufacture / Repack submit, purely to append provenance rows.
2. That save re-runs ``Batch.validate`` -> ``update_inventory_dimentions``, which copies
   the voucher type from the Stock Entry header, or from the batch this one was made from.
3. The copy was unvalidated, so an illegal value propagated into an option-checked field
   and aborted the whole stock movement.

Step 3 is fixed in code (``batch/doc_events/utils.py::_valid_voucher_type``), which stops
the spread and heals a poisoned batch on its next save. This patch removes the values that
are already stored, so the repair does not have to wait for each row to be touched again.

WHY BOTH TABLES, AND THE DEFAULT
--------------------------------
``Stock Entry.customer_voucher_type`` carries the same three options and is the field the
Batch copies FROM. A junk value there also makes the Stock Entry itself un-cancellable and
un-amendable, because ``_validate_selects`` fires on every subsequent save of the voucher.

An illegal Customize Form ``default`` is cleared too: it would re-seed the junk onto every
newly created document, so cleaning only the rows would let the problem come straight back.

Nulling rather than guessing is deliberate -- a value outside the option list carries no
meaning, and every consumer already treats an unset voucher type as "not marked".

Idempotent; a no-op on a clean site. Re-run ad-hoc with::

    bench --site <site> execute jewellery_erpnext.patches.sanitize_customer_voucher_type.execute
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events.utils import (
	CUSTOMER_VOUCHER_TYPES,
)

# (doctype, fieldname) of every field that holds a Customer Voucher Type.
VOUCHER_TYPE_FIELDS = (
	("Batch", "custom_customer_voucher_type"),
	("Stock Entry", "customer_voucher_type"),
)


def execute():
	for doctype, fieldname in VOUCHER_TYPE_FIELDS:
		_clear_bad_default(doctype, fieldname)
		_clear_bad_values(doctype, fieldname)

	frappe.db.commit()


def _clear_bad_values(doctype, fieldname):
	# The app's custom_fields/*.json are not applied by migrate (the patch-only gap
	# documented in batch/doc_events/utils.py::_row_value), so the column can be absent.
	if not frappe.db.has_column(doctype, fieldname):
		return

	placeholders = ", ".join(["%s"] * len(CUSTOMER_VOUCHER_TYPES))
	condition = (
		f"ifnull(`{fieldname}`, '') != '' and `{fieldname}` not in ({placeholders})"
	)

	affected = frappe.db.sql(
		f"select count(*) from `tab{doctype}` where {condition}",
		CUSTOMER_VOUCHER_TYPES,
	)[0][0]

	if not affected:
		return

	frappe.db.sql(
		f"update `tab{doctype}` set `{fieldname}` = NULL where {condition}",
		CUSTOMER_VOUCHER_TYPES,
	)

	frappe.logger().info(
		f"sanitize_customer_voucher_type: cleared {affected} {doctype}.{fieldname} value(s)"
	)


def _clear_bad_default(doctype, fieldname):
	"""Drop a Custom Field / Property Setter ``default`` outside the option list.

	Both layers are checked because ``frappe.get_meta`` applies a Property Setter OVER the
	Custom Field, so either one alone can seed the value onto new documents.
	"""
	custom_field = frappe.db.get_value(
		"Custom Field", {"dt": doctype, "fieldname": fieldname}, ["name", "default"]
	)
	if (
		custom_field
		and custom_field[1]
		and custom_field[1] not in CUSTOMER_VOUCHER_TYPES
	):
		frappe.db.set_value("Custom Field", custom_field[0], "default", None)
		frappe.clear_cache(doctype=doctype)
		frappe.logger().info(
			f"sanitize_customer_voucher_type: cleared default "
			f"{custom_field[1]!r} on {doctype}.{fieldname}"
		)

	for name, value in frappe.db.get_all(
		"Property Setter",
		filters={"doc_type": doctype, "field_name": fieldname, "property": "default"},
		fields=["name", "value"],
		as_list=True,
	):
		if value and value not in CUSTOMER_VOUCHER_TYPES:
			frappe.delete_doc(
				"Property Setter", name, ignore_permissions=True, force=True
			)
			frappe.clear_cache(doctype=doctype)
			frappe.logger().info(
				f"sanitize_customer_voucher_type: removed default Property Setter "
				f"{value!r} on {doctype}.{fieldname}"
			)
