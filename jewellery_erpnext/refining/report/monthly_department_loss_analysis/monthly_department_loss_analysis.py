import frappe
from frappe import _

from jewellery_erpnext.refining.report.utils import entry_conditions, recovery_percent


def execute(filters=None):
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{
			"label": _("Department Name"),
			"fieldname": "department",
			"fieldtype": "Link",
			"options": "Department",
			"width": 170,
		},
		{
			"label": _("Loss Quantity"),
			"fieldname": "loss_quantity",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Dust Generated"),
			"fieldname": "dust_generated",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Loss Percentage"),
			"fieldname": "loss_percentage",
			"fieldtype": "Percent",
			"width": 140,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(
		filters, default_days=30, refining_type="Dust Refining"
	)
	rows = frappe.db.sql(
		f"""
		SELECT
			re.department,
			SUM(re.system_quantity) AS loss_quantity,
			SUM(CASE WHEN re.difference_quantity > 0 THEN re.difference_quantity ELSE 0 END) AS dust_generated
		FROM `tabRefining Entry` re
		WHERE {conditions}
		GROUP BY re.department
		ORDER BY dust_generated DESC
		""",
		values,
		as_dict=True,
	)
	for row in rows:
		row.loss_percentage = recovery_percent(row.dust_generated, row.loss_quantity)
	return rows
