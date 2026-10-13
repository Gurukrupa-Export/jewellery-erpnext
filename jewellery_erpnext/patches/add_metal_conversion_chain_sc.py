"""Provision the masters the Metal Conversions Chain Subcontracting flow reads.

* ``Department.custom_is_chain_manufacturing`` / ``custom_is_chain_subcontracting`` --
  the two department switches that decide whether an employee of that department may
  raise a chain conversion at all, and whether it may go to a subcontractor. They sit in
  a section of their own: ``custom_is_finding`` already anchors the Stock Reconciliation
  Window section, and two custom fields inserted after the same anchor land in an
  undefined order.
* ``Purchase Order.custom_metal_conversion`` -- the back-link on the draft PO the Issue
  step raises, so the conversion can find (and, on cancel, delete) it.
* The ``Semi FG Subcontracting`` service item that PO is raised for, copied from
  ``FG Subcontracting`` -- the service item Gemstone Conversion's resize flow bills -- so
  it carries the same group, UOM, HSN and item defaults. Skipped when that template is
  missing (a fresh / CI site); the Issue step names the missing item instead.

Because ``install-app`` marks patches complete WITHOUT running them on fresh / CI sites,
this is wired in two idempotent places per the app convention: this ``post_model_sync``
patch and ``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_metal_conversion_chain_sc.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)`` and the item is
create-only.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CHAIN_SC_PO_ITEM = "Semi FG Subcontracting"
TEMPLATE_ITEM = "FG Subcontracting"


def execute():
	create_custom_fields(
		{
			"Department": [
				{
					"fieldname": "custom_chain_manufacturing_section",
					"fieldtype": "Section Break",
					"label": "Chain Manufacturing",
					"insert_after": "custom_stock_reconciliation_to_time",
					"module": "Jewellery Erpnext",
				},
				{
					"fieldname": "custom_is_chain_manufacturing",
					"fieldtype": "Check",
					"label": "Is Chain Manufacturing",
					"description": "Employees of this department may raise a Chain Manufacturing Metal Conversion.",
					"insert_after": "custom_chain_manufacturing_section",
					"module": "Jewellery Erpnext",
				},
				{
					"fieldname": "custom_is_chain_subcontracting",
					"fieldtype": "Check",
					"label": "Is Chain Subcontracting",
					"description": "A Chain Manufacturing Metal Conversion from this department may be sent to a subcontractor.",
					"insert_after": "custom_is_chain_manufacturing",
					"module": "Jewellery Erpnext",
				},
			],
			"Purchase Order": [
				{
					"fieldname": "custom_metal_conversion",
					"fieldtype": "Link",
					"label": "Metal Conversion",
					"options": "Metal Conversions",
					"read_only": 1,
					"no_copy": 1,
					"module": "Jewellery Erpnext",
				}
			],
		},
		ignore_validate=True,
	)
	_ensure_po_item()


def _ensure_po_item():
	if frappe.db.exists("Item", CHAIN_SC_PO_ITEM):
		return
	if not frappe.db.exists("Item", TEMPLATE_ITEM):
		frappe.logger().info(
			f"add_metal_conversion_chain_sc: {TEMPLATE_ITEM!r} missing, "
			f"{CHAIN_SC_PO_ITEM!r} not created"
		)
		return

	item = frappe.copy_doc(frappe.get_doc("Item", TEMPLATE_ITEM))
	item.item_code = CHAIN_SC_PO_ITEM
	item.item_name = CHAIN_SC_PO_ITEM
	item.description = CHAIN_SC_PO_ITEM
	item.insert(ignore_permissions=True)
