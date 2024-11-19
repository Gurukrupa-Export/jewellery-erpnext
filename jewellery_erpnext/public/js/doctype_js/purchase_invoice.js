frappe.provide("erpnext.item");

frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		// set_si_reference_field_filter();
		get_items(frm);
	},
	onload(frm) {
		if (frm.doc.from_sales_invoice === 1) {
			hide_table_button(frm);
		} else {
			frm.get_field("items").grid.cannot_add_rows = false;
			frm.refresh_field("items");
		}
	},
	purchase_type(frm) {
		filter_supplier(frm);
	},
	supplier(frm) {
		get_purchase_type(frm);
	},
});

frappe.ui.form.on("Purchase Invoice Item", {
	form_render(frm, cdt, cdn) {
		if (frm.doc.from_sales_invoice === 1) {
			hide_table_button(frm);
			var grid_row = frm.open_grid_row();
			grid_row.grid_form.fields_dict.item_code.df.read_only = true;
			grid_row.grid_form.fields_dict.item_code.refresh();
			grid_row.grid_form.fields_dict.item_name.df.read_only = true;
			grid_row.grid_form.fields_dict.item_name.refresh();
			grid_row.grid_form.fields_dict.qty.df.read_only = true;
			grid_row.grid_form.fields_dict.qty.refresh();
			grid_row.grid_form.fields_dict.serial_no.df.read_only = true;
			grid_row.grid_form.fields_dict.serial_no.refresh();
		}
	},
});

let set_si_reference_field_filter = (frm) => {
	frm.set_query("si_reference_field", function () {
		if (frm.doc.purchase_type === "FG Purchase") {
			return {
				filters: {
					sales_type: "FG Sales",
				},
			};
		}
		return {
			filters: {
				sales_type: "Branch Sales",
			},
		};
	});
};

let get_items = (frm) => {
	/* Function to add custom button for sales invoice in get items and appending table with selected invoice items */
	frm.add_custom_button(
		__("Sales Invoice"),
		function () {
			let query_args = {
				filters: {
					docstatus: ["!=", 2],
					sales_type:
						frm.doc.purchase_type === "FG Purchase" ? "FG Sales" : "Branch Sales",
				},
			};

			let d = new frappe.ui.form.MultiSelectDialog({
				doctype: "Sales Invoice",
				target: frm,
				setters: {
					posting_date: null,
					status: "",
				},
				add_filters_group: 1,
				date_field: "posting_date",
				get_query() {
					return query_args;
				},
				action(selections) {
					if (selections && selections.length) {
						frappe.call({
							method: "jewellery_erpnext.utils.get_sales_invoice_items",
							freeze: true,
							args: {
								sales_invoices: selections,
							},
							callback: function (r) {
								if (r && r.message && r.message.length) {
									r.message.forEach((element) => {
										let new_row = frm.add_child("items", {
											item_code: element.item_code,
											qty: element.qty,
											serial_no: element.serial_no,
											bom: element.bom,
											from_sales_invoice: 1,
										});
										frm.refresh_fields("items");
										frm.trigger("item_code", new_row.doctype, new_row.name);
										frm.script_manager.trigger(
											"item_code",
											new_row.doctype,
											new_row.name
										);
									});
									frm.set_value("from_sales_invoice", 1);
								}
							},
						});
						d.dialog.hide();
						hide_table_button(frm);
					}
				},
			});
		},
		__("Get Items From")
	);
};

let filter_supplier = (frm) => {
	if (frm.doc.purchase_type) {
		//filtering supplier with sales type
		frm.set_query("supplier", function (doc) {
			return {
				query: "jewellery_erpnext.utils.supplier_query",
				filters: {
					purchase_type: frm.doc.purchase_type,
				},
			};
		});
	} else {
		// removing filters
		frm.set_query("supplier", function (doc) {
			return {};
		});
	}
};

let get_purchase_type = (frm) => {
	// get purchase type using supplier
	frm.set_value("purchase_type", "");
	if (frm.doc.supplier) {
		frappe.call({
			method: "jewellery_erpnext.utils.get_type_of_party",
			freeze: true,
			args: {
				doc: "Purchase Type Multiselect",
				parent: frm.doc.supplier,
				field: "purchase_type",
			},
			callback: function (r) {
				frm.set_value("purchase_type", r.message || "");
			},
		});
	}
};

let hide_table_button = (frm) => {
	//function to hide add row , add multiple button and hide item fields
	frm.get_field("items").grid.cannot_add_rows = true;
	// frm.get_field('items').grid.grid_buttons = ''
	// frm.get_field("items").grid.only_sortable()
	$("small form-clickable-section grid-footer").remove();
	let fields = ["item_code", "item_name", "qty", "serial_no"];
	fields.forEach((item) => {
		change_df(item);
	});
	frm.refresh_fields("items");
};

let change_df = (field, frm) => {
	var df = frappe.meta.get_docfield("Purchase Invoice Item", field, frm.doc.name);
	df.read_only = 1;
};
