// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refinery Price List", {
	setup(frm) {
		// The Service Item is what the refining CHARGE is billed as on the PO — a
		// non-stock purchase item (like REF-SVC-001 "Refining Charges"). Stock metal
		// items (M-G/ML-G...) are excluded: billing a stock item would imply goods to
		// receive against the PO, pulling metal into stock that was never bought.
		frm.set_query("service_item", "slabs", () => ({
			filters: { is_purchase_item: 1, is_stock_item: 0 },
		}));
	},
});
