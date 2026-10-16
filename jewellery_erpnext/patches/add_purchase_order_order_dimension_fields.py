"""Carry Order Type / Sales Type / Flow Type / Design Type from the Manufacturing Plan onto the
subcontracting Purchase Order.

``add_order_sales_flow_type_fields`` and ``add_design_type_fields`` already walk these four from the
Quotation down to the Serial No, but the chain had one gap: the Purchase Order that
``make_subcontracting_order`` raises when a plan is submitted. That Purchase Order is what the
supplier's site pulls over REST into its own Quotation, so a value missing here is missing for the
supplier's whole chain. The leg this patch completes::

    Sales Order -> Manufacturing Plan.order_type / sales_type / flow_type / design_type
                   (set_order_dimensions, throws on a mismatch)
      -> Purchase Order.custom_order_type / custom_sales_type / custom_flow_type /
         custom_design_type   (make_subcontracting_order, PO_ORDER_DIMENSION_MAP)

Fieldtypes follow the rest of the chain: Order Type and Flow Type are Data (their option lists are
owned by another site -- see ``add_order_sales_flow_type_fields``), Sales Type and Design Type are
Links to masters that exist on this site. ``read_only`` because the value is a stamp from the plan,
never user input. Deliberately NOT ``no_copy``: amending a cancelled subcontracting Purchase Order
must keep the stamp, and it cannot be re-derived after the fact.

Purchase Order has been through Customize Form, so it carries a DocType-level ``field_order``
Property Setter which OUTRANKS ``insert_after`` -- see ``field_order_utils``; this patch writes both.
The anchor ``ref_customer`` comes from the ``gke_customization`` Custom Field fixture.

Wired in the same two idempotent places as its siblings: this ``post_model_sync`` patch and
``create_test_data.setup_data`` (``install-app`` marks patches complete WITHOUT running them).

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_purchase_order_order_dimension_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``, and the Property Setter and
``insert_after`` chain are recomputed to the same values on every run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	rewrite_field_order,
	rewrite_insert_after_chain,
)

_STAMP = {"module": "Jewellery Erpnext", "read_only": 1}

CUSTOM_FIELDS = {
	"Purchase Order": [
		{
			"fieldname": "custom_order_type",
			"fieldtype": "Data",
			"label": "Order Type",
			"insert_after": "ref_customer",
			**_STAMP,
		},
		{
			"fieldname": "custom_sales_type",
			"fieldtype": "Link",
			"options": "Sales Type",
			"label": "Sales Type",
			"insert_after": "custom_order_type",
			"in_standard_filter": 1,
			**_STAMP,
		},
		{
			"fieldname": "custom_flow_type",
			"fieldtype": "Data",
			"label": "Flow Type",
			"insert_after": "custom_sales_type",
			"in_standard_filter": 1,
			**_STAMP,
		},
		{
			"fieldname": "custom_design_type",
			"fieldtype": "Link",
			"options": "Attribute Value",
			"label": "Design Type",
			"insert_after": "custom_flow_type",
			"in_standard_filter": 1,
			**_STAMP,
		},
	],
}

# Per doctype: the ordered block to splice into `field_order`. Each block starts with an EXISTING
# field so rewrite_field_order has an anchor already present in the Property Setter's list.
FIELD_ORDER_BLOCKS = {
	"Purchase Order": [
		"ref_customer",
		"custom_order_type",
		"custom_sales_type",
		"custom_flow_type",
		"custom_design_type",
	],
}


def ensure_purchase_order_order_dimension_fields():
	"""Create / refresh the four custom fields."""
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	frappe.logger().info(
		"add_purchase_order_order_dimension_fields: ensured "
		+ ", ".join(
			f"{dt}.{f['fieldname']}"
			for dt, fields in CUSTOM_FIELDS.items()
			for f in fields
		)
	)


def place_fields_after_their_anchors():
	"""Write both authorities on field position: the Property Setter and the insert_after chain."""
	for doctype, block in FIELD_ORDER_BLOCKS.items():
		had_property_setter = rewrite_field_order(doctype, block)
		rewrite_insert_after_chain(doctype, block[1:], block[0])
		frappe.clear_cache(doctype=doctype)
		frappe.logger().info(
			f"add_purchase_order_order_dimension_fields: placed {', '.join(block[1:])} after "
			f"{block[0]} on {doctype} (field_order property setter="
			f"{'updated' if had_property_setter else 'absent'})"
		)


def execute():
	ensure_purchase_order_order_dimension_fields()
	place_fields_after_their_anchors()
	frappe.db.commit()
