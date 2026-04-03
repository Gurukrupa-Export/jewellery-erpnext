// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Subcontracting Report"] = {
	filters: [
		{
			fieldname: "batch_no",
			label: "Batch",
			fieldtype: "Link",
			options: "Batch",
			width: "120",
		},

		{
			fieldname: "customer",
			label: "Customer",
			fieldtype: "Link",
			options: "Customer",
			width: "120",
		},

		{
			fieldname: "item_code",
			label: "Item Code",
			fieldtype: "Link",
			options: "Item",
			width: "120",
		},
	],
};
