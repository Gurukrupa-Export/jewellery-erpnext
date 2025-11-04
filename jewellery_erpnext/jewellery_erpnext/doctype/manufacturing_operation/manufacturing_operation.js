// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Manufacturing Operation", {
	refresh: function (frm) {
		if (!frm.doc.__islocal && !frappe.user.has_role("System Manager")) {
			frm.set_df_property("department_target_table", "hidden", 1);
			frm.set_df_property("department_source_table", "hidden", 1);
			frm.set_df_property("employee_target_table", "hidden", 1);
			frm.set_df_property("employee_source_table", "hidden", 1);
		}
		set_html(frm);
		if (
			frm.doc.is_last_operation &&
			frm.doc.for_fg &&
			["Not Started", "WIP"].includes(frm.doc.status)
		) {
			frm.add_custom_button(__("Finish"), async () => {
				await frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_linked_stock_entries_for_serial_number_creator",
					args: {
						mwo: frm.doc.manufacturing_work_order,
						department: frm.doc.department,
						design_id_bom: frm.doc.design_id_bom,
						qty: frm.doc.qty
					},
					callback: function (r) {
						frappe.call({
							method: "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.get_operation_details",
							args: {
								data: r.message,
								docname: frm.doc.name,
								mwo: frm.doc.manufacturing_work_order,
								pmo: frm.doc.manufacturing_order,
								company: frm.doc.company,
								mnf: frm.doc.manufacturer,
								dpt: frm.doc.department,
								for_fg: frm.doc.for_fg,
								design_id_bom: frm.doc.design_id_bom,
							},
						});
					},
				});

				// await frm.call("create_fg")
				// frm.set_value("status", "Finished")
				// frm.save()
			}).addClass("btn-primary");
		}
		// if (in_list(["Not Started", "WIP"], frm.doc.status)) {
		if (["Not Started", "WIP"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Swap Metal"), () => {
			let source_warehouse = "";
			if (frm.doc.mop_balance_table && frm.doc.mop_balance_table.length > 0) {
				source_warehouse = frm.doc.mop_balance_table[0].s_warehouse || "";
			}
				// const serializedMopBalanceTable = JSON.stringify(frm.doc.mop_balance_table);
				frappe.route_options = {
					department: frm.doc.department,
					manufacturing_order: frm.doc.manufacturing_order,
					manufacturer: frm.doc.manufacturer,
					work_order: frm.doc.manufacturing_work_order,
					operation: frm.doc.name,
					employee: frm.doc.employee,
					source_warehouse: source_warehouse,
					target_warehouse:source_warehouse,

				};
				frappe.set_route("Form", "Swap Metal", "new-swap-metal");
			}).addClass("btn-primary");
		}
		if (!frm.doc.__islocal) {
			// if (!in_list(["Finished", "On Hold"], frm.doc.status)) {
			if (!["Finished", "On Hold"].includes(frm.doc.status)) {
				frm.add_custom_button(__("On Hold"), () => {
					frm.set_value("status", "On Hold");
					frm.save();
				});
			}
			// if (in_list(["On Hold"], frm.doc.status)) {
			if (["On Hold"].includes(frm.doc.status)) {
				frm.add_custom_button(__("Resume"), () => {
					frm.set_value(
						"status",
						frm.doc.employee || frm.doc.subcontractor ? "WIP" : "Not Started"
					);
					frm.save();
				});
			}
		}
		// timer code
		frm.toggle_display("started_time", false);
		frm.toggle_display("current_time", false);

		frappe.flags.pause_job = 0;
		frappe.flags.resume_job = 0;

		if (frm.doc.docstatus == 0 && !frm.is_new()) {
			// ****timer custome buttton trigger***
			frm.trigger("prepare_timer_buttons");

			// if Job Card is link to Work Order, the job card must not be able to start if Work Order not "Started"
			// and if stock mvt for WIP is required
			// if (frm.doc.work_order) {
			// 	frappe.db.get_value('Work Order', frm.doc.work_order, ['skip_transfer', 'status'], (result) => {
			// 		if (result.skip_transfer === 1 || result.status == 'In Process' || frm.doc.transferred_qty > 0 || !frm.doc.items.length) {
			// 			frm.trigger("prepare_timer_buttons");
			// 		}
			// 	});
			// } else {
			// 	frm.trigger("prepare_timer_buttons");
			// }
		}

		if (["Draft", "WIP", "Not Started"].includes(frm.doc.status)) {
			frm.trigger("setup_buttons")
		}
	},
	setup(frm) {
		frm.set_query("item_code", "loss_details", function (doc, cdt, cdn) {
			return {
				query: "jewellery_erpnext.query.get_scrap_items",
				filters: { manufacturing_operation: doc.name },
			};
		});
	},
	//# timer code
	prepare_timer_buttons: function (frm) {
		frm.trigger("make_dashboard");

		if (!frm.doc.started_time && !frm.doc.current_time) {
			frm.add_custom_button(__("Start Job"), () => {
				if ((frm.doc.employee && !frm.doc.employee.length) || !frm.doc.employee) {
					// console.log('if HERE')
					frappe.prompt(
						{
							fieldtype: "Link",
							label: __("Select Employees"),
							options: "Employee",
							fieldname: "employees",
						},
						(d) => {
							// console.log(d.employees[0]['employee'])
							frm.events.start_job(frm, "WIP", d.employees);
						},
						__("Assign Job to Employee")
					);
				} else {
					// console.log('else HERE')
					frm.events.start_job(frm, "WIP", frm.doc.employee);
				}
			}).addClass("btn-primary");
		}
		// else if (frm.doc.status == "QC Pending"){
		// 	frm.add_custom_button(__("Resume Job"), () => {
		// 		frm.events.start_job(frm, "Resume Job", frm.doc.employee);
		// 	}).addClass("btn-primary");
		// }
		// else if(frm.doc.status == "Work In Progress"){
		// 	frm.add_custom_button(__("Pause Job"), () => {
		// 		frm.events.start_job(frm, "On Hold");
		// 	});
		// 	// .addClass("btn-primary");
		// 	frm.add_custom_button(__("Complete Job"), () => {
		// 		var sub_operations = frm.doc.sub_operations;

		// 		let set_qty = true;
		// 		if (sub_operations && sub_operations.length > 1) {
		// 			set_qty = false;
		// 			let last_op_row = sub_operations[sub_operations.length - 2];

		// 			if (last_op_row.status == 'Complete') {
		// 				set_qty = true;
		// 			}
		// 		}

		// 		if (set_qty) {
		// 			frm.events.complete_job(frm, "Complete", 0.0);
		// 		}
		// 	}).addClass("btn-primary");
		// }
		// else if (frm.doc.status == "QC Pending" || frm.doc.status == "On Hold") {
		else if (frm.doc.status == "On Hold") {
			if (frm.doc.on_hold == 0) {
				frm.events.start_job(frm, "WIP", frm.doc.employee);
				frm.save();
			} else {
				frm.add_custom_button(__("Resume Job"), () => {
					frm.events.start_job(frm, "Resume Job", frm.doc.employee);
				}).addClass("btn-primary");
			}
		} else if (frm.doc.status == "WIP" && frm.doc.on_hold == 1) {
			frm.events.complete_job(frm, "On Hold");
			frm.add_custom_button(__("Resume Job"), () => {
				frm.events.start_job(frm, "Resume Job", frm.doc.employee);
			}).addClass("btn-primary");
			frm.save();
		} else {
			frm.add_custom_button(__("Pause Job"), () => {
				frm.events.complete_job(frm, "On Hold");
			});

			frm.add_custom_button(__("Complete Job"), () => {
				var sub_operations = frm.doc.sub_operations;

				let set_qty = true;
				if (sub_operations && sub_operations.length > 1) {
					set_qty = false;
					let last_op_row = sub_operations[sub_operations.length - 2];

					if (last_op_row.status == "Finished") {
						set_qty = true;
					}
				}

				if (set_qty) {
					frm.events.complete_job(frm, "Finished", 0.0);
					// 	frappe.prompt({fieldtype: 'Float', label: __('Completed Quantity'),
					// 		fieldname: 'qty', default: frm.doc.for_quantity}, data => {
					// 		frm.events.complete_job(frm, "Complete", data.qty);
					// 	}, __("Enter Value"));
					// } else {
				}
			}).addClass("btn-primary");
		}
	},
	//# timer code
	make_dashboard: function (frm) {
		if (frm.doc.__islocal) return;

		frm.dashboard.refresh();
		const timer = `
			<div class="stopwatch" style="font-weight:bold;margin:0px 13px 0px 2px;
				color:#545454;font-size:18px;display:inline-block;vertical-align:text-bottom;>

			</div>`;

		var section = frm.toolbar.page.add_inner_message(timer);

		let currentIncrement = frm.doc.current_time || 0;
		if (frm.doc.started_time || frm.doc.current_time) {
			if (frm.doc.status == "QC Pending") {
				updateStopwatch(currentIncrement);
			} else if (frm.doc.status == "On Hold") {
				updateStopwatch(currentIncrement);
			} else {
				currentIncrement += moment(frappe.datetime.now_datetime()).diff(
					moment(frm.doc.started_time),
					"seconds"
				);
				initialiseTimer(section, currentIncrement);
			}
		}
	},
	timer: function (frm) {
		return `<button> Start </button>`;
	},
	validate: function (frm) {
		if ((!frm.doc.time_logs || !frm.doc.time_logs.length) && frm.doc.started_time) {
			frm.trigger("reset_timer");
		}
	},
	reset_timer: function (frm) {
		frm.set_value("started_time", "");
	},
	hide_timer: function (frm) {
		frm.toolbar.page.inner_toolbar.find(".stopwatch").remove();
	},
	start_job: function (frm, status, employee) {
		const args = {
			job_card_id: frm.doc.name,
			start_time: frappe.datetime.now_datetime(),
			employees: employee,
			status: status,
		};
		frm.events.make_time_log(frm, args);
	},

	complete_job: function (frm, status) {
		const args = {
			job_card_id: frm.doc.name,
			complete_time: frappe.datetime.now_datetime(),
			status: status,
			// completed_qty: completed_qty
		};
		frm.events.make_time_log(frm, args);
	},
	make_time_log: function (frm, args) {
		frm.events.update_sub_operation(frm, args);
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.make_time_log",
			args: {
				data: args,
			},
			freeze: true,
			callback: function () {
				frm.reload_doc();
				frm.trigger("make_dashboard");
			},
		});
	},
	update_sub_operation: function (frm, args) {
		if (frm.doc.sub_operations && frm.doc.sub_operations.length) {
			let sub_operations = frm.doc.sub_operations.filter((d) => d.status != "Complete");
			if (sub_operations && sub_operations.length) {
				args["sub_operation"] = sub_operations[0].sub_operation;
			}
		}
	},
	setup_buttons: (frm) => {
		frm.add_custom_button(__("Make Receive Entry"), () => {
			this.data = frm.doc.mop_balance_table.map((e) => {
				return {
					"item_code": e.item_code,
					"qty": e.qty,
					"pcs": flt(e.pcs),
					"s_warehouse": e.s_warehouse,
					"batch_no": e.batch_no,
					"inventory_type": e.inventory_type,
					"department": e.department,
					"to_department": e.to_department,
					"customer":e.customer
				}
			})

			let d = new frappe.ui.Dialog({
				title: __("Create Material Receive WO"),
				size: "extra-large",
				fields: [
					{
						fieldname: "receive_entries",
						label: __("Receive Entry Details"),
						fieldtype: "Table",
						cannot_add_rows: 1,
						cannot_delete_rows: 1,
						data: this.data,
						get_data: () => {
							return this.data;
						},
						fields: [
							{
								"label": __("Item Code"),
								"fieldtype": "Link",
								"fieldname": "item_code",
								"options": "Item",
								"in_list_view": 1,
								"read_only": 1,
							},
							{
								"label": __("Source Warehouse"),
								"fieldtype": "Link",
								"fieldname": "s_warehouse",
								"in_list_view": 1,
								"read_only": 1
							},
							{
								"label": __("Qty"),
								"fieldtype": "Float",
								"fieldname": "qty",
								"in_list_view": 1,
								"read_only": 1,
								"columns": 1,

							},
							{
								"label": __("Pcs"),
								"fieldtype": "Float",
								"fieldname": "pcs",
								"in_list_view": 1,
								"read_only": 1,
								"columns": 1,
							},
							{
								"label": __("Receive Quantity"),
								"fieldtype": "Float",
								"fieldname": "receive_qty",
								"in_list_view": 1,
							},
							{
								"label": __("Receive Pcs"),
								"fieldtype": "Float",
								"fieldname": "receive_pcs",
								"in_list_view": 1,
							},
							{
								"label": __("Batch No"),
								"fieldtype": "Link",
								"options": "Batch",
								"fieldname": "batch_no",
								"read_only": 1,
							},
							{
								"label": __("Inventory Type"),
								"fieldtype": "Link",
								"options": "Inventory Type",
								"fieldname": "inventory_type",
								"read_only": 1,
							},
							{
								"label": __("Customer"),
								"fieldtype": "Link",
								"options": "Customer",
								"fieldname": "customer",
								"read_only": 1,
							},
							{
								"label": __("Department"),
								"fieldtype": "Link",
								"options": "Department",
								"fieldname": "department",
								"read_only": 1,
							},
															{
								"label": __("To Department"),
								"fieldtype": "Link",
								"options": "Department",
								"fieldname": "to_department",
								"read_only": 1,
							}
						]
					}
				],
				primary_action_label: __("Create Material Receive Entry"),
				primary_action: (r) => {
					let receive_items = []
					let is_qty_and_pcs = false

					r.receive_entries.forEach(e => {
						let is_metal_or_finding = false
						is_qty_and_pcs = e.receive_qty && e.receive_pcs

						if (["M", "F"].includes(e.item_code.split('-')[0])) {
							is_metal_or_finding = true
							is_qty_and_pcs = false
						}

						if (e.receive_qty > e.qty) {
							frappe.throw(__("Row <b>{0}</b> Item <b>{1}</b> : Receive Qty <b>{2}</b> should not be greater than Balance Qty <b>{3}</b>", [e.idx, e.item_code, e.receive_qty, e.qty]))
						}
						if (e.receive_pcs > e.pcs && e.pcs>0) {
							frappe.throw(__("Row <b>{0}</b> Item <b>{1}</b> : Receive Pcs <b>{2}</b> should not be greater than Balance Pcs <b>{3}</b>", [e.idx, e.item_code, e.receive_pcs, e.pcs]))
						}
						if ((is_qty_and_pcs) && ((e.receive_pcs === e.pcs && e.receive_qty !== e.qty) || (e.receive_qty === e.qty && e.receive_pcs !== e.pcs))) {
							frappe.throw(__("Row <b>{0}</b> Item <b>{1}</b> : Receive Qty and Pcs should be same if receiving all qty or pcs", [e.idx, e.item_code]))
						}

						if (is_qty_and_pcs || (is_metal_or_finding && e.receive_qty)) {
							receive_items.push({
								idx: e.idx,
								item_code: e.item_code,
								s_warehouse: e.s_warehouse,
								qty: e.receive_qty,
								pcs: e.receive_pcs,
								batch_no: e.batch_no,
								inventory_type: e.inventory_type,
								department: e.department,
								to_department: e.to_department,
								customer: e.customer
							})
						}
					});

					if (receive_items.length === 0) {
						frappe.msgprint(__("No Receive Items found for Material Receive stock entry creation"))
					}

					frappe.call({
						method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.create_mr_wo_stock_entry",
						args: {
							se_data: {
								manufacturing_work_order: frm.doc.manufacturing_work_order,
								manufacturing_operation: frm.doc.name,
								manufacturing_order: frm.doc.manufacturing_order,
								department: frm.doc.department,
								receive_items: receive_items
							}
						},
						freeze: true,
						freeze_message: __("Creating Material Receive Entry...."),
						callback: (r) => {
							if (!r.exec) {
								let se_link = frappe.utils.get_form_link(
									r.message.doctype,
									r.message.docname,
									true
								)
								frappe.msgprint({
									message: __("Material Receive Entry has been created successfully {0}", [se_link]),
									title: __("Material Receive Stock Entry Created"),
									indicator: "green"
								})

								frm.reload_doc()
								frm.refresh();
							}
						}
					})

					d.hide()
				}
			})

			d.get_field("receive_entries").grid.wrapper.find(".grid-row-check").hide();
			d.show()

		}).addClass("btn-primary")

	}
});
function initialiseTimer(section, currentIncrement) {
	const interval = setInterval(function () {
		var current = setCurrentIncrement(currentIncrement);
		updateStopwatch(current, section);
	}, 1000);
}

function updateStopwatch(increment, section) {
	var hours = Math.floor(increment / 3600);
	var minutes = Math.floor((increment - hours * 3600) / 60);
	var seconds = increment - hours * 3600 - minutes * 60;

	$(section)
		.find(".hours")
		.text(hours < 10 ? "0" + hours.toString() : hours.toString());
	$(section)
		.find(".minutes")
		.text(minutes < 10 ? "0" + minutes.toString() : minutes.toString());
	$(section)
		.find(".seconds")
		.text(seconds < 10 ? "0" + seconds.toString() : seconds.toString());
}

function setCurrentIncrement(currentIncrement) {
	currentIncrement += 1;
	return currentIncrement;
}

function set_html(frm) {
	if (!frm.doc.__islocal && frm.doc.is_last_operation) {
		//ToDo: add function for stock entry detail for normal manufacturing operations
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_linked_stock_entries",
			args: {
				mwo: frm.doc.manufacturing_work_order,
				department: frm.doc.department
			},
			callback: function (r) {
				frm.get_field("stock_entry_details").$wrapper.html(r.message);
			},
		});
	} else {
		frm.get_field("stock_entry_details").$wrapper.html("");
	}
	// frappe.call({
	// 	method: "get_stock_summary",
	// 	doc: frm.doc,
	// 	args: {
	// 		docname: frm.doc.name,
	// 	},
	// 	callback: function (r) {
	// 		frm.get_field("stock_summery").$wrapper.html(r.message);
	// 	},
	// });
	// frappe.call({
	// 	method: "get_stock_entry",
	// 	doc: frm.doc,
	// 	args: {
	// 		docname: frm.doc.name,
	// 	},
	// 	callback: function (r) {
	// 		frm.get_field("stock_entry").$wrapper.html(r.message);
	// 	},
	// });
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_bom_summary",
		args: {
			design_id_bom: frm.doc.design_id_bom,
		},
		callback: function (r) {
			frm.get_field("bom_summery").$wrapper.html(r.message);
		},
	});
}

//# timer code
frappe.ui.form.on("Manufacturing Operation Time Log", {
	// completed_qty: function(frm) {
	// 	frm.events.set_total_completed_qty(frm);
	// },

	to_time: function (frm) {
		frm.set_value("started_time", "");
	},
});

frappe.ui.form.on("MOP Balance Table", {
	item_code: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		frappe.db.get_value("Item", child.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				child.pcs = 1;
			}
		});
	},
});

frappe.ui.form.on("Department Target Table", {
	item_code: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		frappe.db.get_value("Item", child.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				child.pcs = 1;
			}
		});
	},
});
frappe.ui.form.on("Employee Source Table", {
	item_code: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		frappe.db.get_value("Item", child.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				child.pcs = 1;
			}
		});
	},
});
frappe.ui.form.on("Employee Target Table", {
	item_code: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		frappe.db.get_value("Item", child.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				child.pcs = 1;
			}
		});
	},
});
