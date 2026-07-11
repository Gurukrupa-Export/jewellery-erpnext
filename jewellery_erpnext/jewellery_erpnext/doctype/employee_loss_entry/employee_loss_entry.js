// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Employee Loss Entry", {
	setup(frm) {
		frm.set_query("employee", () => ({ filters: { status: "Active" } }));
		frm.set_query("msl_warehouse", () => ({
			filters: { is_group: 0, warehouse_type: "Raw Material", disabled: 0 },
		}));
		frm.set_query("scrap_warehouse", () => ({
			filters: { is_group: 0, warehouse_type: "Scrap", disabled: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.stock_entry) {
			frm.add_custom_button(__("Loss Stock Entry"), () => {
				frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry);
			});
		}
	},
});
