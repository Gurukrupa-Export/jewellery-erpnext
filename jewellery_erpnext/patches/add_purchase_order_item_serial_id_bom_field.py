"""Add Purchase Order Item.custom_serial_id_bom (Serial Id BOM).

A repair sent to the job worker carries two BOMs on its Purchase Order line:

* ``custom_copy_bom`` -- the BOM the repair is manufactured on (Repair Order New BOM);
* ``custom_serial_id_bom`` -- the serial's own BOM (Repair Order BOM), which the receiving
  site's Unpack Serial No books.

On GK, ``make_subcontracting_order`` stamps it from ``Manufacturing Plan Table.serial_id_bom``.
On KGGK, the ``get_purchase_order_items`` Server Script translates it to the local BOM and the
Quotation client script copies it onto ``Quotation Item.custom_serial_id_bom`` (same fieldname),
as does ``make_quotation`` for a Purchase Order on this site.

``custom_fields/purchase_order_item.json`` describes the field but is not applied on migrate
(``after_migrate`` in hooks.py is deliberately not ``migrate.after_migrate``), so this patch is
what creates it. Purchase Order Item carries a ``field_order`` Property Setter, which outranks
``insert_after``, so both are written. Also wired into ``create_test_data.setup_data``, because
``install-app`` marks patches complete without running them on fresh / CI sites.

Idempotent::

    bench --site <site> execute jewellery_erpnext.patches.add_purchase_order_item_serial_id_bom_field.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	rewrite_field_order,
	rewrite_insert_after_chain,
)

DOCTYPE = "Purchase Order Item"
ANCHOR = "serial_no"

CUSTOM_FIELDS = {
	DOCTYPE: [
		{
			"fieldname": "custom_serial_id_bom",
			"fieldtype": "Link",
			"options": "BOM",
			"label": "Serial Id BOM",
			"description": (
				"BOM of the serial being repaired (Repair Order BOM). The receiving site "
				"unpacks the serial against it."
			),
			"insert_after": ANCHOR,
			"module": "Jewellery Erpnext",
			"read_only": 1,
			"no_copy": 1,
		}
	]
}


def execute():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	if frappe.db.exists("Custom Field", {"dt": DOCTYPE, "fieldname": ANCHOR}) or (
		frappe.get_meta(DOCTYPE).has_field(ANCHOR)
	):
		block = [ANCHOR, "custom_serial_id_bom"]
		rewrite_field_order(DOCTYPE, block)
		rewrite_insert_after_chain(DOCTYPE, block[1:], block[0])
	frappe.clear_cache(doctype=DOCTYPE)
	frappe.db.commit()
