"""Rename the Diamond Conversion ``conversion_type`` option "Sieve Size to Sieve Size Range".

WHY THIS EXISTS
---------------
The Select option was renamed to "Sieve Size to Sieve Size" in ``diamond_conversion.json``, and
that option now carries a real rule: source and target are both read through the
``Diamond Sieve Size Range`` item attribute and the target's sieve must sit inside the source's
UP/DOWN band (``diamond_conversion.validate_sieve_size_band``).

The JSON edit changes the option LIST, not the documents already holding the old string. Frappe
validates Select values server-side in ``Document._validate_selects``, so a document left on the
retired value fails any later re-save with a confusing "not a valid value" error. Six submitted
documents hold it on the live bench (DCON00056, DCON00063, DCON00079, DCON00083, DCON00084,
DCON00086).

WHY RAW SQL
-----------
All six are ``docstatus = 1``. Loading and saving a submitted document is not possible, and this
is a pure data repair -- ``modified`` / ``modified_by`` are deliberately left untouched and no
Version row is written, so the logger line below is the only audit trail.

A raw UPDATE bypasses ``frappe.db.set_value``, which is what normally evicts the redis document
cache, so ``clear_document_cache`` is called per affected document. Idempotent: the UPDATE is a
no-op once no row holds the old value.

NO PROPERTY SETTER
------------------
Unlike ``add_order_type_repair_option``, ``Diamond Conversion.conversion_type`` is NOT shadowed by
a Property Setter (the doctype has only ``autoname`` / ``naming_rule`` setters), so the doctype
JSON is the live option list and ``bench migrate`` syncs it. Writing one here would clobber any
option a site had added through Customize Form.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.rename_diamond_conversion_sieve_type.execute
"""

import frappe

DOCTYPE = "Diamond Conversion"
OLD_VALUE = "Sieve Size to Sieve Size Range"
NEW_VALUE = "Sieve Size to Sieve Size"


def execute():
	affected = frappe.db.get_all(
		DOCTYPE, filters={"conversion_type": OLD_VALUE}, pluck="name"
	)
	if not affected:
		return

	conversion = frappe.qb.DocType(DOCTYPE)
	(
		frappe.qb.update(conversion)
		.set(conversion.conversion_type, NEW_VALUE)
		.where(conversion.conversion_type == OLD_VALUE)
	).run()

	for name in affected:
		frappe.clear_document_cache(DOCTYPE, name)

	frappe.db.commit()
	frappe.logger().info(
		f"rename_diamond_conversion_sieve_type: {OLD_VALUE!r} -> {NEW_VALUE!r} on "
		f"{len(affected)} {DOCTYPE} document(s): " + ", ".join(sorted(affected))
	)
