// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Tree Number", {
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
