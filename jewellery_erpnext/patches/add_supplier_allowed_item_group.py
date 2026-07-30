"""Provision ``Supplier.custom_allowed_item_group`` — the per-supplier Metal whitelist.

The table (child DocType ``Supplier Allowed Item Group``, two Link fields: ``item_group``
and ``item_code``) lists the Metal items a supplier is permitted to supply. Enforcement is
deliberately scoped: it applies **only** to items inside the Metal item groups
(``Metal - T`` / ``Metal - V`` and their descendants). Every other item group — Diamond,
Gemstone, Finding, Design, Consumable — stays completely unrestricted.

See ``doc_events/supplier_allowed_items.py`` for the metal-scope resolution, the link-field
filter and the transaction-time guard.

Because ``after_migrate`` is disabled (``hooks.py:12``) and ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites, a fixture-only column would never reach
the DB. Per the app convention this is wired in two idempotent places: this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_supplier_allowed_item_group.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Supplier": [
			{
				"fieldname": "custom_allowed_item_group",
				"fieldtype": "Table",
				"label": "Allowed Item Group",
				"options": "Supplier Allowed Item Group",
				"insert_after": "operations",
				"module": "Jewellery Erpnext",
				"description": (
					"Metal items this supplier is allowed to supply. Applies to Metal item groups "
					"only — every other item group stays unrestricted."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_supplier_allowed_item_group: ensured Supplier.custom_allowed_item_group"
	)
