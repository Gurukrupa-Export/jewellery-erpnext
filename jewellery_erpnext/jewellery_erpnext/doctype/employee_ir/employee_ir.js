// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Employee IR", {
	refresh(frm) {
		set_child_table_batch_filter(frm);
		set_child_table_item_filter(frm);
		set_html(frm);
		if (frm.doc.docstatus == 0 && !frm.doc.__islocal && frm.doc.type == "Receive" && frm.doc.is_qc_reqd) {
			frm.add_custom_button(__("Generate QC"), function () {
				frm.dirty();
				frm.save();
			});
		}
		add_load_full_casting_tree_button(frm);
		// Auto-load subcategory-driven FG BOM fields for a Receive (only if empty,
		// so user-entered values are never wiped on a refresh).
		if (frm.doc.docstatus == 0 && frm.doc.type == "Receive") {
			load_fg_bom_fields(frm, false);
		}
	},
	refresh_fg_bom_fields(frm) {
		load_fg_bom_fields(frm, true);
	},
	onload(frm) {
		frm.fields_dict["employee_ir_operations"].grid.add_new_row = false;
		$(frm.fields_dict["employee_ir_operations"].grid.wrapper).find(".grid-add-row").hide();
	},
	async validate(frm) {
		await validate_employee_ir_wo_limit(frm);
	},
	setup(frm) {
		frm.ignore_doctypes_on_cancel_all = ["Stock Entry", "Serial and Batch Bundle"];
		frm.set_query("operation", function () {
			return {
				filters: [
					["Department Operation", "department", "=", frm.doc.department],
					["Department Operation", "is_subcontracted", "=", frm.doc.subcontracting == "Yes"],
				],
			};
		});
		frm.set_query("department", function () {
			return {
				filters: [["Department", "company", "=", frm.doc.company]],
			};
		});
		if (frm.doc.subcontracting == "No") {
			frm.set_query("main_slip", function (doc) {
				return {
					filters: {
						docstatus: 0,
						employee: frm.doc.employee,
						for_subcontracting: 0,
						workflow_state: "In Use",
					},
				};
			});
		} else {
			frm.set_query("main_slip", function (doc) {
				return {
					filters: {
						docstatus: 0,
						subcontractor: frm.doc.subcontractor,
						for_subcontracting: 1,
						operation: frm.doc.operation,
						workflow_state: "In Use",
					},
				};
			});
		}
		frm.set_query("employee", function (doc) {
			return {
				filters: {
					department: frm.doc.department,
					custom_operation: frm.doc.operation,
				},
			};
		});
		frm.set_query("manufacturing_operation", "employee_ir_operations", function (doc, cdt, cdn) {
			var filters = {
				department: frm.doc.department,
				operation: ["is", "not set"],
			};
			if (doc.subcontracting == "Yes") {
				filters["employee"] = ["is", "not set"];
			} else {
				filters["subcontractor"] = ["is", "not set"];
			}

			return {
				filters: filters,
			};
		});
		frm.set_query("subcontractor", function () {
			return {
				filters: [["Operation MultiSelect", "operation", "=", frm.doc.operation]],
			};
		});
		var parent_fields = [["transfer_type", "Employee IR Reason"]];
		set_filters_on_parent_table_fields(frm, parent_fields);
	},

	type(frm) {
		frm.clear_table("department_ir_operation");
		frm.refresh_field("department_ir_operation");
	},
	async scan_mwo(frm) {
		if (frm.doc.scan_mwo) {
			frm.doc.employee_ir_operations.forEach(function (item) {
				if (item.manufacturing_work_order == frm.doc.scan_mwo)
					frappe.throw(__("{0} Manufacturing Work Order already exists", [frm.doc.scan_mwo]));
			});
			let wo_limit = await get_employee_ir_wo_limit(frm);
			if (wo_limit > 0 && frm.doc.employee_ir_operations.length >= wo_limit) {
				frappe.throw(
					__("Only {0} work order(s) allowed per Employee IR for department {1}.", [
						wo_limit,
						frm.doc.department,
					])
				);
			}
			var query_filters = {
				department: frm.doc.department,
				manufacturing_work_order: frm.doc.scan_mwo,
			};
			if (frm.doc.type == "Issue") {
				query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
				query_filters["status"] = ["in", ["Not Started"]];
				query_filters["operation"] = ["is", "not set"];
				// query_filters["department_ir_status"] = ["=", "Received"]

				if (frm.doc.subcontracting == "Yes") {
					query_filters["employee"] = ["is", "not set"];
				} else {
					query_filters["subcontractor"] = ["is", "not set"];
				}
			} else {
				query_filters["status"] = ["in", ["On Hold", "WIP", "QC Completed"]];
				query_filters["operation"] = frm.doc.operation;
				if (frm.doc.employee) query_filters["employee"] = frm.doc.employee;
				if (frm.doc.subcontractor && frm.doc.subcontracting == "Yes")
					query_filters["subcontractor"] = frm.doc.subcontractor;
			}

			frappe.db
				.get_value("Manufacturing Operation", query_filters, [
					"name",
					"manufacturing_work_order",
					"status",
					"gross_wt",
					"diamond_wt",
					"diamond_pcs",
					"gemstone_wt",
					"gemstone_pcs",
				])
				.then((r) => {
					let values = r.message;

					if (values.manufacturing_work_order) {
						frappe.db.get_value(
							"QC",
							{
								manufacturing_work_order: values.manufacturing_work_order,
								manufacturing_operation: values.name,
								status: ["!=", "Rejected"],
								docstatus: 1,
							},
							["name", "received_gross_wt"],
							function (a) {
								let row = frm.add_child("employee_ir_operations", {
									manufacturing_work_order: values.manufacturing_work_order,
									manufacturing_operation: values.name,
									qc: a.name,
									received_gross_wt: a.received_gross_wt,
									gross_wt: values.gross_wt,
									diamond_wt: values.diamond_wt,
									diamond_pcs: values.diamond_pcs,
									gemstone_wt: values.gemstone_wt,
									gemstone_pcs: values.gemstone_pcs,
								});
								frm.refresh_field("employee_ir_operations");
							}
						);
					} else {
						// frappe.throw("No Manufacturing Operation Found");
						frappe.throw({
							title: __("Message"),
							message: __("No Manufacturing Operation Found"),
						});
					}
					frm.set_value("scan_mwo", "");
				});
		}
	},
	get_operations(frm) {
		var query_filters = {
			department: frm.doc.department,
		};
		if (frm.doc.main_slip == null) {
			if (frm.doc.type == "Issue") {
				query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
				query_filters["status"] = ["in", ["Not Started"]];
				query_filters["operation"] = ["is", "not set"];

				if (frm.doc.subcontracting == "Yes") {
					query_filters["employee"] = ["is", "not set"];
				} else {
					query_filters["subcontractor"] = ["is", "not set"];
				}
			} else {
				query_filters["status"] = ["in", ["On Hold", "WIP", "QC Completed"]];
				query_filters["operation"] = frm.doc.operation;

				if (frm.doc.employee) query_filters["employee"] = frm.doc.employee;
				if (frm.doc.subcontractor && frm.doc.subcontracting == "Yes")
					query_filters["subcontractor"] = frm.doc.subcontractor;
			}

			erpnext.utils.map_current_doc({
				method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_manufacturing_operations",
				source_doctype: "Manufacturing Operation",
				slip: frm.doc.main_slip,
				target: frm,
				setters: {
					manufacturing_work_order: undefined,
					company: frm.doc.company || undefined,
					department: frm.doc.department,
					manufacturer: frm.doc.manufacturer || undefined,
				},
				get_query_filters: query_filters,
				size: "extra-large",
			});
		} else {
			frappe.db
				.get_value("Main Slip", frm.doc.main_slip, ["metal_colour", "metal_purity"])
				.then((r) => {
					var metal_colour = r.message.metal_colour;
					var metal_purity = r.message.metal_purity;

					if (frm.doc.type == "Issue") {
						query_filters["status"] = ["in", ["Not Started"]];
						query_filters["operation"] = ["is", "not set"];

						if (frm.doc.subcontracting == "Yes") {
							query_filters["employee"] = ["is", "not set"];
						} else {
							query_filters["subcontractor"] = ["is", "not set"];
						}
					} else {
						query_filters["status"] = ["in", ["On Hold", "WIP", "QC Completed"]];
						query_filters["operation"] = frm.doc.operation;

						if (frm.doc.employee) query_filters["employee"] = frm.doc.employee;
						if (frm.doc.subcontractor && frm.doc.subcontracting == "Yes")
							query_filters["subcontractor"] = frm.doc.subcontractor;
					}

					erpnext.utils.map_current_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_manufacturing_operations",
						source_doctype: "Manufacturing Operation",
						slip: frm.doc.main_slip,
						target: frm,
						setters: {
							manufacturing_work_order: undefined,
							company: frm.doc.company || undefined,
							department: frm.doc.department,
							manufacturer: frm.doc.manufacturer || undefined,
							metal_purity: metal_purity || undefined,
							metal_colour: metal_colour || undefined,
						},
						get_query_filters: query_filters,
						size: "extra-large",
					});
				});
		}
	},
	no_of_moulds(frm) {
		frm.doc.mould_reference = [];
		if (frm.doc.no_of_moulds > 0) {
			for (let i = 0; i < frm.doc.no_of_moulds; i++) {
				frm.add_child("mould_reference", {});
			}
			frm.refresh_field("mould_reference");
		}
	},
	employee(frm) {
		frm.set_query("main_slip", function (doc) {
			return {
				filters: {
					docstatus: 0,
					employee: frm.doc.employee,
					for_subcontracting: 0,
					workflow_state: "In Use",
				},
			};
		});
	},
	subcontractor(frm) {
		frm.set_query("main_slip", function (doc) {
			return {
				filters: {
					docstatus: 0,
					subcontractor: frm.doc.subcontractor,
					for_subcontracting: 1,
					operation: frm.doc.operation,
					workflow_state: "In Use",
				},
			};
		});
	},
	subcontracting(frm) {
		if (frm.doc.subcontracting == "Yes") {
			frm.set_value("employee", "");
			frm.set_query("main_slip", function (doc) {
				return {
					filters: {
						docstatus: 0,
						subcontractor: frm.doc.subcontractor,
						for_subcontracting: 1,
						operation: frm.doc.operation,
						workflow_state: "In Use",
					},
				};
			});
		} else {
			frm.set_value("subcontractor", "");
			frm.set_query("main_slip", function (doc) {
				return {
					filters: {
						docstatus: 0,
						employee: frm.doc.employee,
						for_subcontracting: 0,
						workflow_state: "In Use",
					},
				};
			});
		}
	},
});
function set_filters_on_parent_table_fields(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}
frappe.ui.form.on("Employee IR Operation", {
	received_gross_wt: function (frm, cdt, cdn) {
		var child = locals[cdt][cdn];
		// console.log(child.manufacturing_operation);
		if (frm.doc.type == "Issue") {
			frappe.throw(__("Transaction type must be a <b>Receive</b>"));
		}
		if (child.received_gross_wt && frm.doc.type == "Receive") {
			var mwo = child.manufacturing_work_order;
			var gwt = child.gross_wt || 0;
			var opt = child.manufacturing_operation;
			var r_gwt = child.received_gross_wt;
			// frappe.db.get_value("Manufacturing Work Order", mwo, ['multicolour','allowed_colours'])
			// 	.then(r => {
			// 		console.log(r.message);
			// 		if (r.message.multicolour == 1){
			// 			book_loss_details(frm,mwo,opt,gwt,r_gwt);
			// 		}
			// 	})
		}
	},
});

frappe.ui.form.on("Manually Book Loss Details", {
	item_code(frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.item_code[0] === "D" || d.item_code[0] === "G") {
			frm.set_df_property("pcs", "reqd", 1);
			frm.set_df_property("sub_setting_type", "reqd", 1);
		}
		frappe.db.get_value("Item", d.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				d.pcs = 1;
			}
		});
	},
});

// function book_loss_details(frm, mwo, opt, gwt, r_gwt) {
// 	if (gwt == r_gwt) {
// 		frm.clear_table("employee_loss_details");
// 		frm.refresh_field("employee_loss_details");
// 		frm.save();
// 	}
// 	frappe.call({
// 		method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.book_metal_loss",
// 		freeze: true,
// 		args: {
// 			doc: frm.doc,
// 			mwo: mwo,
// 			opt: opt,
// 			gwt: gwt,
// 			r_gwt: r_gwt,
// 		},
// 		callback: function (r) {
// 			if (r.message) {
// 				console.log(r.message);
// 				frm.clear_table("employee_loss_details");
// 				var r_data = r.message[0];
// 				for (var i = 0; i < r_data.length; i++) {
// 					if (r_data[i].proportionally_loss > 0) {
// 						var child = frm.add_child("employee_loss_details");
// 						child.item_code = r_data[i].item_code;
// 						child.net_weight = r_data[i].qty;
// 						child.stock_uom = r_data[i].stock_uom;
// 						child.batch_no = r_data[i].batch_no;
// 						child.manufacturing_work_order = r_data[i].manufacturing_work_order;
// 						child.manufacturing_operation = r_data[i].manufacturing_operation;
// 						child.proportionally_loss = r_data[i].proportionally_loss;
// 						child.received_gross_weight = r_data[i].received_gross_weight;
// 						child.main_slip_consumption = r_data[i].main_slip_consumption;
// 						child.inventory_type = r_data[i].inventory_type;
// 					}
// 				}

// 				frm.set_value("mop_loss_details_total", r.message[1]);
// 				frm.refresh_field("employee_loss_details");
// 				frm.refresh_field("mop_loss_details_total");
// 			}
// 		},
// 	});
// }

function add_subcon_button(frm) {
	if (frm.doc.subcontracting == "Yes") {
		frm.add_custom_button(__("Send To Subcontracting"), () => {
			if (frm.doc.employee_ir_operations.length > 0) {
				frm.doc.employee_ir_operations.forEach((row) => {
					frappe.route_options = {
						department: frm.doc.department,
						manufacturer: frm.doc.manufacturer,
						work_order: row.manufacturing_work_order,
						operation: row.manufacturing_operation,
						supplier: frm.doc.subcontractor,
						employee_ir: frm.doc.name,
						employee_ir_type: frm.doc.type,
					};
				});
				frappe.set_route("Form", "Subcontracting", "new-subcontracting");
			} else {
				frappe.msgprint(__("Please Scan Work Order first"));
			}
		}).addClass("btn-primary");
	}
}

function set_html(frm) {
	var template = `
		<table class="table table-bordered table-hover" width="100%" style="border: 1px solid #d1d8dd;">
			<thead>
				<tr style = "text-align:center">
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Gross WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Net WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Finding WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Diamond WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Gemstone WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Diamond PCs</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Gemstone PCs</th>
				</tr>
			</thead>
			<tbody>
			{% for item in data %}
				<tr style = "text-align:center">
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gross_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.net_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.finding_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.diamond_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gemstone_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.diamond_pcs }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gemstone_pcs }}</td>
				</tr>
			{% endfor %}
			</tbody>
		</table>`;
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.html_utils.get_summary_data",
		args: {
			doc: frm.doc,
		},
		callback: function (r) {
			if (r.message) {
				frm.get_field("summary").$wrapper.html(frappe.render_template(template, { data: r.message }));
			}
		},
	});
}

function set_child_table_batch_filter(frm) {
	frm.fields_dict["manually_book_loss_details"].grid.get_field("batch_no").get_query = function (
		frm,
		cdt,
		cdn
	) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.filters.get_batch_details",
			filters: {
				item_code: d.item_code,
				manufacturing_operation: d.manufacturing_operation,
				manufacturing_work_order: d.manufacturing_work_order,
			},
		};
	};
}

// This button pulls in the still-at-casting siblings of whatever casting tree(s) are already on
// the form so the operator can complete the tree in one click. The submit-time validator
// (doc_events/tree_casting.py) blocks a partial re-issue only when
// MOP Settings.enforce_full_casting_tree_reissue is ticked; this button is shown either way,
// since assembling the full tree is useful regardless of whether the rule is enforced.
function set_child_table_item_filter(frm) {
	frm.fields_dict["manually_book_loss_details"].grid.get_field("item_code").get_query = function (
		frm,
		cdt,
		cdn
	) {
		let d = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.filters.get_manual_loss_items",
			filters: {
				manufacturing_work_order: d.manufacturing_work_order,
				manufacturing_operation: d.manufacturing_operation,
			},
		};
	};
}

// Casting re-issue is all-or-nothing (see doc_events/tree_casting.py). This button pulls in the
// still-at-casting siblings of whatever casting tree(s) are already on the form so the operator
// can complete the tree in one click; the submit-time validator blocks a partial re-issue.
function add_load_full_casting_tree_button(frm) {
	if (frm.doc.docstatus !== 0 || frm.doc.type !== "Issue" || !frm.doc.operation) {
		return;
	}
	frappe.db.get_value("Department Operation", frm.doc.operation, "tree_no_reqd").then((r) => {
		if (!r.message || !r.message.tree_no_reqd) {
			return;
		}
		frm.add_custom_button(__("Load Full Casting Tree"), () => {
			let present = (frm.doc.employee_ir_operations || [])
				.map((row) => row.manufacturing_operation)
				.filter(Boolean);
			if (!present.length) {
				frappe.msgprint(__("Add at least one work order first."));
				return;
			}
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_casting_group_operations",
				args: {
					department: frm.doc.department,
					subcontracting: frm.doc.subcontracting,
					present_operations: JSON.stringify(present),
				},
				freeze: true,
				freeze_message: __("Loading full casting tree..."),
				callback: (r) => {
					let rows = r.message || [];
					if (!rows.length) {
						frappe.msgprint(__("Full casting tree already loaded."));
						return;
					}
					rows.forEach((op) => {
						frm.add_child("employee_ir_operations", {
							manufacturing_work_order: op.manufacturing_work_order,
							manufacturing_operation: op.manufacturing_operation,
							gross_wt: op.gross_wt,
							diamond_wt: op.diamond_wt,
							diamond_pcs: op.diamond_pcs,
							gemstone_wt: op.gemstone_wt,
							gemstone_pcs: op.gemstone_pcs,
						});
					});
					frm.refresh_field("employee_ir_operations");
					frappe.show_alert({
						message: __("Added {0} work order(s) from the casting tree.", [rows.length]),
						indicator: "green",
					});
				},
			});
		});
	});
}

function load_fg_bom_fields(frm, force) {
	if (frm.doc.type !== "Receive") return;
	const ops = (frm.doc.employee_ir_operations || [])
		.filter((r) => r.manufacturing_work_order)
		.map((r) => ({
			manufacturing_operation: r.manufacturing_operation,
			manufacturing_work_order: r.manufacturing_work_order,
		}));
	if (!ops.length) return;
	// Don't clobber values the user is entering unless they explicitly reload.
	if (!force && (frm.doc.custom_fg_bom_fields || []).length) return;

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_fg_bom_fields",
		args: { operations: JSON.stringify(ops) },
		freeze: force,
		freeze_message: __("Loading FG BOM fields..."),
		callback: function (r) {
			const rows = r.message || [];
			// Preserve any already-entered values (keyed by operation + field name).
			const prev = {};
			(frm.doc.custom_fg_bom_fields || []).forEach((row) => {
				prev[`${row.manufacturing_operation}::${row.field_name}`] = row.value;
			});
			frm.clear_table("custom_fg_bom_fields");
			rows.forEach((row) => {
				frm.add_child("custom_fg_bom_fields", {
					manufacturing_operation: row.manufacturing_operation,
					subcategory: row.subcategory,
					field_label: row.field_label,
					field_name: row.field_name,
					field_type: row.field_type,
					options: row.options,
					is_mandatory: row.is_mandatory,
					fg_bom_field: row.fg_bom_field,
					value: prev[`${row.manufacturing_operation}::${row.field_name}`] || "",
				});
			});
			frm.refresh_field("custom_fg_bom_fields");
			if (force && !rows.length) {
				frappe.show_alert({
					message: __("No active FG BOM fields configured for this subcategory."),
					indicator: "orange",
				});
			}
		},
	});
}

// Per-department cap on the number of Manufacturing Work Orders in one Employee IR
// (both Issue and Receive). The limit is maintained on the Department doctype
// (custom_employee_ir_work_order_limit); 0 or unset means no limit. Casting is
// exempted purely by setting the casting department's limit to 0 — there is no
// tree_no_reqd / casting-detection code by design.
async function get_employee_ir_wo_limit(frm) {
	if (!frm.doc.department) return 0;
	const r = await frappe.db.get_value(
		"Department",
		frm.doc.department,
		"custom_employee_ir_work_order_limit"
	);
	return cint(r && r.message && r.message.custom_employee_ir_work_order_limit);
}

async function validate_employee_ir_wo_limit(frm) {
	const limit = await get_employee_ir_wo_limit(frm);
	const count = (frm.doc.employee_ir_operations || []).length;
	if (limit > 0 && count > limit) {
		frappe.throw(
			__("Only {0} work order(s) allowed per Employee IR for department {1}.", [
				limit,
				frm.doc.department,
			])
		);
	}
}
