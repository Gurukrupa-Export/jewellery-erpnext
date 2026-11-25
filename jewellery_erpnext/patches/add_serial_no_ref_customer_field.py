"""Provision ``Serial No.custom_ref_customer``.

Product Certification reads it when a Serial No is scanned or picked into Product Details
(product_certification.js), to fill the row's ``ref_customer``. The field was only ever
added to ``custom_fields/serial_no.json``, which nothing applies — ``after_migrate`` is
disabled — so sites never got it, and every one of those reads failed with
"Field not permitted in query: custom_ref_customer": ``frappe.client.get_value`` validates
the requested fields against the doctype meta and throws on an unknown one.

Wired in the two idempotent places this app uses for custom fields (this
``post_model_sync`` patch and ``create_test_data.setup_data``), for the usual reason:
``install-app`` marks patches complete WITHOUT running them on fresh / CI sites. Can also
be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_serial_no_ref_customer_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Serial No": [
			{
				"fieldname": "custom_ref_customer",
				"fieldtype": "Link",
				"label": "Ref Customer",
				"options": "Customer",
				# Directly under the sales-reference pair, which add_serial_no_sales_reference_fields
				# places under Brand. A field missing from Serial No's field_order Property Setter
				# is positioned by insert_after, so this holds on Customize Form'd sites too.
				"insert_after": "custom_reference_docname",
				"module": "Jewellery Erpnext",
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_serial_no_ref_customer_field: ensured Serial No.custom_ref_customer"
	)
