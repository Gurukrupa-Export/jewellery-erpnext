"""Rename the REF-MD-001 pricing category from "Main Dust" to "Dust Item".

The item CODE is unchanged, so every Refinery Price List, Purchase Order line and Stock
Entry keeps resolving. Only the display name moves, plus the denormalised ``item_name``
that ``fetch_from`` copied onto Refining Material Line rows.

Submitted Purchase Order Item / Stock Entry Detail rows are deliberately LEFT ALONE: they
are the historical record of what was ordered under the old label.

Idempotent: guarded on the name still being "Main Dust".

    bench --site <site> execute jewellery_erpnext.patches.rename_main_dust_to_dust_item.execute
"""

import frappe

ITEM_CODE = "REF-MD-001"
OLD_NAME = "Main Dust"
NEW_NAME = "Dust Item"


def execute():
	if frappe.db.get_value("Item", ITEM_CODE, "item_name") != OLD_NAME:
		return

	frappe.db.set_value("Item", ITEM_CODE, "item_name", NEW_NAME, update_modified=False)
	frappe.db.sql(
		"""
		UPDATE `tabRefining Material Line`
		   SET item_name = %(new)s
		 WHERE item_code = %(code)s AND item_name = %(old)s
		""",
		{"new": NEW_NAME, "code": ITEM_CODE, "old": OLD_NAME},
	)
	frappe.clear_cache(doctype="Item")
	frappe.logger().info(
		f"rename_main_dust_to_dust_item: {ITEM_CODE} renamed to {NEW_NAME!r}"
	)
