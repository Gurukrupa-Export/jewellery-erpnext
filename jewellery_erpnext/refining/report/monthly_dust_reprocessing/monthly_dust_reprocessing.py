import frappe
from frappe import _

from jewellery_erpnext.refining.report.utils import entry_conditions


def execute(filters=None):
	return get_columns(), get_data(filters)


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
			"label": _("Department"),
			"fieldname": "department",
			"fieldtype": "Link",
			"options": "Department",
			"width": 160,
		},
		{
			"label": _("Dust Item"),
			"fieldname": "dust_item",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"label": _("Recovery Quantity"),
			"fieldname": "recovery_quantity",
			"fieldtype": "Float",
			"width": 150,
		},
		{
			"label": _("Remaining Scrap"),
			"fieldname": "remaining_scrap",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 120,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(
		filters, default_days=30, refining_type="Dust Refining"
	)
	return frappe.db.sql(
		f"""
		SELECT
			re.name AS refining_entry,
			re.department,
			re.loss_item AS dust_item,
			re.actual_recovery AS recovery_quantity,
			re.refining_loss AS remaining_scrap,
			re.status
		FROM `tabRefining Entry` re
		WHERE {conditions}
		ORDER BY re.posting_date DESC, re.name DESC
		""",
		values,
		as_dict=True,
	)
