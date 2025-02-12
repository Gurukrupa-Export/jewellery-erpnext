// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Customer Product Tolerance Master", {
	refresh: function (frm) {
		set_filters_ct_fields(frm);
	},
});
// Custom Function's
function set_filters_ct_fields(frm) {
	var filter_for_metal = [];
	var filter_for_diamond = [];
	var filter_for_gemstone = [];
	frappe.db
		.get_list("Tolerance Weight Type", {
			fields: ["name", "metal", "diamond", "gemstone"],
		})
		.then((records) => {
			records.forEach((record) => {
				if (record.metal === 1) {
					filter_for_metal.push(record.name);
				}
				if (record.diamond === 1) {
					filter_for_diamond.push(record.name);
				}
				if (record.gemstone === 1) {
					filter_for_gemstone.push(record.name);
				}
			});
			frm.fields_dict["metal_tolerance_table"].grid.get_field("weight_type").get_query =
				function (doc, cdt, cdn) {
					return {
						filters: [[`name`, `in`, filter_for_metal]],
					};
				};
			frm.fields_dict["diamond_tolerance_table"].grid.get_field("weight_type").get_query =
				function (doc, cdt, cdn) {
					return {
						filters: [[`name`, `in`, filter_for_diamond]],
					};
				};
			frm.fields_dict["gemstone_tolerance_table"].grid.get_field("weight_type").get_query =
				function (doc, cdt, cdn) {
					return {
						filters: [[`name`, `in`, filter_for_gemstone]],
					};
				};
		});
}
frappe.ui.form.on("Metal Tolerance Table", {
	range_type: function (frm, cdt, cdn) {
		toggle_metal_table_field(frm, cdt, cdn);
	},
});
frappe.ui.form.on("Diamond Tolerance Table", {
	weight_type: function (frm, cdt, cdn) {
		toggle_diamond_table_field(frm, cdt, cdn);
	},
});
frappe.ui.form.on("Gemstone Tolerance Table", {
	weight_type: function (frm, cdt, cdn) {
		toggle_gemstone_table_field(frm, cdt, cdn);
	},
});
function toggle_metal_table_field(frm, cdt, cdn) {
	let d = locals[cdt][cdn];
	let percentage_fields = ["plus_percent", "minus_percent"];
	let weight_range_fields = ["from_weight", "to_weight"];
	if (d.range_type == "Percentage") {
		$.each(weight_range_fields || [], function (i, field) {
			frm.fields_dict.metal_tolerance_table.grid.toggle_display(field, false);
		});
		frm.refresh_fields();
	} else {
		$.each(weight_range_fields || [], function (i, field) {
			frm.fields_dict.metal_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	}
	if (d.range_type == "Weight Range") {
		$.each(percentage_fields || [], function (i, field) {
			frm.fields_dict.metal_tolerance_table.grid.toggle_display(field, false);
		});
		frm.refresh_fields();
	} else {
		$.each(percentage_fields || [], function (i, field) {
			frm.fields_dict.metal_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	}
}
function toggle_diamond_table_field(frm, cdt, cdn) {
	let d = locals[cdt][cdn];
	let mm_size_wise = [
		"diamond_type",
		"sieve_size",
		"from_diamond",
		"to_diamond",
		"plus_percent",
		"minus_percent",
	];
	let group_size_wise = [
		"diamond_type",
		"sieve_size_range",
		"from_diamond",
		"to_diamond",
		"plus_percent",
		"minus_percent",
	];
	let weight_wise = [
		"diamond_type",
		"from_diamond",
		"to_diamond",
		"plus_percent",
		"minus_percent",
	];
	let universal = ["plus_percent", "minus_percent"];
	let all_field = [
		"diamond_type",
		"sieve_size",
		"sieve_size_range",
		"from_diamond",
		"to_diamond",
		"plus_percent",
		"minus_percent",
	];
	$.each(all_field || [], function (i, field) {
		frm.fields_dict.diamond_tolerance_table.grid.toggle_display(field, false);
	});
	frm.refresh_fields();
	if (d.weight_type == "MM Size wise") {
		$.each(mm_size_wise || [], function (i, field) {
			frm.fields_dict.diamond_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	} else if (d.weight_type == "Group Size wise") {
		$.each(group_size_wise || [], function (i, field) {
			frm.fields_dict.diamond_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	} else if (d.weight_type == "Weight wise") {
		$.each(weight_wise || [], function (i, field) {
			frm.fields_dict.diamond_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	} else if (d.weight_type == "Universal") {
		$.each(universal || [], function (i, field) {
			frm.fields_dict.diamond_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	}
}
function toggle_gemstone_table_field(frm, cdt, cdn) {
	let d = locals[cdt][cdn];
	let gemstone_type_range = ["gemstone_type"];
	let weight_range = ["gemstone_shape"];
	let weight_wise = ["gemstone_type", "gemstone_shape"];
	// let universal = ['plus_percent','minus_percent']
	let all_field = ["gemstone_type", "gemstone_shape"];
	$.each(all_field || [], function (i, field) {
		frm.fields_dict.gemstone_tolerance_table.grid.toggle_display(field, false);
	});
	frm.refresh_fields();
	if (d.weight_type == "Gemstone Type Range") {
		$.each(gemstone_type_range || [], function (i, field) {
			frm.fields_dict.gemstone_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	} else if (d.weight_type == "Weight Range") {
		$.each(weight_range || [], function (i, field) {
			frm.fields_dict.gemstone_tolerance_table.grid.toggle_display(field, true);
		});
		frm.refresh_fields();
	} else if (d.weight_type == "Weight wise") {
		$.each(weight_wise || [], function (i, field) {
			frm.fields_dict.gemstone_tolerance_table.grid.toggle_display(field, false);
		});
		frm.refresh_fields();
	}
}
