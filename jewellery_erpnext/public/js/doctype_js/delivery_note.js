frappe.ui.form.on("Delivery Note", {
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
