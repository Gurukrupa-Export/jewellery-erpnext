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
			"label": _("MWO Number"),
			"fieldname": "mwo_number",
			"fieldtype": "Link",
			"options": "Manufacturing Work Order",
			"width": 180,
		},
		{
			"label": _("Material Quantity"),
			"fieldname": "material_quantity",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Refining Status"),
			"fieldname": "refining_status",
			"fieldtype": "Data",
			"width": 140,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(
		filters, default_days=7, refining_type="Work Order Refining"
	)
	return frappe.db.sql(
		f"""
		SELECT
			re.name AS refining_entry,
			mwo.manufacturing_work_order AS mwo_number,
			COALESCE(SUM(rml.qty), mwo.metal_weight, 0) AS material_quantity,
			re.status AS refining_status
		FROM `tabRefining Entry` re
		LEFT JOIN `tabManufacturing Work Order Refining Details` mwo ON mwo.parent = re.name
		LEFT JOIN `tabRefining Material Line` rml
			ON rml.parent = re.name AND rml.manufacturing_work_order = mwo.manufacturing_work_order
		WHERE {conditions}
		GROUP BY re.name, mwo.manufacturing_work_order
		ORDER BY re.posting_date DESC, re.name DESC
		""",
		values,
		as_dict=True,
	)
