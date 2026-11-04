"""Split the Sales Order "Finding Weight" total into "Chain Finding" and "Other Finding".

The weight block under the Sales Order item table showed one ``custom_finding_weight``
total. Users need chains and other findings apart, so two fields are added on both
Sales Order and Sales Order Item:

* ``custom_chain_finding_weight`` -- BOM finding rows with ``finding_category == "Chains"``
* ``custom_other_finding_weight`` -- every other finding row

Both are filled in ``doc_events.sales_order._update_bom_totals`` from the same split that
already drives net weight (chains are excluded from net, included in gross).

``custom_finding_weight`` itself is kept and still filled: Delivery Note and Sales Invoice
carry a same-named field and nothing downstream should change meaning. It is only HIDDEN
on the Sales Order form. Sales Order Item keeps it visible -- it is not in the grid.

The Sales Order ``custom_gemstone_weight`` and ``custom_other_weight`` are relabelled
"Gemstone Weight (gms)" and "Other Weight (gms)" -- both values are in grams.

The Sales Order weight block is then re-laid out as 5 columns x 2 rows (the hidden field
takes the last cell, so it occupies no space)::

    Row 1:  Gross Weight    | Metal Weight | Chain Finding | Other Finding | Diamond Weight
    Row 2:  Gemstone Weight (gms) | Other Weight (gms) | Diamond Pcs   | Gemstone Pcs  | (hidden)

This app's ``custom_fields/*.json`` are applied only at install, so a patch is the only
way these reach an existing site. Idempotent. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.split_sales_order_finding_weight.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.compact_sales_weight_field_layout import _compact_one

SALES_ORDER_WEIGHT_FIELD_ORDER = (
	"custom_gross_weight",
	"custom_metal_weight",
	"custom_chain_finding_weight",
	"custom_other_finding_weight",
	"custom_diamond_weight",
	"custom_gemstone_weight",
	"custom_other_weight",
	"custom_diamond_pcs",
	"custom_gemstone_pcs",
	"custom_finding_weight",
)


def _fields(doctype):
	return [
		{
			"fieldname": "custom_chain_finding_weight",
			"fieldtype": "Float",
			"label": "Chain Finding",
			"insert_after": "custom_finding_weight",
			"module": "Jewellery Erpnext",
		},
		{
			"fieldname": "custom_other_finding_weight",
			"fieldtype": "Float",
			"label": "Other Finding",
			"insert_after": "custom_chain_finding_weight",
			"module": "Jewellery Erpnext",
		},
	]


def execute():
	create_custom_fields(
		{dt: _fields(dt) for dt in ("Sales Order", "Sales Order Item")},
		ignore_validate=True,
	)

	# custom_gemstone_weight followed custom_finding_weight; follow the new pair instead,
	# or it ties with custom_chain_finding_weight for the same slot.
	frappe.db.set_value(
		"Custom Field",
		{"dt": "Sales Order Item", "fieldname": "custom_gemstone_weight"},
		"insert_after",
		"custom_other_finding_weight",
	)
	frappe.clear_cache(doctype="Sales Order Item")

	frappe.db.set_value(
		"Custom Field",
		{"dt": "Sales Order", "fieldname": "custom_finding_weight"},
		"hidden",
		1,
	)
	for fieldname, label in (
		("custom_gemstone_weight", "Gemstone Weight (gms)"),
		("custom_other_weight", "Other Weight (gms)"),
	):
		frappe.db.set_value(
			"Custom Field",
			{"dt": "Sales Order", "fieldname": fieldname},
			"label",
			label,
		)
	frappe.clear_cache(doctype="Sales Order")

	_compact_one("Sales Order", SALES_ORDER_WEIGHT_FIELD_ORDER, column_count=5)

	frappe.db.commit()
