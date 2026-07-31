"""Refresh the METADATA that carries the renamed refining vocabulary.

Paired with ``rename_refining_scrap_terminology_data``, which migrates the stored rows;
this patch only moves labels and Select options, so it is naturally idempotent and safe to
re-run.

Three jobs:

1. Re-invoke the two Custom Field patches whose labels/options changed
   (``add_batch_scrap_type_field``, ``add_customer_refining_flags``). Their own
   ``patches.txt`` entries are long since marked complete, so a NEW entry is the only way
   to make ``create_custom_fields(update=True)`` refresh them. Keeping the field
   definitions in those patches means there is still exactly ONE definition of each.

2. Delete any Property Setter shadowing the three renamed Select fields. Property Setters
   win over the DocField during meta resolution, so a stale one (left by Customize Form)
   would silently pin the OLD options and make every save fail Frappe's Select validation.

3. Clear the doctype caches so the new options are visible immediately.

Also called from ``create_test_data.setup_data``: CI re-imports ``Batch.custom_batch_type``
from an external fixture branch that still carries the old ``\\nScrap`` options, which would
make every Receive Unused/Loose Material test fail Select validation. ``create_test_data``
runs last, so it wins.

    bench --site <site> execute jewellery_erpnext.patches.rename_refining_scrap_terminology_metadata.execute
"""

import frappe

from jewellery_erpnext.patches.add_batch_scrap_type_field import (
	execute as ensure_batch_type_field,
)
from jewellery_erpnext.patches.add_customer_refining_flags import (
	execute as ensure_customer_refining_flags,
)

#: (doctype, fieldname) pairs whose Select options were renamed.
RENAMED_SELECTS = (
	("Refining Entry", "refining_type"),
	("Refining Material Line", "source_type"),
	("Batch", "custom_batch_type"),
)


def execute():
	ensure_batch_type_field()
	ensure_customer_refining_flags()

	for doctype, fieldname in RENAMED_SELECTS:
		if not frappe.db.exists("DocType", doctype):
			continue
		stale = frappe.db.count(
			"Property Setter",
			{
				"doc_type": doctype,
				"field_name": fieldname,
				"property": ["in", ["options", "default"]],
			},
		)
		if stale:
			frappe.db.delete(
				"Property Setter",
				{
					"doc_type": doctype,
					"field_name": fieldname,
					"property": ["in", ["options", "default"]],
				},
			)
			# Logged rather than silent: a non-zero count means someone used Customize
			# Form on a renamed field and the options they pinned have just been dropped.
			frappe.logger().warning(
				f"rename_refining_scrap_terminology_metadata: removed {stale} shadowing "
				f"Property Setter(s) on {doctype}.{fieldname}"
			)
		frappe.clear_cache(doctype=doctype)
