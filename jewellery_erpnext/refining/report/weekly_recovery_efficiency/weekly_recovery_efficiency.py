import frappe
from frappe import _

from jewellery_erpnext.refining.report.utils import entry_conditions, recovery_percent


def execute(filters=None):
	data = get_data(filters)
	return get_columns(), data


def get_columns():
	return [
		{
			"label": _("Refining Entry"),
			"fieldname": "refining_entry",
			"fieldtype": "Link",
			"options": "Refining Entry",
			"width": 150,
		},
		{
			"label": _("Material Type"),
			"fieldname": "material_type",
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"label": _("Input Quantity"),
			"fieldname": "input_quantity",
			"fieldtype": "Float",
			"width": 130,
		},
		{
			"label": _("Expected Recovery"),
			"fieldname": "expected_recovery",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Actual Recovery"),
			"fieldname": "actual_recovery",
			"fieldtype": "Float",
			"width": 130,
		},
		{
			"label": _("Recovery %"),
			"fieldname": "recovery_percentage",
			"fieldtype": "Percent",
			"width": 110,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(filters, default_days=7)
	rows = frappe.db.sql(
		f"""
		SELECT
			re.name AS refining_entry,
			re.refining_type AS material_type,
			COALESCE(SUM(rml.qty), re.gross_pure_weight, 0) AS input_quantity,
			re.expected_recovery,
			re.actual_recovery
		FROM `tabRefining Entry` re
		LEFT JOIN `tabRefining Material Line` rml
			ON rml.parent = re.name AND IFNULL(rml.is_consumable, 0) = 0
		WHERE {conditions}
		GROUP BY re.name
		ORDER BY re.posting_date DESC, re.name DESC
		""",
		values,
		as_dict=True,
	)
	for row in rows:
		row.recovery_percentage = recovery_percent(
			row.actual_recovery, row.expected_recovery
		)
	return rows
