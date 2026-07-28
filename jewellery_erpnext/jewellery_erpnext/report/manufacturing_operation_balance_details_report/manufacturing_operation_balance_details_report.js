// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Manufacturing Operation Balance Details Report"] = {
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
			on_change: function (report) {
				// The operation list is scoped to this work order, so a carried-over
				// value would be an operation of the OLD work order -- clear it.
				report.set_filter_value("manufacturing_operation", "");
				report.refresh();
			},
		},
		{
			fieldname: "manufacturing_operation",
			label: __("Manufacturing Operation"),
			fieldtype: "Link",
			options: "Manufacturing Operation",
			reqd: 1,
			get_query: function () {
				const manufacturing_work_order =
					frappe.query_report.get_filter_value("manufacturing_work_order");
				// Every Manufacturing Operation without a work order has it NULL, and
				// `= ''` does not match NULL -- so the dropdown stays empty until a
				// Manufacturing Work Order is picked, instead of listing all of them.
				return {
					filters: { manufacturing_work_order: manufacturing_work_order || "" },
				};
			},
		},
		{
			fieldname: "item_code",
			label: __("Item Code"),
			fieldtype: "Link",
			options: "Item",
			reqd: 0,
		},
	],
};
