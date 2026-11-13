# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Read-only readiness report for switching process control on in a department (Day 4).

It reads and reports; it writes nothing. Existing work orders get their first process only through a
Process Transfer marked Initial Process Assignment (GK-DEC-0004); department name, employee, default
process or last operation are never used to guess one.

    bench --site <site> execute jewellery_erpnext.jewellery_erpnext.doc_events.process_readiness.readiness_report --kwargs "{'department': '<Department>'}"
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	DEPARTMENT_FLAG,
	department_flag_installed,
	get_active_operations,
	is_pilot_enabled,
	readiness_problem,
	report_active_jobs_without_process,
)

SAMPLE = 20


def readiness_report(department: str) -> dict:
	"""What stands between ``department`` and running with process control on.

	``blockers`` lists what must be settled first; an empty list means the configuration is
	complete and every active job already has a process."""
	frappe.only_for(("System Manager", "Process Access Manager"))
	if not department or not frappe.db.exists("Department", department):
		frappe.throw(frappe._("Department {0} does not exist.").format(department))

	processes = frappe.get_all(
		"Department Process",
		filters={"department": department},
		fields=["name", "process_name", "disabled"],
		order_by="process_name asc",
	)
	operations = frappe.get_all(
		"Department Operation",
		filters={"department": department},
		fields=["name", "department_process"],
		order_by="name asc",
	)
	access = frappe.get_all(
		"User Process Access",
		filters={"department": department, "enabled": 1},
		fields=["department_process", "is_default"],
	)
	unassigned = report_active_jobs_without_process(department)
	states = get_active_operations([r.manufacturing_work_order for r in unassigned])
	ready_now, not_at_rest = [], []
	for row in unassigned:
		state = states.get(row.manufacturing_work_order)
		problem = (
			readiness_problem(state, state.work_order)
			if isinstance(state, dict)
			else state
		)
		(not_at_rest if problem else ready_now).append(
			{
				"manufacturing_work_order": row.manufacturing_work_order,
				"manufacturing_operation": row.manufacturing_operation,
				"why_not_now": frappe.utils.strip_html(problem) if problem else None,
			}
		)

	MWO = frappe.qb.DocType("Manufacturing Work Order")
	MOP = frappe.qb.DocType("Manufacturing Operation")
	by_process = dict(
		frappe.qb.from_(MWO)
		.join(MOP)
		.on(MOP.name == MWO.manufacturing_operation)
		.select(MOP.department_process, frappe.query_builder.functions.Count("*"))
		.where(
			(MWO.docstatus == 1)
			& (MWO.status.notin(("Completed", "Closed", "Cancelled", "Stopped")))
			& (MOP.department == department)
			& (MOP.department_process.isnotnull())
			& (MOP.department_process != "")
		)
		.groupby(MOP.department_process)
		.run()
	)

	enabled = [p for p in processes if not p.disabled]
	unmapped = [o.name for o in operations if not o.department_process]
	report = {
		"department": department,
		"pilot_on": is_pilot_enabled(),
		"process_control_on": bool(
			department_flag_installed()
			and frappe.db.get_value("Department", department, DEPARTMENT_FLAG)
		),
		"processes": [
			{
				"process": p.name,
				"disabled": bool(p.disabled),
				"operations": [
					o.name for o in operations if o.department_process == p.name
				],
				"users_with_access": sum(
					1 for a in access if a.department_process == p.name
				),
				"users_with_it_as_default": sum(
					1 for a in access if a.department_process == p.name and a.is_default
				),
				"active_jobs": by_process.get(p.name, 0),
			}
			for p in processes
		],
		"operations_without_process": unmapped,
		"jobs_without_process": len(unassigned),
		"jobs_ready_for_initial_assignment": len(ready_now),
		"jobs_to_finish_first": len(not_at_rest),
		"sample_ready": ready_now[:SAMPLE],
		"sample_to_finish_first": not_at_rest[:SAMPLE],
	}
	blockers = []
	if not enabled:
		blockers.append("no enabled Department Process")
	if unmapped:
		blockers.append(f"{len(unmapped)} Department Operation(s) without a process")
	for p in report["processes"]:
		if not p["disabled"] and not p["users_with_access"]:
			blockers.append(f"nobody has access to process {p['process']}")
	if ready_now:
		blockers.append(
			f"{len(ready_now)} active job(s) without a process: give them their first process "
			"with a Process Transfer marked Initial Process Assignment"
		)
	if not_at_rest:
		blockers.append(
			f"{len(not_at_rest)} active job(s) without a process are mid issue / receive or in "
			"transit: finish that step, then assign their first process"
		)
	report["blockers"] = blockers
	return report
