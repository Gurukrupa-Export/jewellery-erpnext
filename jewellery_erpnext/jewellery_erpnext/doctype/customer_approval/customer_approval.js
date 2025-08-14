frappe.ui.form.on("Customer Approval", {
	before_save: function (frm) {
		var child_row = frm.doc.items || [];
		set_bom_and_weight(child_row[0]);
	},

	delivery_date: function (frm) {
		var child_rows = frm.doc.items || [];
		child_rows.forEach(function (row) {
			frappe.model.set_value(row.doctype, row.name, "delivery_date", frm.doc.delivery_date);
		});
	},

	stock_entry_reference: function (frm) {
		if (frm.doc.stock_entry_reference == "") {
			frm.refresh_field("stock_entry_reference");

			frm.clear_table("items");
			frm.refresh_field("items");

			frm.set_value("sales_person", null);
			frm.refresh_field("sales_person");

			frm.clear_table("sales_person_child");
			frm.refresh_field("sales_person_child");

			frm.set_value("set_warehouse", null);
			frm.refresh_field("set_warehouse");
		} else {
			set_stock_entry_data(frm, frm.doc.stock_entry_reference);
			items_filter(frm);
		}
	},

	refresh: function (frm) {
		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(__("Return Receipt"), function () {
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_material_receipt_for_customer_approval",
					args: {
						source_name: frm.doc.stock_entry_reference,
						cust_name: frm.doc.name,
					},
					callback: function (response) {
						frappe.set_route("Form", "Stock Entry", response.message);
					},
				});
			});
		}

		stock_entry_reference_filter(frm);

		frm.set_value("date", frappe.datetime.nowdate());

		frm.add_custom_button(
			__("Material Issue - Sales Person"),
			function () {
				var item_dialoge = new frappe.ui.form.MultiSelectDialog({
					doctype: "Stock Entry",
					target: frm,
					setters: {},
					add_filters_group: 1,
					get_query() {
						return {
							filters: { stock_entry_type: ["=", "Material Issue - Sales Person"] },
						};
					},
					action(selections) {
						if (selections.length !== 1) {
							frappe.throw(__("Please select exactly one option."));
							return;
						}
						frm.set_value("stock_entry_reference", selections[0]);
						set_stock_entry_data(frm, selections[0]);

						item_dialoge.dialog.hide();
					},
				});
			},
			__("Get Items From")
		);

		frm.add_custom_button(__('Sales Order'), function () {
				frappe.call({
					method: "jewellery_erpnext.utils.get_sales_order_items",
					args: {
						customer_approval_name: frm.doc.name  
					},
					callback: function(r) {
						if (!r.exc && r.message) {
							frappe.model.with_doctype('Sales Order', function() {
								let sales_order = frappe.model.get_new_doc('Sales Order');
								sales_order.customer = frm.doc.customer;
								sales_order.transaction_date = frappe.datetime.nowdate();
								sales_order.company = frm.doc.company;
								sales_order.delivery_date = frm.doc.delivery_date;

								(r.message || []).forEach(item => {
									let child = frappe.model.add_child(sales_order, 'items');
									child.item_code = item.item_code;
									child.item_name = item.item_name;
									child.qty = item.quantity;
									child.rate = item.rate;
									child.amount = item.amount;
									child.uom = item.uom;
									child.serial_no = item.serial_no;
									child.delivery_date = item.delivery_date;
									child.custom_customer_approval = frm.doc.name;
								});

								frappe.set_route('Form', 'Sales Order', sales_order.name);
							});
						}
					}
				});
			}, __('Create'));
	},
});

frappe.ui.form.on("Sales Order Item Child", {
	quantity: function (frm, cdt, cdn) {
		var child_row = locals[cdt][cdn];

		child_row.amount = child_row.quantity * child_row.rate;
		frm.refresh_field("items");

		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval.quantity_calculation",
			args: {
				stock_entry_reference: frm.doc.stock_entry_reference,
			},
			callback: (r) => {
				for (let i = 0; i <= r.message.length; i++) {
					if (r.message[i][0] == child_row.item_code) {
						if (r.message[i][1] < child_row.quantity) {
							disableSaveButton();
							frappe.throw(
								__("Error: Quantity cannot be greater than the allowed quantity.")
							);
							break;
						}
					}
				}
			},
		});
		let item_list = [];

		if (child_row.serial_no && typeof child_row.serial_no === "string") {
			item_list.push(...child_row.serial_no.split("\n"));
		}
		if (child_row.serial_no && item_list.length != child_row.quantity) {
			disableSaveButton();
			frappe.throw(__("Error there are more items in serial no please remove Items"));
		}
	},
	serial_no: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (row.serial_no) {
			frappe.db
				.get_value("Serial No", row.serial_no, [
					"item_code",
					"custom_bom_no",
					"custom_gross_wt",
				])
				.then((r) => {
					if (r.message) {
						const item_code = r.message.item_code;
						const bom_no = r.message.custom_bom_no;
	
						frappe.model.set_value(cdt, cdn, "item_code", item_code);
						frappe.model.set_value(cdt, cdn, "bom_number", bom_no);
						frappe.model.set_value(cdt, cdn, "gross_weight", r.message.custom_gross_wt);
	
						// Get Item details
						frappe.db
							.get_value("Item", item_code, ["item_name", "stock_uom", "description"])
							.then((res) => {
								if (res.message) {
									frappe.model.set_value(cdt, cdn, "item_name", res.message.item_name);
									frappe.model.set_value(cdt, cdn, "uom", res.message.stock_uom);
									frappe.model.set_value(cdt, cdn, "description", res.message.description);
								}
							});
	
							if (bom_no) {
								frappe.db
									.get_value("BOM", bom_no, ["total_bom_amount"])
									.then((bom_res) => {
										if (bom_res.message) {
											let rate = bom_res.message.total_bom_amount || 0;
											let qty = row.qty || 1;
											let amount = rate * qty;
		
											frappe.model.set_value(cdt, cdn, "rate", rate);
											frappe.model.set_value(cdt, cdn, "amount", amount);
										}
									});
						}
					}
				});
		}
	
		let serial_item = [];
		if (row.serial_no && typeof row.serial_no === "string" && row.serial_no != "") {
			serial_item.push(...row.serial_no.split("\n"));
		}
		if (serial_item.length > row.quantity) {
			disableSaveButton();
			frappe.throw(__("Error: Please remove serial no"));
		} else if (serial_item.length < row.quantity) {
			disableSaveButton();
			frappe.throw(__("Error: There are less serial no. Please add"));
		}
	},
});

function items_filter(frm) {
	frm.fields_dict["items"].grid.get_field("item_code").get_query = function (doc, cdt, cdn) {
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval.get_items_filter",
			filters: { stock_entry_reference: frm.doc.stock_entry_reference },
		};
	};
}

function disableSaveButton() {
	var saveButton = $(".btn.btn-primary.btn-sm.primary-action");
	saveButton.prop("disabled", true);
}

function stock_entry_reference_filter(frm) {
	frm.set_query("stock_entry_reference", () => {
		return {
			filters: {
				stock_entry_type: "Material Issue - Sales Person",
			},
		};
	});
}

function set_stock_entry_data(frm, reference) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval.get_stock_entry_data",
		args: {
			stock_entry_reference: reference,
		},
		callback: (response) => {
			frm.clear_table("items");
			frm.clear_table("sales_person_child");
			if (response.message.supporting_staff.length > 0) {
				for (let i = 0; i < response.message.supporting_staff.length; i++) {
					var child_row = frm.add_child("sales_person_child");
					child_row.sales_person = response.message.supporting_staff[i].sales_person;
				}
			} else {
				let child_row = frm.add_child("sales_person_child");
				child_row.sales_person = frm.doc.sales_person;
			}

			for (let i = 0; i < response.message.items.length; i++) {
				if (response.message.items[i].qty > 0) {
					let child_row = frm.add_child("items");

					child_row.item_code = response.message.items[i].item_code;
					child_row.item_name = response.message.items[i].item_name;
					child_row.warehouse = response.message.items[i].t_warehouse;
					child_row.uom = response.message.items[i].uom;
					child_row.description = response.message.items[i].description;
					child_row.serial_no = response.message.items[i].serial_no;
					child_row.batch_no = response.message.items[i].batch_no;
					child_row.rate = response.message.items[i].basic_rate;
					child_row.quantity = response.message.items[i].qty;
					set_bom_and_weight(child_row);
					child_row.amount = child_row.rate * child_row.quantity;
					child_row.uom_conversion_factor = response.message.items[i].conversion_factor;
					child_row.delivery_date = frm.doc.delivery_date;
				}
			}
			frm.doc.set_warehouse = response.message.items[0].t_warehouse;
			frm.refresh_field("set_warehouse");
			frm.refresh_field("items");
			frm.refresh_field("sales_person_child");
		},
	});
}

function set_bom_and_weight(child_row) {
	if (child_row.quantity == 1 && child_row.serial_no != null) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval.get_bom_no",
			args: {
				serial_no: child_row.serial_no,
			},
			callback: (response) => {
				child_row.bom_number = response.message.name;
				child_row.gross_weight = response.message.gross_weight;
			},
		});
	}
}
