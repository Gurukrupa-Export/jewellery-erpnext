"""Add the Material Request fields behind the "Transfer to Department" route.

``custom_operation_type`` (a ``Select`` of ``Transfer to MOP`` / ``Transfer to
Department``, owned by ``gke_customization``) has been inert since it was added: nothing
on the server reads it, nothing writes ``Transfer to MOP``, and every one of the 8140
Material Requests carries that value only because frappe hands a defaultless ``Select``
its first option (``frappe/model/create_new.py`` ``get_default_value``). This patch
provisions the schema that makes the second option real.

Three new fields, all ``allow_on_submit`` so the operator can set them on a *submitted*
request and commit them with **Update** before firing the workflow action:

* ``custom_destination_department`` — where the material is headed.
* ``custom_destination_warehouse``  — the receiving warehouse, target of the Stock Entry.
* ``custom_department_transfer_se`` — the Stock Entry that was created, and the
  idempotency guard that stops ``before_update_after_submit`` minting a second one on a
  re-save in the same workflow state.

The first two are deliberately NOT ``custom_department``. That field is already set on
7776 of 7776 Manufacture requests -- it is the Parent Manufacturing Order's *source*
(bagging) department -- and ``doc_events/material_request.before_update_after_submit``
branches on it to choose between ``make_department_mop_stock_entry`` and
``make_mop_stock_entry``. Reusing it would pre-fill the wrong value, make the new
mandatory rule vacuous, and silently flip that dispatch.

Two edits to fields ``gke_customization`` owns:

* ``custom_operation_type`` gains an explicit ``default``. The behaviour is unchanged --
  the first-option rule already produced it -- but the rule only fires through
  ``new_doc``; ``get_mapped_doc`` and ``frappe.get_doc({...})`` construction bypass it.
* ``custom_manufacturing_operation`` stops being mandatory on a department-bound request.
  It is currently required in ``Material Transferred`` unconditionally, which would force
  the operator to name a Manufacturing Operation they are deliberately not using.

Those two are written here as well as in
``gke_customization/gke_customization/fixtures/custom_field.json`` so the site picks them
up from whichever app migrates first, and so a later ``gke_customization`` fixture import
cannot revert them. Keep the two copies in step.

Why a patch and not ``custom_fields/material_request.json``: this app's ``after_migrate``
hook is commented out (``hooks.py``), so its ``custom_fields/*.json`` never reach a real
site -- the recurring patch-only custom-field gap. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_mr_department_transfer_fields.execute

Idempotent: ``create_custom_fields`` updates in place, and the property writes are
unconditional assignments of a fixed value.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DEPARTMENT_ROUTE = 'eval:doc.custom_operation_type == "Transfer to Department"'

# Visibility is deliberately wider than the route itself. Operation Type is
# ``allow_on_submit``, so it can be switched back to "Transfer to MOP" after a transfer has
# already happened -- and keying display on the route alone would then hide the record of
# where the material actually went. Any value present keeps both fields on show.
DISPLAY_ROUTE = (
	'eval:doc.custom_operation_type == "Transfer to Department"'
	" || doc.custom_destination_department"
	" || doc.custom_destination_warehouse"
)

# Once the Stock Entry exists the two fields describe a submitted document. Editing them
# would let the request contradict it, and the maker's idempotency guard means no corrected
# entry is ever made. Mirrors custom_manufacturing_operation locking in the MOP state.
TRANSFER_DONE = "eval:doc.custom_department_transfer_se"

# The MOP-side conditions are phrased as "not the department route" rather than
# '== "Transfer to MOP"' so a NULL custom_operation_type -- possible on any row created
# before the field existed -- still resolves to the Transfer to MOP behaviour.
NOT_DEPARTMENT_ROUTE = 'doc.custom_operation_type != "Transfer to Department"'

CUSTOM_FIELDS = {
	"Material Request": [
		{
			"fieldname": "custom_destination_department",
			"label": "Destination Department",
			"fieldtype": "Link",
			"options": "Department",
			"insert_after": "custom_operation_type",
			"depends_on": DISPLAY_ROUTE,
			"mandatory_depends_on": DEPARTMENT_ROUTE,
			"read_only_depends_on": TRANSFER_DONE,
			"allow_on_submit": 1,
			"no_copy": 1,
			"module": "Jewellery Erpnext",
			"description": (
				"Department the material is being transferred to. Only used when "
				"Operation Type is Transfer to Department."
			),
		},
		{
			"fieldname": "custom_destination_warehouse",
			"label": "Destination Warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"insert_after": "custom_destination_department",
			"depends_on": DISPLAY_ROUTE,
			"mandatory_depends_on": DEPARTMENT_ROUTE,
			"read_only_depends_on": TRANSFER_DONE,
			"allow_on_submit": 1,
			"no_copy": 1,
			"module": "Jewellery Erpnext",
			"description": (
				"Warehouse in the destination department that receives the material. "
				"The Stock Entry runs Target Warehouse -> this warehouse."
			),
		},
		{
			"fieldname": "custom_department_transfer_se",
			"label": "Department Transfer SE",
			"fieldtype": "Link",
			"options": "Stock Entry",
			"insert_after": "custom_mop_se",
			"read_only": 1,
			"allow_on_submit": 1,
			"print_hide": 1,
			"no_copy": 1,
			"module": "Jewellery Erpnext",
			"description": (
				"Material Transfer (DEPARTMENT) Stock Entry created by the Transfer to "
				"Department action. Its presence stops a second one being created."
			),
		},
	]
}

# {Custom Field name: {property: value}} for the two gke_customization-owned fields.
PROPERTY_UPDATES = {
	"Material Request-custom_operation_type": {
		"default": "Transfer to MOP",
	},
	# "Material Transferred to Department" is in both lists because the department route is
	# not terminal: from there the operator flips Operation Type back and hands the material
	# to an operation. The NOT_DEPARTMENT_ROUTE clause keeps the field hidden and optional
	# while the request is still sitting on the department route, so it appears -- and only
	# then becomes required -- at the moment the MOP route is chosen.
	"Material Request-custom_manufacturing_operation": {
		"depends_on": (
			'eval:(doc.workflow_state == "Material Transferred" || '
			'doc.workflow_state == "Material Transferred to Department" || '
			'doc.workflow_state == "Material Transferred to MOP") && '
			f"{NOT_DEPARTMENT_ROUTE};"
		),
		"mandatory_depends_on": (
			'eval:(doc.workflow_state == "Material Transferred" || '
			'doc.workflow_state == "Material Transferred to Department") && '
			f"{NOT_DEPARTMENT_ROUTE};"
		),
	},
}


def execute():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)

	updated = []
	for name, values in PROPERTY_UPDATES.items():
		if not frappe.db.exists("Custom Field", name):
			# gke_customization is not installed / not migrated yet. The new fields above
			# stand on their own; this half is re-applied on the next run.
			continue
		# db.set_value rather than a doc save: Custom Field.on_update re-runs
		# insert_after resolution for the whole DocType, which is a much larger blast
		# radius than the two property strings this patch actually changes.
		frappe.db.set_value("Custom Field", name, values, update_modified=False)
		updated.append(name)

	if updated:
		frappe.clear_cache(doctype="Material Request")

	frappe.db.commit()
	frappe.logger().info(
		"add_mr_department_transfer_fields: created 3 Material Request fields, "
		f"updated {len(updated)} existing field(s): {', '.join(updated) or 'none'}"
	)
