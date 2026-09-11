"""Carry Order Type / Sales Type / Flow Type from the Quotation down to the finished Serial No.

Order Type already made this whole walk; Sales Type stopped at the Sales Order and Flow Type did
not exist on the chain at all. This patch adds the missing custom fields on the four STOCK doctypes
in the chain. The four app-owned ones (Manufacturing Plan, Parent Manufacturing Order,
Manufacturing Work Order, Serial Number Creator) declare theirs in their own ``.json`` and arrive
with ``bench migrate`` -- they are deliberately not repeated here.

WHY A PATCH AND NOT A FIXTURE
-----------------------------
This app's ``after_migrate`` is commented out (``hooks.py:12``) and ``migrate.after_migrate()`` is
the only reader of ``custom_fields/*.json``, so those files never reach a real site. The ``fixtures``
Custom Field entry (``hooks.py:459``) is a ten-name allowlist, and ``gke_customization``'s Custom
Field fixture is scoped to its own records. A patch is the only delivery mechanism. Note also that
``bench migrate`` runs patches BEFORE importing fixtures (``frappe/migrate.py:278``), so a fixture
would win over anything written here -- none of these six fieldnames appears in any fixture.

WHY EVERY FIELD IS Data, NOT Select
-----------------------------------
The chain's ultimate source is the GKExport site, reached over REST by the ``get_purchase_order_items``
Server Script -- so the Flow Type option list lives on ANOTHER SITE and cannot be seen from here.
A hard-coded Select would silently drop any value that site adds (the browser clears an invalid
Select) and would make ``_validate_selects`` throw on save. This is the same reasoning that already
makes every downstream copy of ``order_type`` a Data field rather than a Select, one site earlier in
the chain. ``read_only`` for the matching reason: on this site the three are a stamp carried down
from the order, never user input, so they must not drift from their source.

THE field_order TRAP
--------------------
Quotation, Sales Order, Material Request and Serial No have all been through Customize Form, so each
carries a DocType-level ``field_order`` Property Setter which OUTRANKS ``insert_after``. Creating the
Custom Field alone would give the column but leave the field invisible on the form. See
``field_order_utils`` -- both must be written, and this patch writes both.

Wired in the same two idempotent places as its siblings (``add_serial_no_order_type_field``,
``add_serial_no_sales_reference_fields``): this ``post_model_sync`` patch and
``create_test_data.setup_data``. That second wiring is not optional -- ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites, so a patch-only column would be missing there and
the ``frappe.db.set_value("Serial No", ..., "custom_sales_type", ...)`` in
``manufacturing_operation.create_manufacturing_entry`` would raise ``1054 Unknown column``.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_order_sales_flow_type_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``, and the Property Setter and
``insert_after`` chain are recomputed to the same values on every run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	rewrite_field_order,
	rewrite_insert_after_chain,
)

# Why Data and not Select: see the module docstring.
_WHY_DATA = (
	"Stamped from the order chain, not entered here. Data rather than Select because the option "
	"list is owned by the GKExport site this value arrives from, so a copy hard-coded on this "
	"site would start rejecting orders the day that list is edited."
)

CUSTOM_FIELDS = {
	"Quotation": [
		{
			"fieldname": "custom_flow_type",
			"fieldtype": "Data",
			"label": "Flow Type",
			"insert_after": "custom_sales_type",
			"module": "Jewellery Erpnext",
			"read_only": 1,
			"in_standard_filter": 1,
			"description": _WHY_DATA,
			# Deliberately NOT no_copy: get_mapped_doc's generic same-fieldname copy is what
			# carries this to the Sales Order, and map_fields() skips no_copy source fields.
		}
	],
	"Sales Order": [
		{
			"fieldname": "custom_flow_type",
			"fieldtype": "Data",
			"label": "Flow Type",
			"insert_after": "sales_type",
			"module": "Jewellery Erpnext",
			"read_only": 1,
			"in_standard_filter": 1,
			"description": _WHY_DATA,
		}
	],
	# Material Request already links to the PMO through the gke fixture field
	# `manufacturing_order`, and already carries custom_order_type fetched over that link. These
	# two ride the same link, so no Python is needed on the PMO -> MR creation path.
	"Material Request": [
		{
			"fieldname": "custom_sales_type",
			"fieldtype": "Link",
			"label": "Sales Type",
			"options": "Sales Type",
			"fetch_from": "manufacturing_order.sales_type",
			"insert_after": "custom_order_type",
			"module": "Jewellery Erpnext",
			"read_only": 1,
		},
		{
			"fieldname": "custom_flow_type",
			"fieldtype": "Data",
			"label": "Flow Type",
			"fetch_from": "manufacturing_order.flow_type",
			"insert_after": "custom_sales_type",
			"module": "Jewellery Erpnext",
			"read_only": 1,
		},
	],
	# Serial No is stamped imperatively (manufacturing_operation.create_manufacturing_entry), not
	# fetched, because it is created by the Stock Entry / Serial and Batch Bundle machinery and has
	# no link back to the Serial Number Creator to fetch over. no_copy: an amended serial must not
	# inherit another piece's sales document.
	"Serial No": [
		{
			"fieldname": "custom_sales_type",
			"fieldtype": "Data",
			"label": "Sales Type",
			"insert_after": "custom_order_type",
			"module": "Jewellery Erpnext",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"description": (
				"Sales Type of the Sales Order this piece was manufactured against, stamped from "
				"the Serial Number Creator at submit. Distinct from Ownership Tag, which is only "
				"seeded from Sales Type and is meant to be overwritten by the ledger-derived value."
			),
		},
		{
			"fieldname": "custom_flow_type",
			"fieldtype": "Data",
			"label": "Flow Type",
			"insert_after": "custom_sales_type",
			"module": "Jewellery Erpnext",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"description": _WHY_DATA,
		},
	],
}

# Per doctype: the ordered block to splice into `field_order`. Each block starts with an EXISTING
# field so rewrite_field_order has an anchor already present in the Property Setter's list.
FIELD_ORDER_BLOCKS = {
	"Quotation": ["custom_sales_type", "custom_flow_type"],
	"Sales Order": ["sales_type", "custom_flow_type"],
	"Material Request": ["custom_order_type", "custom_sales_type", "custom_flow_type"],
	"Serial No": ["custom_order_type", "custom_sales_type", "custom_flow_type"],
}


def ensure_order_sales_flow_type_fields():
	"""Create / refresh the six custom fields."""
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	frappe.logger().info(
		"add_order_sales_flow_type_fields: ensured "
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
			f"add_order_sales_flow_type_fields: placed {', '.join(block[1:])} after "
			f"{block[0]} on {doctype} (field_order property setter="
			f"{'updated' if had_property_setter else 'absent'})"
		)


def execute():
	ensure_order_sales_flow_type_fields()
	place_fields_after_their_anchors()
	frappe.db.commit()
