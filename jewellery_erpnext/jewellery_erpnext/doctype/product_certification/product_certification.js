// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

// Both service types key their rows on tree_no + main_slip, so both accept a scanned Tree Number.
const TREE_SCAN_SERVICE_TYPES = ["Fire Assy Service", "XRF Services"];

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

		// Fire Assy / XRF rows are keyed on tree_no + main_slip, so a Tree Number is the
		// natural thing to scan there. Resolved through the same whitelisted method the
		// tree_no child trigger uses, so there is one resolution path, not two.
		let try_tree_number = () => {
			if (!TREE_SCAN_SERVICE_TYPES.includes(frm.doc.service_type)) {
				return Promise.resolve(false);
			}
			return frappe.db.get_value("Tree Number", scanned_value, "name").then((tree_res) => {
				if (!(tree_res && tree_res.message && tree_res.message.name)) return false;

				// One tree is legitimately scanned more than once -- a tree can go for assay in
				// several samples -- so a repeat scan adds another row rather than being
				// refused. Only the WEIGHT needs care: set_fire_assy_issue_weight sums
				// same-tree rows onto one exploded main row, so auto-filling the tree's full
				// weight again would issue twice the metal. Repeats therefore open at 0 for
				// the operator to type, and validate_fire_assy_weight still blocks a submit
				// that leaves one at 0.
				let already_scanned = find_existing_row(frm, "tree_no", scanned_value);

				return (
					frm
						.call("get_item_from_tree_no", { tree_no: scanned_value })
						.then((r) => {
							if (!(r && r.message)) return false;

							frm.add_child("product_details", {
								tree_no: scanned_value,
								main_slip: r.message.main_slip,
								item_code: r.message.item_code || "",
								// The metal drawn off the tree. 0 for a tree with no ledger,
								// and 0 for a repeat scan so two rows cannot sum to twice the
								// tree's weight -- the operator types it, and
								// validate_fire_assy_weight refuses a submit that still reads 0.
								total_weight: already_scanned ? 0 : r.message.total_weight || 0,
							});

							frappe.show_alert({
								message: already_scanned
									? __("Added Tree No: {0} again (row {1}) — enter its weight", [
											scanned_value,
											already_scanned.idx,
									  ])
									: __("Added Tree No: {0}", [scanned_value]),
								indicator: already_scanned ? "orange" : "green",
							});

							frm.refresh_field("product_details");
							return true;
						})
						// The scanned value IS a Tree Number, so a server-side throw is terminal:
						// the error dialog is already up. Swallow the rejection so it does not fall
						// through to "not a valid MWO or Serial No" and does not leave an unhandled
						// promise rejection behind.
						.catch(() => true)
				);
			});
		};

		try_tree_number().then((handled) => {
			if (handled) return;
			scan_mwo_or_serial(frm, scanned_value);
		});
	},
});

// A repeat scan used to append a second row, silently doubling the issued weight.
// Tree rows are exempt: several scan lines for one tree are intentional and are summed
// onto a single exploded main row.
function find_existing_row(frm, fieldname, value) {
	return (frm.doc.product_details || []).find((row) => row[fieldname] === value);
}

function warn_already_scanned(label, value, row) {
	frappe.show_alert({
		message: __("{0} {1} is already in row {2}", [label, value, row.idx]),
		indicator: "red",
	});
}

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

				let duplicate = find_existing_row(frm, "manufacturing_work_order", mwo.name);
				if (duplicate) {
					warn_already_scanned(__("Manufacturing Work Order"), mwo.name, duplicate);
					return;
				}

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

						let duplicate = find_existing_row(frm, "serial_no", sn.name);
						if (duplicate) {
							warn_already_scanned(__("Serial No"), sn.name, duplicate);
							return;
						}

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
							TREE_SCAN_SERVICE_TYPES.includes(frm.doc.service_type)
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
			method: "get_item_from_tree_no",
			args: { tree_no: d.tree_no },
			doc: frm.doc,
			callback: function (r) {
				if (!(r && r.message)) return;
				// set_value, not a bare assignment on locals — the latter leaves the grid
				// clean, so the resolved item silently reverts on the next refresh.
				frappe.model.set_value(cdt, cdn, "item_code", r.message.item_code);
				frappe.model.set_value(cdt, cdn, "main_slip", r.message.main_slip);
				// Overwritten on a tree change, like the serial_no and work order triggers:
				// a different tree means a different weight.
				frappe.model.set_value(cdt, cdn, "total_weight", r.message.total_weight || 0);
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
