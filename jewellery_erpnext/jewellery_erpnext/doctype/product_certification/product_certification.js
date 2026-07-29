// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt
frappe.ui.form.on("Product Certification", {
	refresh(frm) {
		if (frm.doc.service_type) {
			frm.trigger("set_label_for_service_type");
		}
		if (frm.doc.docstatus == 1 && frm.doc.type == "Issue" && frm.doc.receive_status != "Fully Received") {
			frm.add_custom_button(__("Create Receiving"), function () {
				frappe.model.open_mapped_doc({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.create_product_certification_receive",
					frm: frm,
				});
			});
		}
		if (frm.doc.docstatus == 1 && frm.doc.type == "Issue" && frm.doc.receive_status) {
			const colour = {
				"Not Received": "grey",
				"Partially Received": "orange",
				"Fully Received": "green",
			}[frm.doc.receive_status];
			frm.page.set_indicator(__(frm.doc.receive_status), colour || "grey");
		}
	},
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
	scan: function (frm) {
		if (!frm.doc.scan) return;
		let scanned_value = frm.doc.scan.trim();
		frappe.model.set_value(frm.doctype, frm.docname, "scan", ""); // Clear immediately to prevent double-trigger race conditions

		// Fire Assy rows are keyed on tree_no + main_slip, so a Tree Number is the
		// natural thing to scan there. Resolved through the same whitelisted method the
		// tree_no child trigger uses, so there is one resolution path, not two.
		let try_tree_number = () => {
			if (frm.doc.service_type !== "Fire Assy Service") {
				return Promise.resolve(false);
			}
			return frappe.db.get_value("Tree Number", scanned_value, "name").then((tree_res) => {
				if (!(tree_res && tree_res.message && tree_res.message.name)) return false;

				return frm.call("get_item_from_main_slip", { tree_no: scanned_value }).then((r) => {
					if (!(r && r.message)) return false;

					frm.add_child("product_details", {
						tree_no: scanned_value,
						main_slip: r.message.main_slip,
						item_code: r.message.item_code || "",
						// total_weight is deliberately left blank — the operator
						// types the sample weight actually sent for assay.
					});

					frappe.show_alert({
						message: __("Added Tree No: {0}", [scanned_value]),
					});

					frm.refresh_field("product_details");
					return true;
				});
			});
		};

		try_tree_number().then((handled) => {
			if (handled) return;
			scan_mwo_or_serial(frm, scanned_value);
		});
	},
});

function scan_mwo_or_serial(frm, scanned_value) {
	frappe.db
		.get_value("Manufacturing Work Order", scanned_value, [
			"name",
			"item_code",
			"master_bom",
			"manufacturing_order",
			"jewelex_batch_no",
			"manufacturing_operation",
		])
		.then((mwo_res) => {
			if (mwo_res && mwo_res.message && mwo_res.message.name) {
				let mwo = mwo_res.message;

				let get_total_weight = () => {
					if (mwo.manufacturing_operation) {
						return frappe.db
							.get_value("Manufacturing Operation", mwo.manufacturing_operation, [
								"received_gross_wt",
								"gross_wt",
							])
							.then((mop_res) => {
								if (mop_res && mop_res.message) {
									return mop_res.message.received_gross_wt || mop_res.message.gross_wt || 0;
								}
								return 0;
							});
					}
					return Promise.resolve(0);
				};

				get_total_weight().then((total_weight) => {
					frm.add_child("product_details", {
						manufacturing_work_order: mwo.name,
						item_code: mwo.item_code || "",
						bom: mwo.master_bom || "",
						parent_manufacturing_order: mwo.manufacturing_order,
						jewelex_batch_no: mwo.jewelex_batch_no,
						total_weight: total_weight,
					});

					frappe.show_alert({
						message: __("Added MWO: {0}", [scanned_value]),
					});

					frm.refresh_field("product_details");
				});

				return;
			}

			frappe.db
				.get_value("Serial No", scanned_value, [
					"name",
					"item_code",
					"custom_gross_wt",
					"custom_jwelex_tag_no",
					"custom_bom_no",
				])
				.then((sn_res) => {
					if (sn_res && sn_res.message && sn_res.message.name) {
						let sn = sn_res.message;
						let item_code = sn.item_code;

						frappe.db
							.get_value("Item", item_code, ["item_category", "item_subcategory"])
							.then((item_res) => {
								let item_data = item_res.message || {};

								frm.add_child("product_details", {
									serial_no: sn.name,
									jwelex_tag_no: sn.custom_jwelex_tag_no || "",
									item_code: item_code || "",
									total_weight: sn.custom_gross_wt || "",
									category: item_data.item_category || "",
									sub_category: item_data.item_subcategory || "",
									bom: sn.custom_bom_no || "",
								});

								frappe.show_alert({
									message: __("Added Serial No: {0}", [scanned_value]),
								});

								frm.refresh_field("product_details");
							});
					} else {
						frappe.throw(
							frm.doc.service_type === "Fire Assy Service"
								? __(
										"Scanned value {0} is not a valid Tree Number, Manufacturing Work Order or Serial No.",
										[scanned_value]
								  )
								: __(
										"Scanned value {0} is neither a valid Manufacturing Work Order nor a Serial No.",
										[scanned_value]
								  )
						);
					}
				});
		});
}

frappe.ui.form.on("Product Details", {
	serial_no: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (row.serial_no) {
			frappe.db.get_value("Serial No", row.serial_no, ["item_code", "custom_gross_wt"], (r) => {
				frappe.model.set_value(cdt, cdn, "item_code", r.item_code);
				frappe.model.set_value(cdt, cdn, "total_weight", r.custom_gross_wt);
			});
		}
	},
	manufacturing_work_order(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (!row.manufacturing_work_order) {
			return;
		}
		if (row.serial_no) {
			frappe.db.get_value("Serial No", row.serial_no, ["item_code", "custom_bom_no as bom"], (r) => {
				frappe.model.set_value(cdt, cdn, r);
			});
		} else {
			frappe.db.get_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				["item_code", "master_bom as bom", "gross_wt as total_weight", "manufacturing_operation"],
				(r) => {
					frappe.model.set_value(cdt, cdn, "item_code", r.item_code);
					frappe.model.set_value(cdt, cdn, "bom", r.bom);
					if (r.manufacturing_operation) {
						frappe.db.get_value(
							"Manufacturing Operation",
							r.manufacturing_operation,
							["received_gross_wt", "gross_wt"],
							(mop) => {
								frappe.model.set_value(
									cdt,
									cdn,
									"total_weight",
									mop.received_gross_wt || mop.gross_wt || r.total_weight
								);
							}
						);
					} else {
						frappe.model.set_value(cdt, cdn, "total_weight", r.total_weight);
					}
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
			frappe.db.get_value("Serial No", row.serial_no, ["item_code", "custom_bom_no as bom"], (r) => {
				frappe.model.set_value(cdt, cdn, r);
			});
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
