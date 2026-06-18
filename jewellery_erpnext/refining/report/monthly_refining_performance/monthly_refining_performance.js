// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Monthly Refining Performance"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date" },
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{ fieldname: "department", label: __("Department"), fieldtype: "Link", options: "Department" },
	],
};
