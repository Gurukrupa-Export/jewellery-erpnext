"""Order Type / Sales Type / Flow Type / Design Type on the Purchase Order header.

``doc_events.purchase_order.make_subcontracting_order`` stamps these four from the Manufacturing
Plan header onto every Purchase Order the plan raises, so a supplier's order shows the same order
dimensions as the Sales Orders it was planned from. The plan header itself is filled by
``ManufacturingPlan.set_order_dimensions``. This patch only provides the four fields; without
them ``Document.set`` keeps the values in memory and the save drops them.

WHY Data
--------
On a Purchase Order these are a copy of what the plan already validated, never user input, so
they are read_only and need no Link or Select check of their own. Data also keeps the stamp
from failing a save: a Link would refuse any value with no local master row, and a Select any
value outside its option list, and either would break the Manufacturing Plan submit that raises
the Purchase Order.

WHY options IS BLANKED AND ignore_validate IS LOAD-BEARING
----------------------------------------------------------
Databases that ran an older patch already hold some of these rows as Link (to Sales Type or
Attribute Value) or Select (with a fixed Flow Type list). ``create_custom_fields`` with the
default ``update=True`` merges the definition below into those rows, and ``options: ""`` clears
the stale Link target or option list, which a Data field must not keep.

CustomField.validate allows only a few fieldtype changes (Data <-> Select among them) and
refuses Link -> Data. ``ignore_validate=True`` skips that check. The conversion is safe: Data,
Link and Select share the same varchar column, so the schema sync makes no ALTER and stored
values are kept as they are.

THE field_order TRAP
--------------------
Purchase Order has been through Customize Form, so it carries a DocType-level ``field_order``
Property Setter that outranks ``insert_after``. Creating the Custom Fields alone would give the
columns but could leave the fields out of place on the form, so this patch writes both the
Property Setter and the ``insert_after`` chain. See ``field_order_utils``.

Wired in the same two idempotent places as its siblings: this ``post_model_sync`` patch and the
patch block of ``create_test_data.create_test_data``. That second wiring is not optional:
``install-app`` marks patches complete WITHOUT running them on fresh / CI sites.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_purchase_order_order_dimension_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)`` and re-applies the same
definition, and the Property Setter and ``insert_after`` chain are recomputed to the same values
on every run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	rewrite_field_order,
	rewrite_insert_after_chain,
)

DOCTYPE = "Purchase Order"

# Existing Purchase Order field the block is placed after.
ANCHOR = "ref_customer"

_WHY_READ_ONLY_DATA = (
	"Stamped from the Manufacturing Plan that raised this order, not entered here. Data rather "
	"than Link or Select, so a value the plan already accepted can never block this order's save."
)

_STAMPED = {
	"fieldtype": "Data",
	"options": "",
	"read_only": 1,
	"description": _WHY_READ_ONLY_DATA,
	"module": "Jewellery Erpnext",
}

CUSTOM_FIELDS = {
	DOCTYPE: [
		{
			"fieldname": "custom_order_type",
			"label": "Order Type",
			"insert_after": ANCHOR,
			**_STAMPED,
		},
		{
			"fieldname": "custom_sales_type",
			"label": "Sales Type",
			"insert_after": "custom_order_type",
			**_STAMPED,
		},
		{
			"fieldname": "custom_flow_type",
			"label": "Flow Type",
			"insert_after": "custom_sales_type",
			**_STAMPED,
		},
		{
			"fieldname": "custom_design_type",
			"label": "Design Type",
			"insert_after": "custom_flow_type",
			**_STAMPED,
		},
	]
}

# The ordered block to splice into `field_order`. It starts with the EXISTING anchor so
# rewrite_field_order has a member already present in the Property Setter's list.
FIELD_ORDER_BLOCK = [ANCHOR, *(f["fieldname"] for f in CUSTOM_FIELDS[DOCTYPE])]


def ensure_fields():
	"""Create the four fields, or bring existing rows to this definition."""
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	frappe.logger().info(
		"add_purchase_order_order_dimension_fields: ensured "
		+ ", ".join(f"{DOCTYPE}.{f['fieldname']}" for f in CUSTOM_FIELDS[DOCTYPE])
	)


def place_fields():
	"""Write both authorities on field position: the Property Setter and the insert_after chain."""
	had_property_setter = rewrite_field_order(DOCTYPE, FIELD_ORDER_BLOCK)
	rewrite_insert_after_chain(DOCTYPE, FIELD_ORDER_BLOCK[1:], FIELD_ORDER_BLOCK[0])
	frappe.clear_cache(doctype=DOCTYPE)
	frappe.logger().info(
		f"add_purchase_order_order_dimension_fields: placed {', '.join(FIELD_ORDER_BLOCK[1:])} "
		f"after {ANCHOR} on {DOCTYPE} (field_order property setter="
		f"{'updated' if had_property_setter else 'absent'})"
	)


def execute():
	ensure_fields()
	place_fields()
	frappe.db.commit()
