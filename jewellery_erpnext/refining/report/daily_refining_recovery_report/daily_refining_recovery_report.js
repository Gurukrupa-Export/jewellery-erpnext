frappe.query_reports["Daily Refining Recovery Report"] = {
	filters: [
		{
			fieldname: "posting_date",
			label: __("Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "refining_type",
			label: __("Refining Type"),
			fieldtype: "Select",
			options:
				"\nScrap Refining\nWork Order Refining\nSerial Number Refining\nUnused/Loose Material Refining",
		},
		{
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname == "recovery_efficiency" && data && data.recovery_efficiency < 98) {
			value = "<span style='color:red; font-weight:bold'>" + value + "</span>";
		}
		return value;
	},
};
