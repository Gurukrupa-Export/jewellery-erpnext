"""Provision ``Supplier.custom_metal_weight_limit`` -- the per-supplier pure metal cap.

Grams of PURE metal (stock qty x purity % / 100, across every metal / finding item whatever its
touch) a supplier may hold across all warehouses linked to it (``Warehouse.subcontractor``).
0 / blank means no limit. Enforcement: ``jewellery_erpnext.doc_events.supplier_metal_limit``.

Because ``after_migrate`` is disabled (``hooks.py:12``) and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach the DB. Per the
app convention this is wired in two idempotent places: this ``post_model_sync`` patch and
``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_supplier_metal_weight_limit.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Supplier": [
				{
					"fieldname": "custom_metal_weight_limit",
					"fieldtype": "Float",
					"label": "Metal Weight Limit (Pure Weight)",
					"precision": "3",
					"non_negative": 1,
					"insert_after": "custom_allowed_item_group",
					"module": "Jewellery Erpnext",
					"description": (
						"Maximum pure metal, in grams (qty x purity % / 100, all metal and finding "
						"items of every touch), this supplier may hold across all of its linked "
						"warehouses. Transfers that would exceed it are refused. Leave 0 for no limit."
					),
				}
			]
		},
		ignore_validate=True,
	)
	frappe.logger().info(
		"add_supplier_metal_weight_limit: ensured Supplier.custom_metal_weight_limit"
	)
