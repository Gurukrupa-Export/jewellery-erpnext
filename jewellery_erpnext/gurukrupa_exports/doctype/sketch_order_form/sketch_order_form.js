frappe.ui.form.on("Sketch Order Form", {
	setup: function (frm) {
		// set_item_attribute_filters_in_sketch_order_form_detail_fields(frm);
		frm.set_query("subcategory", "order_details", function (doc, cdt, cdn) {
			let d = locals[cdt][cdn];
			return {
				filters: {
					parent_attribute_value: d.category,
				},
			};
		});
		frm.set_query("sub_setting_type1", "order_details", function (doc, cdt, cdn) {
			let d = locals[cdt][cdn];
			return {
				filters: {
					parent_attribute_value: d.setting_type,
				},
			};
		});
		frm.set_query("sub_setting_type2", "order_details", function (doc, cdt, cdn) {
			let d = locals[cdt][cdn];
			return {
				filters: {
					parent_attribute_value: d.setting_type,
				},
			};
		});
		frm.set_query("tag__design_id", "order_details", function (doc, cdt, cdn) {
			return {
				filters: {
					is_design_code: 1,
				},
			};
		});
		frm.set_query("branch", () => {
			return {
				filters: {
					company: frm.doc.company,
				},
			};
		});
		// set_filter_for_design_n_serial(
		// 	frm,
		// 	[["reference_designid", "reference_tagid"]],
		// 	"order_details"
		// );
		var fields = [
			["category", "Item Category"],
			["setting_type", "Setting Type"],
			["metal_type", "Metal Type"],
			["metal_touch", "Metal Touch"],
			["metal_colour", "Metal Colour"],
			["sizer_type", "Sizer Type"],
			["gemstone_type", "Gemstone Type"],
			["gemstone_size", "Gemstone Size"],
			["stone_changeable","Stone Changeable"]
		];
		set_filters_on_child_table_fields(frm, fields);
	},
	delivery_date: function (frm) {
		validate_dates(frm, frm.doc, "delivery_date");
		set_dates_in_table(frm, "order_details", "delivery_date");
		set_due_days_from_delivery_date_and_order_date(frm);
	},

	estimated_duedate: function (frm) {
		validate_dates(frm, frm.doc, "estimated_duedate");
		set_dates_in_table(frm, "order_details", "estimated_duedate");
	},

	due_days: function (frm) {
		set_delivery_date_from_order_date_and_due_days(frm);
	},

	validate: function (frm) {
		validate_delivery_date_with_order_date(frm);
	},

	customer_code: function (frm) {
		frappe.call({
			method: "jewellery_erpnext.gurukrupa_exports.doctype.sketch_order_form.sketch_order_form.get_customer_orderType",
			args: {
				customer_code: frm.doc.customer_code,
			},
			callback: function (r) {
				if (!r.exc) {
					console.log(r.message);
					var arrayLength = r.message.length;
					if (arrayLength === 1) {
						frm.set_value("order_type", r.message[0].order_type);
						frm.set_df_property("order_type", "read_only", 1);
					} else {
						frm.set_value("order_type", "");
						frm.set_df_property("order_type", "read_only", 0);
					}
				}
			},
		});
	},

	order_type(frm){
		if(frm.doc.order_type=='Purchase'){
			$.each(frm.doc.order_details || [], function (i, d) {
				// d.design_by = frm.doc.order_type;
				d.design_type = 'New Design';
			});
			refresh_field("order_details");
		}
	}
});

frappe.ui.form.on("Sketch Order Form Detail", {
	order_details_add: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		row.delivery_date = frm.doc.delivery_date;
		row.estimated_duedate = frm.doc.estimated_duedate;
		if (frm.doc.order_type === 'Purchase') {
			var df = frappe.utils.filter_dict(cur_frm.fields_dict["order_details"].grid.grid_rows_by_docname[cdn].docfields, { "fieldname": "design_type" })[0];
			if (df) {
				df.read_only = 1
				row.design_type = "New Design"
			}
        }
		refresh_field("order_details");
	},

	tag_id(frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		fetch_item_from_serial(d, "tag_id", "tag__design_id");
		if (d.tag_id) {
			frappe.db.get_value("BOM", { tag_no: d.tag_id }, "name", (r) => {
				frappe.model.set_value(cdt, cdn, "master_bom_no", r.name);
			});
		}
	},

	reference_tagid: function (frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		if (!d.reference_designid && d.reference_tagid) {
			frappe.db.get_value("Serial No", d.reference_tagid, "item_code", (r) => {
				frappe.model.set_value(cdt, cdn, "reference_designid", r.item_code);
			});
		}
	},

	delivery_date(frm, cdt, cdn) {
		let doc = locals[cdt][cdn];
		validate_dates(frm, doc, "delivery_date");
	},

	estimated_duedate(frm, cdt, cdn) {
		let doc = locals[cdt][cdn];
		validate_dates(frm, doc, "estimated_duedate");
	},

	tag__design_id: function (frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		if (d.tag__design_id) {
			frappe.db.get_value(
				"Item",
				{ name: d.tag__design_id },
				["image", "item_category", "item_subcategory", "setting_type", "master_bom"],
				function (value) {
					d.design_image = value.image;
					d.image = value.image;
					d.category = value.item_category;
					d.subcategory = value.item_subcategory;
					d.setting_type = value.setting_type;
					refresh_field("order_details");
				}
			);
		} else {
			d.design_image = "";
			d.image = "";
			d.category = "";
			d.subcategory = "";
			d.setting_type = "";
			d.bom = "";
			refresh_field("order_details");
		}
	},

	// design_image: function (frm, cdt, cdn) {
	// 	refresh_field("order_details");
	// },

	// sketch_image: function (frm, cdt, cdn) {
	// 	refresh_field("order_details");
	// },

	design_type: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		frappe.model.set_value(row.doctype, row.name, "category", "");
		frappe.model.set_value(row.doctype, row.name, "subcategory", "");
		frappe.model.set_value(row.doctype, row.name, "setting_type", "");
		frappe.model.set_value(row.doctype, row.name, "sub_setting_type1", "");
		frappe.model.set_value(row.doctype, row.name, "sub_setting_type2", "");
		frappe.model.set_value(row.doctype, row.name, "qty", "");
		frappe.model.set_value(row.doctype, row.name, "metal_type", "");
		frappe.model.set_value(row.doctype, row.name, "metal_touch", "");
		frappe.model.set_value(row.doctype, row.name, "metal_colour", "");
		frappe.model.set_value(row.doctype, row.name, "budget", "");
		frappe.model.set_value(row.doctype, row.name, "sub_setting_type2", "");
		frappe.model.set_value(row.doctype, row.name, "metal_target", "");
		frappe.model.set_value(row.doctype, row.name, "diamond_target", "");
		frappe.model.set_value(row.doctype, row.name, "product_size", "");
		frappe.model.set_value(row.doctype, row.name, "sizer_type", "");
		// frappe.model.set_value(row.doctype, row.name, "reference_tagdesignid", "");
		// frappe.model.set_value(row.doctype, row.name, "design_image", "");
		// frappe.model.set_value(row.doctype, row.name, "image", "");
		frappe.model.set_value(row.doctype, row.name, "tag__design_id", "");
		frappe.model.set_value(row.doctype, row.name, "item_code", "");
		frappe.model.set_value(row.doctype, row.name, "gemstone_type1", "");
		frappe.model.set_value(row.doctype, row.name, "gemstone_size", "");
	},
	master_bom_no: function (frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		frappe.db.get_value(
			"BOM",
			{ name: d.master_bom_no },
			[
				"length",
				"height",
				"width",
				"sub_setting_type1",
				"sub_setting_type2",
				"metal_type",
				"metal_touch",
				"metal_colour",
				"custom_metal_target",
				"diamond_target",
				"gemstone_type1",
				"gemstone_size",
				"qty",
				"product_size",
				"sizer_type",
			],
			function (value) {
				d.length = value.length;
				d.height = value.height;
				d.width = value.width;
				d.sub_setting_type1 = value.sub_setting_type1;
				d.sub_setting_type2 = value.sub_setting_type2;
				d.metal_type = value.metal_type;
				d.metal_touch = value.metal_touch;
				d.metal_colour = value.metal_colour;
				if (value.custom_metal_target != 0) {
					d.metal_target = value.custom_metal_target;
				} else {
					d.metal_target = value.metal_target;
				}
				d.diamond_target = value.diamond_target;
				d.gemstone_type1 = value.gemstone_type1;
				d.gemstone_size = value.gemstone_size;
				d.qty = value.qty;
				d.product_size = value.product_size;
				d.sizer_type = value.sizer_type;
				refresh_field("order_details");
			}
		);
	},
});

function fetch_item_from_serial(doc, fieldname, itemfield) {
	if (doc[fieldname]) {
		frappe.db.get_value("Serial No", doc[fieldname], "item_code", (r) => {
			frappe.model.set_value(doc.doctype, doc.name, itemfield, r.item_code);
		});
	}
}

// function set_due_days_from_delivery_date_and_order_date(frm) {
// 	frm.set_value(
// 		"due_days",
// 		frappe.datetime.get_day_diff(frm.doc.delivery_date, frm.doc.order_date)
// 	);
// }

function set_due_days_from_delivery_date_and_order_date(frm) {
    if (frm.doc.delivery_date && frm.doc.order_date) {
        let delivery_date = frm.doc.delivery_date.split(" ")[0];
        let order_date = frm.doc.order_date.split(" ")[0];

        let diff_days = frappe.datetime.get_day_diff(delivery_date, order_date);

        frm.set_value("due_days", diff_days);
    }
}


function set_delivery_date_from_order_date_and_due_days(frm) {
	frm.set_value("delivery_date", frappe.datetime.add_days(frm.doc.order_date, frm.doc.due_days));
}

function validate_delivery_date_with_order_date(frm) {
	if (frm.doc.delivery_date < frm.doc.order_date) {
		frappe.msgprint(__("You can not select past date in Delivery Date"));
		frappe.validated = false;
	}
}

function set_filter_for_salesman_name(frm) {
	frm.set_query("salesman_name", function () {
		return {
			filters: { designation: "Sales Person" },
		};
	});
}

// function set_item_attribute_filters_in_sketch_order_form_detail_fields(frm) {
// 	var fields = [
// 		["category", "Item Category"],
// 		["subcategory", "Item Subcategory"],
// 		["setting_type", "Setting Type"],
// 		["subsetting_type", "Sub Setting Type"],
// 		["sub_setting_type2", "Sub Setting Type"],
// 		["gemstone_type1", "Gemstone Type1"],
// 		["gemstone_type2", "Gemstone Type2"],
// 		["gemstone_type3", "Gemstone Type3"],
// 		["gemstone_type4", "Gemstone Type4"],
// 		["gemstone_type5", "Gemstone Type5"],
// 		["gemstone_type6", "Gemstone Type6"],
// 		["gemstone_type7", "Gemstone Type7"],
// 		["gemstone_type8", "Gemstone Type8"],
// 		["gemstone_size", "Gemstone Size"],
// 	];
// 	set_filters_on_child_table_fields(frm, fields, "order_details");
// }

// function set_item_attribute_filters_in_sketch_order_form_setting_type(frm) {
// 	var fields = [["setting_type", "Setting Type"]];
// 	set_filters_on_child_table_fields(frm, fields, "setting_type");
// }

// function set_item_attribute_filters_in_sketch_order_form_colour_stone(frm) {
// 	var fields = [["color_stone", "Gemstone Type"]];
// 	set_filters_on_child_table_fields(frm, fields, "colour_stone");
// }

//set delivery date in all order detail rows from delivery date in parent doc
function set_delivery_date_in_order_details(frm) {
	$.each(frm.doc.order_details || [], function (i, d) {
		d.delivery_date = frm.doc.delivery_date;
	});
	refresh_field("order_details");
}

function set_dates_in_table(frm, table, fieldname) {
	$.each(frm.doc[table] || [], function (i, d) {
		d[fieldname] = frm.doc[fieldname];
	});
	refresh_field(table);
}

function set_filters_on_child_table_fields(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], "order_details", function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}

// function set_filters_on_child_table_fields_with_parent_attribute_value(frm, fields, child_table) {
// 	fields.map(function (field) {
// 		frm.set_query(field[0], "order_details", function () {
// 			return {
// 				query: "jewellery_erpnext.query.item_attribute_query",
// 				filters: { item_attribute: field[1], parent_attribute_value: field[2] },
// 			};
// 		});
// 	});
// 	frm.refresh_field("order_details");
// }

function validate_dates(frm, doc, dateField) {
	let order_date = frm.doc.order_date;
	if (doc[dateField] < order_date) {
		frappe.model.set_value(
			doc.doctype,
			doc.name,
			dateField,
			frappe.datetime.add_days(order_date, 1)
		);
	}
}

function set_filter_for_design_n_serial(frm, fields, table) {
	fields.map(function (field) {
		frm.set_query(field[0], table, function (doc, cdt, cdn) {
			return {
				filters: {
					is_design_code: 1,
				},
			};
		});
		frm.set_query(field[1], table, function (doc, cdt, cdn) {
			var d = locals[cdt][cdn];
			if (d[field[0]]) {
				return {
					filters: {
						item_code: d[field[0]],
					},
				};
			}
			return {};
		});
	});
}

function set_filters_for_design_attributes(frm) {
	frm.set_query("design_attributes", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: "Design Attributes",
			},
		};
	});
	frm.set_query("design_attribute_value_1", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_2", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_3", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_4", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_5", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_6", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_7", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
	frm.set_query("design_attribute_value_8", "design_attributes", function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.query.item_attribute_query",
			filters: {
				item_attribute: d.design_attributes,
			},
		};
	});
}
