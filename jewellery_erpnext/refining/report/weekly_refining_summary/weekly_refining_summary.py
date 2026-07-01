import frappe
from frappe import _

from jewellery_erpnext.refining.report.utils import entry_conditions


def execute(filters=None):
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{
			"label": _("Refining Type"),
			"fieldname": "refining_type",
			"fieldtype": "Data",
			"width": 170,
		},
		{
			"label": _("Total Entries"),
			"fieldname": "total_entries",
			"fieldtype": "Int",
			"width": 120,
		},
		{
			"label": _("Material Refined"),
			"fieldname": "material_refined",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Pure Gold Recovered"),
			"fieldname": "pure_gold_recovered",
			"fieldtype": "Float",
			"width": 160,
		},
		{
			"label": _("Total Dust Generated"),
			"fieldname": "total_dust_generated",
			"fieldtype": "Float",
			"width": 160,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(filters, default_days=7)
	return frappe.db.sql(
		f"""
		SELECT
			re.refining_type,
			COUNT(DISTINCT re.name) AS total_entries,
			COALESCE(SUM(ms.material_refined), 0) AS material_refined,
			COALESCE(SUM(gs.pure_gold_recovered), 0) AS pure_gold_recovered,
			COALESCE(SUM(re.refining_loss), 0) AS total_dust_generated
		FROM `tabRefining Entry` re
		LEFT JOIN (
			SELECT parent, SUM(qty) AS material_refined
			FROM `tabRefining Material Line`
			WHERE IFNULL(is_consumable, 0) = 0
			GROUP BY parent
		) ms ON ms.parent = re.name
		LEFT JOIN (
			SELECT parent, SUM(refining_gold_weight) AS pure_gold_recovered
			FROM `tabRefined Gold`
			GROUP BY parent
		) gs ON gs.parent = re.name
		WHERE {conditions}
		GROUP BY re.refining_type
		ORDER BY re.refining_type
		""",
		values,
		as_dict=True,
	)
