// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Manufacturing Setting", {
	setup: function (frm) {
		filter_departments(frm, "default_department");
		filter_departments(frm, "default_diamond_department");
		filter_departments(frm, "default_gemstone_department");
		filter_departments(frm, "default_finding_department");
		filter_departments(frm, "default_other_material_department");
		// frm.set_query("default_department", function(){
		// 	return {
		// 		filters: {
		// 			"company": frm.doc.company
		// 		}
		// 	}
		// })
		frm.set_query("in_transit", function () {
			return {
				filters: {
					company: frm.doc.company,
					is_group: 0,
				},
			};
		});
		frm.set_query("default_fg_warehouse", function () {
			return {
				filters: {
					company: frm.doc.company,
					is_group: 0,
				},
			};
		});
	},
});

function filter_departments(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				company: frm.doc.company,
			},
		};
	});
}
