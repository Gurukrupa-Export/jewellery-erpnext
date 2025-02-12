// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metal Conversions", {
	refresh(frm) {
		if (frm.doc.multiple_metal_converter == 0) {
			if (frm.doc.source_item) {
				set_batch_filter(frm, "batch");
			}
		}
		if (frm.doc.multiple_metal_converter == 1) {
			set_child_table_batch_filter(frm, "mc_source_table");
		}
		frm.fields_dict["source_item"].get_query = function (frm) {
			return {
				query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.item_query_filters",
			};
		};
	},
	setup(frm) {
		// Set Metal Tab Filter
		set_wh_filter(frm, "source_warehouse");
		set_department_filter(frm, "department");
		if (frm.doc.multiple_metal_converter == 0) {
			set_Metal_filter(frm, "source_item");
			set_Metal_filter(frm, "target_item");
			set_alloy_filter(frm, "source_alloy");
			set_alloy_filter(frm, "target_alloy");
		} else {
			set_source_metal_table_filter(frm, "item_code", "mc_source_table");
			set_Metal_filter(frm, "m_target_item");
			set_alloy_filter(frm, "alloy");
		}
	},
	employee(frm) {
		get_detail_tab_value(frm);
	},
	source_warehouse(frm) {
		frm.set_value("target_warehouse", frm.doc.source_warehouse);
		frm.refresh_field("target_warehouse");
	},
	multiple_metal_converter(frm) {
		// For Clearing All Field's
		frappe.call({
			method: "clear_fields",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback(r) {
				frm.save();
			},
		});
	},
	source_item(frm) {
		clear_metal_field(frm);
		frm.set_value("source_qty", null);
		set_batch_filter(frm, "batch");
	},
	source_qty(frm) {
		// Clear All Fields
		clear_metal_field(frm);
	},
	batch(frm) {
		frm.set_value("batch_available_qty", null);
		frm.set_value("supplier", null);
		frm.set_value("customer", null);
		frm.set_value("inventory_type", null);
		frappe.call({
			method: "get_batch_detail",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback: (r) => {
				frm.set_value("batch_available_qty", r.message[0]);
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
	target_item(frm) {
		// Calculate Metal
		calculate_metal(frm);
	},
	calculate(frm) {
		calculate_Multiple_conversion(frm);
	},
});
frappe.ui.form.on("MC Source Table", {
	item_code: function (frm, cdt, cdn) {
		var child_doc = locals[cdt][cdn];
		frappe.model.set_value(child_doc.doctype, child_doc.name, "qty", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "batch", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "batch_available_qty", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "inventory_type", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "customer", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "supplier", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "total", null);
	},
	qty: function (frm, cdt, cdn) {
		// calculateSum(frm);
		var child = locals[cdt][cdn];
		if (child.qty > 0) {
			frappe.call({
				method: "get_mc_table_purity",
				doc: frm.doc,
				args: {
					item_code: child.item_code,
					qty: child.qty,
				},
				callback: (r) => {
					if (r.message) {
						var child_doc = locals[cdt][cdn];
						frappe.model.set_value(
							child_doc.doctype,
							child_doc.name,
							"total",
							r.message[0]
						);
					}
				},
			});
		}
	},
	batch: function (frm, cdt, cdn) {
		set_batch_value(frm, cdt, cdn);
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
function set_Metal_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				variant_of: ["in", ["M", "F"]],
			},
		};
	});
}
function set_source_metal_table_filter(frm, field, table_name) {
	frm.set_query(field, table_name, () => {
		return {
			filters: {
				variant_of: ["in", ["M", "F"]],
			},
		};
	});
}
function set_batch_filter(frm, field_name) {
	frm.fields_dict[field_name].get_query = function (doc) {
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions.get_filtered_batches",
			filters: {
				item_code: frm.doc.source_item,
				warehouse: frm.doc.source_warehouse,
				company: frm.doc.company,
			},
		};
	};
}
function set_child_table_batch_filter(frm, child_table_name) {
	frm.fields_dict[child_table_name].grid.get_field("batch").get_query = function (
		doc,
		cdt,
		cdn
	) {
		var child = locals[cdt][cdn];
		console.log(child.item_code);
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
function set_alloy_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				item_group: "Alloy",
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
			frm.refresh_field("branch");
			frm.refresh_field("source_warehouse");
			frm.refresh_field("target_warehouse");
		},
	});
}
function calculate_metal(frm) {
	if (frm.doc.target_item) {
		frappe.call({
			method: "calculate_metal_conversion",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback: function (r) {
				if (r.message) {
					frm.set_value("target_qty", r.message[0]);
					frm.refresh_field("target_qty");

					if (r.message[1] < 0) {
						clear_alloy(frm);
						frm.set_value("target_alloy_check", 1);
						frm.set_value("target_alloy_qty", Math.abs(r.message[1]));
						frm.refresh_field("target_alloy_check");
						frm.refresh_field("target_alloy_qty");
						// frm.save();
					} else if (r.message[1] > 0) {
						clear_alloy(frm);
						frm.set_value("source_alloy_check", 1);
						frm.set_value("source_alloy_qty", r.message[1]);

						frm.refresh_field("source_alloy_check");
						frm.refresh_field("source_alloy_qty");
						// frm.save();
					} else {
						clear_alloy(frm);
						frappe.show_alert(
							{
								message: __(
									"Alloy Selection Invisible Due to Calculation is <b>0</b>"
								),
								indicator: "green",
							},
							5
						);
					}
				}
			},
		});
	}
}
function clear_alloy(frm) {
	frm.set_value("source_alloy_check", "0");
	frm.set_value("source_alloy", null);
	frm.set_value("source_alloy_qty", null);
	frm.set_value("target_alloy_check", "0");
	frm.set_value("target_alloy", null);
	frm.set_value("target_alloy_qty", null);

	frm.refresh_field("source_alloy_check");
	frm.refresh_field("source_alloy");
	frm.refresh_field("source_alloy_qty");
	frm.refresh_field("target_alloy_check");
	frm.refresh_field("target_alloy");
	frm.refresh_field("target_alloy_qty");
}
function calculate_Multiple_conversion(frm) {
	frappe.call({
		method: "calculate_Multiple_conversion",
		doc: frm.doc,
		args: {
			docname: frm.doc.name,
		},
		callback: (r) => {
			console.log(r.message);
			if (r.message) {
				frm.set_value("alloy_qty", 0);
				frm.set_value("m_target_qty", r.message[0]);
				frm.refresh_field("m_target_qty");

				if (r.message[1] < 0) {
					// if alloy_qty return minus then consider is target side stock entry
					frm.set_value("alloy_check", 1);
					frm.refresh_field("alloy_check");
					frm.set_value("alloy_qty", Math.abs(r.message[1]));
					frm.refresh_field("alloy_qty");
					// frm.save();
				}
				if (r.message[1] > 0) {
					frm.set_value("alloy_check", 0);
					frm.refresh_field("alloy_check");
					frm.set_value("alloy_qty", r.message[1]);
					frm.refresh_field("alloy_qty");
					// frm.save();
				}
			}
		},
	});
}
function validate_alloy(frm) {
	if (frm.doc.source_alloy_check == 1 && frm.doc.source_alloy == null) {
		frappe.throw(__("Source Alloy is Missing"));
	}
	if (frm.doc.target_alloy_check == 1 && frm.doc.target_alloy == null) {
		frappe.throw(__("Target Alloy is Missing"));
	}
}
function clear_metal_field(frm) {
	frm.set_value("target_item", null);
	frm.set_value("target_qty", null);
	frm.set_value("source_alloy", null);
	frm.set_value("target_alloy", null);
	frm.set_value("source_alloy_qty", null);
	frm.set_value("target_alloy_qty", null);
	frm.set_value("source_alloy_check", "0");
	frm.set_value("target_alloy_check", "0");
	frm.set_value("batch", null);
	// frm.save();
}

// To Set Batch Bailance and Respective Details into child table
function set_batch_value(frm, cdt, cdn) {
	var child_doc = locals[cdt][cdn];
	frappe.call({
		method: "get_child_batch_detail",
		doc: frm.doc,
		args: {
			// docname: frm.doc.name,
			table_item: child_doc.item_code,
			talble_source_warehouse: frm.doc.source_warehouse,
			table_batch: child_doc.batch,
		},
		callback: (r) => {
			if (r.message) {
				var child_doc = locals[cdt][cdn];

				console.log(r.message);
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
				if (!r.message[3]) {
					frappe.model.set_value(
						child_doc.doctype,
						child_doc.name,
						"inventory_type",
						"Regular Stock"
					);
				} else {
					frappe.model.set_value(
						child_doc.doctype,
						child_doc.name,
						"inventory_type",
						r.message[3]
					);
				}
			}
		},
	});
}
