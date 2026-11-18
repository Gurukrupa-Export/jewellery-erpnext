// Copyright (c) 2024, 8848 Digital LLP and contributors
// For license information, please see license.txt

frappe.ui.form.on("Child Stock Reconcilation", {
	stock_reconcillation(frm) {
		//     if(frm.doc.previous_child_stock_reconciliation==0){
		//         {frm.doc.items.forEach(function(item) {
		//         frm.set_query("item_code","items", function() {
		//             return {
		//                 filters: {
		//                     'warehouse': item.warehouse
		//                 }
		//             }
		//         })
		//     })
		// }
		//     }
		if (frm.doc.stock_reconcillation && frm.doc.set_warehouse) {
			frm.doc.items.forEach(function (item) {
				item.warehouse = frm.doc.set_warehouse;
			});
			frm.refresh_field("items");
		}
		frappe.call({
			method: "fetch_stock_reconciliation_item",
			doc: frm.doc,
			callback: function () {
				refresh_field("items");
			},
		});
	},
	get_previous_child_stock_reconcilliation_number(frm) {
		frappe.call({
			method: "fetch_previous_child_stock_reconcilation",
			doc: frm.doc,
			callback: function (r) {
				frm.doc.previous_child_stock.forEach(function (item) {
					if (r.message) {
						// frappe.module.set_value(item.stock_reconciliation_number,r.message)
						item.stock_reconciliation_number = r.message;
					}
				});
				frm.refresh_field("previous_child_stock");
			},
		});
	},
});

// Pure weight = qty x purity % / 100 (metal and finding items, else 0). The server recomputes it on
// save (ChildStockReconcilation.set_pure_weight); this keeps the grid current while typing.
frappe.ui.form.on("Child Stock Reconcilation Item", {
	item_code: set_child_pure_weight,
	qty: set_child_pure_weight,
});

async function set_child_pure_weight(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frm._purity_cache = frm._purity_cache || {};
	if (row.item_code && !(row.item_code in frm._purity_cache)) {
		const found = await frappe.xcall(
			"jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation.get_purity_percentages",
			{ item_codes: [row.item_code] }
		);
		frm._purity_cache[row.item_code] = flt((found || {})[row.item_code]);
	}
	const purity = row.item_code ? frm._purity_cache[row.item_code] : 0;
	const pure_weight = purity ? flt((flt(row.qty) * purity) / 100, 3) : 0;
	if (flt(row.pure_weight) !== pure_weight) {
		frappe.model.set_value(cdt, cdn, "pure_weight", pure_weight);
	}
}

// function:get_items(frm) {

// },
