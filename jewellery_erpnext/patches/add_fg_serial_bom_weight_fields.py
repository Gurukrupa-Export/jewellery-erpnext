"""Add the FG-serial BOM weight fields to Material Request Item and Stock Entry Detail.

Scanning an FG serial on a Material Request now produces one row per serial, and each
row shows the weights of that piece's own as-built BOM (``Serial No.custom_bom_no``).
The same block is carried onto the Stock Entry built by "Material Transfer (In
Transit)". The field list and the BOM columns behind it live in
``customization/utils/bom_weights.py``; this patch only provisions the columns.

**The same fieldname is used on both doctypes on purpose.** ``frappe.model.mapper``
copies any same-named target field whose source field is not ``no_copy``
(``mapper.map_fields``), so Material Request Item -> Stock Entry Detail propagation
needs no mapping code at all. Consequently none of these fields may be given
``no_copy`` -- that would silently break the propagation.

Naming: the ``custom_bom_`` prefix, rather than the ``custom_gross_weight`` /
``custom_metal_weight`` set that already exists on Sales Invoice Item. Those names are
taken there for *different* BOM columns (``total_metal_weight``, ``finding_weight``,
``total_diamond_weight_in_gms``), and ``Stock Entry Detail`` separately already owns a
plain ``gross_weight``. A distinct prefix keeps all three sets legible.

This app's ``custom_fields/*.json`` are dead config -- the ``after_migrate`` hook that
would sync them is commented out (``hooks.py``) and the ``Custom Field`` fixture is a
fixed allow-list -- so a patch is the only way these reach a site. Idempotent (keyed on
``(dt, fieldname)``). Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_fg_serial_bom_weight_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.jewellery_erpnext.customization.utils.bom_weights import (
	BOM_WEIGHT_FIELDS,
)

# Bare labels, no unit suffix and no description -- the grid column header is narrow and
# BOM's own "(In Gram)" / "(In Carat)" wording crowds it out. Units follow the BOM columns
# these mirror: gross/metal/finding are grams, diamond/gemstone are carats. The
# fieldname -> BOM column mapping is in customization/utils/bom_weights.py.
LABELS = {
	"custom_bom_gross_weight": "Gross Weight",
	"custom_bom_metal_weight": "Metal Weight",
	"custom_bom_finding_weight": "Finding Weight",
	"custom_bom_diamond_weight": "Diamond Weight",
	"custom_bom_total_diamond_pcs": "Diamond Pcs",
	"custom_bom_gemstone_weight": "Gemstone Weight",
	"custom_bom_total_gemstone_pcs": "Gemstone Pcs",
}

INT_FIELDS = {"custom_bom_total_diamond_pcs", "custom_bom_total_gemstone_pcs"}

# Where the block starts on each doctype. Note the Stock Entry Detail anchor is itself
# broken upstream: custom_is_customer_item is declared insert_after "is_scrap_item", a
# field ERPNext v16 removed from Stock Entry Detail, so frappe.model.meta appends it to
# the end of the form. The block therefore follows it at the tail. That is deliberate --
# re-anchoring custom_is_customer_item would move an existing field for every user and
# is a separate decision.
ANCHORS = {
	"Material Request Item": "custom_variant_of",
	"Stock Entry Detail": "custom_is_customer_item",
}


def _fields_for(anchor):
	"""The block, each field anchored on its predecessor.

	Chained rather than all sharing one ``insert_after``: when several custom fields
	name the same anchor, ``Meta.sort_fields`` breaks the tie on Custom Field ``idx``,
	which is assigned per site and would order the block differently per install.
	"""
	fields = []
	previous = anchor
	for fieldname in BOM_WEIGHT_FIELDS:
		field = {
			"fieldname": fieldname,
			"fieldtype": "Int" if fieldname in INT_FIELDS else "Float",
			"label": LABELS[fieldname],
			"insert_after": previous,
			"read_only": 1,
			"print_hide": 1,
			"module": "Jewellery Erpnext",
			# Explicitly blank, not omitted: create_custom_fields updates a pre-existing
			# field with `custom_field.update(df)`, which only touches the keys present.
			# An earlier revision of this patch shipped a description, so omitting the key
			# would strand that text on any site that already ran it.
			"description": "",
		}
		if fieldname not in INT_FIELDS:
			# Explicit precision rather than the system default: this app has a
			# documented history of flt(x, 2) rounding a small weight to 0.0 and
			# failing a submit (see property_setter_guard).
			field["precision"] = "3"
		fields.append(field)
		previous = fieldname

	return fields


def execute():
	custom_fields = {dt: _fields_for(anchor) for dt, anchor in ANCHORS.items()}
	create_custom_fields(custom_fields, ignore_validate=True)

	# custom_sub_setting_type also names custom_is_customer_item as its anchor, so it
	# would otherwise race the new block for the same slot. Re-point it at the tail of
	# the block. Direct db.set_value, as in move_quotation_invoice_item: Property
	# Setters do not re-order Custom Fields.
	last_field = list(BOM_WEIGHT_FIELDS)[-1]
	if frappe.db.exists(
		"Custom Field",
		{"dt": "Stock Entry Detail", "fieldname": "custom_sub_setting_type"},
	):
		frappe.db.set_value(
			"Custom Field",
			{"dt": "Stock Entry Detail", "fieldname": "custom_sub_setting_type"},
			"insert_after",
			last_field,
		)

	# A stale insert_after Property Setter would override the anchors set above.
	for doctype in ANCHORS:
		frappe.db.delete(
			"Property Setter",
			{
				"doc_type": doctype,
				"field_name": ["in", list(BOM_WEIGHT_FIELDS)],
				"property": "insert_after",
			},
		)
		frappe.clear_cache(doctype=doctype)

	frappe.logger().info(
		"add_fg_serial_bom_weight_fields: FG BOM weight fields created/updated on "
		"Material Request Item and Stock Entry Detail"
	)
