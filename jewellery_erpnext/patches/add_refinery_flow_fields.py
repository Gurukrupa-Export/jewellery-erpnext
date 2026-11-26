"""Provision the Department Refinery Flow and the consolidated refining batch fields.

- ``Department.custom_enable_refinery_flow`` — switches the department-specific refinery
  process on: a Scrap Refining entry from this department must carry ALL the scrap in its
  Scrap warehouse, and the refining consumables travel on that same entry
  (``RefiningEntry.validate_refinery_flow``). Off = today's behaviour.
- The Refinery Department is not configured per department: Refining Entry takes the
  company's Refinery department (``get_refinery_department``). An earlier version of this
  patch added ``Department.custom_refinery_department``; it is removed where it exists.
- ``Batch.custom_refining_entry`` / ``Batch.custom_refining_loss`` — stamped on the one
  batch per item that ``RefiningEntry.consolidate_refinery_batches`` mints from every batch
  sent for refining, and the share of the refining loss booked against it.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, this is wired in two idempotent places, per the app
convention: this patch and ``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_refinery_flow_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Department": [
			{
				"fieldname": "custom_refinery_flow_section",
				"fieldtype": "Section Break",
				"label": "Refinery Flow",
				"insert_after": "custom_department_ir_work_order_limit",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_enable_refinery_flow",
				"fieldtype": "Check",
				"label": "Enable Refinery Flow",
				"insert_after": "custom_refinery_flow_section",
				"module": "Jewellery Erpnext",
				"description": (
					"Scrap Refining from this department must send all the scrap in its Scrap "
					"warehouse, with the refining consumables on the same entry."
				),
			},
		],
		"Batch": [
			{
				"fieldname": "custom_refining_entry",
				"fieldtype": "Link",
				"label": "Refining Entry",
				"options": "Refining Entry",
				"insert_after": "custom_company",
				"read_only": 1,
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_refining_loss",
				"fieldtype": "Float",
				"label": "Refining Loss",
				"precision": "3",
				"insert_after": "custom_refining_entry",
				"read_only": 1,
				"module": "Jewellery Erpnext",
				"description": "Refining loss (pure weight) booked against this consolidated refining batch.",
			},
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	if frappe.db.exists("Custom Field", "Department-custom_refinery_department"):
		frappe.delete_doc(
			"Custom Field",
			"Department-custom_refinery_department",
			ignore_permissions=True,
		)
	frappe.logger().info(
		"add_refinery_flow_fields: ensured Department refinery flow and Batch refining fields"
	)
