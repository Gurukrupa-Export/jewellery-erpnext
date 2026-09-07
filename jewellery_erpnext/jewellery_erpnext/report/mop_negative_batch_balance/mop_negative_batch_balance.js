// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.query_reports["MOP Negative Batch Balance"] = {
	filters: [
		{
			fieldname: "manufacturing_work_order",
			label: __("Manufacturing Work Order"),
			fieldtype: "Link",
			options: "Manufacturing Work Order",
			on_change: function (report) {
				// The operation list is scoped to the work order; clear a carried-over
				// value rather than silently filtering on a mismatched pair.
				report.set_filter_value("manufacturing_operation", "");
			},
		},
		{
			fieldname: "manufacturing_operation",
			label: __("Manufacturing Operation"),
			fieldtype: "Link",
			options: "Manufacturing Operation",
			get_query: function () {
				const mwo = frappe.query_report.get_filter_value("manufacturing_work_order");
				return mwo ? { filters: { manufacturing_work_order: mwo } } : {};
			},
		},
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			// Default ON. One defect cloned onto ten operations should read as one row
			// with "Clones = 9", not as ten rows -- that is the whole anti-noise story.
			fieldname: "origins_only",
			label: __("Origins only"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "min_understatement_g",
			label: __("Min Gross Wt Suppressed (g)"),
			fieldtype: "Float",
			default: 0,
		},
		{
			fieldname: "allow_full_scan",
			label: __("Scan whole ledger"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "understatement_g" && data && flt(data.understatement_g) > 0) {
			value = `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
		}
		if (column.fieldname === "inherited" && data && !data.inherited) {
			value = `<span title="${__("This is where the defect was minted")}">${value}</span>`;
		}
		return value;
	},
};
