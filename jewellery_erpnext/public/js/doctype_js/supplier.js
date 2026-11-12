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

	refresh(frm) {
		show_pure_metal_holding(frm);
	},

	custom_metal_weight_limit(frm) {
		show_pure_metal_holding(frm);
	},
});

// Read-only indicator under Metal Weight Limit (Pure Weight): pure grams the supplier's linked
// warehouses hold now, plus what unsynced MOP Logs will still move there at the next EOD sync.
function show_pure_metal_holding(frm) {
	const field = frm.get_field("custom_metal_weight_limit");
	if (!field || frm.is_new()) return;
	if (frm.__metal_limit_help === undefined) frm.__metal_limit_help = field.df.description || "";
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doc_events.supplier_metal_limit.get_supplier_pure_holding",
		args: { supplier: frm.doc.name },
		callback(r) {
			const d = r.message;
			if (!d) return;
			const limit = flt(frm.doc.custom_metal_weight_limit);
			const total = flt(d.held + d.pending, 3);
			let text = __("Pure metal held: {0} g", [format_number(d.held, null, 3)]);
			if (flt(d.pending)) {
				text += " " + __("({0} g pending EOD sync)", [format_number(d.pending, null, 3)]);
			}
			if (limit > 0) {
				text += " " + __("of {0} g limit", [format_number(limit, null, 3)]);
			}
			const colour = limit > 0 && total > limit ? "red" : "grey";
			field.set_description(
				`<span style="color: var(--${colour}-600)">${text}</span><br>${frm.__metal_limit_help}`
			);
		},
	});
}
