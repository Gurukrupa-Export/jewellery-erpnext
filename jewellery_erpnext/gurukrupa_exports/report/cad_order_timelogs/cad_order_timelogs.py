# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.query_builder.functions import TimeDiff
from pypika.terms import Function


def execute(filters=None):
	columns = get_columns(filters)
	data = get_data(filters)
	return columns, data


def get_data(filters):
	WorkflowAction = frappe.qb.DocType("Workflow Action")
	main_query = WorkflowAction.as_("main_query")
	sub_query = WorkflowAction.as_("subquery")
	# Construct the subquery
	subquery = (
		frappe.qb.from_(sub_query)
		.select(sub_query.creation)
		.where(
			(sub_query.reference_name == main_query.reference_name)
			& (sub_query.workflow_state != main_query.workflow_state)
			& (sub_query.creation > main_query.creation)
		)
		.orderby(sub_query.creation, order=frappe.qb.asc)
		.limit(1)
	)
	# Construct the main query
	query = (
		frappe.qb.from_(main_query)
		.select(
			main_query.completed_by,
			main_query.status,
			main_query.reference_name,
			main_query.creation,
			main_query.workflow_state,
			subquery.as_("completed_on"),
			TimeDiff(main_query.creation, main_query.completed_on).as_("time_taken"),
		)
		.where(main_query.reference_doctype == "CAD Order")
		.orderby(main_query.creation, order=frappe.qb.desc)
	)
	# conditions
	conditions = get_conditions(filters, main_query)
	for condition in conditions:
		query = query.where(condition)

	data = query.run(as_dict=True)

	return data


def get_columns(filters):
	columns = [
		{
			"label": _("CAD Order"),
			"fieldname": "reference_name",
			"fieldtype": "Link",
			"options": "CAD Order",
		},
		{"label": _("Workflow State"), "fieldname": "workflow_state", "fieldtype": "Data"},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data"},
		{"label": _("Start Time"), "fieldname": "creation", "fieldtype": "Datetime"},
		{"label": _("End Time"), "fieldname": "completed_on", "fieldtype": "Datetime"},
		{"label": _("Time Taken(in Hrs)"), "fieldname": "time_taken", "fieldtype": "Float"},
	]
	return columns


def get_conditions(filters, WorkflowAction):
	conditions = []
	if order := filters.get("cad_order"):
		conditions.append(WorkflowAction.reference_name == order)
	if state := filters.get("workflow_state"):
		conditions.append(WorkflowAction.workflow_state.like(f"%{state}%"))
	return conditions
