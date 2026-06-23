// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Daily Dust and Loss"] = {
	filters: [
		{
			fieldname: "posting_date",
			label: __("Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
		},
	],
};
