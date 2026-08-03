import frappe
from frappe import _

from jewellery_erpnext.refining.report.utils import entry_conditions, recovery_percent


def execute(filters=None):
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{
			"label": _("Department"),
			"fieldname": "department",
			"fieldtype": "Link",
			"options": "Department",
			"width": 170,
		},
		{
			"label": _("Total Material Processed"),
			"fieldname": "total_material_processed",
			"fieldtype": "Float",
			"width": 180,
		},
		{
			"label": _("Total Recovered Metal"),
			"fieldname": "total_recovered_metal",
			"fieldtype": "Float",
			"width": 170,
		},
		{
			"label": _("Total Scrap Generated"),
			"fieldname": "total_dust_generated",
			"fieldtype": "Float",
			"width": 170,
		},
		{
			"label": _("Recovery %"),
			"fieldname": "recovery_percentage",
			"fieldtype": "Percent",
			"width": 120,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(filters, default_days=30)
	rows = frappe.db.sql(
		f"""
		SELECT
			re.department,
			COALESCE(SUM(ms.total_material_processed), 0) AS total_material_processed,
			COALESCE(SUM(gs.total_recovered_metal), 0) AS total_recovered_metal,
			COALESCE(SUM(re.refining_loss), 0) AS total_dust_generated
		FROM `tabRefining Entry` re
		LEFT JOIN (
			SELECT parent, SUM(qty) AS total_material_processed
			FROM `tabRefining Material Line`
			WHERE IFNULL(is_consumable, 0) = 0
			GROUP BY parent
		) ms ON ms.parent = re.name
		LEFT JOIN (
			SELECT parent, SUM(refining_gold_weight) AS total_recovered_metal
			FROM `tabRefined Gold`
			GROUP BY parent
		) gs ON gs.parent = re.name
		WHERE {conditions}
		GROUP BY re.department
		ORDER BY re.department
		""",
		values,
		as_dict=True,
	)
	for row in rows:
		row.recovery_percentage = recovery_percent(
			row.total_recovered_metal, row.total_material_processed
		)
	return rows
