// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refinery Price List", {
	supplier(frm) {
		// Default the service item filter to purchase items when picking one.
		frm.set_query("service_item", () => ({ filters: { is_purchase_item: 1 } }));
	},
	refresh(frm) {
		frm.set_query("service_item", () => ({ filters: { is_purchase_item: 1 } }));
	},
});
