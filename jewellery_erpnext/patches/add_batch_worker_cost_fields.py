"""Provision the Batch Worker Cost fields for Finding Manufacturing - Without SO.

* ``Batch.custom_worker_cost`` -- the labour cost of the work order that produced the batch:
  each operation's Employee IR working hours x the employee's Workstation Hour Rate, summed.
* ``Batch.custom_total_cost`` -- Batch Rate (``custom_metal_rate``) + Worker Cost, as the
  Without-SO specification defines it.

Both are written by ``employee_ir/doc_events/without_so_costing.apply_worker_cost_to_batch``
and only for batches of ``is_without_so`` work orders.

Also declared in ``custom_fields/batch.json``, but that is dead config on real and CI sites
(the ``after_migrate`` hook that would sync it is disabled in ``hooks.py``), so this
``post_model_sync`` patch is what creates them; ``create_test_data`` calls it for the CI
``test_site``. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_batch_worker_cost_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Batch": [
			{
				"fieldname": "custom_batch_worker_cost_section",
				"fieldtype": "Section Break",
				"label": "Batch Worker Cost",
				"insert_after": "custom_alloy_rate",
				"depends_on": "eval:doc.custom_worker_cost",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_worker_cost",
				"fieldtype": "Currency",
				"label": "Worker Cost",
				"insert_after": "custom_batch_worker_cost_section",
				"description": "Cost incurred based on employee working time.",
				"read_only": 1,
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_total_cost",
				"fieldtype": "Currency",
				"label": "Total Cost",
				"insert_after": "custom_worker_cost",
				"description": "Batch Rate + Worker Cost.",
				"read_only": 1,
				"no_copy": 1,
				"module": "Jewellery Erpnext",
			},
		],
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_batch_worker_cost_fields: ensured Batch.custom_worker_cost and custom_total_cost"
	)
