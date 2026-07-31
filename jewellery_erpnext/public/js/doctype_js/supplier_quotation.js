// Per-supplier Metal whitelist (Supplier -> Allowed Item Group). Convenience only: the
// load-bearing check is supplier_allowed_items.validate on the server, which also catches
// the supplier being changed after items were already picked. Mirrors erpnext
// BuyingController.setup_queries so the subcontracting branches behave exactly as before --
// only the query function changes. Non-metal item groups are unaffected.
frappe.ui.form.on("Supplier Quotation", {
	refresh(frm) {
		frm.set_query("item_code", "items", function () {
			let filters = { supplier: frm.doc.supplier };
			if (frm.doc.is_subcontracted) {
				if (frm.doc.is_old_subcontracting_flow) {
					filters["is_sub_contracted_item"] = 1;
				} else {
					filters["is_stock_item"] = 0;
				}
			} else {
				filters["is_purchase_item"] = 1;
				filters["has_variants"] = 0;
			}
			return {
				query: "jewellery_erpnext.jewellery_erpnext.doc_events.supplier_allowed_items.supplier_item_query",
				filters: filters,
			};
		});
	},
});
