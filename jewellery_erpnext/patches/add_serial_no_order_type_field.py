"""Provision ``Serial No.custom_order_type``.

Mirrors ``add_serial_no_ownership_tag_field.py``: a finished piece carries no
indication of what Order Type it was sold under. ``Serial Number Creator``
already fetches ``order_type`` from ``parent_manufacturing_order`` (which in
turn traces back to the Sales Order / Quotation), so the value is available
by the time ``create_manufacturing_entry`` (manufacturing_operation.py) stamps
the other derived fields (``custom_product_type`` / ``custom_gross_wt`` /
``custom_repair_type`` / ``custom_ownership_tag``) onto the newly created
Serial No, right after the Manufacture Stock Entry is submitted. This field
is stamped alongside them there.

The field is ``read_only`` for the same reason as its siblings: it is a
stamp, not user input, so it must not drift from ``doc.order_type`` on the
Serial Number Creator it was minted from.

Wired in the same two idempotent places as ``add_serial_no_ownership_tag_field``
(this ``post_model_sync`` patch and ``create_test_data.setup_data``), for the
same reason: ``after_migrate`` is disabled and ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites, so a fixture-only column
would never reach the DB and ``frappe.db.set_value(..., "custom_order_type", ...)``
would raise ``1054 Unknown column``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_serial_no_order_type_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Serial No": [
			{
				"fieldname": "custom_order_type",
				"fieldtype": "Select",
				"label": "Order Type",
				# Superset of Sales Order's order_type options (the ultimate source of the
				# stamped value): core Sales/Maintenance/Shopping Cart plus Stock Order/Repair,
				# added via add_order_type_repair_option.py's Property Setter.
				"options": "\nSales\nMaintenance\nShopping Cart\nStock Order\nRepair",
				"insert_after": "custom_ownership_tag",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"in_list_view": 1,
				"in_standard_filter": 1,
				"description": (
					"Order Type of the Sales Order / Quotation this piece was manufactured "
					"against. Stamped from the Serial Number Creator's order_type at submit."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_serial_no_order_type_field: ensured Serial No.custom_order_type"
	)
