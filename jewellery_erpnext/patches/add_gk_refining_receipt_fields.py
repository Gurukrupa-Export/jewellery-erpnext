"""Provision the Purchase Order fields that record the refiner's receipt.

When the refiner (GK, a separate site) creates its Refining Entry from KGGK's refining
service Purchase Order and submits it, KGGK marks the PO lines received
(``jewellery_erpnext.refining.kggk_po.apply_gk_receipt``) and stamps:

- ``Purchase Order.custom_gk_refining_entry`` — the refiner's Refining Entry name, which keys
  the receipt so a resend is recognised (``already_applied``) and a second refiner entry is
  refused;
- ``Purchase Order.custom_gk_received_on`` — when it was received.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, this is wired in the usual places: this
``post_model_sync`` patch, ``custom_fields/purchase_order.json`` (applied by ``install.py``)
and ``create_test_data.create_test_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_gk_refining_receipt_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Purchase Order": [
				{
					"fieldname": "custom_gk_refining_entry",
					"fieldtype": "Data",
					"label": "Refiner's Refining Entry",
					"insert_after": "refining_entry",
					"read_only": 1,
					"no_copy": 1,
					"module": "Jewellery Erpnext",
					"description": "The refiner's (GK) Refining Entry that received this Purchase Order.",
				},
				{
					"fieldname": "custom_gk_received_on",
					"fieldtype": "Datetime",
					"label": "Received by Refiner On",
					"insert_after": "custom_gk_refining_entry",
					"read_only": 1,
					"no_copy": 1,
					"module": "Jewellery Erpnext",
				},
			]
		},
		ignore_validate=True,
	)
	frappe.logger().info(
		"add_gk_refining_receipt_fields: ensured Purchase Order.custom_gk_refining_entry / "
		"custom_gk_received_on"
	)
