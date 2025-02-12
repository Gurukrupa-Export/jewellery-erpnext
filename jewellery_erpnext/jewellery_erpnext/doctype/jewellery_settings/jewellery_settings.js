// Copyright (c) 2022, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Jewellery Settings", {
	setup(frm) {
		frm.set_query("metal_type", "system_item", function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Metal Type" },
			};
		});
	},
});
