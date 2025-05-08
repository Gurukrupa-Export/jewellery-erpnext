frappe.ui.form.on("BOM", {
	setup: function (frm) {
		frm.set_query("item", function (doc) {
			return { filters: { is_system_item: 0 } };
		});
	},
	validate: function (frm) {
		calculate_total(frm);
	},
});

frappe.ui.form.on("BOM Metal Detail", {
	cad_weight: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.cad_weight && d.cad_to_finish_ratio) {
			frappe.model.set_value(
				d.doctype,
				d.name,
				"quantity",
				flt((d.cad_weight * d.cad_to_finish_ratio) / 100)
			);
		}
	},
	cad_to_finish_ratio: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.cad_weight && d.cad_to_finish_ratio) {
			frappe.model.set_value(
				d.doctype,
				d.name,
				"quantity",
				flt((d.cad_weight * d.cad_to_finish_ratio) / 100)
			);
		}
	},

	quantity: function (frm) {
		calculate_total(frm);
	},
});
frappe.ui.form.on("BOM Diamond Detail", {
	diamond_sieve_size: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		frappe.db.get_value("Attribute Value", d.diamond_sieve_size, "weight_in_cts").then((r) => {
			frappe.model.set_value(d.doctype, d.name, "weight_per_pcs", r.message.weight_in_cts);
		});
		let filter_value = {
			parent: d.diamond_sieve_size,
			diamond_shape: d.stone_shape,
		};
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.bom.check_diamond_sieve_size_tolerance_value_exist",
			args: {
				filters: filter_value,
			},
			callback: function (r) {
				if (r.message.length == 0 && d.stone_shape == "Round") {
					frappe.msgprint(
						__("Please Insert Diamond Sieve Size Tolerance at Attribute Value")
					);
					frappe.validated = false;
				}
			},
		});
	},
	pcs: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.quantity == 0.0) {
			let filter_value = {
				name: d.diamond_sieve_size,
			};
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.bom.get_weight_in_cts_from_attribute_value",
				args: {
					filters: filter_value,
				},
				callback: function (r) {
					if (r.message) {
						let weight_in_cts = r.message[0].weight_in_cts;
						console.log(weight_in_cts);
						frappe.model.set_value(d.doctype, d.name, "weight_per_pcs", weight_in_cts);
						frappe.model.set_value(
							d.doctype,
							d.name,
							"quantity",
							flt(d.pcs * weight_in_cts)
						);
					}
				},
			});
		}
	},
	quantity: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.pcs > 0) {
			var cal_a = flt(d.quantity / d.pcs, 4);
			console.log(cal_a);
		} else {
			frappe.msgprint(__("Please set PCS value"));
			frappe.validated = false;
		}
		if (d.quantity > 0 && d.stone_shape == "Round" && d.quality) {
			let filter_quality_value = {
				parent: d.diamond_sieve_size,
				diamond_shape: d.stone_shape,
				diamond_quality: d.quality,
			};
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.bom.get_quality_diamond_sieve_size_tolerance_value",
				args: {
					filters: filter_quality_value,
				},
				callback: function (r) {
					console.log(r.message);
					let records = r.message;
					if (records) {
						for (let i = 0; i < records.length; i++) {
							let fromWeight = flt(records[i].from_weight);
							let toWeight = flt(records[i].to_weight);
							if (cal_a >= fromWeight && cal_a <= toWeight) {
								// The cal_a value is within the range, do nothing
								frappe.model.set_value(d.doctype, d.name, "weight_per_pcs", cal_a);
								return;
							} else {
								frappe.msgprint(
									`Calculated value ${cal_a} is outside the allowed tolerance range ${fromWeight} to ${toWeight}`
								);
								frappe.validated = false;
								frappe.model.set_value(d.doctype, d.name, "quantity", null);
								return;
							}
						}
					} else {
						frappe.msgprint(__("Tolerance range record not found"));
						frappe.validated = false;
						frappe.model.set_value(d.doctype, d.name, "quantity", null);
						return;
					}
					frappe.model.set_value(d.doctype, d.name, "weight_per_pcs", cal_a);
				},
			});
		}
		if (d.quantity > 0 && d.stone_shape == "Round" && !d.quality) {
			let filter_universal_value = {
				parent: d.diamond_sieve_size,
				for_universal_value: 1,
			};
			// Get records Universal Attribute Value Diamond Sieve Size
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.bom.get_records_universal_attribute_value",
				args: {
					filters: filter_universal_value,
				},
				callback: function (r) {
					console.log(r.message);
					let records = r.message;
					if (records) {
						for (let i = 0; i < records.length; i++) {
							let fromWeight = flt(records[i].from_weight);
							let toWeight = flt(records[i].to_weight);
							if (cal_a >= fromWeight && cal_a <= toWeight) {
								// The cal_a value is within the range, do nothing
								frappe.model.set_value(d.doctype, d.name, "weight_per_pcs", cal_a);
								return;
							} else {
								frappe.msgprint(
									`Calculated value ${cal_a} is outside the allowed tolerance range ${fromWeight} to ${toWeight}`
								);
								frappe.validated = false;
								frappe.model.set_value(d.doctype, d.name, "quantity", null);
								return;
							}
						}
					} else {
						// If no range includes cal_a for both specific and universal Diamond Sieve Size, throw an error
						frappe.msgprint(__("Tolerance range record not found"));
						frappe.validated = false;
						frappe.model.set_value(d.doctype, d.name, "quantity", null);
						return;
					}
					frappe.model.set_value(d.doctype, d.name, "weight_per_pcs", cal_a);
				},
			});
		}
	},
});
frappe.ui.form.on("BOM Gemstone Detail", {
	quantity: function (frm) {
		calculate_total(frm);
	},
	pcs: function (frm) {
		calculate_total(frm);
	},
});
frappe.ui.form.on("BOM Finding Detail", {
	quantity: function (frm) {
		calculate_total(frm);
	},
});
function calculate_total(frm) {
	let total_metal_weight = 0;
	let diamond_weight = 0;
	let total_gemstone_weight = 0;
	let finding_weight = 0;
	let total_diamond_pcs = 0;
	let total_gemstone_pcs = 0;

	if (frm.doc.metal_detail) {
		frm.doc.metal_detail.forEach(function (d) {
			total_metal_weight += d.quantity;
		});
	}
	if (frm.doc.diamond_detail) {
		frm.doc.diamond_detail.forEach(function (d) {
			diamond_weight += d.quantity;
			total_diamond_pcs += d.pcs;
		});
	}
	if (frm.doc.gemstone_detail) {
		frm.doc.gemstone_detail.forEach(function (d) {
			total_gemstone_weight += d.quantity;
			total_gemstone_pcs += d.pcs;
		});
	}
	if (frm.doc.finding_detail) {
		frm.doc.finding_detail.forEach(function (d) {
			if (d.finding_category != "Chains") {
				finding_weight += d.quantity;
			}
		});
	}
	frm.set_value("total_metal_weight", total_metal_weight);

	frm.set_value("total_diamond_pcs", total_diamond_pcs);
	frm.set_value("diamond_weight", diamond_weight);
	frm.set_value("total_diamond_weight", diamond_weight);

	frm.set_value("total_gemstone_pcs", total_gemstone_pcs);
	frm.set_value("gemstone_weight", total_gemstone_weight);
	frm.set_value("total_gemstone_weight", total_gemstone_weight);

	frm.set_value("finding_weight", finding_weight);
	frm.set_value("metal_and_finding_weight", frm.doc.total_metal_weight + frm.doc.finding_weight);
	if (frm.doc.metal_and_finding_weight) {
		frm.set_value(
			"gold_to_diamond_ratio",
			frm.doc.metal_and_finding_weight / frm.doc.diamond_weight
		);
	}
	if (frm.doc.total_diamond_pcs) {
		frm.set_value("diamond_ratio", frm.doc.diamond_weight / frm.doc.total_diamond_pcs);
	}
}
