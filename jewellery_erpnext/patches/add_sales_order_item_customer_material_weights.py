"""Add Outwork customer-material weight columns to Sales Order Item.

For an Outwork Sales Order each item row shows what the customer supplied, summed from
that row's BOM over the rows ticked "Is Customer Item":

* ``custom_customer_gold_weight``    -- Metal Detail, weight in gms
* ``custom_customer_diamond_weight`` -- Diamond Detail, weight in cts
* ``custom_customer_stone_weight``   -- Gemstone Detail, weight in cts

Filled in ``doc_events.sales_order._update_bom_totals``; 0 for any other sales type. The
grid shows them only on Outwork orders (public/js/doctype_js/sales_order.js).

New fields rather than a change to ``custom_customer_gold`` / ``_diamond`` / ``_stone``:
those are Yes/No flags copied from the Quotation and read by Manufacturing Plan.

This app's ``custom_fields/*.json`` are applied only at install, so a patch is the only
way these reach an existing site. Idempotent. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_sales_order_item_customer_material_weights.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELDS = (
	("custom_customer_gold_weight", "Customer Gold (gms)"),
	("custom_customer_diamond_weight", "Customer Diamond (cts)"),
	("custom_customer_stone_weight", "Customer Stone (cts)"),
)


def execute():
	fields = []
	previous = "custom_company_rm_weight"
	for fieldname, label in FIELDS:
		fields.append(
			{
				"fieldname": fieldname,
				"fieldtype": "Float",
				"label": label,
				"insert_after": previous,
				"read_only": 1,
				"in_list_view": 1,
				"precision": "3",
				"default": "0",
				"module": "Jewellery Erpnext",
			}
		)
		previous = fieldname

	create_custom_fields({"Sales Order Item": fields}, ignore_validate=True)
	frappe.clear_cache(doctype="Sales Order Item")
