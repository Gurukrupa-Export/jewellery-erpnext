// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refinery Price List", {
	setup(frm) {
		// Deliberately NO restrictive query on covered_items.item_code. The default link
		// search is what lets an item TEMPLATE (ML / FL / M / F / G / D) be picked, and
		// templates are the point — one row instead of the thousands of variants some
		// templates have. Wiring erpnext's item_query, or a has_variants: 0 filter, would
		// silently kill template matching.
		frm.set_query("item_code", "covered_items", () => ({
			filters: { disabled: 0 },
		}));
	},
});
