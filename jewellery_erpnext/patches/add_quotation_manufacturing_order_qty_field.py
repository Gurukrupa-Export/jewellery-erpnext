"""Provision the ``Quotation Item.manufacturing_order_qty`` custom field.

WHY THIS EXISTS
---------------
The manufacturing flow now plans directly off the Quotation (Sales Order removed from the
Quotation -> Manufacturing Plan path). The Manufacturing Plan fetch computes each row's pending
qty as ``Quotation Item.qty - Quotation Item.manufacturing_order_qty`` and writes the planned
qty back to ``Quotation Item.manufacturing_order_qty`` on submit/cancel -- mirroring what
``Sales Order Item.manufacturing_order_qty`` did before.

Because ``after_migrate`` is disabled and this app's ``custom_fields/*.json`` are dead config
(see ``fetch_from_guard`` / ``ensure_fetch_from_columns``), a fixture-only column would never
reach the DB and every Quotation-based Manufacturing Plan query would raise ``1054 Unknown
column 'manufacturing_order_qty'``. Per the app convention this is wired in two idempotent
places: this ``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_quotation_manufacturing_order_qty_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Quotation Item": [
			{
				"fieldname": "manufacturing_order_qty",
				"fieldtype": "Float",
				"label": "Manufacturing Order Qty",
				"insert_after": "qty",
				"read_only": 1,
				"no_copy": 1,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_quotation_manufacturing_order_qty_field: ensured Quotation Item.manufacturing_order_qty"
	)
