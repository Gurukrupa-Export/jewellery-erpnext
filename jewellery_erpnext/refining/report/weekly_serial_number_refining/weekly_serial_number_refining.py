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
			"label": _("Serial Number"),
			"fieldname": "serial_number",
			"fieldtype": "Link",
			"options": "Serial No",
			"width": 180,
		},
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"label": _("Reason for Refining"),
			"fieldname": "reason_for_refining",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("Recovery Quantity"),
			"fieldname": "recovery_quantity",
			"fieldtype": "Float",
			"width": 140,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(
		filters, default_days=7, refining_type="Serial Number Refining"
	)
	return frappe.db.sql(
		f"""
		SELECT
			re.name AS refining_entry,
			sn.serial_number,
			sn.item_code,
			re.recovery_remarks AS reason_for_refining,
			COALESCE(re.actual_recovery, sn.pure_weight, sn.gross_weight, 0) AS recovery_quantity
		FROM `tabRefining Entry` re
		LEFT JOIN `tabRefining Serial No Detail` sn ON sn.parent = re.name
		WHERE {conditions}
		ORDER BY re.posting_date DESC, re.name DESC
		""",
		values,
		as_dict=True,
	)
