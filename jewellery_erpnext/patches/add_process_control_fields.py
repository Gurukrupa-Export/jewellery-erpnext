"""Install the process-control custom fields (manufacturing cells).

``Department.custom_enable_process_control`` turns process control on for one department. A
department can only switch it on once its processes and operation mappings are complete
(``doc_events.process_control.validate_department``). It is off for every existing department,
so installing it changes nothing until someone enables it. ``Stock Entry.custom_department_process``
(Day 3) records the process a work-order material movement was made in.

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
	],
	# Day 3 (GK-PROC-0013): the process a work-order material movement is made in. Pre-filled from
	# the user's default process, changeable among their granted processes, checked against the
	# job's process on the server, and stamped with it when left empty.
	"Stock Entry": [
		{
			"fieldname": "custom_department_process",
			"fieldtype": "Link",
			"label": "Department Process",
			"options": "Department Process",
			"insert_after": "manufacturing_operation",
			"description": (
				"The process this material movement is made in. In departments that use process "
				"control it must be the work order's current process, and you need access to it."
			),
			"no_copy": 1,
			"module": "Jewellery Erpnext",
		},
	],
}


def execute():
	create_custom_fields(PROCESS_CONTROL_FIELDS, update=True)
