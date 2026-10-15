// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Subcontracting Settings", {
	setup(frm) {
		// Offer only values finding items actually carry (the Finding Category / Finding
		// Sub-Category Item Attributes); the server rejects anything else on save.
		frm.set_query("finding_category", "hybrid_finding_categories", () => ({
			query: "jewellery_erpnext.jewellery_erpnext.doctype.department_operation.department_operation.get_finding_categories",
		}));
		frm.set_query("finding_sub_category", "hybrid_finding_categories", () => ({
			query: "jewellery_erpnext.customer_subcontracting.hybrid_findings.get_finding_sub_categories",
		}));
	},
});
