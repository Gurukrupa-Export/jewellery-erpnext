// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refining Entry", {
	setup(frm) {
		// Filters
		frm.set_query("department", () => {
			// Global rule: the Source Department dropdown only offers the logged-in
			// user's own department (from their Employee record, stashed in onload).
			// Users without an Employee record (e.g. Administrator) see all departments.
			const filters = { company: frm.doc.company, is_group: 0 };
			if (frm.__employee_department) {
				filters.name = frm.__employee_department;
			}
			return { filters };
		});
		frm.set_query("refining_department", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("loss_item", () => ({
			filters: { is_stock_item: 1 },
		}));
		frm.set_query("pricing_item", () => ({
			// The pricing categories from the price sheet live in these two groups
			// (seeded by seed_refining_masters).
			filters: { item_group: ["in", ["Refining Scrap", "Refining Chemical"]] },
		}));
		frm.set_query("warehouse", () => {
			let wh_type = ["in", ["Raw Material", "Scrap"]];
			if (frm.doc.is_external) {
				// External refining sends loss/dust/semi-finished material picked by
				// the operator — allow any stock-holding warehouse type.
				wh_type = [
					"in",
					[
						"Raw Material",
						"Scrap",
						"Manufacturing",
						"Finished Goods",
						"Transit of Tagging",
						"Product Certification",
					],
				];
			} else if (frm.doc.refining_type === "Work Order Refining") {
				wh_type = "Manufacturing";
			} else if (frm.doc.refining_type === "Serial Number Refining") {
				wh_type = ["in", ["Finished Goods", "Transit of Tagging", "Product Certification"]];
			}
			return {
				filters: {
					company: frm.doc.company,
					warehouse_type: wh_type,
				},
			};
		});

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

	onload(frm) {
		// Fetch the logged-in user's Employee department once: it both defaults the
		// Source Department on new entries AND restricts the department dropdown to
		// only that department (see the set_query in setup) — the global rule that
		// refining is scoped to the user's own department.
		frappe.db.get_value("Employee", { user_id: frappe.session.user }, "department").then((r) => {
			if (r.message && r.message.department) {
				frm.__employee_department = r.message.department;
				if (frm.is_new() && !frm.doc.department) {
					frm.set_value("department", r.message.department);
				}
			}
		});
	},

	refresh(frm) {
		frm.trigger("set_field_visibility");
		frm.trigger("add_action_buttons");
		frm.trigger("set_recovery_grid_editability");

		// Suppress Frappe Workflow's auto-generated action buttons on submitted docs,
		// AND on child duplicate entries (which rely entirely on custom processing buttons).
		// The Workflow transitions only change state but don't call the actual Python
		// methods (which create stock entries, batches, etc). Our custom "Refining Process"
		// group handles both state transitions AND business logic correctly.
		if (frm.doc.docstatus === 1 || frm.doc.parent_refining_entry) {
			setTimeout(() => {
				// Clear the standard Actions menu
				frm.page.clear_actions_menu();

				// Remove Frappe workflow action buttons from the page header
				frm.page.wrapper.find('.btn-primary-dark[data-action="action_btn"]').hide();
				frm.page.wrapper.find('.btn-primary-light[data-action="action_btn"]').hide();
				frm.page.wrapper.find(".actions-btn-group").hide();
			}, 50);

			// Delayed cleanup for async-rendered workflow buttons
			setTimeout(() => {
				frm.page.clear_actions_menu();
				frm.page.wrapper.find('.btn-primary-dark[data-action="action_btn"]').hide();
				frm.page.wrapper.find('.btn-primary-light[data-action="action_btn"]').hide();
				// Remove "like" workflow action entries from inner action bar
				frm.page.wrapper.find('.inner-group-button [data-action="action_btn"]').parent().hide();
			}, 500);
		}
	},

	refining_type(frm) {
		frm.trigger("set_field_visibility");
		frm.trigger("set_naming_series");
		frm.trigger("department");
		// Re-derive the Pricing Category for the new type (only fills when blank).
		if (frm.doc.is_external) {
			frm.set_value("pricing_item", null);
			frm.trigger("set_default_pricing_item");
		}
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

	is_external(frm) {
		frm.trigger("set_field_visibility");
		frm.trigger("add_action_buttons");
		if (!frm.doc.is_external) {
			frm.set_value("supplier", null);
			frm.set_value("supplier_warehouse", null);
			frm.set_value("pricing_item", null);
		} else {
			frm.trigger("set_default_pricing_item");
		}
	},

	set_default_pricing_item(frm) {
		// Pricing Category defaults from the Refining Type (per the price sheet):
		// Serial/MWO consignments price as Finish & Semi Finish Scrap, Scrap refining
		// as Metal Refining Scrap, Dust as Main Dust (operator switches to Vacuum Bag /
		// Tools Dust / Ultra Liquid etc. as applicable). Server-side defaulting in
		// before_submit_external stays authoritative.
		if (!frm.doc.is_external || frm.doc.pricing_item) return;
		const map = {
			"Serial Number Refining": "REF-FSJ-001",
			"Work Order Refining": "REF-FSJ-001",
			"Scrap Refining": "REF-RMS-001",
			"Dust Refining": "REF-MD-001",
		};
		const item = map[frm.doc.refining_type];
		if (!item) return;
		frappe.db.exists("Item", item).then((exists) => {
			if (exists) frm.set_value("pricing_item", item);
		});
	},

	supplier(frm) {
		// External refining: auto-fetch the supplier's linked warehouse
		// (Warehouse.subcontractor) as soon as the supplier is selected. Prefer a Raw
		// Material-type warehouse (mirrors the server-side _get_supplier_warehouse
		// resolution, which stays authoritative at submit).
		if (!frm.doc.is_external || !frm.doc.supplier) {
			frm.set_value("supplier_warehouse", null);
			return;
		}
		const base = {
			subcontractor: frm.doc.supplier,
			company: frm.doc.company,
			disabled: 0,
			is_group: 0,
		};
		frappe.db.get_value("Warehouse", { ...base, warehouse_type: "Raw Material" }, "name").then((r) => {
			if (r.message && r.message.name) {
				frm.set_value("supplier_warehouse", r.message.name);
				return;
			}
			frappe.db.get_value("Warehouse", base, "name").then((r2) => {
				frm.set_value("supplier_warehouse", r2.message && r2.message.name ? r2.message.name : null);
			});
		});
	},

	department(frm) {
		// External refining sends loss/dust/semi-finished material picked by the
		// operator — never auto-fill their manually chosen Source Warehouse (the
		// field is made editable for external in set_field_visibility).
		if (frm.doc.is_external) return;

		if (frm.doc.department) {
			let wh_type = "Scrap";
			if (frm.doc.refining_type === "Scrap Refining") {
				wh_type = "Raw Material";
			} else if (frm.doc.refining_type === "Work Order Refining") {
				wh_type = "Manufacturing";
			} else if (frm.doc.refining_type === "Serial Number Refining") {
				wh_type = ["in", ["Finished Goods", "Transit of Tagging", "Product Certification"]];
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

	set_recovery_grid_editability(frm) {
		// Only the recovered_pcs / recovered_weight columns are operator-editable, and
		// only while recovery is being entered (on the child processing entry). The
		// present pcs/weight columns stay read-only (they are the seeded reference).
		const editable =
			!!frm.doc.parent_refining_entry &&
			["Refining In Progress", "Recovery Entered"].includes(frm.doc.status);
		["recovered_diamond", "recovered_gemstone"].forEach((table) => {
			const grid = frm.fields_dict[table] && frm.fields_dict[table].grid;
			if (!grid) return;
			grid.update_docfield_property("recovered_pcs", "read_only", editable ? 0 : 1);
			grid.update_docfield_property("recovered_weight", "read_only", editable ? 0 : 1);
			grid.refresh();
		});
	},

	// --- Barcode scan handlers ---

	scan_mwo(frm) {
		if (!frm.doc.scan_mwo) return;
		frappe.show_alert(__("Fetching MWO details..."));
		frm.call("scan_mwo_action", { barcode: frm.doc.scan_mwo }).then(() => {
			frm.refresh();
		});
	},

	scan_serial_no(frm) {
		if (!frm.doc.scan_serial_no) return;
		frappe.show_alert(__("Fetching Serial No details..."));
		frm.call("scan_serial_no_action", { barcode: frm.doc.scan_serial_no }).then(() => {
			frm.refresh();
		});
	},

	scan_scrap_qr(frm) {
		if (!frm.doc.scan_scrap_qr) return;
		frappe.show_alert(__("Fetching Scrap details..."));
		frm.call("scan_scrap_qr_action", { barcode: frm.doc.scan_scrap_qr }).then(() => {
			frm.refresh();
		});
	},

	// --- Physical verification ---
	physical_quantity(frm) {
		if (frm.doc.refining_type !== "Dust Refining") return;

		const recompute = () => {
			let diff = flt(frm.doc.physical_quantity) - flt(frm.doc.system_quantity);
			frm.set_value("difference_quantity", diff);
			frm.set_value("additional_dust_qty", diff > 0 ? diff : 0);
		};

		// Refinery Change Step 2: auto-fetch System Quantity from the Scrap warehouse
		// for comparison once a physical quantity is entered.
		if (frm.doc.warehouse || frm.doc.multiple_department) {
			frm.call("fetch_dust_balance")
				.then((r) => {
					if (r && r.message !== undefined && r.message !== null) {
						frm.set_value("system_quantity", r.message);
					}
				})
				.finally(recompute);
		} else {
			recompute();
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
		const is_external = !!frm.doc.is_external;
		// Dust-specific sections. Physical verification applies to external dust
		// refining too — the counted physical quantity is what actually goes to the
		// supplier, and the excess over system stock is receipted and sent along.
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

		// External refining sends loss/dust/semi-finished material picked by the
		// operator — unlike the internal flow (where Source Warehouse is auto-derived
		// and read-only), the operator picks it manually here.
		frm.set_df_property("warehouse", "read_only", is_external ? 0 : 1);
	},

	add_action_buttons(frm) {
		const status = frm.doc.status;

		// Dust-specific: fetch balance (only during initial setup, not after processing has started)
		const show_dust_btn =
			frm.doc.refining_type === "Dust Refining" &&
			frm.doc.docstatus === 0 &&
			(!status || status === "Draft" || status === "Received");

		if (show_dust_btn) {
			frm.add_custom_button(__("Fetch Dust Balance"), () => {
				if (!frm.doc.warehouse && !frm.doc.multiple_department) {
					frappe.msgprint(__("Please select a Source Warehouse first."));
					return;
				}
				frm.call("fetch_dust_balance").then((r) => {
					if (r && r.message !== undefined && r.message !== null) {
						frm.set_value("system_quantity", r.message);
						frappe.show_alert({
							message: __("System Qty updated to: {0}", [r.message]),
							indicator: "green",
						});
					}
				});
			});

			// Refinery Change Step 3: populate the Material Items table with all
			// available scrap materials (grouped by item group) from the Scrap warehouse.
			frm.add_custom_button(__("Fetch Scrap Materials"), () => {
				if (frm.is_new()) {
					frappe.msgprint(__("Please save the entry first."));
					return;
				}
				frappe.show_alert(__("Fetching scrap materials..."));
				frm.call("fetch_dust_materials").then(() => frm.reload_doc());
			});
		}

		// Scrap-specific: fetch all scrap items across all departments
		const show_scrap_btn =
			frm.doc.refining_type === "Scrap Refining" &&
			frm.doc.docstatus === 0 &&
			(!status || status === "Draft");

		if (show_scrap_btn) {
			frm.add_custom_button(__("Fetch Scrap Items"), () => {
				frm.call("get_scrap_items_balance").then((r) => {
					if (r.message && r.message.length > 0) {
						let d = new frappe.ui.Dialog({
							title: "Select Scrap Items",
							size: "extra-large",
							fields: [
								{
									fieldtype: "HTML",
									fieldname: "instruction",
									options:
										'<div class="text-muted mb-2">Tick the rows you want to refine. Physical Qty is pre-filled with the full available balance — adjust it to refine less.</div>',
								},
								{
									fieldname: "scrap_items",
									fieldtype: "Table",
									cannot_add_rows: true,
									cannot_delete_rows: true,
									in_place_edit: true,
									data: r.message,
									get_data: () => {
										return r.message;
									},
									fields: [
										{
											fieldtype: "Data",
											fieldname: "item_code",
											label: "Item Code",
											in_list_view: 1,
											read_only: 1,
											columns: 2,
										},
										{
											fieldtype: "Data",
											fieldname: "item_group",
											label: "Item Group",
											in_list_view: 1,
											read_only: 1,
											columns: 1,
										},
										{
											fieldtype: "Data",
											fieldname: "warehouse",
											label: "Warehouse",
											in_list_view: 1,
											read_only: 1,
											columns: 2,
										},
										{
											fieldtype: "Data",
											fieldname: "batch_no",
											label: "Batch",
											in_list_view: 1,
											read_only: 1,
											columns: 2,
										},
										{
											fieldtype: "Float",
											fieldname: "actual_qty",
											label: "Available Qty",
											in_list_view: 1,
											read_only: 1,
											columns: 1,
										},
										{
											fieldtype: "Float",
											fieldname: "qty",
											label: "Physical Qty",
											in_list_view: 1,
											reqd: 1,
											columns: 2,
										},
									],
								},
							],
							primary_action_label: "Add to Table",
							primary_action: function (values) {
								// Only add the rows the operator actually TICKED. Physical Qty is
								// pre-filled on every row, so filtering by qty>0 alone (the old
								// behaviour) added every fetched row regardless of the checkboxes.
								let checked = d.fields_dict.scrap_items.grid.get_selected_children();
								if (!checked.length) {
									frappe.msgprint(__("Please tick the rows you want to add."));
									return;
								}
								let selected = checked.filter((row) => row.qty > 0);
								if (selected.length === 0) {
									frappe.msgprint(
										__("Set a Physical Qty greater than 0 on the ticked rows.")
									);
									return;
								}
								if (selected.length > 0) {
									selected.forEach((row) => {
										let existing = (frm.doc.material_items || []).find(
											(m) =>
												m.item_code === row.item_code && m.batch_no === row.batch_no
										);
										if (existing) {
											frappe.model.set_value(
												existing.doctype,
												existing.name,
												"qty",
												row.qty
											);
										} else {
											let child = frm.add_child("material_items");
											child.item_code = row.item_code;
											child.item_group = row.item_group;
											child.warehouse = row.warehouse;
											child.batch_no = row.batch_no;
											child.qty = row.qty;
											child.uom = row.uom;
											child.purity = row.purity;
											child.source_type = "Scrap";
										}
									});
									frm.refresh_field("material_items");
									frm.save();
								}
								d.hide();
							},
						});
						d.show();
					} else {
						frappe.msgprint(__("No available scrap items found."));
					}
				});
			});
		}

		if (frm.is_new()) return;

		// External refining has its own submit-only lifecycle (no classify/repack/
		// verify/complete/transfer) — the internal "Refining Process" buttons below don't
		// apply. Everything happens on this one document: submit auto-creates an
		// optional service Purchase Order, and "Receive Material from Supplier"
		// records the physical receipt directly here (no second Refining Entry).
		if (frm.doc.is_external) {
			if (frm.doc.refining_entry_po) {
				frm.add_custom_button(
					__("View Purchase Order"),
					() => frappe.set_route("Form", "Purchase Order", frm.doc.refining_entry_po),
					__("Refining Process")
				);
			}

			if (frm.doc.docstatus === 1 && !frm.doc.repack_se) {
				let btn = frm.add_custom_button(
					__("Receive Material from Supplier"),
					() => {
						frappe.prompt(
							[
								{
									fieldname: "recovery_weight",
									fieldtype: "Float",
									label: __("Recovery Weight"),
									reqd: 1,
									default: frm.doc.qty_to_refine || 0,
								},
								{
									fieldname: "received_qty",
									fieldtype: "Float",
									label: __("Received Quantity (if applicable)"),
								},
							],
							(values) => {
								// Disable the trigger button for the duration of the call —
								// the dialog itself closes immediately on submit, so without
								// this a user can re-open it and fire a second request before
								// the first one's reload_doc() lands.
								btn.prop("disabled", true);
								frappe.show_alert(__("Receiving Material..."));
								frm.call("receive_from_supplier", {
									recovery_weight: values.recovery_weight,
									received_qty: values.received_qty,
								})
									.then(() => frm.reload_doc())
									.finally(() => btn.prop("disabled", false));
							},
							__("Receive Material from Supplier"),
							__("Receive")
						);
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			}
			return;
		}

		// Parent only buttons
		if (!frm.doc.parent_refining_entry) {
			if (status === "Submitted") {
				let btn = frm.add_custom_button(
					__("Receive Materials"),
					() => {
						btn.prop("disabled", true);
						frappe.show_alert(__("Receiving Materials..."));
						frm.call("receive_materials")
							.then((r) => {
								if (r.message) {
									frappe.set_route("Form", "Refining Entry", r.message);
								} else {
									frm.reload_doc();
								}
							})
							.finally(() => btn.prop("disabled", false));
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			} else if (status === "Received" || status === "Transferred" || status === "Completed") {
				frm.add_custom_button(
					__("View Duplicate Processing Entry"),
					() => {
						frappe.db
							.get_value("Refining Entry", { parent_refining_entry: frm.doc.name }, "name")
							.then((r) => {
								if (r.message && r.message.name) {
									frappe.set_route("Form", "Refining Entry", r.message.name);
								} else {
									frappe.msgprint(__("Duplicate processing entry not found."));
								}
							});
					},
					__("Refining Process")
				);
			}
		}

		// Child (Processing Duplicate) only buttons
		if (frm.doc.parent_refining_entry) {
			if (status === "Received" || status === "Draft") {
				let btn = frm.add_custom_button(
					__("Classify & Generate Recovery"),
					() => {
						btn.prop("disabled", true);
						frappe.show_alert(__("Classifying Materials..."));
						frm.call("generate_recovery_table")
							.then(() => frm.reload_doc())
							.finally(() => btn.prop("disabled", false));
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			}

			if (status === "Classified") {
				let btn = frm.add_custom_button(
					__("Start Refining"),
					() => {
						btn.prop("disabled", true);
						frappe.show_alert(__("Starting Refining..."));
						frm.call("start_refining")
							.then(() => frm.reload_doc())
							.finally(() => btn.prop("disabled", false));
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			}

			if (status === "Refining In Progress") {
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
								frappe.show_alert(__("Distributing Recovered Gold..."));
								frm.call("distribute_recovered_gold", {
									total_recovered_weight: values.total_recovered_weight,
								}).then(() => frm.reload_doc());
							},
							__("Recovered Gold"),
							__("Distribute")
						);
					},
					__("Refining Process")
				);
			}

			if (status === "Recovery Entered") {
				let btn = frm.add_custom_button(
					__("Verify Recovery"),
					() => {
						btn.prop("disabled", true);
						frappe.show_alert(__("Verifying Recovery..."));
						frm.call("verify_recovery")
							.then(() => frm.reload_doc())
							.finally(() => btn.prop("disabled", false));
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			}

			if (status === "Recovery Verified") {
				let btn = frm.add_custom_button(
					__("Complete Refining"),
					() => {
						btn.prop("disabled", true);
						frappe.show_alert(__("Completing Refining..."));
						frm.call("complete_refining")
							.then(() => frm.reload_doc())
							.finally(() => btn.prop("disabled", false));
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			}

			if (status === "Completed") {
				let btn = frm.add_custom_button(
					__("Transfer to Department"),
					() => {
						btn.prop("disabled", true);
						frappe.show_alert(__("Transferring Materials..."));
						frm.call("transfer_recovered_materials")
							.then(() => frm.reload_doc())
							.finally(() => btn.prop("disabled", false));
					},
					__("Refining Process")
				);
				btn.addClass("btn-primary");
			}
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

frappe.ui.form.on("Refining Material Line", {
	item_code(frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.item_code) {
			frappe.db
				.get_list("Item Variant Attribute", {
					filters: { parent: d.item_code, attribute: ["in", ["Metal Purity", "Purity"]] },
					fields: ["attribute_value"],
					limit: 1,
				})
				.then((r) => {
					if (r && r.length > 0 && r[0].attribute_value) {
						frappe.model.set_value(cdt, cdn, "purity", r[0].attribute_value);
					} else {
						// Fallback: parse from item code
						let parts = d.item_code.split("-");
						if (parts.length >= 2) {
							let val = parts[parts.length - 2];
							frappe.db.exists("Attribute Value", val).then((exists) => {
								if (exists) {
									frappe.model.set_value(cdt, cdn, "purity", val);
								} else if (parts.length >= 3) {
									let val2 = parts[parts.length - 3];
									frappe.db.exists("Attribute Value", val2).then((exists2) => {
										if (exists2) {
											frappe.model.set_value(cdt, cdn, "purity", val2);
										}
									});
								}
							});
						}
					}
				});
		}
	},
});

function calculate_pure_weight(frm, cdt, cdn) {
	let d = locals[cdt][cdn];
	if (d.refining_gold_weight && d.metal_purity) {
		// Attribute Value stores the purity as `purity_percentage` (e.g. 91.9), NOT
		// `custom_purity_percentage` (which does not exist on the doctype) — reading the
		// wrong field left pure_weight silently un-recomputed on manual grid edits.
		frappe.db.get_value("Attribute Value", d.metal_purity, "purity_percentage").then((r) => {
			if (r.message && r.message.purity_percentage) {
				let pct = flt(r.message.purity_percentage);
				frappe.model.set_value(cdt, cdn, "pure_weight", flt(d.refining_gold_weight) * (pct / 100));
				frm.refresh_field("refined_gold");
			}
		});
	}
}
