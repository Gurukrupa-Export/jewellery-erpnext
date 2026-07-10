"""Provision the ``Warehouse.custom_msl_tracking`` child table (req #7 / #8).

Employee (MSL) warehouses display a maintained item-wise Issue / Receive / Loss /
Pending table (``Warehouse MSL Tracking`` child doctype), recomputed from the
ledger by ``doc_events/warehouse_tracking.recalculate_msl_tracking`` (a form
button + auto after an Employee Loss Entry).

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only field would never reach
the DB and ``doc.append("custom_msl_tracking", ...)`` would be a silent no-op. Per
the app convention (schema fixes go through a guard called from both a patch AND
setup_data), this is wired in two idempotent places: this ``post_model_sync``
patch (existing-site migrate) and ``create_test_data.setup_data`` (fresh / CI).
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_warehouse_msl_tracking_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``. Requires the
``Warehouse MSL Tracking`` child doctype to already be synced (it is, in
post_model_sync).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Warehouse": [
			{
				"fieldname": "custom_msl_tracking_section",
				"fieldtype": "Section Break",
				"label": "MSL Tracking",
				"insert_after": "warehouse_type",
				"depends_on": "eval:doc.employee && doc.warehouse_type=='Raw Material'",
				"collapsible": 1,
			},
			{
				"fieldname": "custom_msl_tracking",
				"fieldtype": "Table",
				"options": "Warehouse MSL Tracking",
				"label": "Item-wise Tracking",
				"insert_after": "custom_msl_tracking_section",
				"read_only": 1,
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_warehouse_msl_tracking_field: ensured Warehouse.custom_msl_tracking"
	)
