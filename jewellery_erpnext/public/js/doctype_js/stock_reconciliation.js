frappe.ui.form.on("Stock Reconciliation", {
	custom_get_child_stock_reconcilliation(frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation.get_child_reconciliation",
			args: {
				doc: frm.doc.name,
			},
			callback: function (r) {
				$.each(r.message, function (i, item) {
					// Check if item already exists in the table
					var existing_item = false;
					frm.doc.items.forEach(function (existing_row) {
						if (existing_row.item_code === item.item_code) {
							existing_item = true;
							return false; // exit loop early
						}
					});
					// Add item if it doesn't exist already
					if (!existing_item) {
						var row = frappe.model.add_child(
							frm.doc,
							"Stock Reconciliation Item",
							"items"
						);
						row.item_code = item.item_code;
						row.warehouse = item.warehouse;
						row.qty = item.qty;
						row.valuation_rate = item.valuation_rate;
					}
				});
				refresh_field("items");
			},
		});
	},
});
