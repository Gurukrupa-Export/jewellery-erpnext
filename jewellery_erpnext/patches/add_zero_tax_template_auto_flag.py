"""Provision ``Item Tax Template.custom_is_auto_zero_tax``.

``jewellery_erpnext/doc_events/purchase_invoice.py::get_or_create_zero_tax_template``
looks up and stamps this field to find/own the one auto-provisioned zero-rated Item
Tax Template per company (used for items with no explicit item_tax_template so they
aren't accidentally taxed at whatever rate the Purchase Taxes and Charges row
carries). The field was referenced in code without ever being created, so any
Purchase Invoice save hitting an untaxed item threw
``Unknown column 'custom_is_auto_zero_tax' in 'WHERE'``.

Because ``after_migrate`` is disabled (``hooks.py:12``) and ``install-app`` marks
patches complete WITHOUT running them on fresh / CI sites, a ``custom_fields/``
declaration alone would never reach the DB -- per the app convention this is a
``post_model_sync`` patch instead (see ``add_stock_entry_type_allowed_roles.py``).

A site may already have a template created by the older, title-only matching logic
(same title, no flag). Leaving it unflagged would make the next lookup miss it and
try to create a second template with the same autoname (title + company abbr),
raising a duplicate-entry error -- so this patch also backfills the flag onto any
existing row matching the well-known auto-template title.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_zero_tax_template_auto_flag.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``; the backfill only
touches rows where the flag isn't already set.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice import (
	ZERO_TAX_TEMPLATE_TITLE,
)


def execute():
	custom_fields = {
		"Item Tax Template": [
			{
				"fieldname": "custom_is_auto_zero_tax",
				"fieldtype": "Check",
				"label": "Is Auto Zero Tax Template",
				"insert_after": "disabled",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"description": (
					"Marks the auto-provisioned, zero-rated Item Tax Template jewellery_erpnext "
					"assigns to items with no tax template of their own. Do not set manually."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)

	for name in frappe.get_all(
		"Item Tax Template",
		filters={
			"title": ZERO_TAX_TEMPLATE_TITLE,
			"custom_is_auto_zero_tax": ["!=", 1],
		},
		pluck="name",
	):
		frappe.db.set_value(
			"Item Tax Template",
			name,
			"custom_is_auto_zero_tax",
			1,
			update_modified=False,
		)

	frappe.logger().info(
		"add_zero_tax_template_auto_flag: ensured Item Tax Template.custom_is_auto_zero_tax "
		"and backfilled existing auto-created templates"
	)
