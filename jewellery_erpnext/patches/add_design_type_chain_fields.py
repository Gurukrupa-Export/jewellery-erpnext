"""Create the design type field carried from Order down to the manufacturing chain.

These carry ``Order.design_type`` from the Quotation down to the Manufacturing Work Order, and onto
the Purchase Order raised by a subcontracting Manufacturing Plan. Manufacturing Plan is an app-owned
DocType, so its field ships in ``manufacturing_plan.json`` and arrives with ``bench migrate`` -- it
is not repeated here.

The fieldname is deliberately ``custom_design_type`` on the stock selling/buying doctypes and bare
``design_type`` on PMO / MWO. Matching the name on both sides of a hop is what lets
``get_mapped_doc`` carry the value for free -- Quotation -> Sales Order, and PMO -> MWO across all
five MWO creation paths (metal, FG, CAD/CAM, finding, and the MWO -> MWO split). ``no_copy`` is left
unset for the same reason: setting it would silently kill both hops.

Link rather than Data, because ``Order.design_type`` is itself a Link to ``Attribute Value``
(values come from the Item Attribute record named "Design Type").

Why a patch and not ``custom_fields/*.json``: this app's ``after_migrate`` hook (``hooks.py:14``)
and ``fixtures`` hook (``hooks.py:247``) are both commented out, and ``migrate.py:after_migrate()``
is the only reader of those files, so a patch is the only delivery mechanism that reaches a real
site.

``insert_after`` is best-effort: ``ignore_validate=True`` skips ``validate_insert_after``, so an
anchor missing on a given site appends the field rather than throwing.

Idempotent: every field is guarded on ``frappe.db.has_column``, so a field that already exists is
left exactly as it is. No defaults are invented -- empty values stay empty.

Ad-hoc: bench --site gk15 execute jewellery_erpnext.patches.add_design_type_chain_fields.execute
"""

import frappe

CUSTOM_FIELDS = {
	"Quotation": [
		{
			"fieldname": "custom_design_type",
			"label": "Design Type",
			"fieldtype": "Link",
			"options": "Attribute Value",
			"insert_after": "custom_sales_type",
			"read_only": 1,
			"is_system_generated": 1,
			"module": "Jewellery Erpnext",
		}
	],
	"Sales Order": [
		{
			"fieldname": "custom_design_type",
			"label": "Design Type",
			"fieldtype": "Link",
			"options": "Attribute Value",
			"insert_after": "sales_type",
			"read_only": 1,
			"is_system_generated": 1,
			"module": "Jewellery Erpnext",
		}
	],
	"Purchase Order": [
		{
			"fieldname": "custom_design_type",
			"label": "Design Type",
			"fieldtype": "Link",
			"options": "Attribute Value",
			"insert_after": "ref_customer",
			"read_only": 1,
			"is_system_generated": 1,
			"module": "Jewellery Erpnext",
		}
	],
	"Parent Manufacturing Order": [
		{
			"fieldname": "design_type",
			"label": "Design Type",
			"fieldtype": "Link",
			"options": "Attribute Value",
			"insert_after": "order_type",
			"read_only": 1,
			"is_system_generated": 1,
			"module": "Jewellery Erpnext",
		}
	],
	"Manufacturing Work Order": [
		{
			"fieldname": "design_type",
			"label": "Design Type",
			"fieldtype": "Link",
			"options": "Attribute Value",
			"insert_after": "order_type",
			"read_only": 1,
			"is_system_generated": 1,
			"module": "Jewellery Erpnext",
		}
	],
}


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	pending = {}
	for doctype, fields in CUSTOM_FIELDS.items():
		missing = [f for f in fields if not frappe.db.has_column(doctype, f["fieldname"])]
		if missing:
			pending[doctype] = missing

	if not pending:
		return

	create_custom_fields(pending, ignore_validate=True)
	frappe.db.commit()
	frappe.logger().info(
		"add_design_type_chain_fields: created "
		+ ", ".join(f"{dt}.{f['fieldname']}" for dt, fields in pending.items() for f in fields)
	)
