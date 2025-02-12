# Copyright (c) 2022, Nirali and contributors
# For license information, please see license.txt

# import frappe


import frappe
from frappe import _


def execute(filters=None):
	columns, data = [], []
	columns = get_columns(filters)
	data = get_data(filters)
	return columns, data


def get_columns(filters):
	columns = [
		{
			"label": _("Job Card"),
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "Job Card",
			"width": 130,
		},
		{
			"label": _("Job Card Status"),
			"fieldname": "job_card_status",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("Employee"),
			"fieldname": "employee",
			"fieldtype": "Link",
			"options": "Employee",
			"width": 80,
		},
		{
			"label": _("Operation"),
			"fieldname": "operation",
			"fieldtype": "Link",
			"options": "Operation",
			"width": 80,
		},
		{
			"label": _("Production Item"),
			"fieldname": "production_item",
			"fieldtype": "Link",
			"options": "Item",
			"width": 80,
			"hidden": 1,
		},
		{"label": _("Metal Purity"), "fieldname": "metal_purity", "fieldtype": "Data", "width": 80},
		{"label": _("In Gold Weight"), "fieldname": "in_gold_weight", "fieldtype": "Float", "width": 80},
		{
			"label": _("In Diamond Weight"),
			"fieldname": "in_diamond_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("In Gemstone Weight"),
			"fieldname": "in_gemstone_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("In Finding Weight"),
			"fieldname": "in_finding_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("In Other Weight"),
			"fieldname": "in_other_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Total In Gross Weight"),
			"fieldname": "total_in_gross_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Total In Fine Weight"),
			"fieldname": "total_in_fine_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Out Gross Weight"),
			"fieldname": "out_gross_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Total Out Fine Weight"),
			"fieldname": "total_out_fine_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Loss Gold Weight"),
			"fieldname": "loss_gold_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Loss Diamond Weight"),
			"fieldname": "loss_diamond_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Loss Gemstone Weight"),
			"fieldname": "loss_gemstone_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Total Out Gross Weight"),
			"fieldname": "total_out_gross_weight",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Balance Gross Weight"),
			"fieldname": "balance_gross",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Balance Fine Weight"),
			"fieldname": "balance_fine",
			"fieldtype": "Float",
			"width": 80,
		},
	]
	return columns


def get_data(filters):

	JobCard = frappe.qb.DocType("Job Card")
	conditions = get_conditions(filters, JobCard)

	query = (
		frappe.qb.from_(JobCard)
		.select(
			JobCard.name,
			JobCard.status.as_("job_card_status"),
			JobCard.work_order,
			JobCard.operation,
			JobCard.production_item,
			JobCard.metal_purity,
			JobCard.in_gold_weight,
			JobCard.in_diamond_weight,
			JobCard.in_gemstone_weight,
			JobCard.in_other_weight,
			JobCard.loss_gold_weight,
			JobCard.loss_diamond_weight,
			JobCard.loss_gemstone_weight,
			JobCard.out_gross_weight,
			JobCard.total_in_gross_weight,
			JobCard.total_in_fine_weight,
			JobCard.total_out_gross_weight,
			JobCard.total_out_fine_weight,
			JobCard.balance_gross,
			JobCard.balance_fine,
		)
		.where(JobCard.status != "Cancelled")
	)
	# Apply additional conditions
	for condition in conditions:
		query = query.where(condition)

	data = query.run(as_dict=True)
	return data


def get_conditions(filters, JobCard):
	conditions = []

	if not filters.get("work_order"):
		frappe.throw(_("Please select work order."))

	if filters.get("work_order"):
		conditions.append(JobCard.work_order == filters.get("work_order"))

	return conditions
