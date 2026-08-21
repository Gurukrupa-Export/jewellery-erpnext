"""Add ``Stock Entry Detail.edit_bom`` -- a Button field mirroring Sales Order
Item's ``edit_bom``, so a Stock Entry row can open the same "view/edit this row's
BOM detail" dialog Sales Order Item already offers.

Stock Entry Detail has no equivalent of Sales Order Item's own ``bom`` Link field.
Checked live data: core ``bom_no`` is 0% populated (0 of ~949k rows on the reference
site) -- unused by this app. The click handler therefore resolves the target BOM via
``row.serial_no -> Serial No.custom_bom_no``, the same lookup
``doc_events/sales_order.py::create_serial_no_bom`` already uses.

Why a patch and not ``custom_fields/stock_entry_detail.json``: this app's
``after_migrate`` hook is disabled (hooks.py), so its ``custom_fields/*.json`` are
never applied by ``bench migrate`` -- the recurring patch-only custom-field gap
documented in ``add_conversion_lane_tag_field`` / ``add_material_request_mwo_field``.
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_stock_entry_edit_bom_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELD = {
	"fieldname": "edit_bom",
	"fieldtype": "Button",
	"label": "Edit BOM",
	"insert_after": "bom_no",
	"module": "Jewellery Erpnext",
}


def execute():
	create_custom_fields({"Stock Entry Detail": [FIELD]}, ignore_validate=True)
	frappe.logger().info(
		"add_stock_entry_edit_bom_field: ensured Stock Entry Detail.edit_bom"
	)
