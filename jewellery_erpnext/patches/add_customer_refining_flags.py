"""Provision two Customer checkboxes that gate the External Refinery Flow.

- ``custom_block_refining`` — when set, ALL of this customer's batches are kept out of
  Scrap Refining and Unused/Loose Material Refining (filtered from the fetch and
  hard-blocked at submit by ``RefiningEntry``).
- ``custom_no_wastage`` — when set, customer-supplied material must not be charged
  manufacturing wastage: process loss on this customer's batches is blocked so the
  unused weight returns as raw material instead of a customer-owned scrap batch
  (enforced in Employee IR ``book_metal_loss`` / loss stock entry).

These are two distinct policies on two different lifecycles (refining vs
manufacturing), so they are separate flags.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach
the DB. Per the app convention this is wired in two idempotent places: this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_customer_refining_flags.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Customer": [
			{
				"fieldname": "custom_block_refining",
				"fieldtype": "Check",
				"label": "Block from Scrap & Unused/Loose Material Refining",
				"insert_after": "custom_separate_hallmarking_invoice",
				"module": "Jewellery Erpnext",
				"description": "Keep this customer's batches out of Scrap Refining and Unused/Loose Material Refining entries.",
			},
			{
				"fieldname": "custom_no_wastage",
				"fieldtype": "Check",
				"label": "No Wastage (Customer Material)",
				"insert_after": "custom_block_refining",
				"module": "Jewellery Erpnext",
				"description": "Do not book manufacturing loss on this customer's material; return the unused weight as raw material.",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_customer_refining_flags: ensured Customer.custom_block_refining / custom_no_wastage"
	)
