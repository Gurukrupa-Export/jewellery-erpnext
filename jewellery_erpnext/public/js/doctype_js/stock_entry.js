frappe.ui.form.off("Stock Entry", "get_items_from_transit_entry");

frappe.ui.form.on("Stock Entry", {
	refresh(frm) {
		set_html(frm);
		if (
			["Material Transfer to Department", "Consumables Issue to  Department"].includes(frm.doc.stock_entry_type) &&
			frm.doc.docstatus == 1
		) {
			frm.remove_custom_button("End Transit");
		}
		frm.trigger("get_items_from_customer_goods");

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
	custom_sales_person: function (frm) {
		frappe.db.get_value(
			"Sales Person",
			frm.doc.custom_sales_person,
			"custom_warehouse",
			function (data) {
				var custom_warehouse = data.custom_warehouse;
				frm.clear_table("items");
				var child_row = frm.add_child("items");
				child_row.t_warehouse = custom_warehouse;
				frm.refresh_field("items");
			}
		);
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
					"Material Transfer (MAIN SLIP)",
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
		frm.set_query("manufacturing_work_order", function (doc) {
			return {
				filters: {
					manufacturing_order: frm.doc.manufacturing_order,
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
		frm.fields_dict["item_template_attribute"].grid.get_field("attribute_value").get_query =
			function (frm, cdt, cdn) {
				var child = locals[cdt][cdn];
				return {
					query: "jewellery_erpnext.query.item_attribute_query",
					filters: { item_attribute: child.item_attribute },
				};
			};
	},
	onload_post_render: function (frm) {
		frm.fields_dict["item_template_attribute"].grid.wrapper.find(".grid-remove-rows").remove();
		frm.fields_dict["item_template_attribute"].grid.wrapper
			.find(".grid-add-multiple-rows")
			.remove();
		frm.fields_dict["item_template_attribute"].grid.wrapper.find(".grid-add-row").remove();
		frm.trigger("stock_entry_type");
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
			[
				"Customer Goods Issue",
				"Customer Goods Received",
				"Customer Goods Transfer",
			].includes(frm.doc.stock_entry_type)
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
				"Material Transfer (MAIN SLIP)",
				"Material Transfer (WORK ORDER)",
				"Material Transfer (Subcontracting Work Order)",
			].includes(frm.doc.stock_entry_type)
		) {
			frm.fields_dict["items"].grid.get_field("s_warehouse").get_query = function (
				frm,
				cdt,
				cdn
			) {
				return {
					query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.warehouse_query_filters",
					filters: {
						company: company,
						stock_entry_type: stock_entry_type,
					},
				};
			};
			frm.fields_dict["items"].grid.get_field("t_warehouse").get_query = function (
				frm,
				cdt,
				cdn
			) {
				return {
					query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.warehouse_query_filters",
					filters: {
						company: company,
						stock_entry_type: stock_entry_type,
					},
				};
			};
			if (frm.doc.stock_entry_type != "Material Transfer (DEPARTMENT)") {
				frm.fields_dict["items"].grid.get_field("item_code").get_query = function (
					frm,
					cdt,
					cdn
				) {
					return {
						query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.item_query_filters",
					};
				};
			} else {
				frm.fields_dict["items"].grid.get_field("item_code").get_query = function (
					frm,
					cdt,
					cdn
				) {
					return {
						filters: {
							is_stock_item: 1,
						},
					};
				};
			}
		} else {
			frm.fields_dict["items"].grid.get_field("item_code").get_query = function (
				frm,
				cdt,
				cdn
			) {
				return {
					filters: {
						is_stock_item: 1,
					},
				};
			};
			frm.fields_dict["items"].grid.get_field("s_warehouse").get_query = function (
				frm,
				cdt,
				cdn
			) {
				return {
					filters: {
						company: company,
						is_group: 0,
					},
				};
			};
			frm.fields_dict["items"].grid.get_field("t_warehouse").get_query = function (
				frm,
				cdt,
				cdn
			) {
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
				[
					"Customer Goods Issue",
					"Customer Goods Received",
					"Customer Goods Transfer",
				].includes(frm.doc.stock_entry_type) ||
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
			} else {
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
			frm.doc.stock_entry_type != "Material Transfer (MAIN SLIP)"
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
			} else {
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
		frappe.db
			.get_value("Manufacturing Work Order", frm.doc.manufacturing_work_order, [
				"manufacturing_order",
				"manufacturing_operation",
			])
			.then((r) => {
				frm.set_value("manufacturing_order", r.message.manufacturing_order);
				frm.set_value("manufacturing_operation", r.message.manufacturing_operation);
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
					if (r.message.status == "WIP")
						frm.set_value("to_employee", r.message.employee);

					if (r.message.status == "Not Started") {
						frm.set_df_property("to_employee", "hidden", 1);
						frm.set_df_property("employee", "hidden", 1);
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
			doc: frm.doc
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
