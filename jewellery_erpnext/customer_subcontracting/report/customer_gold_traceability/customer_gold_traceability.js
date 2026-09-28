// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Customer Gold Traceability"] = {
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
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Receipt Settlement", "Material Position", "Movements", "FG Valuation"],
			default: "Receipt Settlement",
			reqd: 1,
			on_change: function (report) {
				const fg = report.get_filter_value("view") === "FG Valuation";
				["stock_entry", "serial_number_creator"].forEach((f) => report.toggle_filter_display(f, !fg));
				["customer", "receipt", "receipt_row", "batch", "warehouse", "item_code"].forEach((f) =>
					report.toggle_filter_display(f, fg)
				);
				report.refresh();
			},
		},
		{
			fieldname: "customer",
			label: __("Customer"),
			fieldtype: "Link",
			options: "Customer",
		},
		{
			fieldname: "receipt",
			label: __("Receipt"),
			fieldtype: "Link",
			options: "Stock Entry",
			get_query: function () {
				const company = frappe.query_report.get_filter_value("company");
				return { filters: { company: company, docstatus: 1, purpose: "Material Receipt" } };
			},
		},
		{
			fieldname: "receipt_row",
			label: __("Receipt Row"),
			fieldtype: "Data",
		},
		{
			fieldname: "batch",
			label: __("Batch (any generation)"),
			fieldtype: "Link",
			options: "Batch",
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
		},
		{
			fieldname: "item_code",
			label: __("Receipt Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "from_date",
			label: __("Movements From"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("As Of"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "stock_entry",
			label: __("Manufacture Entry"),
			fieldtype: "Link",
			options: "Stock Entry",
			hidden: 1,
			get_query: function () {
				return { filters: { purpose: "Manufacture", docstatus: 1 } };
			},
		},
		{
			fieldname: "serial_number_creator",
			label: __("Serial Number Creator"),
			fieldtype: "Link",
			options: "Serial Number Creator",
			hidden: 1,
		},
	],
};
