// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Item Wise MWO MOP Log"] = {
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
			fieldname: "manufacturing_work_order",
			label: __("Manufacturing Work Order"),
			fieldtype: "Link",
			options: "Manufacturing Work Order",
			reqd: 1,
			get_query: function () {
				const company = frappe.query_report.get_filter_value("company");
				return company ? { filters: { company } } : {};
			},
		},
		{
			fieldname: "item_code",
			label: __("Item Code"),
			fieldtype: "Link",
			options: "Item",
			reqd: 0,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 0,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			reqd: 0,
		},
	],
};
