// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Diamond Conversion", {
	validate(frm) {
		validate_diamond(frm);
	},
	setup(frm) {
		// Set Diamond Tab Filter
		set_wh_filter(frm, "source_warehouse");
		set_department_filter(frm, "department");
		set_diamond_filter(frm, "item_code", "sc_source_table");
		set_diamond_filter(frm, "item_code", "sc_target_table");
		set_diamond_attribute_filter(frm, "qty", "sc_source_table");
		set_diamond_attribute_filter(frm, "qty", "sc_target_table");
	},
	refresh(frm) {
		set_batch_filter(frm, "sc_source_table");
		// set_batch_filter(frm,"sc_target_table");
		frm.fields_dict["sc_source_table"].grid.get_field("item_code").get_query = function (
			frm,
			cdt,
			cdn
		) {
			return {
				query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.item_query_filters",
			};
		};
	},
	employee(frm) {
		get_detail_tab_value(frm);
	},
	source_warehouse(frm) {
		frm.set_value("target_warehouse", frm.doc.source_warehouse);
		frm.refresh_field("target_warehouse");
	},
});
frappe.ui.form.on("SC Source Table", {
	qty: function (frm, cdt, cdn) {
		calculateSum(frm);
	},
	batch: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.batch) {
			set_batch_value(frm, cdt, cdn);
		}
	},
});
frappe.ui.form.on("SC Target Table", {
	qty: function (frm, cdt, cdn) {
		calculateSum(frm);
	},
	// batch:function(frm,cdt,cdn){
	// 	set_batch_value(frm,cdt,cdn)
	// }
});
//To Set Batch Field Filter
function set_batch_filter(frm, child_table_name) {
	frm.fields_dict[child_table_name].grid.get_field("batch").get_query = function (
		doc,
		cdt,
		cdn
	) {
		var child = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions.get_filtered_batches",
			filters: {
				item_code: child.item_code,
				warehouse: frm.doc.source_warehouse,
				company: frm.doc.company,
			},
		};
	};
}
function set_wh_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				department: frm.doc.department,
			},
		};
	});
}
function set_department_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				company: frm.doc.company,
			},
		};
	});
}
function set_diamond_filter(frm, field, table_name) {
	frm.set_query(field, table_name, () => {
		return {
			filters: {
				variant_of: "D",
			},
		};
	});
}
function set_diamond_attribute_filter(frm, field, table_name) {
	frm.set_query(field, table_name, () => {
		return {
			filters: {
				is_diamond_grade: "1",
			},
		};
	});
}
function get_detail_tab_value(frm) {
	frappe.call({
		method: "get_detail_tab_value",
		doc: frm.doc,
		args: {
			docname: frm.doc.name,
		},
		callback: function (r) {
			frm.refresh_field("department");
			frm.refresh_field("manufacturer");
			frm.refresh_field("source_warehouse");
			frm.refresh_field("target_warehouse");
			frm.refresh_field("branch");
		},
	});
}

function validate_diamond(frm) {
	if (flt(frm.doc.sum_source_table, 3) != flt(frm.doc.sum_target_table, 3)) {
		frappe.throw(__("Source & Target Sum Must be Equal"));
	}
}
function calculateSum(frm) {
	if (frm.doc.sc_source_table) {
		let sum = 0;
		frm.doc.sc_source_table.forEach(function (row) {
			sum += flt(row.qty) || 0;
		});
		frm.set_value("sum_source_table", flt(sum, 3));
		frm.refresh_field("sum_source_table");
	}
	if (frm.doc.sc_target_table) {
		let sum = 0;
		frm.doc.sc_target_table.forEach(function (row) {
			sum += flt(row.qty) || 0;
		});
		frm.set_value("sum_target_table", flt(sum, 3));
		frm.refresh_field("sum_target_table");
	}
}
// To Set Batch Bailance and Respective Details into child table
function set_batch_value(frm, cdt, cdn) {
	frappe.call({
		method: "get_batch_detail",
		doc: frm.doc,
		args: {
			docname: frm.doc.name,
		},
		callback: (r) => {
			if (r.message) {
				var child_doc = locals[cdt][cdn];
				frappe.model.set_value(
					child_doc.doctype,
					child_doc.name,
					"batch_available_qty",
					r.message[0]
				);
				frappe.model.set_value(
					child_doc.doctype,
					child_doc.name,
					"supplier",
					r.message[1]
				);
				frappe.model.set_value(
					child_doc.doctype,
					child_doc.name,
					"customer",
					r.message[2]
				);
				frappe.model.set_value(
					child_doc.doctype,
					child_doc.name,
					"inventory_type",
					r.message[3]
				);
			}
		},
	});
}
