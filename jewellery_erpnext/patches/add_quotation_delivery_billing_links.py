"""Provision the Quotation back-links used by the Sales-Order-free delivery / billing closeout.

WHY THIS EXISTS
---------------
The manufacturing flow no longer produces a Sales Order, so finished serials are delivered and
invoiced straight against the **Quotation**. Two link columns are needed to carry that reference
through the delivery -> billing chain:

- ``Delivery Note Item.custom_against_quotation`` -- the Quotation analogue of the core
  ``against_sales_order``. ``doc_events/delivery_note.validate`` uses it to pull the e-invoice
  rows from ``Quotation E Invoice Item`` (the Quotation already carries a ``custom_invoice_item``
  table of that child doctype, provisioned by gke_customization) instead of
  ``Sales Order E Invoice Item``.
- ``Sales Invoice Item.custom_quotation`` -- lets ``sales_invoice.update_bom_details`` resolve the
  invoice-line source document for Quotation-based rows, where ``sales_order`` is blank.

Both are additive and legacy-safe: Sales-Order-based documents keep using
``against_sales_order`` / ``sales_order`` untouched.

Because ``after_migrate`` is disabled and this app's ``custom_fields/*.json`` are dead config, a
fixture-only column would never reach the DB. Per the app convention this is wired in two
idempotent places: this ``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be
run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_quotation_delivery_billing_links.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Delivery Note Item": [
			{
				"fieldname": "custom_against_quotation",
				"fieldtype": "Link",
				"options": "Quotation",
				"label": "Against Quotation",
				"insert_after": "against_sales_order",
				"read_only": 1,
				"no_copy": 1,
			}
		],
		"Sales Invoice Item": [
			{
				"fieldname": "custom_quotation",
				"fieldtype": "Link",
				"options": "Quotation",
				"label": "Quotation",
				"insert_after": "sales_order",
				"read_only": 1,
				"no_copy": 1,
			}
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_quotation_delivery_billing_links: ensured Delivery Note Item.custom_against_quotation "
		"and Sales Invoice Item.custom_quotation"
	)
