// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refinery Price List", {
	setup(frm) {
		// Service items are purchase items (the charge billed on the service PO).
		frm.set_query("service_item", "slabs", () => ({
			filters: { is_purchase_item: 1 },
		}));
	},
});
