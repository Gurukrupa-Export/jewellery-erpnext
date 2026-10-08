"""Provision the ownership-split amount fields on ``Sales Order Item``.

``_update_bom_totals`` in ``doc_events/sales_order.py`` splits every row's amount into the
portion priced from company-owned material (metal, finding, diamond, gemstone plus the
certification / hallmarking / duty / freight / sale charges) and the portion that merely
passes through customer-supplied material. The split is written to
``row.custom_company_owned_amount`` and ``row.custom_customer_supplied_amount`` and later
read back by the header-level ownership totals and the subcontracting charge rows.

Both fields are declared in ``custom_fields/sales_order_item.json``, but that file is dead
config on real and CI sites: the ``after_migrate`` hook that would sync it is disabled
(``hooks.py``) and ``install-app`` marks patches complete without running them on fresh
sites. Without the columns the values assigned in ``_update_bom_totals`` are silently
dropped on save, so the header totals always read zero.

``insert_after`` anchors on the STANDARD ``base_price_list_rate``: ``post_model_sync``
patches run before ``sync_fixtures()`` / ``sync_customizations()``, so no custom field is
guaranteed to exist the first time this runs on a fresh site.

Wired in the two idempotent places the app convention requires: this ``post_model_sync``
patch and ``create_test_data`` (for the disposable CI ``test_site``). Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_sales_order_item_ownership_amount_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Sales Order Item": [
			{
				"fieldname": "custom_company_owned_amount",
				"fieldtype": "Currency",
				"insert_after": "base_price_list_rate",
				"label": "Company Owned Amount",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_customer_supplied_amount",
				"fieldtype": "Currency",
				"insert_after": "custom_company_owned_amount",
				"label": "Customer Supplied Amount",
				"module": "Jewellery Erpnext",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_sales_order_item_ownership_amount_fields: ensured "
		"custom_company_owned_amount / custom_customer_supplied_amount on Sales Order Item"
	)
