// Copyright (c) 2022, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Attribute Value", {
	setup: function (frm) {
		var parent_fields = [
			["sieve_size_range", "Diamond Sieve Size Range"],
			["metal_touch", "Metal Touch"],
		];
		set_item_attribute_filters_on_fields_in_parent_doctype(frm, parent_fields);
	},
	refresh: function (frm) {
		if (frm.doc.is_diamond_sieve_size) {
			frm.set_df_property("height", "label", "UP");
			frm.set_df_property("weight", "label", "DOWN");
		} else {
			frm.set_df_property("height", "label", "Height");
			frm.set_df_property("weight", "label", "Weight");
		}
	},

	is_diamond_sieve_size: function (frm) {
		if (frm.doc.is_diamond_sieve_size) {
			frm.set_df_property("height", "label", "UP");
			frm.set_df_property("weight", "label", "DOWN");
		} else {
			frm.set_df_property("height", "label", "Height");
			frm.set_df_property("weight", "label", "Weight");
		}
	},
});

function set_item_attribute_filters_on_fields_in_parent_doctype(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}
