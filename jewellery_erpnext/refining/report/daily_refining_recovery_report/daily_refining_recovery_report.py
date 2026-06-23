import frappe
from frappe import _


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)
	report_summary = get_report_summary(data)

	return columns, data, None, chart, report_summary


def get_columns():
	return [
		{
			"fieldname": "refining_entry",
			"label": _("Refining Entry"),
			"fieldtype": "Link",
			"options": "Refining Entry",
			"width": 150,
		},
		{
			"fieldname": "refining_type",
			"label": _("Refining Type"),
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"fieldname": "department",
			"label": _("Department"),
			"fieldtype": "Link",
			"options": "Department",
			"width": 140,
		},
		{
			"fieldname": "status",
			"label": _("Status"),
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"fieldname": "gross_pure_weight",
			"label": _("Gross Pure Input (g)"),
			"fieldtype": "Float",
			"width": 150,
		},
		{
			"fieldname": "refined_fine_weight",
			"label": _("Recovered Fine (g)"),
			"fieldtype": "Float",
			"width": 150,
		},
		{
			"fieldname": "refining_loss",
			"label": _("Refining Loss (g)"),
			"fieldtype": "Float",
			"width": 130,
		},
		{
			"fieldname": "recovery_efficiency",
			"label": _("Efficiency %"),
			"fieldtype": "Percent",
			"width": 110,
		},
	]


def get_data(filters):
	conditions = get_conditions(filters)

	entries = frappe.db.sql(
		f"""
		SELECT
			name as refining_entry,
			refining_type,
			department,
			status,
			gross_pure_weight,
			refined_fine_weight,
			refining_loss
		FROM `tabRefining Entry`
		WHERE docstatus = 1 {conditions}
		ORDER BY creation DESC
	""",
		filters,
		as_dict=1,
	)

	for entry in entries:
		if entry.gross_pure_weight and entry.gross_pure_weight > 0:
			entry.recovery_efficiency = (
				entry.refined_fine_weight / entry.gross_pure_weight
			) * 100
		else:
			entry.recovery_efficiency = 0.0

	return entries


def get_conditions(filters):
	filters = frappe._dict(filters or {})
	conditions = ""
	if filters.get("posting_date"):
		conditions += " AND posting_date = %(posting_date)s"
	if filters.get("refining_type"):
		conditions += " AND refining_type = %(refining_type)s"
	if filters.get("department"):
		conditions += " AND department = %(department)s"
	return conditions


def get_chart(data):
	if not data:
		return None

	labels = [d.refining_entry for d in data]
	input_weights = [d.gross_pure_weight for d in data]
	recovered_weights = [d.refined_fine_weight for d in data]

	return {
		"data": {
			"labels": labels,
			"datasets": [
				{"name": _("Input Pure Weight"), "values": input_weights},
				{"name": _("Recovered Fine Weight"), "values": recovered_weights},
			],
		},
		"type": "bar",
		"colors": ["#f39c12", "#2ecc71"],
	}


def get_report_summary(data):
	if not data:
		return []

	total_input = sum(d.gross_pure_weight for d in data)
	total_recovered = sum(d.refined_fine_weight for d in data)
	avg_efficiency = (total_recovered / total_input * 100) if total_input > 0 else 0

	return [
		{
			"value": total_input,
			"indicator": "Blue",
			"label": _("Total Input Wt"),
			"datatype": "Float",
		},
		{
			"value": total_recovered,
			"indicator": "Green",
			"label": _("Total Recovered Wt"),
			"datatype": "Float",
		},
		{
			"value": avg_efficiency,
			"indicator": "Red" if avg_efficiency < 98 else "Green",
			"label": _("Avg Efficiency %"),
			"datatype": "Percent",
		},
	]
