// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Gemstone Conversion", {
	refresh: function (frm) {
		if (frm.doc.g_source_item) {
			set_batch_filter(frm, "batch");
		}
		frm.fields_dict["g_source_item"].get_query = function (frm) {
			return {
				query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.item_query_filters",
			};
		};
	},
	g_source_item(frm) {
		clear_gemstone_field(frm);
		set_batch_filter(frm, "batch");
		frm.set_value("g_source_qty", null);
		// frm.set_value("g_loss_item", frm.doc.g_source_item);
		if (frm.doc.g_source_item) {
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_loss_item",
				args: {
					company: frm.doc.company,
					souce_item: frm.doc.g_source_item,
					loss_type: frm.doc.loss_type,
				},
				callback: (r) => {
					frm.set_value("g_loss_item", r.message);
				},
			});
			frm.refresh_field("g_loss_item");
		}
	},
	validate(frm) {
		calculate_Gemstone(frm);
	},
	setup(frm) {
		// Set Gemstone Tab Filter
		set_wh_filter(frm, "source_warehouse");
		set_department_filter(frm, "department");
		set_gemstone_filter(frm, "g_source_item");
		set_gemstone_filter(frm, "g_target_item");
	},
	employee(frm) {
		get_detail_tab_value(frm);
	},
	g_target_item(frm) {
		// Calculate Gemstone
		calculate_Gemstone(frm);
	},
	g_source_qty(frm) {
		// Calculate Gemstone
		calculate_Gemstone(frm);
	},
	g_target_qty(frm) {
		// Calculate Gemstone
		calculate_Gemstone(frm);
	},
	source_warehouse(frm) {
		frm.set_value("target_warehouse", frm.doc.source_warehouse);
		frm.refresh_field("target_warehouse");
	},
	batch(frm) {
		frappe.call({
			method: "get_batch_detail",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback: (r) => {
				frm.set_value("batch_avail_qty", r.message[0]);
				frm.set_value("supplier", r.message[1]);
				frm.set_value("customer", r.message[2]);
				frm.set_value("inventory_type", r.message[3]);

				frm.refresh_field("batch_available_qty");
				frm.refresh_field("supplier");
				frm.refresh_field("customer");
				frm.refresh_field("inventory_type");
			},
		});
	},
});
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
function set_gemstone_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				variant_of: "G",
			},
		};
	});
}
function set_batch_filter(frm, field_name) {
	frm.fields_dict[field_name].get_query = function (doc) {
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions.get_filtered_batches",
			filters: {
				item_code: frm.doc.g_source_item,
				warehouse: frm.doc.source_warehouse,
				company: frm.doc.company,
			},
		};
	};
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
function calculate_Gemstone(frm) {
	let g_loss_qty = frm.doc.g_source_qty - frm.doc.g_target_qty;
	frm.set_value("g_loss_qty", g_loss_qty);
	frm.refresh_field("g_loss_qty");
}
function clear_gemstone_field(frm) {
	frm.set_value("g_target_item", null);
	frm.set_value("g_target_qty", 0);
	frm.set_value("g_loss_item", null);
	frm.set_value("g_loss_qty", 0);
}
