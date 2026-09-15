"""Carry Design Type from the GKExport Purchase Order onto the Quotation and its Sales Order.

Order Type / Sales Type / Flow Type already make this walk (``add_order_sales_flow_type_fields``).
Design Type did not exist on the chain at all.

This patch adds only the two STOCK doctypes' custom fields -- Quotation and Sales Order. The three
app-owned doctypes below them (Manufacturing Plan, Parent Manufacturing Order, Manufacturing Work
Order) declare ``design_type`` in their own ``.json`` and arrive with ``bench migrate``, exactly as
their ``flow_type`` siblings do; they are deliberately not repeated here. The full chain is::

    Quotation.custom_design_type        <- get_purchase_order_items (GKExport)
      -> Sales Order.custom_design_type     get_mapped_doc, same fieldname
        -> Manufacturing Plan.design_type   set_order_dimensions(), ORDER_DIMENSION_MAP
        -> Parent Manufacturing Order.design_type   fetch_from sales_order.custom_design_type
          -> Manufacturing Work Order.design_type   fetch_from manufacturing_order.design_type

WHERE THE VALUE COMES FROM
--------------------------
The ``get_purchase_order_items`` Server Script pulls a Purchase Order from the GKExport site over
REST. Design Type is NOT on the remote Purchase Order, Sales Order or Manufacturing Plan -- it lives
on the remote ``Order`` doctype, one per Sales Order Item row::

    PO.manufacturing_plan
      -> Manufacturing Plan.manufacturing_plan_table[].sales_order
        -> Sales Order.items[].order_form_id   (where items[].order_form_type == "Order")
          -> Order.design_type

The Server Script refuses the pull when one Purchase Order's rows disagree, so by the time the value
reaches the Quotation it is single-valued. See ``DESIGN_TYPE_DB_SCRIPTS.md`` at the bench root -- the
Server Script and Client Script are database rows, not files, and are not version controlled.

WHY Link AND NOT Data
---------------------
The opposite choice from its ``custom_flow_type`` sibling, for a concrete reason: ``Order.design_type``
is itself a Link to ``Attribute Value``, and ``Attribute Value`` exists on THIS site. Flow Type is
Data only because its option list lives on the GKExport site and has no local master to point at.

The consequence is that a design type with no local ``Attribute Value`` row cannot be saved. Two
values live on the remote today -- "Mod" and "As Per Serial No" -- that have no row here, so the
Server Script checks ``frappe.db.exists`` and refuses at the button rather than letting the Quotation
fail link validation at save. This patch deliberately does NOT seed those two records; creating them
is a decision about master data, not about this field.

``read_only`` because on this site the value is a stamp carried down from the order, never user
input, so it must not drift from its source.

THE field_order TRAP
--------------------
Quotation and Sales Order have both been through Customize Form, so each carries a DocType-level
``field_order`` Property Setter which OUTRANKS ``insert_after``. Creating the Custom Field alone
would give the column but leave the field invisible on the form. See ``field_order_utils`` -- both
must be written, and this patch writes both.

Wired in the same two idempotent places as its siblings: this ``post_model_sync`` patch and
``create_test_data.setup_data``. That second wiring is not optional -- ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites.

Must run AFTER ``add_order_sales_flow_type_fields``: ``custom_flow_type`` is this patch's
``insert_after`` anchor on both doctypes.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_design_type_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``, and the Property Setter and
``insert_after`` chain are recomputed to the same values on every run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	rewrite_field_order,
	rewrite_insert_after_chain,
)

_DESIGN_TYPE_FIELD = {
	"fieldname": "custom_design_type",
	"fieldtype": "Link",
	"options": "Attribute Value",
	"label": "Design Type",
	"insert_after": "custom_flow_type",
	"module": "Jewellery Erpnext",
	"read_only": 1,
	"in_standard_filter": 1,
	# Deliberately NOT no_copy: get_mapped_doc's generic same-fieldname copy is what carries this
	# from the Quotation to the Sales Order, and map_fields() skips no_copy source fields. That is
	# also why both doctypes use the identical fieldname -- no bridge in doc_events is needed, for
	# the same reason fetch_sales_type_from_quotation needs none for Flow Type.
}

CUSTOM_FIELDS = {
	"Quotation": [dict(_DESIGN_TYPE_FIELD)],
	"Sales Order": [dict(_DESIGN_TYPE_FIELD)],
}

# Per doctype: the ordered block to splice into `field_order`. Each block starts with an EXISTING
# field so rewrite_field_order has an anchor already present in the Property Setter's list.
FIELD_ORDER_BLOCKS = {
	"Quotation": ["custom_flow_type", "custom_design_type"],
	"Sales Order": ["custom_flow_type", "custom_design_type"],
}


def ensure_design_type_fields():
	"""Create / refresh the two custom fields."""
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	frappe.logger().info(
		"add_design_type_fields: ensured "
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
			f"add_design_type_fields: placed {', '.join(block[1:])} after {block[0]} on "
			f"{doctype} (field_order property setter="
			f"{'updated' if had_property_setter else 'absent'})"
		)


def execute():
	ensure_design_type_fields()
	place_fields_after_their_anchors()
	frappe.db.commit()
