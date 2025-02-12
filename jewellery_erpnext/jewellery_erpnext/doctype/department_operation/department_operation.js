// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Department Operation", {
	setup: function (frm) {
		frm.set_query("service_item", function () {
			return {
				filters: {
					is_stock_item: 0,
				},
			};
		});
	},
});
