// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Employee Warehouse Tracking"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
		},
		{
			fieldname: "employee",
			label: __("Employee"),
			fieldtype: "Link",
			options: "Employee",
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: function () {
				return { filters: { is_group: 0 } };
			},
		},
		{
			fieldname: "item_code",
			label: __("Item Code"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "group_by_month",
			label: __("Group by Month"),
			fieldtype: "Check",
			default: 0,
			// One row per month instead of one cumulative row. Note Pending Qty then
			// reads as that month's movement (issue - receive - loss), not a running
			// balance, so it can be negative for a month that returned older metal.
		},
	],
};
