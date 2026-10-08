// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

// The Company Accounts pickers offer only ledgers that the server-side rules
// (subcontracting_settings.py, _validate_account) would accept, so a Liability
// can no longer be chosen as the COGS Adjustment Account. The server still
// validates on save and again at posting; this only stops the wrong pick.
const CUSTOMER_GOLD_BLOCKED_ACCOUNT_TYPES = ["Receivable", "Payable", "Stock"];

function customer_gold_account_query(root_type) {
	return (doc, cdt, cdn) => {
		const row = locals[cdt][cdn];
		const filters = {
			is_group: 0,
			disabled: 0,
			account_type: ["not in", CUSTOMER_GOLD_BLOCKED_ACCOUNT_TYPES],
			root_type: root_type,
		};
		if (row.company) {
			filters.company = row.company;
		}
		return { filters: filters };
	};
}

frappe.ui.form.on("Subcontracting Settings", {
	setup(frm) {
		frm.set_query(
			"customer_gold_liability_account",
			"company_accounts",
			customer_gold_account_query("Liability")
		);
		frm.set_query(
			"customer_gold_cogs_adjustment_account",
			"company_accounts",
			customer_gold_account_query(["!=", "Liability"])
		);
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
