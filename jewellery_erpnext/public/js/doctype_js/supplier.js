frappe.ui.form.on("Supplier", {
	setup(frm) {
		frm.set_query("operation", "operations", function (doc, cdt, cdn) {
			return {
				filters: {
					is_subcontracted: 1,
					supplier_group: frm.doc.supplier_group,
				},
			};
		});
	},
});
