frappe.ui.form.off("Stock Entry", "get_items_from_transit_entry");

frappe.ui.form.on("Stock Entry", {
	gold_rate_with_gst(frm) {
		if (frm.doc.gold_rate_with_gst) {
			frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate").then((gold_gst_rate) => {
				let gold_rate = flt(frm.doc.gold_rate_with_gst / (1 + flt(gold_gst_rate) / 100), 3);
				if (gold_rate != flt(frm.doc.gold_rate, 3)) {
					frappe.model.set_value(frm.doc.doctype, frm.doc.name, "gold_rate", gold_rate);
				}
			});
		}
	},
	gold_rate(frm) {
		if (frm.doc.gold_rate) {
			frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate").then((gold_gst_rate) => {
				let gold_rate_with_gst = flt(frm.doc.gold_rate * (1 + flt(gold_gst_rate) / 100), 3);
				if (gold_rate_with_gst != flt(frm.doc.gold_rate_with_gst, 3)) {
					frappe.model.set_value(
						frm.doc.doctype,
						frm.doc.name,
						"gold_rate_with_gst",
						gold_rate_with_gst
					);
				}
			});
		}
	},
	refresh(frm) {
		set_html(frm);
		if (
			["Material Transfer to Department", "Consumables Issue to  Department"].includes(
				frm.doc.stock_entry_type
			) &&
			frm.doc.docstatus == 1
		) {
			frm.remove_custom_button("End Transit");
		}
		frm.trigger("get_items_from_customer_goods");

		// Issue: open a NEW unsaved Customer Goods Issue fetched from this Received
		// entry. make_stock_in_entry maps Received -> Issue (purpose Material Issue,
		// custom_cg_issue_against = this entry) and returns an unsaved doc that
		// open_mapped_doc routes the user to. The transit In/End entries are created
		// separately by the user via the existing buttons.
		if (frm.doc.docstatus == 1 && frm.doc.stock_entry_type == "Customer Goods Received") {
			frm.add_custom_button(
				__("Issue"),
				function () {
					frappe.model.open_mapped_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.make_stock_in_entry",
						frm: frm,
					});
				},
				__("Create")
			);
		}

		frm.add_custom_button(
			__("Parent Manufacturing Order"),
			function () {
				erpnext.utils.map_current_doc({
					method: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.update_utils.make_stock_in_entry",
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

		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(
				__("Create Return"),
				function () {
					frappe.model.open_mapped_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.make_mr_on_return",
						frm: frm,
					});
				},
				__("Create")
			);
		}
		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(
				__("Return Receipt"),
				function () {
					return_receipt_button_click(frm);
				},
				__("Create")
			);
			frm.add_custom_button(
				__("Create Return"),
				function () {
					frappe.model.with_doctype("Material Request", function () {
						var mr = frappe.model.get_new_doc("Material Request");
						var items = frm.get_field("items").grid.get_selected_children();
						if (!items.length) {
							items = frm.doc.items;
						}

						mr.work_order = frm.doc.work_order;
						mr.material_request_type = "Material Transfer";
						mr.inventory_type = frm.doc.inventory_type;
						mr._customer = frm.doc._customer;

						items.forEach(function (item) {
							var mr_item = frappe.model.add_child(mr, "items");
							mr_item.item_code = item.item_code;
							mr_item.item_name = item.item_name;
							mr_item.uom = item.uom;
							mr_item.stock_uom = item.stock_uom;
							mr_item.conversion_factor = item.conversion_factor;
							mr_item.item_group = item.item_group;
							mr_item.description = item.description;
							mr_item.image = item.image;
							mr_item.qty = item.qty;
							mr_item.warehouse = item.s_warehouse;
							mr_item.custom_batch_no = item.batch_no;
							mr_item.required_date = frappe.datetime.nowdate();
						});
						frappe.set_route("Form", "Material Request", mr.name);
					});
				},
				__("Create")
			);
		}
	},
	custom_source_employee: function (frm) {
		if (frm.doc.custom_source_employee) {
			frappe.db
				.get_value(
					"Warehouse",
					{ employee: frm.doc.custom_source_employee, warehouse_type: "Raw Material" },
					"name"
				)
				.then((r) => {
					frm.set_value("from_warehouse", r.message.name);
					frappe.db
						.get_value("Employee", frm.doc.custom_source_employee, "department")
						.then((r) => {
							frappe.db
								.get_value(
									"Warehouse",
									{ department: r.message.department, warehouse_type: "Raw Material" },
									"name"
								)
								.then((k) => {
									frm.set_value("to_warehouse", k.message.name);
								});
						});
				});
		}
	},
	custom_target_employee: function (frm) {
		if (frm.doc.custom_target_employee) {
			frappe.db
				.get_value(
					"Warehouse",
					{ employee: frm.doc.custom_target_employee, warehouse_type: "Raw Material" },
					"name"
				)
				.then((r) => {
					frm.set_value("to_warehouse", r.message.name);
					frappe.db
						.get_value("Employee", frm.doc.custom_target_employee, "department")
						.then((r) => {
							frappe.db
								.get_value(
									"Warehouse",
									{ department: r.message.department, warehouse_type: "Raw Material" },
									"name"
								)
								.then((k) => {
									frm.set_value("from_warehouse", k.message.name);
								});
						});
				});
		}
	},
	custom_sales_person: function (frm) {
		frappe.db.get_value("Sales Person", frm.doc.custom_sales_person, "custom_warehouse", function (data) {
			var custom_warehouse = data.custom_warehouse;
			frm.clear_table("items");
			var child_row = frm.add_child("items");
			child_row.t_warehouse = custom_warehouse;
			frm.refresh_field("items");
		});
	},
	validate(frm) {
		var idx = [];
		$.each(frm.doc.items || [], function (i, row) {
			row.custom_insurance_amount = flt(row.custom_insurance_rate) * flt(row.qty);
			row.inventory_type = row.inventory_type ? row.inventory_type : frm.doc.inventory_type;
			row.customer = row.customer ? row.customer : frm.doc._customer;
			row.branch = frm.doc.branch;
			row.department = row.department ? row.department : frm.doc.department;
			row.to_department = row.to_department ? row.to_department : frm.doc.to_department;
			row.main_slip = frm.doc.main_slip;
			row.to_main_slip = frm.doc.to_main_slip;
			row.employee = frm.doc.employee;
			row.to_employee = frm.doc.to_employee;
			row.subcontractor = frm.doc.subcontractor;
			row.to_subcontractor = frm.doc.to_subcontractor;
			row.project = frm.doc.project;
			row.manufacturing_operation = frm.doc.manufacturing_operation
				? frm.doc.manufacturing_operation
				: row.manufacturing_operation;
			row.custom_manufacturing_work_order = frm.doc.manufacturing_work_order
				? frm.doc.manufacturing_work_order
				: row.custom_manufacturing_work_order;
			if (
				// !in_list(
				// 	[
				// 		"Customer Goods Issue",
				// 		"Customer Goods Received",
				// 		"Customer Goods Transfer",
				// 		"Metal Conversion Repack",
				// 		"Material Transfer (WORK ORDER)",
				// 		"Material Transfer to Department",
				// 		"Material Transfer to Employee",
				// 	],
				// 	frm.doc.stock_entry_type
				// ) &&
				![
					"Customer Goods Issue",
					"Customer Goods Received",
					"Customer Goods Transfer",
					"Metal Conversion Repack",
					"Material Transfer (WORK ORDER)",
					"Material Transfer (Department)",
					"Material Transfer (Employee)",
					"Material Transfer",
				].includes(frm.doc.stock_entry_type) &&
				row.inventory_type == "Customer Goods" &&
				!frm.doc.manufacturing_work_order
			) {
				idx.push(row.idx);
			}
			if (row.inventory_type == "Customer Goods") {
				row.allow_zero_valuation_rate = 1;
			}
		});
		if (idx.length > 0) {
			frappe.throw(
				`Rows #${idx}: Inventory Type is selected as Customer Goods, please select stock entry type of customer goods`
			);
		}
		refresh_field("items");
	},
	get_items_from_customer_goods(frm) {
		if (frm.doc.docstatus === 0 && frm.doc.stock_entry_type == "Customer Goods Issue") {
			frm.add_custom_button(
				__("Customer Goods Received"),
				function () {
					erpnext.utils.map_current_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.make_stock_in_entry",
						source_doctype: "Stock Entry",
						target: frm,
						date_field: "posting_date",
						setters: {
							stock_entry_type: "Customer Goods Received",
							purpose: "Material Receipt",
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
	get_items_from_transit_entry: function (frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(
				__("Transit Entry"),
				function () {
					erpnext.utils.map_current_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.make_stock_in_entry_on_transit_entry",
						source_doctype: "Stock Entry",
						target: frm,
						date_field: "posting_date",
						setters: {
							stock_entry_type: "Material Transfer",
							purpose: "Material Transfer",
						},
						get_query_filters: {
							docstatus: 1,
							purpose: "Material Transfer",
							add_to_transit: 1,
						},
					});
				},
				__("Get Items From")
			);
		}
	},

	setup: function (frm) {
		frm.set_query("item_template", function (doc) {
			return { filters: { has_variants: 1 } };
		});
		// docstatus 1 only: MWO.manufacturing_operation is stamped in on_submit
		// (create_manufacturing_operation), so a draft MWO carries a PMO but no
		// operation and silently half-fills the header.
		frm.set_query("manufacturing_work_order", function (doc) {
			return {
				filters: {
					manufacturing_order: frm.doc.manufacturing_order,
					docstatus: 1,
				},
			};
		});
		frm.set_query("manufacturing_operation", function (doc) {
			return {
				filters: {
					manufacturing_work_order: frm.doc.manufacturing_work_order,
					status: ["not in", ["Finished", "Revert"]],
				},
			};
		});
		frm.set_query("department", function (doc) {
			return {
				filters: {
					company: frm.doc.company,
				},
			};
		});
		frm.set_query("to_department", function (doc) {
			return {
				filters: {
					company: frm.doc.company,
				},
			};
		});
		frm.set_query("employee", function (doc) {
			return {
				filters: {
					department: frm.doc.department,
				},
			};
		});
		frm.set_query("to_employee", function (doc) {
			return {
				filters: {
					department: frm.doc.to_department,
				},
			};
		});
		frm.set_query("main_slip", function (doc) {
			return {
				filters: {
					docstatus: 0,
				},
			};
		});
		frm.set_query("to_main_slip", function (doc) {
			return {
				filters: {
					docstatus: 0,
				},
			};
		});
		frm.fields_dict["item_template_attribute"].grid.get_field("attribute_value").get_query = function (
			frm,
			cdt,
			cdn
		) {
			var child = locals[cdt][cdn];
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: child.item_attribute },
			};
		};
	},
	onload_post_render: function (frm) {
		frm.fields_dict["item_template_attribute"].grid.wrapper.find(".grid-remove-rows").remove();
		frm.fields_dict["item_template_attribute"].grid.wrapper.find(".grid-add-multiple-rows").remove();
		frm.fields_dict["item_template_attribute"].grid.wrapper.find(".grid-add-row").remove();
		// Drafts only. frm.trigger() fans out to every registered stock_entry_type handler,
		// including erpnext's, which chains into add_to_transit and unconditionally runs
		// frm.set_value("to_warehouse", "") (erpnext/.../stock_entry.js:922). On a submitted
		// transit entry that dirties the form on load and makes Update raise
		// UpdateAfterSubmitError, since to_warehouse has no allow_on_submit.
		if (frm.doc.docstatus === 0) {
			frm.trigger("stock_entry_type");
		}
	},
	from_job_card: function (frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.from_job_card = frm.doc.from_job_card;
		});
	},
	to_job_card: function (frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.to_job_card = frm.doc.to_job_card;
		});
	},
	item_template: function (frm) {
		if (frm.doc.item_template) {
			frm.doc.item_template_attribute = [];
			frappe.model.with_doc("Item", frm.doc.item_template, function () {
				var item_template = frappe.model.get_doc("Item", frm.doc.item_template);
				$.each(item_template.attributes, function (index, d) {
					let row = frm.add_child("item_template_attribute");
					row.item_attribute = d.attribute;
				});
				frm.refresh_field("item_template_attribute");
			});
		}
	},
	add_item: function (frm) {
		if (!frm.doc.item_template_attribute || !frm.doc.item_template) {
			frappe.throw(__("Please select Item Template."));
		}
		frappe.call({
			method: "jewellery_erpnext.utils.set_items_from_attribute",
			args: {
				item_template: frm.doc.item_template,
				item_template_attribute: frm.doc.item_template_attribute,
			},
			callback: function (r) {
				if (r.message) {
					let item = frm.add_child("items");
					item.item_code = r.message.name;
					item.qty = 1;
					item.transfer_qty = 1;
					item.uom = r.message.stock_uom;
					item.stock_uom = r.message.stock_uom;
					item.conversion_factor = 1;
					frm.refresh_field("items");
					frm.set_value("item_template", "");
					frm.doc.item_template_attribute = [];
					frm.refresh_field("item_template_attribute");
				}
			},
		});
	},
	stock_entry_type(frm) {
		if (
			["Customer Goods Issue", "Customer Goods Received", "Customer Goods Transfer"].includes(
				frm.doc.stock_entry_type
			)
		) {
			frm.set_value("inventory_type", "Customer Goods");
			frm.trigger("get_items_from_customer_goods");
			return;
		}
		if (
			["Material Transfer to Department"].includes(frm.doc.stock_entry_type) &&
			frm.doc.auto_created === 0 &&
			frm.doc.docstatus != 1
		) {
			frm.set_value("add_to_transit", "1");
			frm.set_df_property("add_to_transit", "read_only", 1);
		}
		if (
			// in_list(["Material Transfer to Department"], frm.doc.stock_entry_type) &&
			["Material Transfer to Department"].includes(frm.doc.stock_entry_type) &&
			frm.doc._customer &&
			frm.doc.auto_created === 0
		) {
			frm.set_value("inventory_type", "Customer Goods");
			// frm.set_value("add_to_transit", "1");

			return;
		}
		// frm.set_value("inventory_type", "Regular Stock");

		let company = frm.doc.company;
		let stock_entry_type = frm.doc.stock_entry_type;
		if (
			[
				"Material Transfer (DEPARTMENT)",
				"Material Transfer",
				"Material Transfer (WORK ORDER)",
				"Material Transfer (Subcontracting Work Order)",
			].includes(frm.doc.stock_entry_type)
		) {
			frm.fields_dict["items"].grid.get_field("s_warehouse").get_query = function (frm, cdt, cdn) {
				return {
					query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.warehouse_query_filters",
					filters: {
						company: company,
						stock_entry_type: stock_entry_type,
					},
				};
			};
			frm.fields_dict["items"].grid.get_field("t_warehouse").get_query = function (frm, cdt, cdn) {
				return {
					query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.warehouse_query_filters",
					filters: {
						company: company,
						stock_entry_type: stock_entry_type,
					},
				};
			};
			if (frm.doc.stock_entry_type != "Material Transfer (DEPARTMENT)") {
				frm.fields_dict["items"].grid.get_field("item_code").get_query = function (frm, cdt, cdn) {
					return {
						query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.item_query_filters",
					};
				};
			} else {
				frm.fields_dict["items"].grid.get_field("item_code").get_query = function (frm, cdt, cdn) {
					return {
						filters: {
							is_stock_item: 1,
						},
					};
				};
			}
		} else {
			frm.fields_dict["items"].grid.get_field("item_code").get_query = function (frm, cdt, cdn) {
				return {
					filters: {
						is_stock_item: 1,
					},
				};
			};
			frm.fields_dict["items"].grid.get_field("s_warehouse").get_query = function (frm, cdt, cdn) {
				return {
					filters: {
						company: company,
						is_group: 0,
					},
				};
			};
			frm.fields_dict["items"].grid.get_field("t_warehouse").get_query = function (frm, cdt, cdn) {
				return {
					filters: {
						company: company,
						is_group: 0,
					},
				};
			};
		}
	},
	inventory_type(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			if (
				// in_list(["Customer Goods Issue", "Customer Goods Received", "Customer Goods Transfer"],frm.doc.stock_entry_type) ||
				["Customer Goods Issue", "Customer Goods Received", "Customer Goods Transfer"].includes(
					frm.doc.stock_entry_type
				) ||
				!d.inventory_type
			) {
				d.inventory_type = frm.doc.inventory_type;
			}
		});
	},
	_customer(frm) {
		if (!frm.doc._customer) return;
		$.each(frm.doc.items || [], function (i, d) {
			d.customer = frm.doc._customer;
		});
	},
	branch(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.branch = frm.doc.branch;
		});
	},
	department(frm) {
		if (frm.doc.purpose != "Manufacture" && frm.doc.purpose != "Repack") {
			frappe.db
				.get_value(
					"Warehouse",
					{ department: frm.doc.department, warehouse_type: "Raw Material" },
					"name"
				)
				.then((r) => {
					if (!frm.doc.from_warehouse) frm.set_value("from_warehouse", r.message.name);
				});
		}
	},
	to_department(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.to_department = frm.doc.to_department;
		});
	},
	main_slip(frm) {
		if (frm.doc.main_slip) {
			frappe.db.get_value("Main Slip", frm.doc.main_slip, "employee", (r) => {
				frm.set_value("employee", r.employee);
			});
		}
	},
	to_main_slip(frm) {
		if (frm.doc.to_main_slip) {
			frappe.db.get_value("Main Slip", frm.doc.to_main_slip, "employee", (r) => {
				frm.set_value("to_employee", r.employee);
			});
		}
		if (frm.doc.to_employee) {
			frappe.db
				.get_value(
					"Warehouse",
					{ employee: frm.doc.to_employee, warehouse_type: "Raw Material" },
					"name"
				)
				.then((r) => {
					frm.set_value("to_warehouse", r.message.name);
				});
		}

		$.each(frm.doc.items || [], function (i, d) {
			d.to_main_slip = frm.doc.to_main_slip;
		});
	},
	employee(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.employee = frm.doc.employee;
		});
		if (frm.doc.stock_entry_type == "Material Receive (WORK ORDER)") {
			if (frm.doc.employee) {
				frappe.db
					.get_value(
						"Warehouse",
						{ employee: frm.doc.employee, warehouse_type: "Manufacturing" },
						"name"
					)
					.then((r) => {
						frm.set_value("from_warehouse", r.message.name);
						frm.set_df_property("from_warehouse", "read_only", 1);
					});
				frappe.db
					.get_value(
						"Manufacturing Operation",
						{ name: frm.doc.manufacturing_operation },
						"department"
					)
					.then((r) => {
						frm.set_value("department", r.message.department);
						frm.set_value("to_department", r.message.department);
					});
			} else if (frm.doc.docstatus === 0) {
				frm.set_value("from_warehouse", null);
				frm.set_value("department", null);
			}
		}
	},
	to_employee(frm) {
		// $.each(frm.doc.items || [], function (i, d) {
		// 	d.to_employee = frm.doc.to_employee;
		// });
		if (
			frm.doc.purpose != "Manufacture" &&
			frm.doc.purpose != "Repack" &&
			frm.doc.stock_entry_type != "Material Transfer"
		) {
			if (frm.doc.to_employee) {
				frappe.db
					.get_value(
						"Warehouse",
						{ employee: frm.doc.to_employee, warehouse_type: "Manufacturing" },
						"name"
					)
					.then((r) => {
						frm.set_value("to_warehouse", r.message.name);
					});
			} else if (frm.doc.docstatus === 0) {
				frm.set_value("to_warehouse", null);
			}
		}

		frappe.db.get_value("Employee", frm.doc.to_employee, "department").then((r) => {
			frm.set_value("to_department", r.message.department);
		});
		// frappe.db.get_value("Main Slip", { employee: frm.doc.to_employee }, "name").then((r) => {
		// 	console.log(r.message.name);
		// 	frm.set_value("to_main_slip", r.message.name);
		// });
	},
	manufacturing_work_order(frm) {
		// Guard the empty case: frappe.call drops an undefined `filters` arg, and
		// frappe.client.get_value then runs an unfiltered get_list and hands back the
		// first arbitrary MWO in the table.
		if (!frm.doc.manufacturing_work_order) {
			return frm.set_value({ manufacturing_order: "", manufacturing_operation: "" });
		}
		// Returned so script_manager.trigger awaits this call instead of falling back
		// to frappe.after_server_call(); two quick MWO changes used to land out of order.
		return frappe.db
			.get_value("Manufacturing Work Order", frm.doc.manufacturing_work_order, [
				"manufacturing_order",
				"manufacturing_operation",
			])
			.then((r) => {
				// r.message is undefined whenever the request errored. Dereferencing it
				// raised inside the promise and aborted *both* set_value calls silently,
				// leaving manufacturing_operation blank. before_validate fills it server
				// side either way; this just stops the form going out of sync.
				const mwo = r.message || {};
				return frm.set_value({
					manufacturing_order: mwo.manufacturing_order || "",
					manufacturing_operation: mwo.manufacturing_operation || "",
				});
			});
	},
	subcontractor(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.subcontractor = frm.doc.subcontractor;
		});
	},
	to_subcontractor(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.to_subcontractor = frm.doc.to_subcontractor;
		});
	},
	project(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.project = frm.doc.project;
		});
	},
	manufacturing_operation(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.manufacturing_operation = frm.doc.manufacturing_operation;
		});

		if (frm.doc.stock_entry_type == "Material Transfer (WORK ORDER)") {
			frappe.db
				.get_value("Manufacturing Operation", frm.doc.manufacturing_operation, [
					"status",
					"employee",
					"department",
				])
				.then((r) => {
					if (r.message.status == "WIP") frm.set_value("to_employee", r.message.employee);

					if (r.message.status == "Not Started") {
						frm.set_df_property("to_employee", "hidden", 1);
						frm.set_df_property("employee", "hidden", 1);
						frappe.db
							.get_value(
								"Warehouse",
								{
									department: r.message.department,
									warehouse_type: "Manufacturing",
								},
								"name"
							)
							.then((k) => {
								if (k.message) frm.set_value("to_warehouse", k.message.name);
							});
					} else {
						frm.set_df_property("to_employee", "hidden", 0);
						frm.set_df_property("employee", "hidden", 0);
					}
					frm.set_value("to_department", r.message.department);
					frm.set_value("department", r.message.department);
				});
		}
		if (frm.doc.stock_entry_type == "Material Receive (WORK ORDER)") {
			frappe.db
				.get_value("Manufacturing Operation", frm.doc.manufacturing_operation, [
					"status",
					"employee",
					"department",
				])
				.then((r) => {
					if (r.message.status == "WIP") frm.set_value("employee", r.message.employee);

					if (r.message.status == "Not Started") {
						frappe.db
							.get_value(
								"Warehouse",
								{
									department: r.message.department,
									warehouse_type: "Manufacturing",
								},
								"name"
							)
							.then((k) => {
								frm.set_value("from_warehouse", k.message.name);
							});
					}

					if (frm.doc.stock_entry_type) {
						frm.set_df_property("to_employee", "hidden", 1);
						frm.set_df_property("employee", "read_only", 1);

						if (frm.doc.department) {
							frm.set_df_property("department", "read_only", 1);
						}
					} else {
						frm.set_df_property("to_employee", "hidden", 0);
					}
					frm.set_value("to_department", r.message.department);
					frm.set_value("department", r.message.department);
				});
		}
	},
	custom_get_pmo(frm) {
		let type_list = [];

		if (frm.doc.stock_entry_type == "Work Order for Customer Approval Issue") {
			type_list = ["Issue", "Receive"];
		} else if (frm.doc.stock_entry_type == "Work Order for Customer Approval Receive") {
			type_list = ["Issue", "", null];
		}

		erpnext.utils.map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo.get_items_for_pmo",
			source_doctype: "Parent Manufacturing Order",
			target: frm,
			setters: {
				customer: frm.doc.customer || undefined,
			},
			get_query_filters: {
				docstatus: 1,
				sent_for_customer_approval: 1,
				customer_status: ["NOT IN", type_list],
			},
		});
		refresh_field("items");
		// frappe.db.get_value("Parent Manufacturing Order", source_name, "customer_status", "Issue")
	},
});

frappe.ui.form.on("Stock Entry Detail", {
	item_code: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		frappe.db.get_value("Item", child.item_code, "item_group", function (r) {
			if (r.item_group == "Metal - V") {
				child.pcs = 1;
			}
		});
	},
	batch_no: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.batch_no) {
			frappe.db.get_value("Batch", d.batch_no, "custom_inventory_type", function (r) {
				frappe.model.set_value(cdt, cdn, "inventory_type", r.custom_inventory_type);
			});
			frappe.db.get_value("Batch", d.batch_no, "custom_customer", function (r) {
				frappe.model.set_value(cdt, cdn, "customer", r.custom_customer);
			});
		}
	},
	qty: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		let item_list = [];

		if (row.serial_no && typeof row.serial_no === "string") {
			item_list.push(...row.serial_no.split("\n"));
		}
		if (row.serial_no && item_list.length != row.qty) {
			disableSaveButton();
			frappe.throw(__("Error there are more items in serial no please remove Items"));
		}
	},
	serial_no: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		let serial_item = [];

		if (row.serial_no) {
			frappe.db.get_value("Serial No", row.serial_no, ["custom_gross_wt"]).then((r) => {
				frappe.model.set_value(cdt, cdn, "gross_weight", r.message.custom_gross_wt);
			});
		}

		// Jwelex tag mirrors the row's Serial No. The field holds one tag, so a
		// multi-serial row takes the first serial (same as the edit_bom handler below).
		let first_serial = ((row.serial_no || "") + "").split("\n")[0].trim();
		if (first_serial) {
			frappe.db.get_value("Serial No", first_serial, "custom_jwelex_tag_no").then((r) => {
				frappe.model.set_value(
					cdt,
					cdn,
					"custom_jwelex_tag_no",
					(r.message && r.message.custom_jwelex_tag_no) || ""
				);
			});
		} else {
			frappe.model.set_value(cdt, cdn, "custom_jwelex_tag_no", "");
		}

		if (row.serial_no && typeof row.serial_no === "string" && row.serial_no != "") {
			disableSaveButton();
			serial_item.push(...row.serial_no.split("\n"));
		}
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.validation_of_serial_item",
			args: {
				issue_doc: frm.doc.name,
			},
			callback: function (response) {
				var serial_item_list = response.message;
				if (serial_item.length > row.qty) {
					disableSaveButton();
					frappe.throw(__("Error: Please remove serial no"));
				} else if (serial_item.length < row.qty) {
					disableSaveButton();
					frappe.throw(__("Error: There are less serial no. Please add"));
				} else {
					for (let i = 0; i <= serial_item.length; i++) {
						if (serial_item[i] != serial_item_list[row.item_code][i]) {
							disableSaveButton();
							frappe.throw(__("Error: Serial number is  not present"));
							return;
						}
					}
					frm.refresh();
				}
			},
		});
	},
	edit_bom: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];

		if (frm.doc.__islocal) {
			frappe.throw(__("Please save document to edit the BOM."));
		}

		let serial_no = ((row.serial_no || "") + "").split("\n")[0].trim();
		if (!serial_no) {
			frappe.msgprint(__("Row #{0} has no Serial No, so it has no BOM to edit.", [row.idx]));
			return;
		}

		frappe.db.get_value("Serial No", serial_no, "custom_bom_no").then((r) => {
			let bom = r.message && r.message.custom_bom_no;
			if (!bom) {
				frappe.msgprint(__("Serial No {0} has no linked BOM.", [serial_no]));
				return;
			}
			open_stock_entry_edit_bom_dialog(frm, serial_no, bom);
		});
	},
	items_add: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		row.from_job_card = frm.doc.from_job_card;
		row.to_job_card = frm.doc.to_job_card;
		row.inventory_type = frm.doc.inventory_type;
		row.customer = frm.doc._customer;
		row.branch = frm.doc.branch;
		row.department = frm.doc.department;
		row.to_department = frm.doc.to_department;
		row.main_slip = frm.doc.main_slip;
		row.to_main_slip = frm.doc.to_main_slip;
		row.employee = frm.doc.employee;
		row.to_employee = frm.doc.to_employee;
		row.subcontractor = frm.doc.subcontractor;
		row.to_subcontractor = frm.doc.to_subcontractor;
		row.project = frm.doc.project;
		row.manufacturing_operation = frm.doc.manufacturing_operation;
		refresh_field("items");

		if (frm.doc.stock_entry_type == "Material Issue - Sales Person") {
			frappe.db.get_value(
				"Sales Person",
				frm.doc.custom_sales_person,
				"custom_warehouse",
				function (data) {
					var custom_warehouse = data.custom_warehouse;
					row.t_warehouse = custom_warehouse;
					frm.refresh_field("items");
				}
			);
		}
	},
});

// -- Edit BOM (Stock Entry Detail) -------------------------------------------
// Mirrors Sales Order Item's "Edit BOM" button/dialog (public/js/doctype_js/
// sales_order.js), adapted for Stock Entry:
//  - Stock Entry Detail has no field like Sales Order Item's own `bom` Link (its
//    core `bom_no` is unused by this app -- confirmed 0% populated on live data),
//    so the target BOM is resolved via row.serial_no -> Serial No.custom_bom_no
//    (see the `edit_bom` handler above), same lookup
//    doc_events/sales_order.py::create_serial_no_bom already uses.
//  - Sales Order's dialog re-derives several display columns (actual_rate,
//    customer_metal_purity, difference) from the *customer's* metal purity and
//    the Sales Order's own gold_rate_with_gst -- neither concept applies to a
//    Stock Entry row. This version reads those columns directly off the BOM's
//    own stored child-row values instead (BOM Metal/Diamond/Gemstone/Finding
//    Detail already carry rate/amount/difference/customer_metal_purity), so no
//    customer/gold-rate context is needed and no async per-row recompute runs.
let open_stock_entry_edit_bom_dialog = (frm, serial_no, bom) => {
	const metal_fields = [
		{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
		{
			fieldtype: "Link",
			fieldname: "metal_type",
			label: __("Metal Type"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "metal_touch",
			label: __("Metal Touch"),
			read_only: 1,
			columns: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "metal_purity",
			label: __("Metal Purity"),
			read_only: 1,
			columns: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "customer_metal_purity",
			label: __("Customer Metal Purity"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "metal_colour",
			label: __("Metal Colour"),
			read_only: 1,
			columns: 1,
			options: "Attribute Value",
		},
		{ fieldtype: "Column Break", fieldname: "clb1" },
		{
			fieldtype: "Float",
			fieldname: "quantity",
			label: __("Weight In Gms"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "quantity_3",
			label: __("Weight In Gms(2 digits)"),
			read_only: 1,
			columns: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "rate",
			label: __("Gold Rate"),
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "amount",
			label: __("Gold Amount"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
		},
		{ fieldtype: "Column Break", fieldname: "clb2" },
		{
			fieldtype: "Currency",
			fieldname: "making_rate",
			label: __("Making Rate"),
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "making_amount",
			label: __("Making Amount"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "wastage_rate",
			label: __("Wastage Rate"),
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "wastage_amount",
			label: __("Wastage Amount"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "difference",
			label: __("Difference(Based on Metal Purity)"),
			columns: 1,
			read_only: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "difference_qty",
			label: __("Difference(Based on Roundoff)"),
			read_only: 1,
		},
		{
			fieldtype: "Check",
			fieldname: "is_customer_item",
			label: __("Is Customer Item"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
	];

	const diamond_fields = [
		{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
		{
			fieldtype: "Link",
			fieldname: "diamond_type",
			label: __("Diamond Type"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "stone_shape",
			label: __("Stone Shape"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Data",
			fieldname: "diamond_cut",
			label: __("Diamond Cut"),
			columns: 1,
			read_only: 1,
		},
		{
			fieldtype: "Link",
			fieldname: "quality",
			label: __("Diamond Quality"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Currency",
			fieldname: "handling_rate",
			label: __("Diamond Handling Rate"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{ fieldtype: "Column Break", fieldname: "clb1" },
		{
			fieldtype: "Link",
			fieldname: "sub_setting_type",
			label: __("Sub Setting Type"),
			columns: 1,
			read_only: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Int",
			fieldname: "pcs",
			label: __("Pcs"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "quantity",
			label: __("Weight In Cts"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "quantity_3",
			label: __("Weight In Cts(2 digits)"),
			columns: 1,
			read_only: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "total_diamond_rate",
			columns: 1,
			label: __("Total Diamond Rate"),
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "diamond_rate_for_specified_quantity",
			columns: 1,
			label: __("Amount"),
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "difference",
			label: __("Difference"),
			read_only: 1,
		},
		{
			fieldtype: "Check",
			fieldname: "is_customer_item",
			label: __("Is Customer Item"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
	];

	const gemstone_fields = [
		{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
		{
			fieldtype: "Link",
			fieldname: "gemstone_type",
			label: __("Gemstone Type"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "cut_or_cab",
			label: __("Cut And Cab"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "stone_shape",
			label: __("Stone Shape"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{ fieldtype: "Column Break", fieldname: "clb1" },
		{
			fieldtype: "Link",
			fieldname: "gemstone_quality",
			label: __("Gemstone Quality"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "gemstone_size",
			label: __("Gemstone Size"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "sub_setting_type",
			label: __("Sub Setting Type"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{ fieldtype: "Column Break", fieldname: "clb2" },
		{
			fieldtype: "Int",
			fieldname: "pcs",
			label: __("Pcs"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "quantity",
			label: __("Weight In Cts"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "quantity_3",
			label: __("Weight In Cts(2 digits)"),
			columns: 1,
			read_only: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "total_gemstone_rate",
			columns: 1,
			label: __("Total Gemstone Rate"),
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "gemstone_rate_for_specified_quantity",
			columns: 1,
			label: __("Amount"),
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "difference",
			label: __("Difference"),
			read_only: 1,
		},
		{
			fieldtype: "Check",
			fieldname: "is_customer_item",
			label: __("Is Customer Item"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
	];

	const finding_fields = [
		{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
		{
			fieldtype: "Link",
			fieldname: "metal_type",
			columns: 1,
			label: __("Metal Type"),
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "finding_category",
			columns: 1,
			label: __("Category"),
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "finding_type",
			columns: 1,
			label: __("Type"),
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "metal_touch",
			columns: 1,
			label: __("Metal Touch"),
			read_only: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "metal_purity",
			columns: 1,
			label: __("Metal Purity"),
			read_only: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "customer_metal_purity",
			label: __("Customer Metal Purity"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{ fieldtype: "Column Break", fieldname: "clb1" },
		{
			fieldtype: "Link",
			fieldname: "finding_size",
			columns: 1,
			label: __("Size"),
			read_only: 1,
			in_list_view: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Link",
			fieldname: "metal_colour",
			columns: 1,
			label: __("Metal Colour"),
			read_only: 1,
			options: "Attribute Value",
		},
		{
			fieldtype: "Float",
			fieldname: "quantity",
			columns: 1,
			label: __("Quantity"),
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "quantity_3",
			columns: 1,
			label: __("Quantity(2 digits)"),
			read_only: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "rate",
			columns: 1,
			label: __("Rate"),
			in_list_view: 1,
		},
		{ fieldtype: "Column Break", fieldname: "clb2" },
		{
			fieldtype: "Currency",
			fieldname: "amount",
			columns: 1,
			label: __("Amount"),
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "making_rate",
			label: __("Making Rate"),
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "making_amount",
			label: __("Making Amount"),
			read_only: 1,
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "wastage_rate",
			label: __("Wastage Rate"),
			columns: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "wastage_amount",
			label: __("Wastage Amount"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Currency",
			fieldname: "difference",
			label: __("Difference(Based on Metal Purity)"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Check",
			fieldname: "is_customer_item",
			label: __("Is Customer Item"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
	];

	const other_fields = [
		{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
		{
			fieldtype: "Link",
			fieldname: "item_code",
			read_only: 1,
			options: "Item",
			columns: 2,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "weight",
			read_only: 1,
			label: __("WT in (GMS)"),
			columns: 2,
			in_list_view: 1,
		},
		{
			fieldtype: "Float",
			fieldname: "qty",
			read_only: 1,
			label: __("Qty"),
			columns: 2,
			in_list_view: 1,
		},
		{
			fieldtype: "Link",
			fieldname: "uom",
			columns: 1,
			read_only: 1,
			label: __("UOM"),
			in_list_view: 1,
			options: "UOM",
		},
		{
			fieldtype: "Check",
			fieldname: "is_customer_item",
			label: __("Is Customer Item"),
			columns: 1,
			read_only: 1,
			in_list_view: 1,
		},
	];

	const dialog = new frappe.ui.Dialog({
		title: __("Edit BOM"),
		fields: [
			{
				fieldname: "serial_no",
				fieldtype: "Link",
				label: __("Serial No"),
				options: "Serial No",
				read_only: 1,
				default: serial_no,
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "bom",
				fieldtype: "Link",
				label: __("BOM"),
				options: "BOM",
				read_only: 1,
				default: bom,
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "metal_detail",
				fieldtype: "Table",
				label: __("Metal Detail"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				fields: metal_fields,
			},
			{
				fieldname: "finding_detail",
				fieldtype: "Table",
				label: __("Finding Detail"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				fields: finding_fields,
			},
			{
				fieldname: "diamond_detail",
				fieldtype: "Table",
				label: __("Diamond Detail"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				fields: diamond_fields,
			},
			{
				fieldname: "gemstone_detail",
				fieldtype: "Table",
				label: __("Gemstone Detail"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				fields: gemstone_fields,
			},
			{
				fieldname: "other_detail",
				fieldtype: "Table",
				label: __("Other Detail"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				fields: other_fields,
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "gross_weight",
				fieldtype: "Float",
				label: __("Gross Weight (In Gram)"),
				read_only: 1,
			},
			{ fieldname: "net_weight", fieldtype: "Float", label: __("Net Weight"), read_only: 1 },
			{ fieldtype: "Column Break" },
			{ fieldname: "metal_amount", fieldtype: "Currency", label: __("Metal Amount"), read_only: 1 },
			{ fieldname: "making_amount", fieldtype: "Currency", label: __("Making Amount"), read_only: 1 },
			{ fieldtype: "Section Break" },
			{ fieldname: "finding_weight", fieldtype: "Float", label: __("Finding Weight"), read_only: 1 },
			{ fieldname: "finding_amount", fieldtype: "Currency", label: __("Finding Amount"), read_only: 1 },
			{ fieldtype: "Column Break" },
			{
				fieldname: "other_weight",
				fieldtype: "Float",
				label: __("Other Materials Weight (in Gram)"),
				read_only: 1,
			},
			{
				fieldname: "other_material_amount",
				fieldtype: "Currency",
				label: __("Other Materials Amount"),
				read_only: 1,
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "diamond_weight",
				fieldtype: "Float",
				label: __("Diamond Weight (in carat)"),
				read_only: 1,
			},
			{ fieldname: "diamond_amount", fieldtype: "Currency", label: __("Diamond Amount"), read_only: 1 },
			{ fieldtype: "Column Break" },
			{
				fieldname: "gemstone_weight",
				fieldtype: "Float",
				label: __("Gemstone Weight (in carat)"),
				read_only: 1,
			},
			{
				fieldname: "gemstone_amount",
				fieldtype: "Currency",
				label: __("Gemstone Amount"),
				read_only: 1,
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "wastage_amount", fieldtype: "Currency", label: __("Wastage Amount"), read_only: 1 },
			{ fieldtype: "Column Break" },
			{
				fieldname: "certification_amount",
				fieldtype: "Currency",
				label: __("Certification Amount"),
				read_only: 1,
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "hallmarking_amount",
				fieldtype: "Currency",
				label: __("Hallmarking Amount"),
				read_only: 1,
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "custom_duty_amount",
				fieldtype: "Currency",
				label: __("Custom Duty Amount"),
				read_only: 1,
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "freight_amount", fieldtype: "Currency", label: __("Freight Amount"), read_only: 1 },
		],
		primary_action: function () {
			const metal_detail = dialog.get_values()["metal_detail"] || [];
			const diamond_detail = dialog.get_values()["diamond_detail"] || [];
			const gemstone_detail = dialog.get_values()["gemstone_detail"] || [];
			const finding_detail = dialog.get_values()["finding_detail"] || [];
			const other_detail = dialog.get_values()["other_detail"] || [];

			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.update_bom_detail",
				freeze: true,
				args: {
					parent_doctype: "BOM",
					parent_doctype_name: bom,
					metal_detail: metal_detail,
					diamond_detail: diamond_detail,
					gemstone_detail: gemstone_detail,
					finding_detail: finding_detail,
					other_detail: other_detail,
				},
				callback: function () {
					frm.reload_doc();
				},
			});
			dialog.hide();
		},
		primary_action_label: __("Update"),
	});

	frappe.call({
		method: "frappe.client.get",
		freeze: true,
		args: { doctype: "BOM", name: bom },
		callback(r) {
			if (r.message) {
				populate_stock_entry_bom_dialog(r.message, dialog);
			}
		},
	});

	dialog.show();
	dialog.$wrapper.find(".modal-dialog").css("max-width", "90%");
};

// Reads the BOM's own stored child-row values directly -- no customer/gold-rate
// recompute, unlike Sales Order's set_edit_bom_details (see comment above).
let populate_stock_entry_bom_dialog = (doc, dialog) => {
	dialog.fields_dict.metal_detail.df.data = (doc.metal_detail || []).map((d) => ({
		docname: d.name,
		metal_type: d.metal_type,
		metal_touch: d.metal_touch,
		metal_purity: d.metal_purity,
		customer_metal_purity: d.customer_metal_purity,
		metal_colour: d.metal_colour,
		is_customer_item: d.is_customer_item,
		quantity: d.quantity,
		quantity_3: d.quantity_3,
		rate: d.rate,
		amount: d.amount,
		making_rate: d.making_rate,
		making_amount: d.making_amount,
		wastage_rate: d.wastage_rate,
		wastage_amount: d.wastage_amount,
		difference: d.difference,
		difference_qty: d.difference_qty,
	}));
	dialog.fields_dict.metal_detail.grid.refresh();

	dialog.fields_dict.diamond_detail.df.data = (doc.diamond_detail || []).map((d) => ({
		docname: d.name,
		diamond_type: d.diamond_type,
		stone_shape: d.stone_shape,
		diamond_cut: d.diamond_cut,
		quality: d.quality,
		handling_rate: d.handling_rate,
		sub_setting_type: d.sub_setting_type,
		pcs: d.pcs,
		quantity: d.quantity,
		quantity_3: d.quantity_3,
		total_diamond_rate: d.total_diamond_rate,
		diamond_rate_for_specified_quantity: d.diamond_rate_for_specified_quantity,
		difference: d.difference,
		is_customer_item: d.is_customer_item,
	}));
	dialog.fields_dict.diamond_detail.grid.refresh();

	dialog.fields_dict.gemstone_detail.df.data = (doc.gemstone_detail || []).map((d) => ({
		docname: d.name,
		gemstone_type: d.gemstone_type,
		cut_or_cab: d.cut_or_cab,
		stone_shape: d.stone_shape,
		gemstone_quality: d.gemstone_quality,
		gemstone_size: d.gemstone_size,
		sub_setting_type: d.sub_setting_type,
		pcs: d.pcs,
		quantity: d.quantity,
		quantity_3: d.quantity_3,
		total_gemstone_rate: d.total_gemstone_rate,
		gemstone_rate_for_specified_quantity: d.gemstone_rate_for_specified_quantity,
		difference: d.difference,
		is_customer_item: d.is_customer_item,
	}));
	dialog.fields_dict.gemstone_detail.grid.refresh();

	dialog.fields_dict.finding_detail.df.data = (doc.finding_detail || []).map((d) => ({
		docname: d.name,
		metal_type: d.metal_type,
		finding_category: d.finding_category,
		finding_type: d.finding_type,
		finding_size: d.finding_size,
		metal_touch: d.metal_touch,
		metal_purity: d.metal_purity,
		customer_metal_purity: d.customer_metal_purity,
		metal_colour: d.metal_colour,
		quantity: d.quantity,
		quantity_3: d.quantity_3,
		rate: d.rate,
		amount: d.amount,
		making_rate: d.making_rate,
		making_amount: d.making_amount,
		wastage_rate: d.wastage_rate,
		wastage_amount: d.wastage_amount,
		difference: d.difference,
		is_customer_item: d.is_customer_item,
	}));
	dialog.fields_dict.finding_detail.grid.refresh();

	dialog.fields_dict.other_detail.df.data = (doc.other_detail || []).map((d) => ({
		docname: d.name,
		item_code: d.item_code,
		qty: d.qty,
		weight: d.weight,
		uom: d.uom,
	}));
	dialog.fields_dict.other_detail.grid.refresh();

	let total_wastage_amount = doc.total_wastage_amount || 0;
	for (let row of doc.finding_detail || []) {
		total_wastage_amount += row.wastage_amount || 0;
	}

	dialog.set_value("gross_weight", doc.gross_weight);
	dialog.set_value("net_weight", doc.metal_and_finding_weight || 0);
	dialog.set_value("metal_amount", doc.total_metal_amount);
	dialog.set_value("making_amount", doc.making_charge);
	dialog.set_value("finding_weight", doc.total_finding_weight_per_gram || 0);
	dialog.set_value("finding_amount", doc.finding_bom_amount);
	dialog.set_value("other_weight", doc.other_weight || 0);
	dialog.set_value("diamond_weight", doc.diamond_weight || 0);
	dialog.set_value("diamond_amount", doc.total_diamond_amount);
	dialog.set_value("gemstone_weight", doc.gemstone_weight || 0);
	dialog.set_value("gemstone_amount", doc.total_gemstone_amount);
	dialog.set_value("wastage_amount", total_wastage_amount);
	dialog.set_value("certification_amount", doc.certification_amount);
	dialog.set_value("hallmarking_amount", doc.hallmarking_amount);
	dialog.set_value("custom_duty_amount", doc.custom_duty_amount);
	dialog.set_value("freight_amount", doc.freight_amount);
};

erpnext.stock.select_batch_and_serial_no = (frm, item) => {
	let get_warehouse_type_and_name = (item) => {
		let value = "";
		if (frm.fields_dict.from_warehouse.disp_status === "Write") {
			value = cstr(item.s_warehouse) || "";
			return {
				type: "Source Warehouse",
				name: value,
			};
		} else {
			value = cstr(item.t_warehouse) || "";
			return {
				type: "Target Warehouse",
				name: value,
			};
		}
	};

	if (item && !item.has_serial_no && !item.has_batch_no) return;
	if (frm.doc.purpose === "Material Receipt") return;

	frappe.require("assets/jewellery_erpnext/js/utils/serial_no_batch_selector.js", function () {
		if (frm.batch_selector?.dialog?.display) return;
		frm.batch_selector = new erpnext.SerialNoBatchSelector({
			frm: frm,
			item: item,
			warehouse_details: get_warehouse_type_and_name(item),
		});
	});
};

erpnext.show_serial_batch_selector = function (frm, d, callback, on_close, show_dialog) {
	let warehouse, receiving_stock, existing_stock;
	if (frm.doc.is_return) {
		if (["Purchase Receipt", "Purchase Invoice"].includes(frm.doc.doctype)) {
			existing_stock = true;
			warehouse = d.warehouse;
		} else if (["Delivery Note", "Sales Invoice"].includes(frm.doc.doctype)) {
			receiving_stock = true;
		}
	} else {
		if (frm.doc.doctype == "Stock Entry") {
			if (frm.doc.purpose == "Material Receipt") {
				receiving_stock = true;
			} else {
				existing_stock = true;
				warehouse = d.s_warehouse;
			}
		} else {
			existing_stock = true;
			warehouse = d.warehouse;
		}
	}

	if (!warehouse) {
		if (receiving_stock) {
			warehouse = ["like", ""];
		} else if (existing_stock) {
			warehouse = ["!=", ""];
		}
	}

	frappe.require("assets/jewellery_erpnext/js/utils/serial_no_batch_selector.js", function () {
		new erpnext.SerialNoBatchSelector(
			{
				frm: frm,
				item: d,
				warehouse_details: {
					type: "Warehouse",
					name: warehouse,
				},
				callback: callback,
				on_close: on_close,
			},
			show_dialog
		);
	});
};

function return_receipt_button_click(frm) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_material_receipt_for_sales_person",
		args: {
			source_name: frm.doc.name,
		},
		callback: function (response) {
			frappe.set_route("Form", "Stock Entry", response.message.name);
		},
	});
}
function disableSaveButton() {
	var saveButton = $(".btn.btn-primary.btn-sm.primary-action");
	saveButton.prop("disabled", true);
}

function set_html(frm) {
	var template = `
		<table class="table table-bordered table-hover" width="100%" style="border: 1px solid #d1d8dd;">
			<thead>
				<tr style = "text-align:center">
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Item Code</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Qty</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">PCs</th>
				</tr>
			</thead>
			<tbody>
			{% for item in data %}
				<tr>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.item_code }}</td>
					<td style="text-align:center;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.qty }}</td>
					<td style="text-align:center;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.pcs }} </td>
				</tr>
			{% endfor %}
			</tbody>
		</table>`;

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry.get_html_data",
		args: {
			doc: frm.doc,
		},
		callback: function (r) {
			if (r.message) {
				frm.get_field("custom_item_wise_data").$wrapper.html(
					frappe.render_template(template, { data: r.message })
				);
			}
		},
	});
}
