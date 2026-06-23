// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refining Entry", {
	setup(frm) {
		// Filters
		frm.set_query("department", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("refining_department", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("loss_item", () => ({
			filters: { is_stock_item: 1 },
		}));
		frm.set_query("scrap_item", () => ({
			filters: { is_stock_item: 1 },
		}));
		frm.set_query("dust_item", () => ({
			filters: { is_stock_item: 1 },
		}));

		// Child table filters
		frm.set_query("item_code", "material_items", () => ({
			filters: { is_stock_item: 1 },
		}));
		frm.set_query("item_code", "refined_gold", () => ({
			filters: { variant_of: "M" },
		}));
		frm.set_query("item", "recovered_diamond", () => ({
			filters: { variant_of: "D" },
		}));
		frm.set_query("item", "recovered_gemstone", () => ({
			filters: { variant_of: "G" },
		}));
	},

	refresh(frm) {
		frm.trigger("set_field_visibility");
		frm.trigger("add_action_buttons");
		frm.trigger("render_raw_material_html");
	},

	refining_type(frm) {
		frm.trigger("set_field_visibility");
		frm.trigger("set_naming_series");
		frm.trigger("department");
	},

	refining_department(frm) {
		if (frm.doc.refining_department) {
			frappe.db
				.get_value(
					"Warehouse",
					{ department: frm.doc.refining_department, warehouse_type: "Raw Material" },
					"name"
				)
				.then((r) => {
					if (r.message && r.message.name) {
						frm.set_value("refining_warehouse", r.message.name);
					}
				});
		}
	},

	department(frm) {
		if (frm.doc.department) {
			let wh_type = "Manufacturing";
			if (frm.doc.department.toLowerCase().includes("final polish")) {
				if (frm.doc.refining_type === "Work Order Refining") {
					wh_type = "Manufacturing";
				} else {
					wh_type = "Scrap";
				}
			}
			frappe.db
				.get_value("Warehouse", { department: frm.doc.department, warehouse_type: wh_type }, "name")
				.then((r) => {
					if (r.message && r.message.name) {
						frm.set_value("warehouse", r.message.name);
					}
				});
		}
	},

	// --- Barcode scan handlers ---

	scan_mwo(frm) {
		if (!frm.doc.scan_mwo) return;
		frm.call("scan_mwo_action", { barcode: frm.doc.scan_mwo }).then(() => {
			frm.reload_doc();
		});
	},

	scan_serial_no(frm) {
		if (!frm.doc.scan_serial_no) return;
		frm.call("scan_serial_no_action", { barcode: frm.doc.scan_serial_no }).then(() => {
			frm.reload_doc();
		});
	},

	scan_scrap_qr(frm) {
		if (!frm.doc.scan_scrap_qr) return;
		frm.call("scan_scrap_qr_action", { barcode: frm.doc.scan_scrap_qr }).then(() => {
			frm.reload_doc();
		});
	},

	// --- Physical verification ---
	physical_quantity(frm) {
		if (frm.doc.refining_type === "Dust Refining") {
			let diff = flt(frm.doc.physical_quantity) - flt(frm.doc.system_quantity);
			frm.set_value("difference_quantity", diff);
		}
	},

	// --- Custom triggers ---

	set_naming_series(frm) {
		const series_map = {
			"Dust Refining": "RFN-DST-.YY.-.#####",
			"Work Order Refining": "RFN-MWO-.YY.-.#####",
			"Serial Number Refining": "RFN-SRN-.YY.-.#####",
			"Scrap Refining": "RFN-SCP-.YY.-.#####",
		};
		if (series_map[frm.doc.refining_type]) {
			frm.set_value("naming_series", series_map[frm.doc.refining_type]);
		}
	},

	set_field_visibility(frm) {
		const type = frm.doc.refining_type;
		// Dust-specific sections
		const is_dust = type === "Dust Refining";
		frm.toggle_display("section_break_dust", is_dust);
		frm.toggle_display("section_break_verification", is_dust || type === "Scrap Refining");

		// MWO-specific
		frm.toggle_display("scan_mwo", type === "Work Order Refining");
		frm.toggle_display("mwo_details", type === "Work Order Refining");

		// SN-specific
		frm.toggle_display("scan_serial_no", type === "Serial Number Refining");
		frm.toggle_display("serial_no_details", type === "Serial Number Refining");

		// Scrap-specific
		frm.toggle_display("scan_scrap_qr", type === "Scrap Refining");
	},

	add_action_buttons(frm) {
		if (frm.is_new()) return;

		const status = frm.doc.status;

		if (status === "Submitted") {
			frm.add_custom_button(
				__("Receive Materials"),
				() => {
					frm.call("receive_materials").then(() => frm.reload_doc());
				},
				__("Actions")
			).addClass("btn-primary");
		}

		if (status === "Received") {
			frm.add_custom_button(
				__("Classify & Generate Recovery"),
				() => {
					frm.call("generate_recovery_table").then(() => frm.reload_doc());
				},
				__("Actions")
			).addClass("btn-primary");
		}

		if (status === "Classified") {
			frm.add_custom_button(
				__("Start Refining"),
				() => {
					frm.call("start_refining").then(() => frm.reload_doc());
				},
				__("Actions")
			).addClass("btn-primary");
		}

		if (status === "Refining In Progress" || status === "Classified") {
			frm.add_custom_button(
				__("Enter Recovered Gold"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "total_recovered_weight",
								fieldtype: "Float",
								label: __("Total Recovered Gold Weight"),
								reqd: 1,
								default: frm.doc.actual_recovery || 0,
							},
						],
						(values) => {
							frm.call("distribute_recovered_gold", {
								total_recovered_weight: values.total_recovered_weight,
							}).then(() => frm.reload_doc());
						},
						__("Recovered Gold"),
						__("Distribute")
					);
				},
				__("Actions")
			);
		}

		if (status === "Recovery Entered") {
			frm.add_custom_button(
				__("Verify Recovery"),
				() => {
					frm.call("verify_recovery").then(() => frm.reload_doc());
				},
				__("Actions")
			).addClass("btn-primary");
		}

		if (status === "Recovery Verified") {
			frm.add_custom_button(
				__("Complete Refining"),
				() => {
					frm.call("complete_refining").then(() => frm.reload_doc());
				},
				__("Actions")
			).addClass("btn-primary");
		}

		if (status === "Completed") {
			frm.add_custom_button(
				__("Transfer to Department"),
				() => {
					frm.call("transfer_recovered_materials").then(() => frm.reload_doc());
				},
				__("Actions")
			).addClass("btn-primary");
		}

		// Dust-specific: fetch balance
		if (frm.doc.refining_type === "Dust Refining" && frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Fetch Dust Balance"), () => {
				frm.call("fetch_dust_balance").then((r) => {
					if (r.message !== undefined) {
						frappe.show_alert({
							message: __("System Qty: {0}", [r.message]),
							indicator: "green",
						});
						frm.reload_doc();
					}
				});
			});
		}
	},

	render_raw_material_html(frm) {
		if (frm.doc.refining_type === "Work Order Refining" && !frm.is_new()) {
			frm.call("get_linked_stock_entries_html").then((r) => {
				if (r.message) {
					frm.get_field("raw_material_html").$wrapper.html(r.message);
				}
			});
		} else {
			frm.get_field("raw_material_html").$wrapper.html("");
		}
	},
});

// --- Child Table Event Handlers ---

frappe.ui.form.on("Refined Gold", {
	refining_gold_weight(frm, cdt, cdn) {
		calculate_pure_weight(frm, cdt, cdn);
	},
	metal_purity(frm, cdt, cdn) {
		calculate_pure_weight(frm, cdt, cdn);
	},
});

function calculate_pure_weight(frm, cdt, cdn) {
	let d = locals[cdt][cdn];
	if (d.refining_gold_weight && d.metal_purity) {
		frappe.db.get_value("Attribute Value", d.metal_purity, "custom_purity_percentage").then((r) => {
			if (r.message && r.message.custom_purity_percentage) {
				let pct = flt(r.message.custom_purity_percentage);
				frappe.model.set_value(cdt, cdn, "pure_weight", flt(d.refining_gold_weight) * (pct / 100));
				frm.refresh_field("refined_gold");
			}
		});
	}
}
