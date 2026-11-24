"""Provision ``Manufacturer.custom_refinery_department`` for the PMO Refinery Work Order.

The department the manufacturer runs its refinery process in. Create Refinery Work Order is
offered on a PMO only when one of its work orders is in this department, and the Refinery
Work Order is created and kept there (``manufacturing_work_order/doc_events/refinery_mwo.py``).

Wired in two idempotent places, per the app convention (``after_migrate`` is disabled and
``install-app`` marks patches complete without running them): this patch and
``create_test_data.setup_data``. Idempotent: ``create_custom_fields`` keys on
``(dt, fieldname)``.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

REFINERY_WORK_ORDER_FIELDS = {
	"Manufacturer": [
		{
			"fieldname": "custom_refinery_department",
			"fieldtype": "Link",
			"label": "Refinery Department",
			"options": "Department",
			"insert_after": "custom_central_department",
			"module": "Jewellery Erpnext",
			"description": (
				"Create Refinery Work Order is shown on a Parent Manufacturing Order only when "
				"one of its work orders is in this department."
			),
		},
	],
}


def execute():
	create_custom_fields(REFINERY_WORK_ORDER_FIELDS, ignore_validate=True)
