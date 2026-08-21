"""Provision the ``Batch.custom_employee`` marker used by employee-wise refining.

Dust and Scrap Refining can be scoped to a single Employee: the operator picks an
Employee on the Refining Entry and only that employee's scrap/dust batches are
fetched. The link lives on the batch — stamped when the scrap/dust batch is minted
(Employee Loss Entry, Employee IR Process Loss, and the "Receive Scrap Item"
Manufacturing Operation action). ``RefiningEntry.get_scrap_items_balance`` /
``_dust_employee_batch_rows`` then filter on ``Batch.custom_employee``. This is
forward-only: batches minted before the field existed carry no employee and are
simply excluded when an employee is selected.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach
the DB and ``batch.custom_employee = ...`` would raise ``1054 Unknown column``.
Per the app convention this mirrors ``add_batch_scrap_type_field``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_batch_employee_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Batch": [
			{
				"fieldname": "custom_employee",
				"fieldtype": "Link",
				"options": "Employee",
				"label": "Employee",
				"insert_after": "custom_customer",
				"read_only": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info("add_batch_employee_field: ensured Batch.custom_employee")
