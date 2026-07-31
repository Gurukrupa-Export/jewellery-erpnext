"""Rename the Check field ``is_main_slip_required`` -> ``is_raw_material`` on Department
Operation and its ``fetch_from`` copy on Employee IR.

The flag never described a Main Slip requirement: it is a routing switch. When ticked, the
Employee IR process-loss / gain metal goes to the employee's or subcontractor's **Raw
Material** warehouse and keeps the same item code, instead of taking the scrap/dust path
(``loss_stock_entry._resolve_t_warehouse`` / ``_resolve_loss_item``). Pure rename -- the
fieldtype (``Check``), the ``0`` default, the field_order position and every gate stay as
they are.

Runs in ``[pre_model_sync]`` so the physical column is renamed BEFORE model-sync reads the
new JSON: model-sync then finds ``is_raw_material`` already present -- it does not mint a
fresh empty column (which would silently read every ticked row back as ``0``) and leaves no
orphaned ``is_main_slip_required`` column behind.

Also repoints site-level metadata a column rename cannot reach: Property Setters created via
Customize Form, and any Custom Field anchored on the old fieldname via ``insert_after``.

No-op on fresh CI (no old column exists, so model-sync creates ``is_raw_material`` directly)
and idempotent on re-run (guarded on old-column-present / new-column-absent).

Ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.rename_is_main_slip_required_to_is_raw_material.execute
"""

import frappe

OLD_FIELDNAME = "is_main_slip_required"
NEW_FIELDNAME = "is_raw_material"
TARGET_DOCTYPES = ["Department Operation", "Employee IR"]


def execute():
	renamed = []

	for doctype in TARGET_DOCTYPES:
		if frappe.db.has_column(doctype, OLD_FIELDNAME) and not frappe.db.has_column(
			doctype, NEW_FIELDNAME
		):
			frappe.db.rename_column(doctype, OLD_FIELDNAME, NEW_FIELDNAME)
			renamed.append(doctype)

		# Customize Form tweaks on the old fieldname would dangle after the rename.
		frappe.db.sql(
			"""
			UPDATE `tabProperty Setter`
			   SET field_name = %(new)s
			 WHERE doc_type = %(dt)s AND field_name = %(old)s
			""",
			{"new": NEW_FIELDNAME, "dt": doctype, "old": OLD_FIELDNAME},
		)

		# A Custom Field anchored after the old field would lose its position.
		frappe.db.sql(
			"""
			UPDATE `tabCustom Field`
			   SET insert_after = %(new)s
			 WHERE dt = %(dt)s AND insert_after = %(old)s
			""",
			{"new": NEW_FIELDNAME, "dt": doctype, "old": OLD_FIELDNAME},
		)

		frappe.clear_cache(doctype=doctype)

	frappe.logger().info(
		f"rename_is_main_slip_required_to_is_raw_material: renamed column on "
		f"{renamed or 'no'} doctype(s); property setters and custom-field anchors repointed"
	)
