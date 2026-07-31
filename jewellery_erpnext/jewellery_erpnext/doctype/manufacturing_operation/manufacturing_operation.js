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

		if (frm.doc.is_last_operation && frm.doc.for_fg && ["Not Started", "WIP"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Finish"), async () => {
				await frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_linked_stock_entries_for_serial_number_creator",
					args: {
						mwo: frm.doc.manufacturing_work_order,
						department: frm.doc.department,
						design_id_bom: frm.doc.design_id_bom,
						qty: frm.doc.qty,
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
					target_warehouse: source_warehouse,
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

		if (["Draft", "WIP", "Not Started"].includes(frm.doc.status) && frm.doc.manufacturing_work_order) {
			frm.trigger("setup_buttons");
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
			open_wo_transfer_dialog(frm, {
				fetch_method:
					"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_make_receive_entry_rows",
				create_method:
					"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.create_mr_wo_stock_entry",
				no_sre_title: __("Make Receive Entry"),
				dialog_title: __("Create Material Receive (WORK ORDER)"),
				primary_action_label: __("Create Material Receive Entry"),
				create_freeze_message: __("Creating Material Receive Entry..."),
				created_title: __("Material Receive Stock Entry Created"),
				existing_title: __("Existing Material Receive Stock Entry"),
				result_label: __("Material Receive Entry: {0}"),
			});
		}).addClass("btn-primary");

		// Receive Unused/Loose Material: receives the operation's unused material into the
		// department Raw Material warehouse (SRE machinery, same item code as Make Receive
		// Entry), then repacks it onto the dedicated unused/loose item — M- becomes ML-,
		// F- becomes FL-, at the same purity and colour — under a new batch tagged
		// custom_batch_type="Unused/Loose Material" so refining can fetch it.
		frm.add_custom_button(__("Receive Unused/Loose Material"), () => {
			open_wo_transfer_dialog(frm, {
				fetch_method:
					"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_make_scrap_entry_rows",
				create_method:
					"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.create_scrap_wo_stock_entry",
				no_sre_title: __("Receive Unused/Loose Material"),
				dialog_title: __("Receive Unused/Loose Material — Material Receive (WORK ORDER)"),
				primary_action_label: __("Receive Unused/Loose Material"),
				create_freeze_message: __("Receiving Unused/Loose Material..."),
				created_title: __("Unused/Loose Material Stock Entry Created"),
				existing_title: __("Existing Unused/Loose Material Stock Entry"),
				result_label: __("Unused/Loose Material Entry: {0}"),
			});
		}).addClass("btn-primary");
	},
});

// Shared dialog body for the "Make Receive Entry" and "Receive Unused/Loose Material"
// buttons. Both fetch auto-filled rows from the MWO's Stock Reservation
// Entries, render the same editable table, and POST the selected rows to a
// whitelisted creator. Only the endpoints, warehouse target, and labels differ
// (passed via `opts`).
function open_wo_transfer_dialog(frm, opts) {
	frappe.call({
		method: opts.fetch_method,
		args: { manufacturing_operation: frm.doc.name },
		freeze: true,
		freeze_message: __("Loading Stock Reservation Entries..."),
		callback: (r) => {
			const msg = (r && r.message) || {};
			const rows = Array.isArray(msg) ? msg : msg.rows || [];
			const skipped = Array.isArray(msg) ? [] : msg.skipped || [];
			const active_sre_count = Array.isArray(msg) ? rows.length : msg.active_sre_count || 0;

			if (!rows.length) {
				if (active_sre_count === 0) {
					frappe.msgprint(
						__("No active Stock Reservation Entries found for this Manufacturing Work Order.")
					);
				} else {
					const lines = (skipped || []).map((s) =>
						__("• SRE {0} — Item {1}{2}: SRE remaining {3}, MOP available {4}", [
							s.sre,
							s.item_code,
							s.batch_no ? ` / Batch ${s.batch_no}` : "",
							s.sre_remaining,
							s.mop_available_qty,
						])
					);
					frappe.msgprint({
						title: opts.no_sre_title,
						indicator: "orange",
						message:
							__(
								"Stock Reservation Entries exist, but no receivable MOP balance was found. Please check MOP Log balance for this Manufacturing Operation."
							) + (lines.length ? "<br><br>" + lines.join("<br>") : ""),
					});
				}
				return;
			}

			// Per-dialog request id — stamped on the resulting Stock Entry to
			// short-circuit double-clicks and resubmits server-side.
			// Frappe JS exposes `get_random(N)`, not `get_random_string`.
			const request_id = frappe.utils.get_random(36);

			// Spec field names: reserved_* / mop_available_* / available_to_receive_*.
			// Server endpoint emits these directly; the JS just forwards them
			// into the dialog and surfaces `qty_to_receive` / `pcs_to_receive`
			// as the editable inputs.
			const dialog_data = rows.map((e) => ({
				stock_reservation_entry: e.stock_reservation_entry,
				stock_reservation_entry_detail: e.stock_reservation_entry_detail,
				item_code: e.item_code,
				s_warehouse: e.s_warehouse,
				t_warehouse: e.t_warehouse,
				batch_no: e.batch_no,
				reserved_qty: e.reserved_qty,
				reserved_pcs: e.reserved_pcs || 0,
				delivered_qty: e.delivered_qty,
				already_received_qty: e.already_received_qty,
				already_received_pcs: e.already_received_pcs || 0,
				mop_available_qty: e.mop_available_qty || 0,
				mop_available_pcs: e.mop_available_pcs || 0,
				available_to_receive_qty: e.available_to_receive_qty,
				available_to_receive_pcs: e.available_to_receive_pcs || 0,
				is_pcs_item: e.is_pcs_item ? 1 : 0,
				mop_log_reference: e.mop_log_reference || "",
				warning: e.warning || "",
				qty_to_receive: 0,
				pcs_to_receive: 0,
				stock_uom: e.stock_uom,
			}));

			const d = new frappe.ui.Dialog({
				title: opts.dialog_title,
				size: "extra-large",
				fields: [
					{
						fieldname: "receive_entries",
						label: __("Receive Entry Details"),
						fieldtype: "Table",
						cannot_add_rows: 1,
						cannot_delete_rows: 1,
						data: dialog_data,
						get_data: () => dialog_data,
						fields: [
							{
								label: __("SRE"),
								fieldtype: "Link",
								fieldname: "stock_reservation_entry",
								options: "Stock Reservation Entry",
								read_only: 1,
							},
							{
								label: __("Item Code"),
								fieldtype: "Link",
								fieldname: "item_code",
								options: "Item",
								in_list_view: 1,
								read_only: 1,
							},
							{
								label: __("Source Warehouse"),
								fieldtype: "Link",
								fieldname: "s_warehouse",
								options: "Warehouse",
								read_only: 1,
							},
							{
								label: __("Target Warehouse"),
								fieldtype: "Link",
								fieldname: "t_warehouse",
								options: "Warehouse",
								read_only: 1,
							},
							{
								label: __("Batch No"),
								fieldtype: "Link",
								fieldname: "batch_no",
								options: "Batch",
								in_list_view: 1,
								read_only: 1,
							},
							{
								label: __("Reserved Qty"),
								fieldtype: "Float",
								fieldname: "reserved_qty",
								read_only: 1,
							},
							{
								label: __("Reserved PCS"),
								fieldtype: "Float",
								fieldname: "reserved_pcs",
								read_only: 1,
							},
							{
								label: __("Delivered Qty"),
								fieldtype: "Float",
								fieldname: "delivered_qty",
								read_only: 1,
							},
							{
								label: __("MOP Available Qty"),
								fieldtype: "Float",
								fieldname: "mop_available_qty",
								in_list_view: 0,
								read_only: 1,
							},
							{
								label: __("MOP Available PCS"),
								fieldtype: "Float",
								fieldname: "mop_available_pcs",
								// in_list_view: 1,
								read_only: 1,
							},
							{
								label: __("Already Received"),
								fieldtype: "Float",
								fieldname: "already_received_qty",
								read_only: 1,
							},
							{
								label: __("Already Received PCS"),
								fieldtype: "Float",
								fieldname: "already_received_pcs",
								read_only: 1,
							},
							{
								label: __("Available to Receive Qty"),
								fieldtype: "Float",
								fieldname: "available_to_receive_qty",
								in_list_view: 1,
								read_only: 1,
							},
							{
								label: __("Available to Receive PCS"),
								fieldtype: "Float",
								in_list_view: 1,
								fieldname: "available_to_receive_pcs",
								read_only: 1,
							},
							{
								label: __("Qty to Receive"),
								fieldtype: "Float",
								fieldname: "qty_to_receive",
								in_list_view: 1,
								default: 0,
							},
							{
								label: __("PCS to Receive"),
								fieldtype: "Float",
								fieldname: "pcs_to_receive",
								in_list_view: 1,
								default: 0,
								// Read-only for non-D/G rows. Frappe Table fields
								// don't support per-row hidden cells, so we use
								// read_only_depends_on; server still force-zeros
								// PCS for non-D/G regardless of dialog value.
								// read_only_depends_on: "eval:!doc.is_pcs_item",
							},
							{
								label: __("PCS Item"),
								fieldtype: "Check",
								fieldname: "is_pcs_item",
							},
							{
								label: __("MOP Log Reference"),
								fieldtype: "Link",
								fieldname: "mop_log_reference",
								options: "MOP Log",
								read_only: 1,
							},
							{
								label: __("Warning"),
								fieldtype: "Small Text",
								fieldname: "warning",
								in_list_view: 0,
								read_only: 1,
							},
							{
								label: __("Stock UOM"),
								fieldtype: "Link",
								fieldname: "stock_uom",
								options: "UOM",
								read_only: 1,
							},
						],
					},
				],
				primary_action_label: opts.primary_action_label,
				primary_action: (r_) => {
					const receive_items = [];
					r_.receive_entries.forEach((e) => {
						// Qty must be capped by min(SRE remaining, MOP balance);
						// the server endpoint surfaces that as available_to_receive_qty.
						if (e.qty_to_receive > e.available_to_receive_qty) {
							frappe.throw(
								__(
									"Row <b>{0}</b> Item <b>{1}</b>: Qty to Receive <b>{2}</b> exceeds Available to Receive Qty <b>{3}</b>",
									[e.idx, e.item_code, e.qty_to_receive, e.available_to_receive_qty]
								)
							);
						}
						// PCS validation: D/G rows must respect Available to
						// Receive PCS; non-D/G rows are force-zeroed regardless
						// of dialog value. Server revalidates either way.
						let pcs_to_receive = 0;
						if (e.is_pcs_item) {
							pcs_to_receive = e.pcs_to_receive || 0;
							if (pcs_to_receive < 0) {
								frappe.throw(
									__("Row <b>{0}</b> Item <b>{1}</b>: PCS to Receive must be >= 0", [
										e.idx,
										e.item_code,
									])
								);
							}
							if (e.available_to_receive_pcs && pcs_to_receive > e.available_to_receive_pcs) {
								frappe.throw(
									__(
										"Row <b>{0}</b> Item <b>{1}</b>: PCS to Receive <b>{2}</b> exceeds Available to Receive PCS <b>{3}</b>",
										[e.idx, e.item_code, pcs_to_receive, e.available_to_receive_pcs]
									)
								);
							}
						}
						if (e.qty_to_receive && e.qty_to_receive > 0) {
							receive_items.push({
								idx: e.idx,
								stock_reservation_entry: e.stock_reservation_entry,
								stock_reservation_entry_detail: e.stock_reservation_entry_detail,
								item_code: e.item_code,
								s_warehouse: e.s_warehouse,
								// Server still consumes `qty` and `pcs` keys for
								// receive_items — the dialog field rename is
								// cosmetic only.
								qty: e.qty_to_receive,
								pcs: pcs_to_receive,
								batch_no: e.batch_no,
							});
						}
					});

					if (!receive_items.length) {
						frappe.msgprint(__("No Receive Items selected for Material Receive Stock Entry"));
						return;
					}

					d.disable_primary_action();
					frappe.call({
						method: opts.create_method,
						args: {
							se_data: {
								manufacturing_work_order: frm.doc.manufacturing_work_order,
								manufacturing_operation: frm.doc.name,
								manufacturing_order: frm.doc.manufacturing_order,
								department: frm.doc.department,
								receive_items: receive_items,
							},
							request_id: request_id,
						},
						freeze: true,
						freeze_message: opts.create_freeze_message,
						callback: (resp) => {
							if (resp && resp.message) {
								const se_link = frappe.utils.get_form_link(
									resp.message.doctype,
									resp.message.docname,
									true
								);
								const title = resp.message.idempotent
									? opts.existing_title
									: opts.created_title;
								frappe.msgprint({
									message: __(opts.result_label, [se_link]),
									title: title,
									indicator: "green",
								});
								frm.reload_doc();
							}
						},
						error: () => {
							d.enable_primary_action();
						},
					});

					d.hide();
				},
			});

			d.get_field("receive_entries").grid.wrapper.find(".grid-row-check").hide();
			d.show();
		},
	});
}
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
				department: frm.doc.department,
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
