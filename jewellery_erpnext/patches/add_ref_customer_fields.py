"""Provision the Ref Customer fields for customer-batch-only Hybrid findings.

* ``Stock Entry.ref_customer`` -- the end customer a "Customer Goods Received" entry receives
  goods for. On KG GK every receipt's Customer (``_customer``) is GK Export, so this is the only
  record of whose goods they are.
* ``Batch.custom_ref_customer`` -- the same value on every batch that entry mints, stamped by
  ``batch_rename`` and ``update_inventory_dimentions`` and inherited by child batches.

``customer_subcontracting/hybrid_findings.py`` matches a Hybrid order's ``ref_customer`` against
the batch value.

Both fields are also declared in ``custom_fields/stock_entry.json`` / ``batch.json``, but that is
dead config on real and CI sites (the ``after_migrate`` hook that would sync it is disabled in
``hooks.py``), so this ``post_model_sync`` patch is what creates them. It is wired the two ways
the app convention requires: here, and in ``create_test_data`` for the disposable CI
``test_site``. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_ref_customer_fields.execute

Both forms carry a ``field_order`` Property Setter on the live sites. A field absent from that
list is placed by its ``insert_after`` (``field_order_utils`` docstring), so anchoring next to the
existing customer field is enough. On a fresh site the anchors are fixture fields that may not
exist yet; ``ignore_validate`` lets the field be created anyway, at the end of the form.

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "ref_customer",
				"fieldtype": "Link",
				"options": "Customer",
				"label": "Ref Customer",
				"insert_after": "_customer",
				"depends_on": 'eval:doc.stock_entry_type=="Customer Goods Received"',
				"description": "End customer these goods are received for. Every batch this "
				"entry creates carries it, and Hybrid orders of that Ref Customer use those "
				"batches for the finding categories set in Subcontracting Settings.",
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			},
		],
		"Batch": [
			{
				"fieldname": "custom_ref_customer",
				"fieldtype": "Link",
				"options": "Customer",
				"label": "Ref Customer",
				"insert_after": "custom_customer",
				"read_only": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
				"module": "Jewellery Erpnext",
			},
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_ref_customer_fields: ensured Stock Entry.ref_customer and Batch.custom_ref_customer"
	)
