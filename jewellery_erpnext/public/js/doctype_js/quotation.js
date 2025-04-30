frappe.ui.form.on("Quotation", {
	onload(frm) {
		if (frm.doc.company) {
			frappe.db.get_value("Company", frm.doc.company, "supplier_code", function (r) {
				frm.doc.supplier_code = r.supplier_code;
			});
		}
	},
	refresh(frm) {
		frm.add_custom_button(
			__("Purchase Order"),
			function () {
				erpnext.utils.map_current_doc({
					method: "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.make_quotation",
					source_doctype: "Purchase Order",
					target: frm,
					setters: [
						{
							label: "Supplier",
							fieldname: "supplier",
							fieldtype: "Link",
							options: "Supplier",
							read_only: 1,
							default: frm.doc.supplier_code,
						},
					],
					get_query_filters: {
						purchase_type: ["=", "FG Purchase"],
						custom_quotation: ["is", "not set"],
						docstatus: 1,
					},
				});
			},
			__("Get Items From")
		);
		if (frm.has_perm("submit")) {
			if (frm.doc.docstatus == 1) {
				if (frm.doc.status != "Closed") {
					frm.add_custom_button(
						__("Close"),
						() => {
							frappe.call({
								method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.update_status",
								args: {
									quotation_id: frm.doc.name,
								},
								callback: function (r) {
									if (!r.exc) {
										frm.reload_doc();
										frappe.msgprint(
											__("Status updated to closed for Quotation.")
										);
									}
								},
							});
						},
						__("Status")
					);
				} else if (frm.doc.status == "Closed") {
					frm.add_custom_button(
						__("Open"),
						() => {
							frappe.call({
								method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.update_status",
								args: {
									quotation_id: frm.doc.name,
								},
								callback: function (r) {
									if (!r.exc) {
										frm.reload_doc();
										frappe.msgprint(
											__("Status updated to Open for Quotation.")
										);
									}
								},
							});
						},
						__("Status")
					);
				}
			}
		}
		if (frm.doc.workflow_state == "Creating BOM" && frm.doc.custom_bom_creation_logs) {
			frm.add_custom_button(__("Create BOM"), () => {
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.generate_bom",
					args: {
						name: frm.doc.name,
					},
					callback: function (r) {
						if (!r.exc) {
							frm.reload_doc();
							frappe.msgprint(__("BOM Generation started in Background."));
						}
					},
				});
			});
		}
	},
	// 	frm.add_custom_button(
	// 		__("Serial No and Design Code Order"),
	// 		function () {
	// 			erpnext.utils.map_current_doc({
	// 				method: "jewellery_erpnext.gurukrupa_exports.doctype.serial_no_and_design_code_order.serial_no_and_design_code_order.make_quotation",
	// 				source_doctype: "Serial No and Design Code Order",
	// 				target: frm,
	// 				setters: [
	// 					{
	// 						label: "Serial No and Design Code Order Form",
	// 						fieldname: "serial_and_design_id_order_form",
	// 						fieldtype: "Link",
	// 						options: "Serial No and Design Code Order Form",
	// 					},
	// 					{
	// 						label: "Customer",
	// 						fieldname: "customer_code",
	// 						fieldtype: "Link",
	// 						options: "Customer",
	// 						reqd: 1,
	// 						default: frm.doc.party_name || undefined,
	// 					},
	// 					{
	// 						label: "Order Type",
	// 						fieldname: "order_type",
	// 						fieldtype: "Select",
	// 						options: ["Sales", "Stock Order"],
	// 						reqd: 1,
	// 						default: frm.doc.order_type || undefined,
	// 					},
	// 				],
	// 				get_query_filters: {
	// 					item: ["is", "set"],
	// 					docstatus: 1,
	// 				},
	// 			});
	// 		},
	// 		__("Get Items From")
	// 	);
	// },
	// validate: function (frm) {
	// 	frm.doc.items.forEach(function (d) {
	// 		if (d.quotation_bom) {
	// 			frappe.db.get_value(
	// 				"Item Price",
	// 				{
	// 					item_code: d.item_code,
	// 					price_list: frm.doc.selling_price_list,
	// 					bom_no: d.quotation_bom,
	// 				},
	// 				"price_list_rate",
	// 				function (r) {
	// 					if (r.price_list_rate) {
	// 						frappe.model.set_value(d.doctype, d.name, "rate", r.price_list_rate);
	// 					}
	// 				}
	// 			);
	// 		}
	// 	});
	// },
	setup: function (frm) {
		var parent_fields = [
			["diamond_quality", "Diamond Quality"],
			["diamond_grade", "Diamond Grade"],
			["gemstone_cut_or_cab", "Cut Or Cab"],
			["colour", "Metal Colour"],
			["gemstone_quality", "Gemstone Quality"],
		];
		set_item_attribute_filters_on_fields_in_parent_doctype(frm, parent_fields);
		set_item_attribute_filters_on_fields_in_child_doctype(frm, parent_fields);
		frm.set_query("diamond_quality", function (doc) {
			var customer = null;
			if (doc.quotation_to == "Customer") {
				customer = doc.party_name;
			}
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Diamond Quality", customer_code: customer },
			};
		});
		frm.set_query("diamond_quality", "items", function (doc, cdt, cdn) {
			var customer = null;
			if (doc.quotation_to == "Customer") {
				customer = doc.party_name;
			}
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Diamond Quality", customer_code: customer },
			};
		});
	},

	diamond_quality: function (frm) {
		frm.doc.items.filter((item) => {
			item.diamond_quality = frm.doc.diamond_quality;
			refresh_field("items");
		});
	},

	party_name: function (frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.get_gold_rate",
			args: {
				party_name: frm.doc.party_name,
				currency: frm.doc.currency,
			},
			callback: function (r) {
				console.log(r.message);
				frm.doc.gold_rate_with_gst = r.message;
			},
		});
	},
	currency: function (frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.get_gold_rate",
			args: {
				party_name: frm.doc.party_name,
				currency: frm.doc.currency,
			},
			callback: function (r) {
				frm.doc.gold_rate_with_gst = r.message;
			},
		});
	},
	gold_rate_with_gst: function (frm) {
		if (frm.doc.gold_rate_with_gst) {
			let gold_rate = flt(frm.doc.gold_rate_with_gst / 1.03, 3);
			if (gold_rate != frm.doc.gold_rate) {
				frappe.model.set_value(frm.doc.doctype, frm.doc.name, "gold_rate", gold_rate);
			}
		}
	},
	gold_rate: function (frm) {
		if (frm.doc.gold_rate) {
			let gold_rate_with_gst = flt(frm.doc.gold_rate * 1.03, 3);
			if (gold_rate_with_gst != frm.doc.gold_rate_with_gst) {
				frappe.model.set_value(
					frm.doc.doctype,
					frm.doc.name,
					"gold_rate_with_gst",
					gold_rate_with_gst
				);
			}
		}
	},
	custom_customer_gold: function (frm) {
		$.each(frm.doc.items || [], function (i, d) {
			if (!d.custom_customer_gold) d.custom_customer_gold = frm.doc.custom_customer_gold;
		});
		refresh_field("items");
	},
	custom_customer_diamond: function (frm) {
		$.each(frm.doc.items || [], function (i, d) {
			if (!d.custom_customer_diamond)
				d.custom_customer_diamond = frm.doc.custom_customer_diamond;
		});
		refresh_field("items");
	},
	custom_customer_stone: function (frm) {
		$.each(frm.doc.items || [], function (i, d) {
			if (!d.custom_customer_stone) d.custom_customer_stone = frm.doc.custom_customer_stone;
		});
		refresh_field("items");
	},
	custom_customer_good: function (frm) {
		$.each(frm.doc.items || [], function (i, d) {
			if (!d.custom_customer_good) d.custom_customer_good = frm.doc.custom_customer_good;
		});
		refresh_field("items");
	},
});

function set_item_attribute_filters_on_fields_in_parent_doctype(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}

function set_item_attribute_filters_on_fields_in_child_doctype(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], "items", function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}

frappe.ui.form.on("Quotation Item", {
	item_code: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		frappe.db.get_value("Attribute Value", row.item_category, "default_sales_type", (r) => {
			frm.set_value("custom_sales_type", r.default_sales_type);
		});
		// row.quotation_bom = ''
	},
	item_category: function (frm, cdt, cdn) {
		var row = locals[cdt][cdn];

		// row.quotation_bom = ''
	},
	serial_no(frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		if (d.serial_no) {
			frappe.db.get_value(
				"Serial No",
				d.serial_no,
				["item_code", "custom_bom_no", "custom_gross_wt"],
				(r) => {
					frappe.model.set_value(cdt, cdn, "item_code", r.item_code);
					frappe.model.set_value(cdt, cdn, "custom_serial_id_bom", r.custom_bom_no);
					// frappe.model.set_value(cdt, cdn, "item_code", r.item_code);
				}
			);
		}
	},
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
				read_only: 1,
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
				fieldtype: "Currency",
				fieldname: "difference",
				label: __("Difference(Based on Metal Purity)"),
				columns: 1,
				read_only: 1,
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
				fieldtype: "Float",
				fieldname: "actual_rate",
				label: __("Actual Rate"),
				read_only: 1,
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
				fieldname: "actual_rate",
				label: __("Actual Rate"),
				read_only: 1,
			},
			{
				fieldtype: "Float",
				fieldname: "difference_qty",
				label: __("Difference(Based on Roundoff)"),
				read_only: 1,
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
				fieldtype: "Flaot",
				fieldname: "difference_qty",
				label: __("Difference(Based on Roundoff)"),
				columns: 1,
				read_only: 1,
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
				fieldname: "actual_rate",
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
				fieldname: "rate",
				columns: 1,
				label: __("Rate"),
				reqd: 1,
				in_list_view: 1,
				default: 1,
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
									console.log(row_);
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
					default: row.quotation_bom,
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
					read_only: 1,
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
				const other_detail = dialog.get_values()["other_detail"] || [];

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
						other_detail: other_detail,
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

		if (row.quotation_bom) {
			edit_bom_documents(
				dialog,
				row.quotation_bom,
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
					if (r.message && r.message.name) {
						dialog.set_value("bom_no", r.message.name);
					}
				});
		}

		dialog.show();
		dialog.$wrapper.find(".modal-dialog").css("max-width", "90%");
	},
});

function humanize(str) {
	var i,
		frags = str.split("_");
	for (i = 0; i < frags.length; i++) {
		frags[i] = frags[i].charAt(0).toUpperCase() + frags[i].slice(1);
	}
	return frags.join(" ");
}

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
	var gemstone_amount = 0;
	var other_material_amount = 0;

	// metal details table append
	frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate").then(gold_gst_rate => {
		$.each(doc.metal_detail, function (index, d) {
			metal_amount += d.amount;
			making_amount += d.making_amount;
			wastage_amount += d.wastage_amount;
			frappe.call({
				method: "jewellery_erpnext.query.get_customer_mtel_purity",
				args: {
					customer: cur_frm.doc.party_name,
					metal_type: d.metal_type,
					metal_touch: d.metal_touch,
				},
				callback: function (response) {
					let metal_purity_value = response.message || "N/A";
					let gold_rate_with_gst = flt(cur_frm.doc.gold_rate_with_gst || 0);
					
					let metal_purity = flt(metal_purity_value || 0);
					let calculated_actual_rate = (metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
					console.log("gold_rate",calculated_actual_rate)
					let calculated_gold_rate = (d.metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
					let calculated_gold_rate_quantity = calculated_gold_rate * d.quantity
					let calculated_actual_rate_quantity = calculated_actual_rate * d.quantity
					difference_actual_gold_rate = calculated_actual_rate_quantity - calculated_gold_rate_quantity
					// console.log("Customer calculated_actual_rate_quantity", index, ":", difference_actual_gold_rate);
				// console.log("Customer Name: ", cur_frm.doc.customer_name || cur_frm.doc.customer);
				dialog.fields_dict.metal_detail.df.data.push({
					docname: d.name,
					metal_type: d.metal_type,
					metal_touch: d.metal_touch,
					metal_purity: d.metal_purity,
					customer_metal_purity: metal_purity_value,
					metal_colour: d.metal_colour,
					amount: d.amount,
					// rate: d.rate,
					rate: calculated_gold_rate,
					actual_rate: calculated_actual_rate,
					quantity: d.quantity,
					wastage_rate: d.wastage_rate,
					wastage_amount: d.wastage_amount,
					making_rate: d.making_rate,
					making_amount: d.making_amount,
					// difference: difference_actual_gold_rate,
				});
				metal_data = dialog.fields_dict.metal_detail.df.data;
				dialog.fields_dict.metal_detail.grid.refresh();
			
			}
		});
	});
	});
	
	// diamond details table append
	$.each(doc.diamond_detail, function (index, d) {
			diamond_amount += d.diamond_rate_for_specified_quantity;
	
			let witout_precision = d.quantity;
			// console.log(witout_precision);
			let without_precision_rate = witout_precision * d.total_diamond_rate;
		
			frappe.call({
				method: "frappe.client.get_value",
				args: {
					doctype: "Customer",
					filters: { name: cur_frm.doc.customer },
					fieldname: "custom_consider_2_digit_for_diamond"
				},
				callback: function (response) {
					let precision = 0;
		
					// Check if the custom_consider_2_digit_for_diamond field is checked
					if (response.message && response.message.custom_consider_2_digit_for_diamond) {
						precision = 2;  // Set precision to 2 if the checkbox is checked
					}
		
					let quantity_value = precision === 2 ? parseFloat(d.quantity).toFixed(2) : d.quantity;
					let with_precision_rate = quantity_value * d.total_diamond_rate;
		
					// Calculate the difference
					let difference_qty = without_precision_rate - with_precision_rate;
					dialog.fields_dict.diamond_detail.df.data.push({
						docname: d.name,
						diamond_type: d.diamond_type,
						stone_shape: d.stone_shape,
						quality: d.quality,
						pcs: d.pcs,
						diamond_cut: d.diamond_cut,
						sub_setting_type: d.sub_setting_type,
						diamond_grade: d.diamond_grade,
						diamond_sieve_size: d.diamond_sieve_size,
						sieve_size_range: d.sieve_size_range,
						size_in_mm: d.size_in_mm,
						quantity: d.quantity,
						weight_per_pcs: d.weight_per_pcs,
						total_diamond_rate: d.total_diamond_rate,
						diamond_rate_for_specified_quantity: d.diamond_rate_for_specified_quantity,
						difference_qty: difference_qty,  // Store the difference here
					});
	
				let grid = dialog.fields_dict.diamond_detail.grid;
				grid.update_docfield_property("quantity", "precision", 2);
				grid.refresh();
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
				fieldname: "custom_consider_2_digit_for_gemstone"
			},
			callback: function (response) {
				let precision = 0;
	
				// Check if the custom_consider_2_digit_for_diamond field is checked
				if (response.message && response.message.custom_consider_2_digit_for_gemstone) {
					precision = 2;  // Set precision to 2 if the checkbox is checked
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
				quantity: d.quantity,
				total_gemstone_rate: d.total_gemstone_rate,
				gemstone_rate_for_specified_quantity: d.gemstone_rate_for_specified_quantity,
				difference_qty: difference_qty,
			});
			// gemstone_data = dialog.fields_dict.gemstone_detail.df.data;
			// dialog.fields_dict.gemstone_detail.grid.refresh();
		
			let grid = dialog.fields_dict.gemstone_detail.grid;
			grid.update_docfield_property("quantity", "precision", 2);
			grid.refresh();
	}
	});
	});
	
	// finding details table append
	frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate").then(gold_gst_rate => {
		$.each(doc.finding_detail, function (index, d) {
			finding_amount += d.amount;
			frappe.call({
				method: "jewellery_erpnext.query.get_customer_mtel_purity",
				args: {
					customer: cur_frm.doc.party_name,
					metal_type: d.metal_type,
					metal_touch: d.metal_touch,
				},
				callback: function (response) {
					let metal_purity_value = response.message || "N/A";
					let gold_rate_with_gst = flt(cur_frm.doc.gold_rate_with_gst || 0);
					let metal_purity = flt(metal_purity_value || 0);
					let calculated_actual_rate = (metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
					let calculated_gold_rate = (d.metal_purity * gold_rate_with_gst) / (100 + parseInt(gold_gst_rate));
					let calculated_gold_rate_quantity = calculated_gold_rate * d.quantity
					let calculated_actual_rate_quantity = calculated_actual_rate * d.quantity
					let difference_actual_gold_rate = calculated_actual_rate_quantity - calculated_gold_rate_quantity
			dialog.fields_dict.finding_detail.df.data.push({
				docname: d.name,
				metal_type: d.metal_type,
				finding_category: d.finding_category,
				finding_type: d.finding_type,
				finding_size: d.finding_size,
				metal_touch: d.metal_touch,
				metal_purity: d.metal_purity,
				customer_metal_purity: metal_purity_value,
				amount: d.amount,
				// rate: d.rate,
				rate: calculated_gold_rate,
				actual_rate:calculated_actual_rate,
				metal_colour: d.metal_colour,
				quantity: d.quantity,
				wastage_rate: d.wastage_rate,
				wastage_amount: d.wastage_amount,
				making_rate: d.making_rate,
				making_amount: d.making_amount,
				difference:difference_actual_gold_rate,
			});
			finding_data = dialog.fields_dict.finding_detail.df.data;
			dialog.fields_dict.finding_detail.grid.refresh();
		}
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
	dialog.set_value("gross_weight", doc.gross_weight);
	// dialog.set_value("certification_amount", doc.certification_amount)
	// dialog.set_value("hallmarking_amount", doc.hallmarking_amount)
	// dialog.set_value("custom_duty_amount", doc.custom_duty_amount)
	// dialog.set_value("freight_amount", doc.freight_amount)
	// dialog.set_value("sale_amount", doc.sale_amount)

	dialog.set_value("metal_amount", metal_amount);
	dialog.set_value("making_amount", making_amount);
	dialog.set_value("wastage_amount", wastage_amount);
	dialog.set_value("gemstone_amount", gemstone_amount);
	dialog.set_value("diamond_amount", diamond_amount);
	dialog.set_value("net_weight", doc.metal_and_finding_weight || 0);
	dialog.set_value("finding_weight", doc.finding_weight_ || 0);
	dialog.set_value("other_weight", doc.other_weight || 0);
	dialog.set_value("diamond_weight", doc.diamond_weight || 0);
	dialog.set_value("gemstone_weight", doc.gemstone_weight || 0);
	if (dialog.get_value("sale_key"))
		dialog.set_value(
			"saleAmount",
			dialog.get_value("sale_amount") / dialog.get_value("sale_key")
		);
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
					frappe.model.set_value(
						new_row.doctype,
						new_row.name,
						"quotation_bom",
						bom.name
					);
					new_row.bom = bom.name;
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
