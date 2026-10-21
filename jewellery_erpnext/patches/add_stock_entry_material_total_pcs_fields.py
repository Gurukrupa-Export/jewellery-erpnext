"""Add Total Diamond Pcs / Total Gemstone Pcs to the Stock Entry header.

Stock Entry already carries four material totals -- ``custom_total_metal_weight``,
``custom_total_diamond_weight``, ``custom_total_finding_weight`` and
``custom_total_gemstone_weight`` -- as Custom Fields shipped by ``gke_customization``
(module *GKE Order Forms*). Stones are also counted, not just weighed, so the block needs
a pcs twin for diamond and gemstone. Metal and finding have no pcs counterpart: they are
weighed material, and every other weight surface in the app (Manufacturing Operation,
MOP Log) likewise keeps ``*_pcs`` only for diamond and gemstone.

``Int`` rather than the ``Data`` that ``Stock Entry Detail.pcs`` uses. The per-row field
is Data for historical reasons and its values are cast with ``cint`` on the way in (see
``customization/utils/material_weights``); a header total has no reason to inherit that.

This app's ``custom_fields/*.json`` are dead config -- the ``after_migrate`` hook that
would sync them is commented out (``hooks.py`` declares neither ``fixtures`` nor
``custom_fields``) -- so a patch is the only way these reach a site. Idempotent (keyed on
``(dt, fieldname)``). Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_stock_entry_material_total_pcs_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Stock Entry"

# The last of the four weight fields gke_customization ships. Anchoring here appends the
# pcs pair to that block rather than starting a second one elsewhere on the form.
ANCHOR = "custom_total_gemstone_weight"

FIELDS = (
	("custom_total_diamond_pcs", "Total Diamond Pcs"),
	("custom_total_gemstone_pcs", "Total Gemstone Pcs"),
)


def _fields():
	"""The block, each field anchored on its predecessor.

	Chained rather than both naming ``ANCHOR``: when several Custom Fields share one
	anchor, ``Meta.sort_fields`` breaks the tie on Custom Field ``idx``, which is assigned
	per site -- so the pair would order differently on different installs.
	"""
	out = []
	previous = ANCHOR
	for fieldname, label in FIELDS:
		out.append(
			{
				"fieldname": fieldname,
				"fieldtype": "Int",
				"label": label,
				"insert_after": previous,
				# Server-derived on every save by material_weights.set_material_totals;
				# a typed value would be overwritten at the next validate anyway.
				"read_only": 1,
				"module": "Jewellery Erpnext",
				# Explicitly blank, not omitted: create_custom_fields updates a
				# pre-existing field with ``custom_field.update(df)``, which only touches
				# the keys present. Omitting it would strand any description a future
				# revision of this patch had shipped.
				"description": "",
			}
		)
		previous = fieldname
	return out


def execute():
	create_custom_fields({DOCTYPE: _fields()}, ignore_validate=True)

	# A stale insert_after Property Setter would override the anchors set above.
	for fieldname, _label in FIELDS:
		frappe.db.delete(
			"Property Setter",
			{
				"doc_type": DOCTYPE,
				"field_name": fieldname,
				"property": "insert_after",
			},
		)
	frappe.clear_cache(doctype=DOCTYPE)
