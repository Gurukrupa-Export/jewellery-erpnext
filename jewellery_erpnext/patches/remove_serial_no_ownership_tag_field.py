"""Retire ``Serial No.custom_ownership_tag``.

The field was an ownership/source marker (Outright / Outwork / Hybrid) seeded from the
source Sales Order's ``sales_type``. Its only writer -- the stamp in
``create_manufacturing_entry`` (manufacturing_operation.py) -- has been removed along with
the ``_derive_ownership_tag`` deriver that was meant to overwrite it, and
``add_serial_no_ownership_tag_field`` (the patch that provisioned it) is gone, so fresh
sites never get the field at all. This patch is what removes it from sites that already
ran that patch, so the retired field does not linger on the Serial No form and list.

Only the Custom Field is deleted. Frappe's ``CustomField.on_trash`` also drops the
per-field Property Setters, but it does NOT alter the table -- the ``custom_ownership_tag``
column and whatever it holds stay on ``tabSerial No``. That is deliberate: the values are
historical and this patch is about the form, not the data. Dropping the column, if it is
ever wanted, is a separate and irreversible decision.

The doctype-level ``field_order`` Property Setter is pruned separately: ``on_trash`` only
clears property setters whose ``field_name`` is this field, and Serial No has been through
Customize Form, so a stale entry there would keep the fieldname in the rendered order.

Dependent anchors are re-pointed BEFORE the delete. ``custom_order_type`` was created with
``insert_after = custom_ownership_tag`` on every site that ran the original patch (its own
patch entry has since been re-anchored, but ``patches.txt`` entries run once, so the stored
value is unchanged there). Deleting this field would leave that anchor pointing at nothing
and the field's position undefined on the next meta build, so it inherits the anchor this
field itself used -- the standard ``customer`` field.

Idempotent: both steps are no-ops once the field is gone. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.remove_serial_no_ownership_tag_field.execute
"""

import frappe

from jewellery_erpnext.patches.field_order_utils import strip_field_order_entries

FIELDNAME = "custom_ownership_tag"

# The anchor the retired field itself used, and therefore the one its dependants inherit.
ANCHOR = "customer"


def execute():
	strip_field_order_entries("Serial No", [FIELDNAME])
	_reanchor_dependants()

	docname = frappe.db.exists(
		"Custom Field", {"dt": "Serial No", "fieldname": FIELDNAME}
	)
	if not docname:
		return

	# force=True for the same reason remove_se_custom_fields uses it: the field is
	# Administrator-owned on every site that ran the original patch, and on_trash refuses
	# a non-Administrator deletion of such a field.
	frappe.delete_doc("Custom Field", docname, force=True, ignore_permissions=True)
	frappe.clear_cache(doctype="Serial No")
	frappe.logger().info(
		f"remove_serial_no_ownership_tag_field: deleted Serial No.{FIELDNAME}"
	)


def _reanchor_dependants():
	"""Re-point every Serial No Custom Field anchored on the retired field."""
	dependants = frappe.get_all(
		"Custom Field",
		filters={"dt": "Serial No", "insert_after": FIELDNAME},
		pluck="name",
	)
	for name in dependants:
		frappe.db.set_value("Custom Field", name, "insert_after", ANCHOR)

	if dependants:
		frappe.logger().info(
			"remove_serial_no_ownership_tag_field: re-anchored "
			+ ", ".join(dependants)
			+ f" onto {ANCHOR}"
		)
