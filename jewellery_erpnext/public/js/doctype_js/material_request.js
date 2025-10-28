frappe.ui.form.on("Material Request", {
	refresh(frm) {
		frm.trigger("get_items_from_customer_goods");
		frm.trigger("manufacturing_operation_query");
		if (frm.doc.material_request_type === "Material Transfer") {
			frm.add_custom_button(
				__("Material Transfer (In Transit)"),
				() => frm.events.make_in_transit_stock_entry(frm),
				__("Create")
			);
		}
		frm.add_custom_button(
			__("Parent Manufacturing Order"),
			function () {
				erpnext.utils.map_current_doc({
					method: "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request.get_pmo_data",
					source_doctype: "Parent Manufacturing Order",
					target: frm,
					date_field: "posting_date",
					setters: {
						company: frm.doc.company,
					},
					get_query_filters: {
						docstatus: 1,
					},
					size: "extra-large",
				});
			},
			__("Get Items From")
		);
		// if (!frm.doc.custom_mop_se && frm.doc.docstatus == 1) {
		// 	frm.add_custom_button(__("Transfer To MOP"), () => frm.events.make_stock_entry(frm));
		// }
		if (['Material Transferred to Department','Material Transferred'].includes(frm.doc.workflow_state) && frm.doc.custom_operation_type == 'Transfer to Department') {
            if (!frm.custom_buttons['Update Department']) {
                frm.add_custom_button('Update Department', function() {
                    let dialog = new frappe.ui.Dialog({
                        title: 'Update Department',
                        fields: [
                            {
                                label: 'Department',
                                fieldname: 'department',
                                fieldtype: 'Link',
                                options: 'Department',
                                reqd: 1,
								get_query: function() {
                                    return {
                                        filters: {
                                            company: frm.doc.company
                                        }
                                    };
                                }
                            }
                        ],
                        primary_action_label: 'Submit',
                        primary_action: function(values) {
                            frappe.call({
                                method: 'jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request.update_department_and_create_stock_entry',
                                // args: {
                                //     docname: frm.doc.name,
                                //     new_department: values.department
                                // },
                                args: {
									material_request_name: frm.doc.name,      // <-- use correct argument name
									new_department: values.department
								},
								callback: function(r) {
                                    if (!r.exc) {
                                        frappe.msgprint('Department updated successfully');
                                        frm.reload_doc();
                                    }
                                }
                            });
                            dialog.hide();
                        }
                    });
                    dialog.show();
                });
            }
        }
	},
	// manufacturing_operation_query(frm) {
	// 	frappe.db
	// 		.get_list("Manufacturing Work Order", {
	// 			fields: ["manufacturing_operation"],
	// 			filters: {
	// 				manufacturing_order: frm.doc.manufacturing_order,
	// 				docstatus: 1,
	// 			},
	// 		})
	// 		.then((records) => {
	// 			const mop_list = records.map((item) => item.manufacturing_operation);

	// 			frm.set_query("custom_manufacturing_operation", function () {
	// 				return {
	// 					filters: {
	// 						name: ["in", mop_list],
	// 						department_ir_status: ["not in", "In-Transit"],
	// 						"is_finding":0,
	// 					},
	// 				};
	// 			});
	// 		});
	// },
	manufacturing_operation_query(frm) {
		if (frm.doc.custom_manufacturing_work_order) {
			frappe.db
				.get_list("Manufacturing Operation", {
					fields: ["name"],
					filters: {
						manufacturing_work_order: frm.doc.custom_manufacturing_work_order,
						status: "Not Started",
					},
				})
				.then((records) => {
					const mop_list = records.map((item) => item.name);

					frm.set_query("custom_manufacturing_operation", function () {
						return {
							filters: {
								name: ["in", mop_list],
								department_ir_status: ["not in", "In-Transit"],
								"is_finding": 0,
							},
						};
					});
				});

		}
		else {
			frappe.db
				.get_list("Manufacturing Work Order", {
					fields: ["manufacturing_operation"],
					filters: {
						manufacturing_order: frm.doc.manufacturing_order,
						docstatus: 1,
					},
				})
				.then((records) => {
					const mop_list = records.map((item) => item.manufacturing_operation);

					frm.set_query("custom_manufacturing_operation", function () {
						return {
							filters: {
								name: ["in", mop_list],
								department_ir_status: ["not in", "In-Transit"],
								"is_finding": 0,
							},
						};
					});
				});
		}

	},
	// before_workflow_action(frm) {
	// 	if (frm.doc.workflow_state == "Material Transferred") {
	// 		if (!frm.doc.custom_manufacturing_operation) {
	// 			frappe.throw("Please Select Manufacturing Operation");
	// 		}

	// 		frm.events.make_stock_entry(frm);

	// 		// if(!frm.doc.custom_mop_se)
	// 		// 	frappe.throw("Stock Entry Not Created")
	// 	}
	// },
	// make_stock_entry(frm) {
	// 	if (frm.doc.custom_manufacturing_operation) {
	// 		frappe.call({
	// 			method: "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request.make_mop_stock_entry",
	// 			args: {
	// 				self: frm.doc,
	// 				mop: frm.doc.custom_manufacturing_operation,
	// 			},
	// 			// freeze: true,
	// 			callback: function (r) {
	// 				if (r.message) {
	// 					frappe.msgprint(__("Stock Entry Created"));
	// 					frm.set_value("custom_mop_se", r.message);
	// 				}
	// 				// d.hide();
	// 			},
	// 			error: function (err) {
	// 				console.log;
	// 			},
	// 		});
	// 	}
	// },
	make_in_transit_stock_entry(frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.make_in_transit_stock_entry",
			args: {
				source_name: frm.doc.name,
				to_warehouse: frm.doc.set_warehouse,
				transfer_type: frm.doc.custom_transfer_type,
				pmo: frm.doc.manufacturing_order,
				mnfr: frm.doc.custom_manufacturer,
			},
			callback: function (r) {
				if (r.message) {
					let doc = frappe.model.sync(r.message);
					frappe.set_route("Form", doc[0].doctype, doc[0].name);
				}
			},
		});
	},
	validate(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.custom_insurance_amount = flt(d.custom_insurance_rate) * flt(d.qty);
			// d.serial_no = d.custom_serial_no;
		});
		frm.refresh_field("items");
	},
	get_items_from_customer_goods(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(
				__("Stock Entry"),
				function () {
					erpnext.utils.map_current_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.make_stock_in_entry",
						source_doctype: "Stock Entry",
						target: frm,
						date_field: "posting_date",
						setters: {
							stock_entry_type: null,
							purpose: "Material Transfer",
							_customer: frm.doc._customer,
							inventory_type: frm.doc.inventory_type,
						},
						get_query_filters: {
							docstatus: 1,
							purpose: "Material Receipt",
						},
						size: "extra-large",
					});
				},
				__("Get Items From")
			);
		} else {
			frm.remove_custom_button(__("Customer Goods Received"), __("Get Items From"));
		}
	},
});

frappe.ui.form.on("Material Request Item", {
	custom_scan_alternate_item: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.custom_scan_alternate_item) {
			frappe
				.call({
					method: "erpnext.stock.utils.scan_barcode",
					args: {
						search_value: d.custom_scan_alternate_item,
					},
				})
				.then((r) => {
					frappe.model.set_value(cdt, cdn, "custom_scan_alternate_item", null);
					if (r.message.item_code) {
						frappe.model.set_value(
							cdt,
							cdn,
							"custom_alternative_item",
							r.message.item_code
						);
						refresh_field("items");
					} else {
						frappe.msgprint(__("Not able to find Alternative item from Barcode"));
					}
				});
		}
	},
	serial_no: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		if (child.serial_no) {
			if (!child.item_code) {
				frappe.db
					.get_value("Serial No", child.serial_no, [
						"item_code",
						"custom_bom_no",
						"custom_gross_wt",
					])
					.then((r) => {
						frappe.model.set_value(cdt, cdn, "item_code", r.message.item_code);
						frappe.model.set_value(cdt, cdn, "bom_no", r.message.custom_bom_no);
					});
			}
		}
	},
	item_code(frm, cdt, cdn) {
		frm.trigger("custom_insurance_rate");
		let d = locals[cdt][cdn];
		frappe.db.get_value("Item", d.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				d.pcs = 1;
				frm.refresh_field("items");
			}
		});
		if (d.item_code) {
			var args = {
				company: frm.doc.company,
				item_code: d.item_code,
				warehouse: cstr(d.s_warehouse) || cstr(d.t_warehouse),
				transfer_qty: d.transfer_qty,
				serial_no: d.serial_no,
				batch_no: d.batch_no,
				bom_no: d.bom_no,
				expense_account: d.expense_account,
				cost_center: d.cost_center,
				qty: d.qty,
				voucher_type: frm.doc.doctype,
				voucher_no: d.name,
				allow_zero_valuation: 1,
			};

			return frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.get_item_details",
				args: {
					args: args,
				},
				callback: function (r) {
					if (r.message) {
						var d = locals[cdt][cdn];
						$.each(r.message, function (k, v) {
							if (v) {
								frappe.model.set_value(cdt, cdn, k, v); // qty and it's subsequent fields weren't triggered
							}
						});
						refresh_field("items");

						let no_batch_serial_number_value = false;
						if (d.has_serial_no || d.has_batch_no) {
							no_batch_serial_number_value = true;
						}
						frappe.flags.hide_serial_batch_dialog = false;
						frappe.flags.dialog_set = false;

						if (
							no_batch_serial_number_value &&
							!frappe.flags.hide_serial_batch_dialog &&
							!frappe.flags.dialog_set
						) {
							frappe.flags.dialog_set = true;
							frappe.flags.hide_serial_batch_dialog = true;
							erpnext.stock.select_batch_and_serial_no(frm, d);
						} else {
							frappe.flags.dialog_set = false;
						}
					}
				},
			});
		}
	},

	custom_insurance_rate(frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		d.custom_insurance_amount = flt(d.custom_insurance_rate) * flt(d.qty);
		console.log(d.custom_insurance_amount);
		frm.refresh_field("items");
	},
});
erpnext.stock.select_batch_and_serial_no = (frm, item) => {
	let path = "assets/erpnext/js/utils/serial_no_batch_selector.js";

	frappe.db.get_value("Item", item.item_code, ["has_batch_no", "has_serial_no"]).then((r) => {
		if (r.message && (r.message.has_batch_no || r.message.has_serial_no)) {
			item.has_serial_no = r.message.has_serial_no;
			item.has_batch_no = r.message.has_batch_no;
			item.type_of_transaction = item.s_warehouse ? "Outward" : "Inward";

			new erpnext.SerialBatchPackageSelector(frm, item, (r) => {
				var sr_list = [];
				if (r) {
					if (r.entries) {
						r.entries.forEach((element) => {
							if (item.has_batch_no) {
								frappe.model.set_value(item.doctype, item.name, {
									batch_no: element.batch_no,
									qty:
										Math.abs(r.total_qty) /
										flt(
											item.conversion_factor || 1,
											precision("conversion_factor", item)
										),
								});
							} else if (item.has_serial_no) {
								sr_list.push(element.serial_no);
							}
						});
						if (sr_list) {
							var serial_no = sr_list.join(",");
							frappe.model.set_value(
								item.doctype,
								item.name,
								"serial_no",
								serial_no
							);
						}
					}
				}
			});
		}
	});
};
