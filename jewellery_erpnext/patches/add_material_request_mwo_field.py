"""Ensure ``Material Request.custom_manufacturing_work_order`` exists.

Splitting a Manufacturing Work Order mints one new Material Request per split MWO
and cancels the original pre-split MRD. Both halves of that hand-off key off this
field, and neither worked because the field was never created on Material Request:

* ``manufacturing_work_order.create_mr_for_split_work_order`` stamps the new MR
  with ``new_mr.custom_manufacturing_work_order = docname``. With no such field in
  the meta, ``get_valid_dict`` drops the attribute on save -- a silent no-op, so
  the tag was never persisted.
* ``manufacturing_work_order.create_split_work_order`` then filters
  ``{"custom_manufacturing_work_order": ["is", "not set"]}`` to find the ORIGINAL
  MRDs (the ones no split MWO has claimed) and cancel them. On v16 that filter
  does not degrade quietly: ``frappe.db.get_list`` raises ``PermissionError: You
  do not have permission to access field`` because an unknown field can never be
  in the permitted set. ``frappe.get_all`` on the same filter gives the honest
  ``OperationalError: Unknown column``.

The field is declared for ``Stock Entry Detail`` and ``Stock Entry MOP Item``
under ``custom_fields/``, but never for ``Material Request`` -- and the 40 Material
Request custom fields that do exist are all owned by ``gke_customization``'s
module-scoped ``custom_field`` fixture, which this one is not part of.

Why a patch and not ``custom_fields/material_request.json``: this app's
``after_migrate`` hook is disabled (hooks.py), so its ``custom_fields/*.json`` are
never applied by ``bench migrate`` -- the same patch-only custom-field gap
documented in ``add_conversion_lane_tag_field``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_material_request_mwo_field.execute

Note this patch is what ACTIVATES the MRD cancellation in ``create_split_work_order``:
until the column exists that code path cannot run at all. Existing Material Requests
all start with the field empty, so ``["is", "not set"]`` matches every prior MRD of
the parent PMO.

Idempotent: guarded on ``frappe.db.has_column``.
"""

import frappe

FIELD = {
	"fieldname": "custom_manufacturing_work_order",
	"label": "Manufacturing Work Order",
	"fieldtype": "Link",
	"options": "Manufacturing Work Order",
	"insert_after": "manufacturing_order",
	"is_system_generated": 1,
	"read_only": 1,
	"module": "Jewellery Erpnext",
	"description": (
		"Set by a Manufacturing Work Order split on the Material Request minted for "
		"the new MWO. Blank means this MRD predates the split and is the one the "
		"split cancels."
	),
}


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	if frappe.db.has_column("Material Request", FIELD["fieldname"]):
		return

	create_custom_fields({"Material Request": [FIELD]}, ignore_validate=True)
	frappe.db.commit()
	frappe.logger().info(
		"add_material_request_mwo_field: created Material Request.custom_manufacturing_work_order"
	)
