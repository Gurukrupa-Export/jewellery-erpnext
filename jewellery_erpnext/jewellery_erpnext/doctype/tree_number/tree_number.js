// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Tree Number", {
	refresh(frm) {
		// Issue Material posts a Stock Entry on both standalone and casting trees.
		// Receive Material returns leftover material for both tree kinds: standalone trees receive
		// directly; casting trees return the post-cast leftover after the Employee IR books output.
		// The dialog's pending>0 filter + the server (recv+loss)<=pending cap keep it leftover-only.
		if (frm.is_new()) return;

		let status = frm.doc.status;
		if (["Draft", "Issued", "Partially Received"].includes(status)) {
			frm.add_custom_button(__("Issue Material"), () => issue_material_dialog(frm), __("Material"));
		}
		if (["Issued", "Partially Received"].includes(status)) {
			frm.add_custom_button(__("Receive Material"), () => receive_material_dialog(frm), __("Material"));
		}
		// Submit Tree: manual finalize once the tree has had some receive activity (Received or
		// Partially Received). Locks the tree at "Submitted" — no further Issue/Receive
		// (server-enforced). A Partially Received tree writes off its remaining pending as loss first.
		if (["Received", "Partially Received"].includes(status)) {
			frm.add_custom_button(
				__("Submit Tree"),
				() => {
					let msg =
						status === "Received"
							? __(
									"Finalize this tree? It will be locked at 'Submitted' — no further Issue/Receive."
							  )
							: __(
									"This tree is only Partially Received. Submitting will write off the remaining pending as loss (to Scrap) and lock the tree at 'Submitted' — no further Issue/Receive. Continue?"
							  );
					frappe.confirm(msg, () => {
						frm.call({
							method: "submit_tree",
							doc: frm.doc,
							freeze: true,
							freeze_message: __("Submitting..."),
						}).then((r) => {
							if (!r.exc) {
								frappe.show_alert({ message: __("Tree submitted"), indicator: "green" });
								frm.reload_doc();
							}
						});
					});
				},
				__("Material")
			);
		}
		// A submitted (locked) tree exposes no mutation actions.
		if (status !== "Submitted") {
			frm.add_custom_button(
				__("Reverse Tree Stock Entries"),
				() => {
					frappe.confirm(
						__("Cancel all Stock Entries this tree created and reset its ledger?"),
						() => {
							frm.call({
								method: "reverse_tree_stock_entries",
								doc: frm.doc,
								freeze: true,
								freeze_message: __("Reversing..."),
							}).then((r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("Reversed {0} Stock Entry(s)", [
											(r.message || []).length,
										]),
										indicator: "orange",
									});
									frm.reload_doc();
								}
							});
						}
					);
				},
				__("Material")
			);
		}
	},
	department(frm) {
		// Default the Issue source to the department's Raw Material warehouse.
		if (!frm.doc.department || frm.doc.source_warehouse) return;
		frappe.db
			.get_value(
				"Warehouse",
				{ department: frm.doc.department, warehouse_type: "Raw Material", disabled: 0 },
				"name"
			)
			.then((r) => {
				if (r && r.message && r.message.name) frm.set_value("source_warehouse", r.message.name);
			});
	},
	tree_wax_wt(frm) {
		frm.trigger("compute_gold_wt");
	},
	metal_touch(frm) {
		frm.trigger("compute_gold_wt");
	},
	compute_gold_wt(frm) {
		// Mirror of main_slip.js:147-168 — Wax Tree Weight -> Computed Gold Weight
		if (!frm.doc.metal_touch || !frm.doc.tree_wax_wt) {
			frm.set_value("computed_gold_wt", 0);
			return;
		}
		let field_map = {
			"10KT": "wax_to_gold_10",
			"14KT": "wax_to_gold_14",
			"18KT": "wax_to_gold_18",
			"22KT": "wax_to_gold_22",
			"24KT": "wax_to_gold_24",
		};
		let field = field_map[frm.doc.metal_touch];
		if (!field) return;
		frappe.db.get_value("Manufacturing Setting", frm.doc.manufacturer, field, (r) => {
			frm.set_value("computed_gold_wt", flt(frm.doc.tree_wax_wt) * flt(r[field]));
		});
	},
	powder_wt(frm) {
		frm.trigger("calculate_powder_wt");
	},
	is_wax_setting(frm) {
		frm.trigger("calculate_powder_wt");
	},
	calculate_powder_wt(frm) {
		// Mirror of main_slip.js:86-145 — Powder Wt -> water / boric / special powder weights
		if (!frm.doc.powder_wt) {
			frm.set_value("water_weight", 0);
			frm.set_value("boric_powder_weight", 0);
			frm.set_value("special_powder_weight", 0);
			return;
		}
		frappe.db.get_value(
			"Manufacturing Setting",
			frm.doc.manufacturer,
			[
				"powder_value",
				"water_value",
				"boric_value",
				"special_powder_boric_value",
				"power_value_individual",
				"water_value_individual",
			],
			(r) => {
				let water_value = r.water_value;
				let powder_value = r.powder_value;
				if (frm.doc.is_wax_setting) {
					water_value = r.water_value_individual;
					powder_value = r.power_value_individual;
					frm.set_value(
						"boric_powder_weight",
						(frm.doc.powder_wt * r.boric_value) / r.powder_value
					);
					frm.set_value(
						"special_powder_weight",
						(frm.doc.powder_wt * r.special_powder_boric_value) / r.powder_value
					);
				} else {
					frm.set_value("boric_powder_weight", 0);
					frm.set_value("special_powder_weight", 0);
				}
				frm.set_value("water_weight", (frm.doc.powder_wt * water_value) / powder_value);
			}
		);
	},
});

frappe.ui.form.on("Tree Material Detail", {
	issue_qty: (frm, cdt, cdn) => recompute_pending(frm, cdt, cdn),
	receive_qty: (frm, cdt, cdn) => recompute_pending(frm, cdt, cdn),
	loss_qty: (frm, cdt, cdn) => recompute_pending(frm, cdt, cdn),
});

function recompute_pending(frm, cdt, cdn) {
	let d = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "pending_qty", flt(d.issue_qty) - flt(d.receive_qty) - flt(d.loss_qty));
}

function issue_material_dialog(frm) {
	frappe.prompt(
		[
			{ fieldtype: "Link", options: "Item", fieldname: "item_code", label: __("Item"), reqd: 1 },
			{ fieldtype: "Float", fieldname: "qty", label: __("Qty"), reqd: 1 },
			{
				fieldtype: "Link",
				options: "Warehouse",
				fieldname: "source_warehouse",
				label: __("Source Warehouse"),
				default: frm.doc.source_warehouse,
				description: __("Defaults to the department's Raw Material warehouse."),
			},
		],
		(values) => {
			frm.call({
				method: "issue_material",
				doc: frm.doc,
				args: values,
				freeze: true,
				freeze_message: __("Issuing Material..."),
			}).then((r) => {
				if (!r.exc) {
					frappe.show_alert({
						message: __("Issued via Stock Entry {0}", [r.message]),
						indicator: "green",
					});
					frm.reload_doc();
				}
			});
		},
		__("Issue Material"),
		__("Issue")
	);
}

function receive_material_dialog(frm) {
	let pending_rows = (frm.doc.material_details || []).filter((d) => flt(d.pending_qty) > 0);
	if (!pending_rows.length) {
		frappe.msgprint(__("No pending material to receive."));
		return;
	}

	// Receive Material returns leftover: all issued material (standalone) or the post-cast leftover
	// (casting trees). The per-item pending cap on the server keeps casting returns leftover-only.
	let grid_fields = [
		{
			fieldtype: "Link",
			options: "Item",
			fieldname: "item_code",
			label: __("Item"),
			read_only: 1,
			in_list_view: 1,
			columns: 4,
		},
		{
			fieldtype: "Float",
			fieldname: "pending_qty",
			label: __("Pending"),
			read_only: 1,
			in_list_view: 1,
			columns: 2,
		},
		{
			fieldtype: "Float",
			fieldname: "receive_qty",
			label: __("Receive Qty"),
			in_list_view: 1,
			columns: 3,
		},
		{
			fieldtype: "Float",
			fieldname: "loss_qty",
			label: __("Loss Qty"),
			in_list_view: 1,
			columns: 3,
		},
	];

	let fields = [
		{
			fieldtype: "Table",
			fieldname: "rows",
			label: __("Material"),
			cannot_add_rows: true,
			cannot_delete_rows: true,
			in_place_edit: false,
			data: pending_rows.map((row) => ({
				item_code: row.item_code,
				pending_qty: row.pending_qty,
				receive_qty: 0,
				loss_qty: 0,
			})),
			fields: grid_fields,
		},
	];

	let d = new frappe.ui.Dialog({
		title: __("Receive Material"),
		size: "large",
		fields: fields,
		primary_action_label: __("Receive"),
		primary_action: (data) => {
			let rows = (data.rows || []).filter((r) => flt(r.receive_qty) > 0 || flt(r.loss_qty) > 0);
			if (!rows.length) {
				frappe.msgprint(__("Enter a Receive Qty or Loss Qty on at least one row."));
				return;
			}
			frm.call({
				method: "receive_material",
				doc: frm.doc,
				args: { rows: JSON.stringify(rows) },
				freeze: true,
				freeze_message: __("Receiving Material..."),
			}).then((r) => {
				if (!r.exc) {
					d.hide();
					// receive_material returns a list of 1-2 SE names (received transfer + loss Repack).
					let names = Array.isArray(r.message) ? r.message.join(", ") : r.message;
					frappe.show_alert({
						message: __("Received via Stock Entry(s) {0}", [names]),
						indicator: "green",
					});
					frm.reload_doc();
				}
			});
		},
	});
	d.show();
}
