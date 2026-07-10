# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Employee Warehouse Tracking (req #7).

Per (employee warehouse x item): Issue / Receive / Loss / Pending, where
	Pending = Issue - Receive - Loss.

Computed on demand from Stock Ledger Entry (item-level qty, so the v16
NULL-batch_no-in-SLE caveat does not apply). Loss is any outward movement whose
voucher is a Process Loss Stock Entry; every other outward movement is a Receive
(material returned by the employee); every inward movement is an Issue.
"""

import frappe
from frappe import _

from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking import (
	get_warehouse_item_tracking,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate_filters(filters)
	columns = _get_columns()
	data = _get_data(filters)
	return columns, data


def _validate_filters(filters):
	if not filters.get("company"):
		frappe.throw(_("Company is mandatory."))


def _get_columns():
	return [
		{
			"label": _("Warehouse"),
			"fieldname": "warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 220,
		},
		{
			"label": _("Employee"),
			"fieldname": "employee",
			"fieldtype": "Link",
			"options": "Employee",
			"width": 120,
		},
		{
			"label": _("Employee Name"),
			"fieldname": "employee_name",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": _("Department"),
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
			"width": 200,
		},
		{
			"label": _("Issue Qty"),
			"fieldname": "issue_qty",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Receive Qty"),
			"fieldname": "receive_qty",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Loss Qty"),
			"fieldname": "loss_qty",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Pending Qty"),
			"fieldname": "pending_qty",
			"fieldtype": "Float",
			"width": 110,
		},
	]


def _get_data(filters):
	# Same SLE-derived aggregation the Warehouse form uses, so the report and the
	# on-warehouse child table can never disagree.
	return get_warehouse_item_tracking(filters)
