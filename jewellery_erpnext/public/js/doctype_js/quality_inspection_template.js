frappe.ui.form.on("Quality Inspection Template", {
	setup(frm) {
		frm.set_query("category", "categories", function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Item Category" },
			};
		});
	},
});
