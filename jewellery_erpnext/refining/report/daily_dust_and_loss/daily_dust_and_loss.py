import frappe
from frappe import _

from jewellery_erpnext.refining.report.utils import entry_conditions


def execute(filters=None):
	columns = get_columns()
	data = get_data(filters)
	return columns, data


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
			"label": _("Department Name"),
			"fieldname": "department",
			"fieldtype": "Link",
			"options": "Department",
			"width": 160,
		},
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"label": _("System Loss Quantity"),
			"fieldname": "system_loss_quantity",
			"fieldtype": "Float",
			"width": 150,
		},
		{
			"label": _("Physical Scrap Quantity"),
			"fieldname": "physical_dust_quantity",
			"fieldtype": "Float",
			"width": 160,
		},
		{
			"label": _("Difference Quantity"),
			"fieldname": "difference_quantity",
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"label": _("Scrap Item"),
			"fieldname": "dust_item",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
	]


def get_data(filters):
	conditions, values = entry_conditions(
		filters, default_days=1, refining_type="Scrap Refining"
	)
	return frappe.db.sql(
		f"""
		SELECT
			re.name AS refining_entry,
			re.department,
			COALESCE(rml.item_code, re.loss_item) AS item_code,
			re.system_quantity AS system_loss_quantity,
			re.physical_quantity AS physical_dust_quantity,
			re.difference_quantity,
			re.loss_item AS dust_item
		FROM `tabRefining Entry` re
		LEFT JOIN `tabRefining Material Line` rml ON rml.parent = re.name
		WHERE {conditions}
		ORDER BY re.posting_date DESC, re.name DESC
		""",
		values,
		as_dict=True,
	)
