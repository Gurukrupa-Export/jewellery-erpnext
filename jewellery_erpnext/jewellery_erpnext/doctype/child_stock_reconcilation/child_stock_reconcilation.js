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

// function:get_items(frm) {

// },
