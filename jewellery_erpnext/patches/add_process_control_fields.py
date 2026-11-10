"""Install the Department switch for process control (manufacturing cells).

``Department.custom_enable_process_control`` turns process control on for one department. A
department can only switch it on once its processes and operation mappings are complete
(``doc_events.process_control.validate_department``). It is off for every existing department,
so installing it changes nothing until someone enables it.

``custom_fields/department.json`` reaches only fresh installs: the after_migrate loader is off.
This patch reaches existing sites, and ``create_test_data`` calls it for CI sites, because
install-app marks patches complete without running them.

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``. Ad hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_process_control_fields.execute
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

PROCESS_CONTROL_FIELDS = {
	"Department": [
		{
			"fieldname": "custom_process_control_section",
			"fieldtype": "Section Break",
			"label": "Process Control",
			"insert_after": "custom_is_chain_subcontracting",
			"module": "Jewellery Erpnext",
		},
		{
			"fieldname": "custom_enable_process_control",
			"fieldtype": "Check",
			"label": "Enable Process Control",
			"description": (
				"Track which Department Process each work order is in and move work orders between "
				"processes with Process Transfer. Needs at least one enabled Department Process and a "
				"Process on every Department Operation of this department."
			),
			"insert_after": "custom_process_control_section",
			"module": "Jewellery Erpnext",
		},
	]
}


def execute():
	create_custom_fields(PROCESS_CONTROL_FIELDS, update=True)
