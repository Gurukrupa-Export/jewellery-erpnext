"""Rename the propagated field ``mould_no`` -> ``mould_id`` on the three manufacturing
targets: Manufacturing Plan Table, Parent Manufacturing Order, Manufacturing Work Order.

The field previously held the Mould's ``mould_no`` *location* string (warehouse/rake/
tray/box), which is blank for auto-created Moulds. It now holds the Mould record's
docname -- the "Mould List ID" -- so the field is renamed to avoid clashing with the
Mould doctype's own ``mould_no`` field (which stays, as the location string). This is a
native doctype-JSON field carrying only blank data everywhere, so a plain column rename
is safe and preserves the (empty) values.

Runs in ``[pre_model_sync]`` so the physical column is renamed BEFORE model-sync reads
the new JSON: model-sync then finds ``mould_id`` already present -- no fresh empty column
and no orphaned ``mould_no`` column left behind. No-op on fresh CI (no ``mould_no`` column
exists yet, so model-sync creates ``mould_id`` directly) and idempotent on re-run (guarded
on old-column-present / new-column-absent).

Ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.rename_mould_no_to_mould_id.execute
"""

import frappe

TARGET_DOCTYPES = [
	"Manufacturing Plan Table",
	"Parent Manufacturing Order",
	"Manufacturing Work Order",
]


def execute():
	for doctype in TARGET_DOCTYPES:
		if frappe.db.has_column(doctype, "mould_no") and not frappe.db.has_column(
			doctype, "mould_id"
		):
			frappe.db.rename_column(doctype, "mould_no", "mould_id")
			frappe.logger().info(
				f"rename_mould_no_to_mould_id: renamed {doctype}.mould_no -> mould_id"
			)
