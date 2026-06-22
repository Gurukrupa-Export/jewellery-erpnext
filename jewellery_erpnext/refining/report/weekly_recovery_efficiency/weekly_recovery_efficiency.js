// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Weekly Recovery Efficiency"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date" },
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "refining_type",
			label: __("Refining Type"),
			fieldtype: "Select",
			options: "\nDust Refining\nWork Order Refining\nSerial Number Refining\nScrap Refining",
		},
		{ fieldname: "department", label: __("Department"), fieldtype: "Link", options: "Department" },
	],
};
