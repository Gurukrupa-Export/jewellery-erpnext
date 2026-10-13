"""Provision ``custom_repack_stock_entry`` on Journal Entry for the Batch form Repack.

``customization/batch/repack.py`` posts a Journal Entry that moves a Repack Stock Entry's
net Stock Adjustment into Stock Received But Not Billed. The Journal Entry records that
Stock Entry in this field, and cancelling the Stock Entry cancels the Journal Entry in the
same transaction (``repack.cancel_linked_journal_entries``, a Stock Entry ``on_cancel``
hook), so a Repack is reversed as one unit. The tool refuses to run on a site where the
field is missing.

CREATE ONLY, NEVER UPDATE
-------------------------
The field is created only where Journal Entry has neither the column nor a Custom Field
row for it, and ``create_custom_fields`` is called with ``update=False``. An ORPHANED
column (the column exists but neither a Custom Field nor a DocField defines it) is logged
and left alone.

Because the after_migrate hook that would sync ``custom_fields/*.json`` is disabled and
``install-app`` marks patches complete WITHOUT running them on fresh / CI sites, this is
wired in the same two idempotent places as its siblings: this ``post_model_sync`` patch and
``create_test_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_journal_entry_repack_stock_entry_field.execute

Idempotent: a re-run finds the field and writes nothing.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Journal Entry"
FIELDNAME = "custom_repack_stock_entry"

CUSTOM_FIELDS = {
	DOCTYPE: [
		{
			"fieldname": FIELDNAME,
			"fieldtype": "Link",
			"options": "Stock Entry",
			"label": "Repack Stock Entry",
			"insert_after": "user_remark",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			# The Stock Entry cancel looks Journal Entries up by this field.
			"search_index": 1,
			"description": "The Batch form Repack Stock Entry this Journal Entry belongs "
			"to. Cancelling that Stock Entry cancels this Journal Entry.",
			"module": "Jewellery Erpnext",
		}
	]
}


def ensure_repack_stock_entry_field():
	"""Create the field when it is missing everywhere; return True when it was created."""
	if frappe.db.exists("Custom Field", {"dt": DOCTYPE, "fieldname": FIELDNAME}):
		return False

	if frappe.db.has_column(DOCTYPE, FIELDNAME):
		if not frappe.db.exists(
			"DocField", {"parent": DOCTYPE, "fieldname": FIELDNAME}
		):
			message = (
				"add_journal_entry_repack_stock_entry_field: column exists with no Custom "
				f"Field or DocField, left as is (no field created): {DOCTYPE}.{FIELDNAME}"
			)
			# print is what shows in migrate / create_test_data output; ERROR is the
			# lowest level Frappe's file logger keeps outside the dev server.
			frappe.logger().error(message)
			print(message)
		return False

	create_custom_fields(
		{DOCTYPE: [dict(df) for df in CUSTOM_FIELDS[DOCTYPE]]},
		ignore_validate=True,
		update=False,
	)
	print(f"add_journal_entry_repack_stock_entry_field: created {DOCTYPE}.{FIELDNAME}")
	return True


def execute():
	ensure_repack_stock_entry_field()
	frappe.db.commit()
