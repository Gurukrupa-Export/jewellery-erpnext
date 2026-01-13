frappe.ui.form.on("Sales Order", {
	gold_rate_with_gst: function (frm) {
		if (frm.doc.gold_rate_with_gst) {
			let gold_rate = flt(frm.doc.gold_rate_with_gst / 1.03, 3);
			if (gold_rate != flt(frm.doc.gold_rate, 3)) {
				frappe.model.set_value(frm.doc.doctype, frm.doc.name, "gold_rate", gold_rate);
			}
		}
	},
	gold_rate: function (frm) {
		if (frm.doc.gold_rate) {
			let gold_rate_with_gst = flt(frm.doc.gold_rate * 1.03, 3);
			if (gold_rate_with_gst != flt(frm.doc.gold_rate_with_gst, 3)) {
				frappe.model.set_value(
					frm.doc.doctype,
					frm.doc.name,
					"gold_rate_with_gst",
					gold_rate_with_gst
				);
			}
		}
	},
	refresh: function (frm) {
		frm.add_custom_button(
			__("Customer Approval"),
			function () {
				var customer_approval_dialoge = new frappe.ui.form.MultiSelectDialog({
					doctype: "Customer Approval",
					target: frm,
					setters: [
						{
							label: "date",
							fieldname: "date",
							fieldtype: "Date",
						},
					],
					add_filters_group: 1,
					get_query() {
						return {
							query: "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.customer_approval_filter",
						};
					},

					action(selections) {
						if (selections.length !== 1) {
							frappe.throw(__("Please select exactly one option."));
							return;
						}
						frappe.call({
							method: "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.get_customer_approval_data",
							args: {
								customer_approval_data: selections[0],
							},

							callback: (response) => {
								frm.set_value("company", response.message["company"]);
								frm.set_value("order_type", response.message["order_type"]);
								frm.set_value("customer", response.message["customer"]);
								frm.set_value("set_warehouse", response.message["set_warehouse"]);

								frm.clear_table("items");

								for (let items_row of response.message.items) {
									var child_row = frm.add_child("items");
									child_row.item_code = items_row["item_code"];
									child_row.item_name = items_row["item_name"];
									child_row.delivery_date = items_row["delivery_date"];
									child_row.description = items_row["description"];
									child_row.uom = items_row["uom"];
									child_row.conversion_factor =
										items_row["uom_conversion_factor"];
									child_row.qty = items_row["quantity"];
									child_row.amount = items_row["amount"];
									child_row.serial_no = items_row["serial_no"];
									child_row.bom = items_row["bom_number"];
									child_row.custom_customer_approval = items_row["parent"];
									child_row.custom_customer_approval_item = items_row["name"];
								}
								for (let sales_person_row of response.message.sales_person_child) {
									child_row = frm.add_child("custom_sales_teams");
									child_row.sales_person = sales_person_row["sales_person"];
								}

								customer_approval_dialoge.dialog.hide();
							},
						});
					},
				});
			},
			__("Get Items From")
		);

		frm.set_df_property("order_type", "options", [
			"",
			"Sales",
			"Maintenance",
			"Shopping Cart",
			"Stock Order",
			"Repair",
		]);

		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(__("Create Production Order"), () => {
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.production_order.production_order._make_production_order",
					args: {
						sales_order: frm.doc.name,
					},
					callback: function (r) {
						if (r.message) {
							console.log("Message");
						}
					},
				});
			});
		}
	},
	validate: function (frm) {
		frm.doc.items.forEach(function (d) {
			if (d.bom) {
				frappe.db.get_value(
					"Item Price",
					{
						item_code: d.item_code,
						price_list: frm.doc.selling_price_list,
						bom_no: d.bom,
					},
					"price_list_rate",
					function (r) {
						if (r.price_list_rate) {
							frappe.model.set_value(d.doctype, d.name, "rate", r.price_list_rate);
						}
					}
				);
			}
		});
	},

	onload_post_render(frm) {
		filter_customer(frm);
	},
	sales_type(frm) {
		filter_customer(frm);
	},
	customer(frm) {
		get_sales_type(frm);
	},
});

let filter_customer = (frm) => {
	if (frm.doc.sales_type) {
		//filtering customer with sales type
		frm.set_query("customer", function (doc) {
			return {
				query: "jewellery_erpnext.utils.customer_query",
				filters: {
					sales_type: frm.doc.sales_type,
				},
			};
		});
	} else {
		// removing filters
		frm.set_query("customer", function (doc) {
			return {};
		});
	}
};

frappe.ui.form.on("Sales Order Item", {
	edit_bom: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];

		if (frm.doc.__islocal) {
			frappe.throw(__("Please save document to edit the BOM."));
		}

		// child table data variables
		let metal_data = [];
		let diamond_data = [];
		let gemstone_data = [];
		let finding_data = [];
		let other_data = [];

		// Type	Colour	Purity	Weight in gms	Rate	Amount
		const metal_fields = [
			{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
			{
				fieldtype: "Link",
				fieldname: "metal_type",
				label: __("Metal Type"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Type" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "metal_touch",
				label: __("Metal Touch"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Touch" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "metal_purity",
				label: __("Metal Purity"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Purity" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "customer_metal_purity",
				label: __("Customer Metal Purity"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
				options: "Attribute Value",
			},
			{
				fieldtype: "Link",
				fieldname: "metal_colour",
				label: __("Metal Colour"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Colour" },
					};
				},
			},
			{ fieldtype: "Column Break", fieldname: "clb1" },
			{
				fieldtype: "Float",
				fieldname: "quantity",
				label: __("Weight In Gms"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "rate",
				label: __("Gold Rate"),
				reqd: 1,
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "amount",
				label: __("Gold Amount"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
			},
			{ fieldtype: "Column Break", fieldname: "clb2" },
			{
				fieldtype: "Float",
				fieldname: "making_rate",
				label: __("Making Rate"),
				reqd: 1,
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "making_amount",
				label: __("Making Amount"),
				read_only: 1,
				reqd: 1,
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "wastage_rate",
				label: __("Wastage Rate"),
				columns: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "wastage_amount",
				label: __("Wastage Amount"),
				columns: 1,
				read_only: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "actual_rate",
				columns: 1,
				label: __("Actual Rate"),
				read_only: 1,
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
				fieldtype: "Float",
				fieldname: "difference_qty",
				label: __("Difference(Based on Roundoff)"),
				read_only: 1,
			},
			{
				fieldtype: "Currency",
				fieldname: "labour_charge",
				label: __("Labour Charge"),
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

		const diamond_fields = [
			{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
			{
				fieldtype: "Link",
				fieldname: "diamond_type",
				label: __("Diamond Type"),
				columns: 1,
				reqd: 1,
				read_only: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Diamond Type" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "stone_shape",
				label: __("Stone Shape"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Stone Shape" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "diamond_cut",
				label: __("Diamond Cut"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Stone Shape" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "quality",
				label: __("Diamond Quality"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Diamond Quality" },
					};
				},
			},
			{ fieldtype: "Column Break", fieldname: "clb1" },
			{
				fieldtype: "Link",
				fieldname: "sub_setting_type",
				label: __("Sub Setting Type"),
				read_only: 1,
				columns: 1,
				reqd: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Sub Setting Type" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "diamond_sieve_size",
				label: __("Diamond Sieve Size"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Diamond Sieve Size" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "sieve_size_range",
				label: __("Sieve Size Range"),
				columns: 1,
				read_only: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Diamond Sieve Size Range" },
					};
				},
			},
			{
				fieldtype: "Float",
				fieldname: "size_in_mm",
				label: __("Size (in MM)"),
				columns: 1,
				read_only: 1,
				in_list_view: 1,
			},
			{ fieldtype: "Column Break", fieldname: "clb2" },
			{
				fieldtype: "Float",
				fieldname: "pcs",
				label: __("Pcs"),
				reqd: 1,
				in_list_view: 1,
				read_only: 1,
				columns: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "quantity",
				label: __("Weight In Cts"),
				precision : 0,
				reqd: 1,
				columns: 1,
				read_only: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "weight_per_pcs",
				label: __("Weight Per Piece"),
				read_only: 1,
				columns: 1,
			},

			{
				fieldtype: "Float",
				fieldname: "total_diamond_rate",
				label: __("Rate"),
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "diamond_rate_for_specified_quantity",
				columns: 1,
				read_only: 1,
				label: __("Amount"),
				in_list_view: 1,
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

		const gemstone_fields = [
			{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
			{
				fieldtype: "Link",
				fieldname: "gemstone_type",
				label: __("Gemstone Type"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Gemstone Type" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "cut_or_cab",
				label: __("Cut And Cab"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Cut Or Cab" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "stone_shape",
				label: __("Stone Shape"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Stone Shape" },
					};
				},
			},
			{ fieldtype: "Column Break", fieldname: "clb1" },
			{
				fieldtype: "Link",
				fieldname: "gemstone_quality",
				label: __("Gemstone Quality"),
				read_only: 1,
				columns: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Gemstone Quality" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "gemstone_size",
				label: __("Gemstone Size"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Gemstone Size" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "sub_setting_type",
				label: __("Sub Setting Type"),
				columns: 1,
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Sub Setting Type" },
					};
				},
			},
			{ fieldtype: "Column Break", fieldname: "clb2" },
			{
				fieldtype: "Float",
				fieldname: "pcs",
				label: __("Pcs"),
				columns: 1,
				reqd: 1,
				read_only: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "quantity",
				label: __("Weight In Cts"),
				precision : 0,
				columns: 1,
				reqd: 1,
				read_only: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "total_gemstone_rate",
				columns: 1,
				label: __("Total Gemstone Rate"),
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "gemstone_rate_for_specified_quantity",
				columns: 1,
				label: __("Amount"),
				read_only: 1,
				in_list_view: 1,
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

		const finding_fields = [
			{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
			{
				fieldtype: "Link",
				fieldname: "metal_type",
				columns: 1,
				label: __("Metal Type"),
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Type" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "finding_category",
				columns: 1,
				label: __("Category"),
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Finding Category" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "finding_type",
				columns: 1,
				label: __("Type"),
				reqd: 1,
				read_only: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Finding Sub-Category" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "metal_touch",
				columns: 1,
				label: __("Metal Touch"),
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Touch" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "metal_purity",
				columns: 1,
				label: __("Metal Purity"),
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Purity" },
					};
				},
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
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Finding Size" },
					};
				},
			},
			{
				fieldtype: "Link",
				fieldname: "metal_colour",
				columns: 1,
				label: __("Metal Colour"),
				read_only: 1,
				reqd: 1,
				in_list_view: 1,
				options: "Attribute Value",
				get_query() {
					return {
						query: "jewellery_erpnext.query.item_attribute_query",
						filters: { item_attribute: "Metal Colour" },
					};
				},
			},
			{
				fieldtype: "Float",
				fieldname: "quantity",
				columns: 1,
				label: __("Quantity"),
				reqd: 1,
				read_only: 1,
				in_list_view: 1,
				default: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "rate",
				columns: 1,
				label: __("Rate"),
				reqd: 1,
				in_list_view: 1,
				default: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "actual_rate",
				columns: 1,
				label: __("Actual Rate"),
				read_only: 1,
			},
			{ fieldtype: "Column Break", fieldname: "clb2" },
			{
				fieldtype: "Float",
				fieldname: "amount",
				columns: 1,
				label: __("Amount"),
				reqd: 1,
				read_only: 1,
				in_list_view: 1,
				default: 1,
			},

			{
				fieldtype: "Float",
				fieldname: "making_rate",
				label: __("Making Rate"),
				reqd: 1,
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "making_amount",
				label: __("Making Amount"),
				reqd: 1,
				read_only: 1,
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "wastage_rate",
				label: __("Wastage Rate"),
				columns: 1,
				in_list_view: 1,
			},
			{
				fieldtype: "Float",
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
				fieldtype: "Float",
				fieldname: "difference_qty",
				label: __("Difference(Based on Roundoff)"),
				read_only: 1,
			},
			{
				fieldtype: "Currency",
				fieldname: "labour_charge",
				label: __("Labour Charge"),
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
				reqd: 2,
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
			title: __("Update"),
			fields: [
				{
					fieldname: "barcode_scanner",
					fieldtype: "Data",
					label: "Scan Barcode",
					options: "Barcode",
					onchange: (e) => {
						if (!e) {
							return;
						}

						if (e.target.value) {
							scan_api_call(e.target.value, (r) => {
								if (r.message) {
									update_dialog_values(dialog, row.item_code, r, row);
								}
							});
						}
					},
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "serial_no",
					fieldtype: "Link",
					label: "Serial No",
					options: "Serial No",
					// read_only: 1,
					default: row.serial_no,
					onchange: (e) => {
						if (e && e.target && e.target.value && dialog.get_value("serial_no")) {
							add_row(dialog.get_value("serial_no"), frm, row)
								.then((row_) => {
									dialog.set_value("bom_no", row_.bom);
								})
								.catch((error) => {
									console.error(error);
								});
						}
					},
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "bom_no",
					fieldtype: "Link",
					label: "BOM-No",
					options: "BOM",
					read_only: 1,
					default: row.bom,
					onchange: () => {
						if (dialog.get_value("bom_no")) {
							edit_bom_documents(
								dialog,
								dialog.get_value("bom_no"),
								metal_data,
								diamond_data,
								gemstone_data,
								finding_data,
								other_data
							);
						}
					},
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "metal_detail",
					fieldtype: "Table",
					label: "Metal Detail",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					data: metal_data,
					get_data: () => {
						return metal_data;
					},
					fields: metal_fields,
				},
				{
					fieldname: "finding_detail",
					fieldtype: "Table",
					label: "Finding Detail",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					data: finding_data,
					get_data: () => {
						return finding_data;
					},
					fields: finding_fields,
				},
				{
					fieldname: "diamond_detail",
					fieldtype: "Table",
					label: "Diamond Detail",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					data: diamond_data,
					get_data: () => {
						return diamond_data;
					},
					fields: diamond_fields,
				},
				{
					fieldname: "gemstone_detail",
					fieldtype: "Table",
					label: "Gemstone Detail",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					data: gemstone_data,
					get_data: () => {
						return gemstone_data;
					},
					fields: gemstone_fields,
				},
				{
					fieldname: "other_detail",
					fieldtype: "Table",
					label: "Other Detail",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					data: other_data,
					get_data: () => {
						return other_data;
					},
					fields: other_fields,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "gross_weight",
					fieldtype: "Float",
					label: "Gross Weight (In Gram)",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "net_weight",
					fieldtype: "Float",
					label: "Net Weight",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "metal_amount",
					fieldtype: "Currency",
					label: "Metal Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "making_amount",
					fieldtype: "Currency",
					label: "Making Amount",
					read_only: 1,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "finding_weight",
					fieldtype: "Float",
					label: "Finding Weight",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "finding_amount",
					fieldtype: "Currency",
					label: "Finding Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "other_weight",
					fieldtype: "Float",
					label: "Other Materials Weight (in Gram)",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "other_material_amount",
					fieldtype: "Currency",
					label: "Other Materials Amount",
					read_only: 1,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "diamond_weight",
					fieldtype: "Float",
					label: "Diamond Weight (in carat)",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "diamond_amount",
					fieldtype: "Currency",
					label: "Diamond Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "gemstone_weight",
					fieldtype: "Float",
					label: "Gemstone Weight (in carat)",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "gemstone_amount",
					fieldtype: "Currency",
					label: "Gemstone Amount",
					read_only: 1,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "wastage_amount",
					fieldtype: "Currency",
					label: "Wastage Amount",
					read_only: 1,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "certification_amount",
					fieldtype: "Currency",
					label: "Certification Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "hallmarking_amount",
					fieldtype: "Currency",
					label: "Hallmarking Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "custom_duty_amount",
					fieldtype: "Currency",
					label: "Custom Duty Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "freight_amount",
					fieldtype: "Currency",
					label: "Freight Amount",
					read_only: 1,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "sale_key",
					fieldtype: "Currency",
					label: "Sale Key",
					onchange: () => {
						if (dialog.get_value("sale_key"))
							dialog.set_value(
								"saleAmount",
								dialog.get_value("sale_amount") / dialog.get_value("sale_key") || 0
							);
					},
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "sale_amount",
					fieldtype: "Currency",
					label: "MRP Sale Amount",
					read_only: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "rate",
					fieldtype: "Currency",
					label: "Rate",
					// read_only: 1,
					default: row.rate,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "amount",
					fieldtype: "Currency",
					label: "Amount",
					read_only: 1,
					default: row.amount,
				},
				{
					fieldtype: "Section Break",
				},
				{
					fieldname: "saleAmount",
					fieldtype: "Currency",
					label: "Sale Amount",
					read_only: 1,
					hidden: 1,
				},
				// {
				//     fieldtype: "Column Break",
				// },
				{
					fieldname: "other_amount_",
					fieldtype: "Currency",
					label: "Other Amount",
				},
			],
			primary_action: function () {
				const metal_detail = dialog.get_values()["metal_detail"] || [];
				const diamond_detail = dialog.get_values()["diamond_detail"] || [];
				const gemstone_detail = dialog.get_values()["gemstone_detail"] || [];
				const finding_detail = dialog.get_values()["finding_detail"] || [];

				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.update_bom_detail",
					freeze: true,
					args: {
						parent_doctype: "BOM",
						parent_doctype_name: dialog.get_value("bom_no") || row.bom,
						metal_detail: metal_detail,
						diamond_detail: diamond_detail,
						gemstone_detail: gemstone_detail,
						finding_detail: finding_detail,
					},
					callback: function (r) {
						frm.is_dirty() ? frm.save() : frm.reload_doc();
					},
				});
				// dialog.hide();
				refresh_field("items");
			},
			primary_action_label: __("Update"),
		});

		if (row.bom) {
			edit_bom_documents(
				dialog,
				row.bom,
				metal_data,
				diamond_data,
				gemstone_data,
				finding_data,
				other_data
			);
		}
		// displaying scan icon
		let scan_btn = dialog.$wrapper.find(".link-btn");
		scan_btn.css("display", "inline");

		// setting bom no if missing from child
		if (!dialog.get_value("bom_no") && dialog.get_value("serial_no")) {
			console.log("in else case");
			frappe.db
				.get_value(
					"BOM",
					{
						tag_no: dialog.get_value("serial_no"),
						is_active: 1,
						customer: frm.doc.customer,
					},
					"name"
				)
				.then((r) => {
					console.log(r);
					if (r.message && r.message.name) {
						dialog.set_value("bom_no", r.message.name);
					}
				});
		}

		dialog.show();
		dialog.$wrapper.find(".modal-dialog").css("max-width", "90%");
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
						frappe.model.set_value(cdt, cdn, "bom", r.message.custom_bom_no);
						frappe.model.set_value(
							cdt,
							cdn,
							"custom_gross_weight",
							r.message.custom_gross_wt
						);
					});
			}
		}
	},
});

let edit_bom_documents = (
	dialog,
	bom_no,
	metal_data,
	diamond_data,
	gemstone_data,
	finding_data,
	other_data
) => {
	/*
      function to get BOM doc from model or client
      args using:
          bom_no: Link of BOM
  */
	var doc = frappe.model.get_doc("BOM", bom_no);
	if (!doc) {
		frappe.call({
			method: "frappe.client.get",
			freeze: true,
			args: {
				doctype: "BOM",
				name: bom_no,
			},
			callback(r) {
				if (r.message) {
					set_edit_bom_details(
						r.message,
						dialog,
						metal_data,
						diamond_data,
						gemstone_data,
						finding_data,
						other_data
					);
				}
			},
		});
	} else {
		set_edit_bom_details(
			doc,
			dialog,
			metal_data,
			diamond_data,
			gemstone_data,
			finding_data,
			other_data
		);
	}
};

let set_edit_bom_details = (
	doc,
	dialog,
	metal_data,
	diamond_data,
	gemstone_data,
	finding_data,
	other_data
) => {
	/*
      function to set fields and child tables values of dialog ui
      args:
          dialog: dialog form
          metal_data, diamond_data, gemstone_data, finding_data, other_data => variables used to append table data
  */

	// clearing all tables
	dialog.fields_dict.metal_detail.df.data = [];
	dialog.fields_dict.metal_detail.grid.refresh();

	dialog.fields_dict.diamond_detail.df.data = [];
	dialog.fields_dict.diamond_detail.grid.refresh();

	dialog.fields_dict.gemstone_detail.df.data = [];
	dialog.fields_dict.gemstone_detail.grid.refresh();

	dialog.fields_dict.finding_detail.df.data = [];
	dialog.fields_dict.finding_detail.grid.refresh();

	dialog.fields_dict.other_detail.df.data = [];
	dialog.fields_dict.other_detail.grid.refresh();

	// clearing all field values
	dialog.set_value("gross_weight", 0);
	// dialog.set_value("certification_amount", 0)
	// dialog.set_value("hallmarking_amount", 0)
	// dialog.set_value("custom_duty_amount", 0)
	// dialog.set_value("freight_amount", 0)
	// dialog.set_value("sale_amount", 0)

	dialog.set_value("metal_amount", 0);
	dialog.set_value("making_amount", 0);
	dialog.set_value("wastage_amount", 0);
	dialog.set_value("gemstone_amount", 0);
	dialog.set_value("diamond_amount", 0);
	dialog.set_value("saleAmount", 0);

	// total amount calculation
	var metal_amount = 0;
	var making_amount = 0;
	var wastage_amount = 0;
	var diamond_amount = 0;
	var finding_amount = 0;
	var finding_weight = 0;
	var gemstone_amount = 0;
	var other_material_amount = 0;

	frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate").then(gold_gst_rate => {
		let metal_data = [];
		$.each(doc.metal_detail, function (index, d) {
			metal_amount += d.amount;
			// making_amount += d.making_amount;
					
			// dialog.set_value("making_amount", making_amount);
			dialog.set_value("metal_amount", metal_amount);
			wastage_amount += d.wastage_amount;
	
			frappe.call({
				method: "jewellery_erpnext.query.get_customer_mtel_purity",
				args: {
					customer: cur_frm.doc.customer,
					metal_type: d.metal_type,
					metal_touch: d.metal_touch,
				},
				callback: function (response) {
					let metal_purity_value = response.message || "N/A";
					let gold_rate_with_gst = flt(cur_frm.doc.gold_rate_with_gst || 0);
					let metal_purity = flt(metal_purity_value || 0);
					
					let calculated_actual_rate = (metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
					let calculated_gold_rate = (d.metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
					
					let calculated_gold_rate_quantity = calculated_gold_rate * d.quantity;
					let calculated_actual_rate_quantity = calculated_actual_rate * d.quantity;

					let difference_actual_gold_rate = calculated_actual_rate_quantity - calculated_gold_rate_quantity;
					
					let making_rate_to_use = d.making_rate;
					
					let rate_to_use = calculated_actual_rate;
					// let rate_to_use = gold_rate_with_gst;
					let gold_amount = calculated_actual_rate * d.quantity;
					
					if (
						cur_frm.doc.company === "KG GK Jewellers Private Limited" &&
						cur_frm.doc.customer === "GJCU0009"
					) {
						rate_to_use = d.se_rate;
						making_rate_to_use = d.making_rate;
						d.making_amount = making_rate_to_use * d.quantity;
					} else if (
						cur_frm.doc.company === "Gurukrupa Export Private Limited" &&
						cur_frm.doc.customer_name === "Gurukrupa Export Private Limited - Chennai"
					) {
						making_rate_to_use = d.making_rate || 0;
					}
	
					metal_data.push({
						docname: d.name,
						metal_type: d.metal_type,
						metal_touch: d.metal_touch,
						metal_purity: d.metal_purity,
						is_customer_item: d.is_customer_item,
						metal_colour: d.metal_colour,
						amount:d.amount,
						rate: d.rate,
						actual_rate: calculated_gold_rate,
						quantity: d.quantity,
						wastage_rate: d.wastage_rate,
						wastage_amount: d.wastage_amount,
						making_rate: making_rate_to_use,
						making_amount: d.making_amount,
						labour_charge:d.labour_charge,
						customer_metal_purity: metal_purity_value,
						difference:difference_actual_gold_rate
					});
						
					// Check if last call
					if (metal_data.length === doc.metal_detail.length) {
						dialog.fields_dict.metal_detail.df.data = metal_data;
						let grid = dialog.fields_dict.metal_detail.grid;
						grid.update_docfield_property("rate", "precision", 2);
						grid.update_docfield_property("amount", "precision", 2);
						dialog.fields_dict.metal_detail.grid.refresh();
					}
				}
			});
		});
	});
	
	
	

	
	let total_sum_diamond = 0; 
	let count = 0;  
	let total_calls = doc.diamond_detail.length; 
	
	$.each(doc.diamond_detail, function (index, d) { 
		let witout_precision = d.quantity;
		let without_precision_rate = witout_precision * d.total_diamond_rate;
	
		frappe.call({
			method: "frappe.client.get_value",
			args: {
				doctype: "Customer",
				filters: { name: cur_frm.doc.customer },
				fieldname: "custom_precision_variable"
			},
			callback: function (response) {
				let precision = 0;
				// Check if the custom_consider_2_digit_for_diamond field is checked
				if (response.message && response.message.custom_precision_variable) {
					precision =response.message.custom_precision_variable ;  // Set precision to 2 if the checkbox is checked
				}
	
				let quantity_value = precision === 2 ? parseFloat(d.quantity).toFixed(2) : d.quantity;
				let with_precision_rate = quantity_value * d.total_diamond_rate;
	
				// Calculate the difference
				let difference_qty = without_precision_rate - with_precision_rate;
				let total_diamond_rate_qty = (parseFloat(quantity_value) * parseFloat(d.total_diamond_rate)).toFixed(2);
				total_sum_diamond += parseFloat(total_diamond_rate_qty);
				amount = parseFloat(total_diamond_rate_qty);
				let rate_to_use = d.total_diamond_rate;
				
				if (
					cur_frm.doc.company === "KG GK Jewellers Private Limited" &&
					cur_frm.doc.customer === "GJCU0009"        
				) {
					rate_to_use = flt(d.se_rate || 0);
				}
	
				if (
					cur_frm.doc.company === "Gurukrupa Export Private Limited" &&
					cur_frm.doc.customer === "TNCU0002" &&
					cur_frm.doc.sales_type === "Branch"
					
				) {
					
					rate_to_use = flt(d.fg_purchase_rate || 0);
				}
	
				dialog.fields_dict.diamond_detail.df.data.push({
					docname: d.name,
					diamond_type: d.diamond_type,
					stone_shape: d.stone_shape,
					quality: d.quality,
					pcs: d.pcs,
					diamond_rate_for_specified_quantity:amount,
					diamond_cut: d.diamond_cut,
					sub_setting_type: d.sub_setting_type,
					diamond_grade: d.diamond_grade,
					diamond_sieve_size: d.diamond_sieve_size,
					sieve_size_range: d.sieve_size_range,
					size_in_mm: d.size_in_mm,
					quantity: quantity_value,  
					weight_per_pcs: d.weight_per_pcs,
					total_diamond_rate: rate_to_use,
					// diamond_rate_for_specified_quantity: d.diamond_rate_for_specified_quantity,
					// outright_handling_charges_rate:d.outright_handling_charges_rate,
					// outright_handling_charges_amount:d.outright_handling_charges_amount,
					is_customer_item: d.is_customer_item,
					total_diamond_rate_qty: total_diamond_rate_qty,
					difference_qty: difference_qty,   
				});
	
				count++;
				// Set diamond amount only after all calls are done
				if (count === total_calls) {
					dialog.set_value("diamond_amount", total_sum_diamond.toFixed(2));
					let grid = dialog.fields_dict.diamond_detail.grid;
					
					grid.update_docfield_property("quantity", "precision", precision);
					// grid.update_docfield_property("total_diamond_rate_qty", "precision", 2); 
					 
					grid.refresh();
				}
			}
			
		});
	});
	


	// gemstone details table append
	$.each(doc.gemstone_detail, function (index, d) {
		gemstone_amount += d.gemstone_rate_for_specified_quantity;
	
		let witout_precision = d.quantity;
		let without_precision_rate = witout_precision * d.total_gemstone_rate;
		
		frappe.call({
			method: "frappe.client.get_value",
			args: {
				doctype: "Customer",
				filters: { name: cur_frm.doc.customer },
				fieldname: "custom_precision_variable"
			},
			callback: function (response) {
				let precision = 0;
	
				// Check if the custom_consider_2_digit_for_diamond field is checked
				if (response.message && response.message.custom_precision_variable) {
					precision = response.message.custom_precision_variable;  // Set precision to 2 if the checkbox is checked
				}
	
				let quantity_value = precision === 2 ? parseFloat(d.quantity).toFixed(2) : d.quantity;
				let with_precision_rate = quantity_value * d.total_gemstone_rate;
	
				// Calculate the difference
				let difference_qty = without_precision_rate - with_precision_rate;
	
				dialog.fields_dict.gemstone_detail.df.data.push({
					docname: d.name,
					gemstone_type: d.gemstone_type,
					stone_shape: d.stone_shape,
					sub_setting_type: d.sub_setting_type,
					cut_or_cab: d.cut_or_cab,
					pcs: d.pcs,
					gemstone_quality: d.gemstone_quality,
					gemstone_grade: d.gemstone_grade,
					gemstone_size: d.gemstone_size,
					quantity: quantity_value,  
					total_gemstone_rate: d.total_gemstone_rate,
					gemstone_rate_for_specified_quantity: d.gemstone_rate_for_specified_quantity,
					is_customer_item: d.is_customer_item,
					difference_qty: difference_qty,
				});
	
				let grid = dialog.fields_dict.gemstone_detail.grid;
				grid.update_docfield_property("quantity", "precision", precision);
				
	
				// Refresh the grid
				gemstone_data = dialog.fields_dict.gemstone_detail.df.data;
				grid.refresh();
			}
		});
	});

	// finding details table append
	frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate").then(gold_gst_rate => {
	let pending = doc.finding_detail.length; 
    dialog.fields_dict.finding_detail.df.data = [];
	$.each(doc.finding_detail, function (index, d) {
		// finding_amount += amount;
		// let rate_to_use = d.rate;
		// dialog.set_value("finding_amount", finding_amount);
		let making_rate_to_use = d.making_rate;
	
		frappe.call({
			method: "jewellery_erpnext.query.get_customer_mtel_purity",
			args: {
				customer: cur_frm.doc.customer,
				metal_type: d.metal_type,
				metal_touch: d.metal_touch,
			},
			callback: function (response) {
				let metal_purity_value = response.message || "N/A";
				let metal_purity = flt(metal_purity_value || 0);
				let gold_rate_with_gst = flt(cur_frm.doc.gold_rate_with_gst || 0);
				
				let calculated_actual_rate = (metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
				let calculated_gold_rate = (d.metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
				let rate_to_use = calculated_gold_rate;
				let amount = calculated_actual_rate* d.quantity
				let calculated_gold_rate_quantity = calculated_gold_rate * d.quantity
				let calculated_actual_rate_quantity = calculated_actual_rate * d.quantity
				difference_actual_gold_rate = calculated_actual_rate_quantity - calculated_gold_rate_quantity
				d.amount=amount
				// finding_amount += d.amount;
				// finding_weight +=d.quantity;
				// dialog.set_value("finding_amount", finding_amount);
				// dialog.set_value("finding_weight", finding_weight);
				if (
					cur_frm.doc.company === "KG GK Jewellers Private Limited" &&
					cur_frm.doc.customer === "GJCU0009"
				) {
					rate_to_use = flt(d.se_rate || 0);
					making_rate_to_use = d.making_rate;
					
				} else if (
					cur_frm.doc.company === "Gurukrupa Export Private Limited" &&
					cur_frm.doc.customer_name === "Gurukrupa Export Private Limited - Chennai"
				) {
					making_rate_to_use = d.fg_purchase_rate;
					
				}
					d.making_amount = making_rate_to_use * d.quantity;
					
				dialog.fields_dict.finding_detail.df.data.push({
					docname: d.name,
					metal_type: d.metal_type,
					finding_category: d.finding_category,
					finding_type: d.finding_type,
					finding_size: d.finding_size,
					metal_touch: d.metal_touch,
					metal_purity: d.metal_purity,
					customer_metal_purity : metal_purity_value,
					amount:d.amount,
					rate: d.rate,
					actual_rate : calculated_gold_rate,
					metal_colour: d.metal_colour,
					quantity: d.quantity,
					wastage_rate: d.wastage_rate,
					wastage_amount: d.wastage_amount,
					labour_charge:d.labour_charge,
					making_rate: making_rate_to_use,
					making_amount: d.making_amount,
					difference: difference_actual_gold_rate,
					is_customer_item: d.is_customer_item,
				});
	
				// finding_data = dialog.fields_dict.finding_detail.df.data;
				// dialog.fields_dict.finding_detail.grid.refresh();
				pending--; 
                if (pending === 0) {
				let grid = dialog.fields_dict.finding_detail.grid;
				let precision = 3;
				grid.update_docfield_property("rate", "precision", 2);
				grid.update_docfield_property("amount", "precision", 2);
				// Refresh the grid
				finding_data = dialog.fields_dict.finding_detail.df.data;
				grid.refresh();
			}}
		});
	});
	});


	// other details table append
	$.each(doc.other_detail, function (index, d) {
		dialog.fields_dict.other_detail.df.data.push({
			docname: d.name,
			item_code: d.item_code,
			qty: d.qty,
			weight: d.weight,
			uom: d.uom,
		});
		other_data = dialog.fields_dict.other_detail.df.data;
		dialog.fields_dict.other_detail.grid.refresh();
	});

	// dialog fields value fetch from BOM
	// dialog.set_value("gross_weight", doc.metal_and_finding_weight);
	dialog.set_value("gross_weight", doc.gross_weight);
	dialog.set_value("making_amount", doc.making_charge);
	dialog.set_value("certification_amount", doc.certification_amount)
	dialog.set_value("hallmarking_amount", doc.hallmarking_amount)
	dialog.set_value("custom_duty_amount", doc.custom_duty_amount)
	dialog.set_value("freight_amount", doc.freight_amount)
	dialog.set_value("sale_amount", doc.sale_amount)

	frappe.call({
		method: "frappe.client.get_value",
		args: {
			doctype: "Customer",
			filters: { name: cur_frm.doc.customer },
			fieldname: "custom_consider_2_digit_for_diamond"
		},
		callback: function (response) {
			let precision = 3;
	
			if (response.message && response.message.custom_consider_2_digit_for_diamond) {
				precision = 2;  
			}
	
			
			// Set all fields from BOM
			dialog.set_value("metal_amount", metal_amount || 0);
			dialog.set_value("making_amount", doc.making_charge);
			// dialog.set_value("making_amount", making_amount || 0);
			dialog.set_value("wastage_amount", wastage_amount || 0);
			dialog.set_value("gemstone_amount", gemstone_amount || 0);
			dialog.set_value("finding_amount", doc.finding_bom_amount);
			
			dialog.set_value("amount", (doc.total_bom_amount+ doc.hallmarking_amount + doc.making_charge))
			
			// Set remaining fields
			// dialog.set_value("net_weight", doc.metal_and_finding_weight || 0);
			// dialog.set_value("finding_weight", doc.finding_weight_ || 0);
			dialog.set_value("net_weight", doc.metal_and_finding_weight || 0);
			// dialog.set_value(
			// 	"gross_weight",
			// 	((doc.total_metal_weight) || 0) +
			// 	((doc.total_finding_weight_per_gram) || 0) +
			// 	((doc.total_diamond_weight_in_gms) || 0) +
			// 	((doc.total_gemstone_weight_in_gms) || 0)
			// );
			dialog.set_value("gross_weight", doc.gross_weight);
			dialog.set_value("finding_weight", doc.total_finding_weight_per_gram || 0);
			// Set diamond_weight with dynamic precision
			let diamond_weight = doc.diamond_weight || 0;
			dialog.set_value("diamond_weight", parseFloat(diamond_weight).toFixed(precision));
			dialog.set_df_property("diamond_weight", "precision", precision);
			
			let gemstone_weight = doc.gemstone_weight || 0;
			dialog.set_value("gemstone_weight", parseFloat(gemstone_weight).toFixed(precision));
			dialog.set_df_property("gemstone_weight", "precision", precision);
			dialog.set_value("other_weight", doc.other_weight || 0);
	
			// Sale Amount calculation
			if (dialog.get_value("sale_key")) {
				let sale_amount = dialog.get_value("sale_amount") || 0;
				let sale_key = dialog.get_value("sale_key") || 1;
				dialog.set_value("saleAmount", (sale_amount / sale_key).toFixed(2));
			}
		}
	});
};


function scan_api_call(input, callback) {
	frappe
		.call({
			method: "erpnext.stock.utils.scan_barcode",
			args: {
				search_value: input,
			},
		})
		.then((r) => {
			callback(r);
		});
}

function update_dialog_values(dialog, scanned_item, r, row) {
	const { item_code, barcode, batch_no, serial_no } = r.message;

	dialog.set_value("barcode_scanner", "");
	if (item_code === scanned_item) {
		if (serial_no) {
			dialog.set_value("serial_no", serial_no);
			row.serial_no = serial_no;
		}
	}
}

let add_row = (serial_no, frm, row) => {
	/*
      function to add row of Sales Invoice Item table using bom details
      args:
          serial_no: link of serail no( to fetch bom using serial)
          frm: current form
          row: current row
  */
	return new Promise((resolve, reject) => {
		let new_row;
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.bom.get_bom_details",
			freeze: true,
			args: {
				serial_no: serial_no,
				customer: frm.doc.customer,
			},
			callback: function (r) {
				if (r.message) {
					let bom = r.message;
					new_row = frm.add_child("items");
					new_row.item_code = bom.item;
					new_row.serial_no = bom.tag_no;
					new_row.bom = bom.name;
					frappe.model.set_value(new_row.doctype, new_row.name, "bom", bom.name);
					refresh_field("items");
					frm.trigger("item_code", new_row.doctype, new_row.name);
					frm.script_manager.trigger("item_code", new_row.doctype, new_row.name);
					row = new_row;
					resolve(new_row);
				} else {
					reject("Failed to fetch BOM details");
				}
			},
		});
	});
};

let get_sales_type = (frm) => {
	// get purchase type using customer
	frm.set_value("sales_type", "");
	if (frm.doc.customer) {
		frappe.call({
			method: "jewellery_erpnext.utils.get_type_of_party",
			freeze: true,
			args: {
				doc: "Sales Type Multiselect",
				parent: frm.doc.customer,
				field: "sales_type",
			},
			callback: function (r) {
				frm.set_value("sales_type", r.message || "");
			},
		});
	}
};
