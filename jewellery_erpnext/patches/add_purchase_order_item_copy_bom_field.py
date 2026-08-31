"""Ensure ``Purchase Order Item.custom_copy_bom`` exists.

``copy_bom`` is the origin-BOM stamp minted on the Quotation from the Order's own BOM
(``Order.new_bom``). It rides Quotation Item -> Sales Order Item for free (same fieldname
on both child tables, so ``get_mapped_doc`` copies it), and now rides on to
``Manufacturing Plan Table.copy_bom``. The chain dead-ended at the Purchase Order because
``Purchase Order Item`` had no such field: ``make_subcontracting_order`` could put the key
on the row dict and ``get_valid_dict`` would silently drop it on save -- no error, just a
missing value.

Why a patch and not ``custom_fields/purchase_order_item.json``: this app's
``after_migrate`` hook is commented out (``hooks.py:14``) and ``migrate.py:after_migrate()``
is the only reader of ``custom_fields/*.json``, so those files never reach a real site. The
``fixtures`` hook is commented out too (``hooks.py:242``), and ``gke_customization``'s
Custom Field fixture is scoped to its own modules. A patch is the only delivery mechanism.
Can also be run ad-hoc::

    bench --site gk execute jewellery_erpnext.patches.add_purchase_order_item_copy_bom_field.execute

Idempotent: guarded on ``frappe.db.has_column``.
"""

import frappe

FIELD = {
	"fieldname": "custom_copy_bom",
	"label": "Copy BOM",
	"fieldtype": "Link",
	"options": "BOM",
	"insert_after": "manufacturing_bom",
	"read_only": 1,
	"is_system_generated": 1,
	"module": "Jewellery Erpnext",
	"description": (
		"Origin BOM this row came from, carried through from the Order via Quotation, "
		"Sales Order and Manufacturing Plan. Not the item's master BOM."
	),
}


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	if frappe.db.has_column("Purchase Order Item", FIELD["fieldname"]):
		return

	create_custom_fields({"Purchase Order Item": [FIELD]}, ignore_validate=True)
	frappe.db.commit()
	frappe.logger().info(
		"add_purchase_order_item_copy_bom_field: created Purchase Order Item.custom_copy_bom"
	)
