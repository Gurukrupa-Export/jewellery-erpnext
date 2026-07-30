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

		// Restrict the grid's finding_category to values of the "Finding Category"
		// Item Attribute — the same strings finding items carry on their Item
		// Variant Attribute rows, which is what the loss gate matches against.
		frm.set_query("finding_category", "finding_loss_booking", function () {
			return {
				query: "jewellery_erpnext.jewellery_erpnext.doctype.department_operation.department_operation.get_finding_categories",
			};
		});
	},
});
