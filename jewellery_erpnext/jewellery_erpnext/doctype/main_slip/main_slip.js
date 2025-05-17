// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Main Slip", {
	refresh(frm) {
		frm.add_custom_button(
			__("Stock Ledger"),
			async function () {
				var item = (
					await frappe.call({
						method: "jewellery_erpnext.utils.get_item_from_attribute",
						args: {
							metal_type: frm.doc.metal_type,
							metal_touch: frm.doc.metal_touch,
							metal_purity: frm.doc.metal_purity,
							metal_colour: frm.doc.metal_colour || null,
						},
					})
				).message;
				frappe.route_options = {
					main_slip: [frm.doc.name],
					item_code: item,
					from_date: moment(frm.doc.creation).format("YYYY-MM-DD"),
					to_date: moment(frm.doc.modified).format("YYYY-MM-DD"),
					company: frm.doc.company,
					show_cancelled_entries: frm.doc.docstatus === 2,
				};
				frappe.set_route("query-report", "Stock Ledger");
			},
			__("View")
		);
	},
	multicolour: function (frm) {
		if (frm.doc.multicolour == 1) {
			frm.set_value("metal_colour", null);
			// frm.save()
		}
		if (frm.doc.multicolour == 0) {
			frm.set_value("allowed_colours", null);
			// frm.refresh("allowed_colours")
			// frm.save()
		}
		// frappe.throw("hi")
	},
	setup: function (frm) {
		frm.set_query("metal_touch", function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Metal Touch" },
			};
		});
		frm.set_query("metal_purity", function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Metal Purity", metal_touch: frm.doc.metal_touch },
			};
		});
		frm.set_query("metal_type", function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Metal Type" },
			};
		});
	},
	validate(frm) {
		if (frm.doc.check_color) {
			frm.set_value(
				"naming_series",
				".dep_abbr.-.type_abbr.-.metal_touch.-.metal_purity.-.color_abbr.-.#####"
			);
		} else {
			frm.set_value(
				"naming_series",
				".dep_abbr.-.type_abbr.-.metal_touch.-.metal_purity.-.#####"
			);
		}
		if (frm.doc.multicolour == 1 && frm.doc.allowed_colours == null) {
			frappe.throw(
				__("Mandatory fields required in Main Slip: </br><b>Allowed Colours</b>")
			);
		}
	},
	powder_wt(frm) {
		frm.trigger("calculate_powder_wt");
	},
	calculate_powder_wt(frm) {
		if (!frm.doc.powder_wt) return;
		// frappe.db.get_value(
		// 	"Manufacturing Setting",
		// 	frm.doc.company,
		// 	[
		// 		"powder_value",
		// 		"water_value",
		// 		"boric_value",
		// 		"special_powder_boric_value",
		// 		"power_value_individual",
		// 		"water_value_individual",
		// 	],
		// 	(r) => {
		// 		let water_value = r.water_value;
		// 		let powder_value = r.powder_value;
		// 		if (frm.doc.is_wax_setting) {
		// 			water_value = r.water_value_individual;
		// 			powder_value = r.power_value_individual;
		// 			frm.set_value(
		// 				"boric_powder_weight",
		// 				(frm.doc.powder_wt * r.boric_value) / r.powder_value
		// 			);
		// 			frm.set_value(
		// 				"special_powder_weight",
		// 				(frm.doc.powder_wt * r.special_powder_boric_value) / r.powder_value
		// 			);
		// 		}
		// 		frm.set_value("water_weight", (frm.doc.powder_wt * water_value) / powder_value);
		// 	}
		// );
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
				}
				frm.set_value("water_weight", (frm.doc.powder_wt * water_value) / powder_value);
			}
		);
	},
	tree_wax_wt(frm) {
		if (frm.doc.metal_touch) {
			let field_map = {
				"10KT": "wax_to_gold_10",
				"14KT": "wax_to_gold_14",
				"18KT": "wax_to_gold_18",
				"22KT": "wax_to_gold_22",
				"24KT": "wax_to_gold_24",
			};
			frappe.db.get_value(
				"Manufacturing Setting",
				frm.doc.manufacturer,
				field_map[frm.doc.metal_touch],
				(r) => {
					frm.set_value(
						"computed_gold_wt",
						flt(frm.doc.tree_wax_wt) * flt(r[field_map[frm.doc.metal_touch]])
					);
				}
			);
		}
	},
	// async before_submit(frm) {
	// let promise = new Promise((resolve, reject) => {
	//     var dialog = new frappe.ui.Dialog({
	//         title: __("Submit"),
	//         fields: [
	//             {
	//                 "fieldtype": "Float",
	//                 "label": __("Actual Pending Gold"),
	//                 "fieldname": "actual_pending_metal",
	//                 onchange: () => {
	//                     let actual = flt(dialog.get_value('actual_pending_metal'))
	//                     if (actual > frm.doc.pending_metal) {
	//                         frappe.msgprint("Actual pending gold cannot be greater than pending gold")
	//                         dialog.set_value('actual_pending_metal', 0)
	//                         return
	//                     }
	//                     let loss = frm.doc.pending_metal - actual
	//                     dialog.set_value('metal_loss', loss)
	//                 }
	//             },
	//             {
	//                 "fieldtype": "Float",
	//                 "label": __("Gold Loss"),
	//                 "fieldname": "metal_loss",
	//                 "read_only": 1
	//             }
	//         ],
	//         primary_action: function () {
	//             let values = dialog.get_values();
	//             frappe.call({
	//             	method: 'jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_stock_entries',
	//             	args: {
	//                     'main_slip': frm.doc.name,
	//                     'actual_qty': flt(values.actual_pending_metal),
	//                     'metal_loss': flt(values.metal_loss),
	//             		'metal_type': frm.doc.metal_type,
	//             		'metal_touch': frm.doc.metal_touch,
	//             		'metal_purity': frm.doc.metal_purity,
	//             		'metal_colour': frm.doc.metal_colour,
	//             	},
	//             	callback: function(r) {
	//                     console.log(r.message)
	//             		dialog.hide();
	//                     resolve()
	//             	},
	//             });
	//         },
	//         primary_action_label: __('Submit')
	//     });
	//     dialog.show();
	// });
	//     await promise.catch(() => {
	//     });
	// }
});

frappe.ui.form.on("Main Slip SE Details", {
	create_loss_entry(frm, cdt, cdn) {
		let d = locals[cdt][cdn];

		if (d.qty == d.consume_qty) {
			frappe.throw(__("Can not process loss as qty not available for consumption"));
		}
		var dialog = new frappe.ui.Dialog({
			title: __("Submit"),
			fields: [
				{
					fieldtype: "Link",
					label: __("Manufacturing Operation"),
					fieldname: "manufacturing_operation",
					options: "Manufacturing Operation",
					onchange: () => {
						dialog.set_value("metal_loss", 0);
					},
				},
				{
					fieldtype: "Float",
					label: __("Gold Loss"),
					fieldname: "metal_loss",
				},
			],
			primary_action: function () {
				let values = dialog.get_values();
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_process_loss",
					args: {
						main_slip: frm.doc.name,
						mop: values.manufacturing_operation,
						item: d.item_code,
						qty: flt(d.qty),
						consume_qty: flt(d.consume_qty),
						metal_loss: flt(values.metal_loss),
						batch_no: d.batch_no,
						inventory_type: d.inventory_type,
						customer: d.customer,
					},
					freeze: true,
					freeze_message: __("Processing Loss Entry ..."),
					callback: function (r) {
						if (!r.exc) {
							frappe.msgprint(__(`Loss Entry {0} has been generated`), [r.message]);
							dialog.hide();
						}
					},
				});
			},
			primary_action_label: __("Create"),
		});
		dialog.show();
	},
});
