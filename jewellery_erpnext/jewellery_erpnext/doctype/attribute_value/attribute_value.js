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
		set_sieve_size_labels(frm);
	},

	is_diamond_sieve_size: function (frm) {
		set_sieve_size_labels(frm);
	},

	is_diamond_sieve_size_range: function (frm) {
		set_sieve_size_labels(frm);
	},
});

// For any sieve value, "height" is the UP bound and "weight" is the DOWN bound -- they are the
// two endpoints of the sieve band, not physical dimensions. The Range values need this just as
// much as the plain sieve sizes do: Diamond Conversion's "Sieve Size to Sieve Size" check reads
// UP/DOWN off the Range record, and the form would otherwise label them "Height"/"Weight" and
// leave nobody any idea what to enter.
function set_sieve_size_labels(frm) {
	let is_sieve_size = frm.doc.is_diamond_sieve_size || frm.doc.is_diamond_sieve_size_range;
	frm.set_df_property("height", "label", is_sieve_size ? "UP" : "Height");
	frm.set_df_property("weight", "label", is_sieve_size ? "DOWN" : "Weight");
}

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
