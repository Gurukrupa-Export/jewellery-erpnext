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

		// Allowed Item Group table -- this restriction covers Metal only, so the group
		// picker is limited to the Metal item groups and the item picker to that group.
		frm.set_query("item_group", "custom_allowed_item_group", function () {
			return {
				filters: {
					name: ["in", frm.__metal_item_groups || []],
				},
			};
		});

		frm.set_query("item_code", "custom_allowed_item_group", function (doc, cdt, cdn) {
			const row = locals[cdt][cdn];
			return {
				filters: {
					item_group: row.item_group || ["in", frm.__metal_item_groups || []],
				},
			};
		});
	},

	onload(frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.supplier_allowed_items.get_metal_item_group_names",
			callback: function (r) {
				frm.__metal_item_groups = r.message || [];
			},
		});
	},
});
