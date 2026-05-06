// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Product Certification", {
	refresh(frm) {
		if (frm.doc.service_type) {
			frm.trigger("set_label_for_service_type");
		}
		if (frm.doc.docstatus == 1 && frm.doc.type == "Issue") {
			frm.add_custom_button(__("Create Receiving"), function () {
				frappe.model.open_mapped_doc({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.create_product_certification_receive",
					frm: frm,
				});
			});
		}
	},
	// service_type(frm) {
	// 	if (frm.doc.service_type){
	// 		frm.trigger("set_label_for_service_type")
	// 	}
	// },
	// set_label_for_service_type(frm) {
	// 	frm.fields_dict['exploded_product_details'].grid.get_docfield('huid').label = frm.doc.service_type == "Hall Marking Service" ? "HUID" : "Certification No";
	//     frm.fields_dict['exploded_product_details'].grid.refresh();
	// },
	setup: function (frm) {
		var fields = [
			["category", "Item Category"],
			["subcategory", "Item Subcategory"],
			["setting_type", "Setting Type"],
			["metal_type", "Metal Type"],
			["metal_purity", "Metal Purity"],
			["metal_touch", "Metal Touch"],
			["metal_colour", "Metal Colour"],
		];

		set_filters_on_child_table_fields(frm, fields, "exploded_product_details");

		frm.set_query("serial_no", "product_details", function (doc, cdt, cdn) {
			var row = locals[cdt][cdn];
			return {
				filters: row.item_code
					? {
							item_code: row.item_code,
					  }
					: {},
			};
		});
	},
});

frappe.ui.form.on("Product Details", {
	serial_no: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (row.serial_no) {
			frappe.db.get_value(
				"Serial No",
				row.serial_no,
				["item_code", "custom_gross_wt"],
				(r) => {
					frappe.model.set_value(cdt, cdn, "item_code", r.item_code);
					frappe.model.set_value(cdt, cdn, "total_weight", r.custom_gross_wt);
				}
			);
		}
	},
	manufacturing_work_order(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (!row.manufacturing_work_order) {
			return;
		}
		if (row.serial_no) {
			frappe.db.get_value(
				"Serial No",
				row.serial_no,
				["item_code", "custom_bom_no as bom"],
				(r) => {
					frappe.model.set_value(cdt, cdn, r);
				}
			);
		} else {
			frappe.db.get_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				["item_code", "master_bom as bom"],
				(r) => {
					frappe.model.set_value(cdt, cdn, r);
				}
			);
		}
	},
	parent_manufacturing_order(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (!row.parent_manufacturing_order) {
			return;
		}
		frappe.db.get_value(
			"Parent Manufacturing Order",
			row.parent_manufacturing_order,
			"gross_weight as total_weight",
			(r) => {
				frappe.model.set_value(cdt, cdn, r);
			}
		);
		if (row.serial_no) {
			frappe.db.get_value(
				"Serial No",
				row.serial_no,
				["item_code", "custom_bom_no as bom"],
				(r) => {
					frappe.model.set_value(cdt, cdn, r);
				}
			);
		} else {
			frappe.db.get_value(
				"Parent Manufacturing Order",
				row.parent_manufacturing_order,
				["item_code", "master_bom as bom"],
				(r) => {
					frappe.model.set_value(cdt, cdn, r);
				}
			);
		}
	},
	tree_no(frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (!d.tree_no) {
			return;
		}
		frappe.call({
			method: "get_item_from_main_slip",
			args: { tree_no: d.tree_no },
			doc: frm.doc,
			callback: function (r) {
				d.item_code = r.message.item_code;
				d.main_slip = r.message.main_slip;
			},
		});
	},
});

function set_filters_on_child_table_fields(frm, fields, tablename) {
	fields.map(function (field) {
		frm.set_query(field[0], tablename, function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}
