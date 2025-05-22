// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Employee IR", {
	refresh(frm) {
		set_child_table_batch_filter(frm);
		set_html(frm);
		if (
			frm.doc.docstatus == 0 &&
			!frm.doc.__islocal &&
			frm.doc.type == "Receive" &&
			frm.doc.is_qc_reqd
		) {
			frm.add_custom_button(__("Generate QC"), function () {
				frm.dirty();
				frm.save();
			});
		}
	},
	onload(frm) {
		frm.fields_dict["employee_ir_operations"].grid.add_new_row = false;
		$(frm.fields_dict["employee_ir_operations"].grid.wrapper).find(".grid-add-row").hide();
	},
	// validate: function (frm) {
	// 	if (frm.doc.employee_ir_operations.length > 50) {
	// 		frappe.throw(__("Only 50 MOP allowed in one document"));
	// 	}
	// },
	setup(frm) {
		frm.ignore_doctypes_on_cancel_all = ["Stock Entry", "Serial and Batch Bundle"];
		frm.set_query("operation", function () {
			return {
				filters: [
					["Department Operation", "department", "=", frm.doc.department],
					[
						"Department Operation",
						"is_subcontracted",
						"=",
						frm.doc.subcontracting == "Yes",
					],
				],
			};
		});
		frm.set_query("department", function () {
			return {
				filters: [["Department", "company", "=", frm.doc.company]],
			};
		});
		if (frm.doc.subcontracting == 'No'){
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
		else{
			frm.set_query("main_slip", function (doc) {
				return {
					filters: {
						docstatus: 0,
						subcontractor: frm.doc.subcontractor,
						for_subcontracting: 1,
						operation:frm.doc.operation,
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
		frm.set_query(
			"manufacturing_operation",
			"employee_ir_operations",
			function (doc, cdt, cdn) {
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
			}
		);
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
	scan_mwo(frm) {
		if (frm.doc.scan_mwo) {
			frm.doc.employee_ir_operations.forEach(function (item) {
				if (item.manufacturing_work_order == frm.doc.scan_mwo)
					frappe.throw(
						__("{0} Manufacturing Work Order already exists", [frm.doc.scan_mwo])
					);
			});
			// if (frm.doc.employee_ir_operations.length > 30) {
			// 	frappe.throw(__("Only 30 MOP allowed in one document"));
			// }
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
	employee(frm){
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
	subcontractor(frm){
		frm.set_query("main_slip", function (doc) {
			return {
				filters: {
					docstatus: 0,
					subcontractor: frm.doc.subcontractor,
					for_subcontracting: 1,
					operation:frm.doc.operation,
					workflow_state: "In Use",
				},
			};
		});
	},
	subcontracting(frm){
		if(frm.doc.subcontracting== 'Yes'){
			frm.set_value("employee", "");
			frm.set_query("main_slip", function (doc) {
				return {
					filters: {
						docstatus: 0,
						subcontractor: frm.doc.subcontractor,
						for_subcontracting: 1,
						operation:frm.doc.operation,
						workflow_state: "In Use",
					},
				};
			});
		}
		else{
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
	}
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
			book_loss_details(frm, mwo, opt, gwt, r_gwt);
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

function book_loss_details(frm, mwo, opt, gwt, r_gwt) {
	if (gwt == r_gwt) {
		frm.clear_table("employee_loss_details");
		frm.refresh_field("employee_loss_details");
		frm.save();
	}
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.book_metal_loss",
		freeze: true,
		args: {
			doc: frm.doc,
			mwo: mwo,
			opt: opt,
			gwt: gwt,
			r_gwt: r_gwt,
		},
		callback: function (r) {
			if (r.message) {
				console.log(r.message);
				frm.clear_table("employee_loss_details");
				var r_data = r.message[0];
				for (var i = 0; i < r_data.length; i++) {
					if (r_data[i].proportionally_loss > 0) {
						var child = frm.add_child("employee_loss_details");
						child.item_code = r_data[i].item_code;
						child.net_weight = r_data[i].qty;
						child.stock_uom = r_data[i].stock_uom;
						child.batch_no = r_data[i].batch_no;
						child.manufacturing_work_order = r_data[i].manufacturing_work_order;
						child.manufacturing_operation = r_data[i].manufacturing_operation;
						child.proportionally_loss = r_data[i].proportionally_loss;
						child.received_gross_weight = r_data[i].received_gross_weight;
						child.main_slip_consumption = r_data[i].main_slip_consumption;
						child.inventory_type = r_data[i].inventory_type;
					}
				}

				frm.set_value("mop_loss_details_total", r.message[1]);
				frm.refresh_field("employee_loss_details");
				frm.refresh_field("mop_loss_details_total");
			}
		},
	});
}

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
				frm.get_field("summary").$wrapper.html(
					frappe.render_template(template, { data: r.message })
				);
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
			},
		};
	};
}
