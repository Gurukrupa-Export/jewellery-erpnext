// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["Manufacturing Operation Balance"] = {
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
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
			reqd: 0,
		},
		{
			fieldname: "manufacturer",
			label: __("Manufacturer"),
			fieldtype: "Link",
			options: "Manufacturer",
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
				const manufacturer = frappe.query_report.get_filter_value("manufacturer");
				const filters = {};
				if (company) filters["company"] = company;
				if (manufacturer) filters["manufacturer"] = manufacturer;
				return { filters };
			},
			on_change: function () {
				// Clear MOP filter when MWO changes so stale selection is not submitted
				frappe.query_report.set_filter_value("manufacturing_operation", "");
			},
		},
		{
			fieldname: "manufacturing_operation",
			label: __("Manufacturing Operation"),
			fieldtype: "Link",
			options: "Manufacturing Operation",
			reqd: 1,
			get_query: function () {
				const mwo = frappe.query_report.get_filter_value("manufacturing_work_order");
				if (!mwo) {
					frappe.msgprint(__("Please select a Manufacturing Work Order first."));
					return { filters: { name: "__nonexistent__" } };
				}
				return { filters: { manufacturing_work_order: mwo } };
			},
		},
	],
};
