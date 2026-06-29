// frappe.ui.form.on("Stock Reconciliation", {
// 	custom_get_child_stock_reconcilliation(frm) {
// 		frappe.call({
// 			method: "jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation.get_child_reconciliation",
// 			args: {
// 				doc: frm.doc.name,
// 			},
// 			callback: function (r) {
// 				$.each(r.message, function (i, item) {
// 					// Check if item already exists in the table
// 					var existing_item = false;
// 					frm.doc.items.forEach(function (existing_row) {
// 						if (existing_row.item_code === item.item_code) {
// 							existing_item = true;
// 							return false; // exit loop early
// 						}
// 					});
// 					// Add item if it doesn't exist already
// 					if (!existing_item) {
// 						var row = frappe.model.add_child(
// 							frm.doc,
// 							"Stock Reconciliation Item",
// 							"items"
// 						);
// 						row.item_code = item.item_code;
// 						row.warehouse = item.warehouse;
// 						row.qty = item.qty;
// 						row.valuation_rate = item.valuation_rate;
// 					}
// 				});
// 				refresh_field("items");
// 			},
// 		});
// 	},
// });




frappe.ui.form.on("Stock Reconciliation", {
	refresh(frm){


     frm.page.remove_inner_button("Get Items");
        // frm.page.remove_inner_button("Fetch Items from Warehouse");
        if (frm.doc.docstatus == 0) {
        frm.add_custom_button("Get Work Orders", function () {

            let d = new frappe.ui.Dialog({
                title: "Get Items",
                fields: [
                    {
                        fieldtype: "Link",
                        fieldname: "warehouse",
                        label: "Warehouse",
                        options: "Warehouse",
                        reqd: 1
                    },
                    // {
                    //     fieldtype: "Link",
                    //     fieldname: "item_code",
                    //     label: "Item Code",
                    //     options: "Item"
                    // },
                    // {
                    //     fieldtype: "Check",
                    //     fieldname: "ignore_empty_stock",
                    //     label: "Ignore Empty Stock",
                    //     default: 1
                    // }
                ],
                primary_action_label: "Update",
                primary_action(values) {

                    if (!values.warehouse) {
                        frappe.msgprint("Please select Warehouse");
                        return;
                    }

                    // Clear existing items
                    frm.clear_table("items");
                    frm.refresh_field("items");

                    frappe.call({
                        method: "frappe.client.get_list",
                        args: {
                            doctype: "Manufacturing Operation",
                            filters: [
                                ["Manufacturing Operation", "department_ir_status", "!=", "In-Transit"],
                                ["Manufacturing Operation", "department", "=", frm.doc.custom_department],
                                ["Manufacturing Operation", "status", "=", "Not Started"]
                            ],
                            fields: ["name", "manufacturing_work_order"],
                            limit_page_length: 0 
                        },
                        callback: function (r) {

                            if (!r.message || r.message.length === 0) {
                                frappe.msgprint("No Manufacturing Operations Found");
                                return;
                            }

                            r.message.forEach(row => {
                                let child = frm.add_child("items");
                                child.custom_manufacturing_work_order = row.manufacturing_work_order;
                                child.warehouse = values.warehouse; // set selected warehouse also
                            });

                            frm.refresh_field("items");

                            frappe.msgprint("Work Orders Loaded Successfully");
                            d.hide();
                        }
                    });
                }
            });
            d.fields_dict.warehouse.get_query = function () {
                return {
                    filters: {
                        warehouse_type: "Manufacturing",
                        department: frm.doc.custom_department
                    }
                };
            };

            d.show();
            setTimeout(() => {
    d.fields_dict.warehouse.get_query = function () {
        return {
            filters: {
                warehouse_type: "Manufacturing",
                department: frm.doc.custom_department
            }
        };
    };
}, 500);
        });
    }






if (!frm.doc.custom_stock_comparison || frm.doc.custom_stock_comparison.length === 0) {
    frm.fields_dict.custom_mwo.$wrapper.html("");   // clear html
    return;
}


// collect MWOs from Stock Reconciliation items
let stock_mwo_set = new Set();
(frm.doc.items || []).forEach(row => {
    if (row.custom_manufacturing_work_order) {
        stock_mwo_set.add(row.custom_manufacturing_work_order);
    }
});

// collect MWOs from custom_stock_comparison
let comparison_mwo_set = new Set();
(frm.doc.custom_stock_comparison || []).forEach(row => {
    if (row.custom_manufacturing_work_order) {
        comparison_mwo_set.add(row.custom_manufacturing_work_order);
    }
});

// MWOs in Stock Recon but not in Child Reconciliation
let not_in_child_reconciliation = [];
stock_mwo_set.forEach(mwo => {
    if (!comparison_mwo_set.has(mwo)) {
        not_in_child_reconciliation.push(mwo);
    }
});

// MWOs in Child Reconciliation but not in Stock Recon
let not_in_stock_reconciliation = [];
comparison_mwo_set.forEach(mwo => {
    if (!stock_mwo_set.has(mwo)) {
        not_in_stock_reconciliation.push(mwo);
    }
});

// let html = `
//     <div style="padding:10px; background:#f8f9fa; border:1px solid #ddd; border-radius:6px;">
//         <b>Manufacturing Work Order Comparison</b>
//         <table style="width:100%; margin-top:10px; border-collapse:collapse;" border="1">
//             <thead>
//                 <tr style="background:#e9ecef;">
//                     <th style="padding:8px;">Not Available in Child Reconciliation</th>
//                     <th style="padding:8px;">Not Available in Stock Reconciliation</th>
//                 </tr>
//             </thead>
//             <tbody>
//                 <tr>
//                     <td style="padding:8px; vertical-align:top;">
//                         ${
//                             not_in_child_reconciliation.length > 0
//                             ? `<ul>${not_in_child_reconciliation.map(mwo => `<li>${mwo}</li>`).join("")}</ul>`
//                             : `<span style="color:green;"><b>All Available</b></span>`
//                         }
//                     </td>

//                     <td style="padding:8px; vertical-align:top;">
//                         ${
//                             not_in_stock_reconciliation.length > 0
//                             ? `<ul>${not_in_stock_reconciliation.map(mwo => `<li>${mwo}</li>`).join("")}</ul>`
//                             : `<span style="color:green;"><b>All Available</b></span>`
//                         }
//                     </td>
//                 </tr>
//             </tbody>
//         </table>
//     </div>
// `;




let html = `
    <div style="
        padding:10px;
        background:var(--card-bg);
        border:1px solid var(--border-color);
        border-radius:6px;
        color:var(--text-color);
    ">
        <b>Manufacturing Work Order Comparison</b>

        <table style="
            width:100%;
            margin-top:10px;
            border-collapse:collapse;
            color:var(--text-color);
        " border="1">
            <thead>
                <tr style="background:var(--subtle-fg);">
                    <th style="padding:8px; border-color:var(--border-color);">
                        Not Available in Child Reconciliation
                    </th>
                    <th style="padding:8px; border-color:var(--border-color);">
                        Not Available in Stock Reconciliation
                    </th>
                </tr>
            </thead>

            <tbody>
                <tr>
                    <td style="
                        padding:8px;
                        vertical-align:top;
                        border-color:var(--border-color);
                    ">
                        ${
                            not_in_child_reconciliation.length > 0
                            ? `<ul>${not_in_child_reconciliation.map(mwo => `<li>${mwo}</li>`).join("")}</ul>`
                            : `<span class="text-success"><b>All Available</b></span>`
                        }
                    </td>

                    <td style="
                        padding:8px;
                        vertical-align:top;
                        border-color:var(--border-color);
                    ">
                        ${
                            not_in_stock_reconciliation.length > 0
                            ? `<ul>${not_in_stock_reconciliation.map(mwo => `<li>${mwo}</li>`).join("")}</ul>`
                            : `<span class="text-success"><b>All Available</b></span>`
                        }
                    </td>
                </tr>
            </tbody>
        </table>
    </div>
`;

frm.fields_dict.custom_mwo.$wrapper.html(html);
	},















    
// 	custom_get_child_stock_reconcilliation(frm) {
// 		frappe.call({
// 			method: "jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation.get_child_reconciliation",
// 			args: {
// 				doc: frm.doc.name,
// 			},
// 			// callback: function (r) {				
// 			// 	$.each(r.message, function (i, item) {
// 			// 		// Check if item already exists in the table
// 			// 		var existing_item = false;
// 			// 		frm.doc.items.forEach(function (existing_row) {
// 			// 			if (existing_row.item_code === item.item_code) {
// 			// 				existing_item = true;
// 			// 				return false; // exit loop early
// 			// 			}
// 			// 		});
// 			// 		// Add item if it doesn't exist already
// 			// 		if (!existing_item) {
// 			// 			var row = frappe.model.add_child(
// 			// 				frm.doc,
// 			// 				"Stock Reconciliation Item",
// 			// 				"items"
// 			// 			);
// 			// 			row.item_code = item.item_code;
// 			// 			row.warehouse = item.warehouse;
// 			// 			row.qty = item.qty;
// 			// 			row.valuation_rate = item.valuation_rate;
// 			// 		}
// 			// 	});
// 			// 	refresh_field("items");
// 			// },
// 			callback: function (r) {
//     console.log(r.message);

//     $.each(r.message, function (i, item) {

//         // Check if item already exists in custom child table
//         var existing_item = false;

//         frm.doc.custom_stock_comparison.forEach(function (existing_row) {
//             if (existing_row.item_code === item.item_code) {
//                 existing_item = true;
//                 return false;
//             }
//         });

//         // Add item if it doesn't exist already
//         if (!existing_item) {
//             var row = frappe.model.add_child(
//                 frm.doc,
//                 "Stock Comparison",                 // child table doctype
//                 "custom_stock_comparison"           // child table fieldname
//             );

//             row.item_code = item.item_code;
//             row.warehouse = item.warehouse;
//             row.qty = item.qty;
//             row.valuation_rate = item.valuation_rate;
//         }
//     });

//     refresh_field("custom_stock_comparison");
// },
// 		});
// 	},
















// custom_get_child_stock_reconcilliation(frm) {

//     let d = new frappe.ui.Dialog({
//         title: "Select Child Stock Reconcilation",
//         fields: [
//             {
//                 fieldtype: "Link",
//                 label: "Child Stock Reconcilation",
//                 fieldname: "child_doc",
//                 options: "Child Stock Reconcilation",   // ✅ correct doctype
//                 reqd: 1,
//                 get_query: function () {
//                     return {
//                         filters: {
//                             stock_reconcillation: frm.doc.name   // ✅ correct fieldname
//                         }
//                     };
//                 }
//             }
//         ],
//         primary_action_label: "Get Data",
//         primary_action(values) {

//             if (!values.child_doc) {
//                 frappe.msgprint("Please select Child Stock Reconcilation");
//                 return;
//             }

//             d.hide();

//             // Fetch Child Stock Reconcilation document
//             frappe.call({
//                 method: "frappe.client.get",
//                 args: {
//                     doctype: "Child Stock Reconcilation",   // ✅ FIXED
//                     name: values.child_doc
//                 },
//                 callback: function (res) {

//                     if (!res.message) return;

//                     let child_doc = res.message;
//                     let child_items = child_doc.items || [];

//                     // Step 1: Create map item_code => total qty from Stock Reconciliation items table
//                     let items_qty_map = {};

//                     (frm.doc.items || []).forEach(row => {
//                         if (row.item_code) {
//                             items_qty_map[row.item_code] =
//                                 (items_qty_map[row.item_code] || 0) + flt(row.qty);
//                         }
//                     });

//                     // Step 2: Add into custom_stock_comparison
//                     child_items.forEach(item => {

//                         let existing_item = false;

//                         (frm.doc.custom_stock_comparison || []).forEach(existing_row => {
//                             if (existing_row.item_code === item.item_code) {
//                                 existing_item = true;
//                             }
//                         });

//                         if (!existing_item) {

//                             let api_qty = flt(item.qty);
//                             let current_qty = flt(items_qty_map[item.item_code] || 0);

//                             let row = frappe.model.add_child(
//                                 frm.doc,
//                                 "Stock Comparison",
//                                 "custom_stock_comparison"
//                             );

//                             row.item_code = item.item_code;
//                             row.warehouse = item.warehouse;
//                             row.qty = api_qty;
//                             row.currenty_qty = current_qty;
//                             row.diff_qty = current_qty - api_qty;
//                             row.valuation_rate = item.valuation_rate;
//                         }
//                     });

//                     refresh_field("custom_stock_comparison");
//                 }
//             });
//         }
//     });

//     d.show();
// }















































custom_get_child_stock_reconcilliation(frm) {

    let d = new frappe.ui.Dialog({
        title: "Select Child Stock Reconcilation",
        fields: [
            {
                fieldtype: "Link",
                label: "Child Stock Reconcilation",
                fieldname: "child_doc",
                options: "Child Stock Reconcilation",
                reqd: 1,
                get_query: function () {
                    return {
                        filters: {
                            stock_reconcillation: frm.doc.name
                        }
                    };
                }
            }
        ],
        primary_action_label: "Get Data",
        primary_action(values) {

            if (!values.child_doc) {
                frappe.msgprint("Please select Child Stock Reconcilation");
                return;
            }

            d.hide();
            frm.clear_table("custom_stock_comparison");
            frm.refresh_field("custom_stock_comparison");

            frappe.call({
                method: "frappe.client.get",
                args: {
                    doctype: "Child Stock Reconcilation",
                    name: values.child_doc
                },
                callback: function (res) {

                    if (!res.message) return;

                    let child_doc = res.message;
                    let child_items = child_doc.items || [];

                    // ---------------------------------------------------
                    // STEP 1: Check if Stock Reconciliation has any MWO
                    // ---------------------------------------------------
                    let has_mwo = false;

                    (frm.doc.items || []).forEach(row => {
                        if (row.custom_manufacturing_work_order) {
                            has_mwo = true;
                        }
                    });

                    // ---------------------------------------------------
                    // STEP 2: Create map from Stock Reconciliation items
                    // Store qty + custom_gross_weight
                    // ---------------------------------------------------
                    let stock_map = {};

                    (frm.doc.items || []).forEach(row => {

                        let key = has_mwo
                            ? row.custom_manufacturing_work_order
                            : row.item_code;

                        if (!key) return;

                        if (!stock_map[key]) {
                            stock_map[key] = {
                                qty: 0,
                                custom_gross_weight: 0
                            };
                        }

                        stock_map[key].qty += flt(row.qty);
                        stock_map[key].custom_gross_weight += flt(row.custom_gross_weight);
                    });

                    // ---------------------------------------------------
                    // STEP 3: Create map from Child Stock Reconciliation
                    // Store qty + gross_weight
                    // ---------------------------------------------------
                    let child_map = {};

                    child_items.forEach(item => {

                        let key = has_mwo
                            ? item.manufacturing_work_order
                            : item.item_code;

                        if (!key) return;

                        if (!child_map[key]) {
                            child_map[key] = {
                                item_code: item.item_code,
                                manufacturing_work_order: item.manufacturing_work_order,
                                warehouse: item.warehouse,
                                valuation_rate: item.valuation_rate,
                                qty: 0,
                                gross_weight: 0
                            };
                        }

                        child_map[key].qty += flt(item.qty);
                        child_map[key].gross_weight += flt(item.gross_weight);
                    });

                    // ---------------------------------------------------
                    // STEP 4: Insert into custom_stock_comparison table
                    // ---------------------------------------------------
                    Object.keys(child_map).forEach(key => {

                        let exists = false;

                        (frm.doc.custom_stock_comparison || []).forEach(existing_row => {

                            let existing_key = has_mwo
                                ? existing_row.custom_manufacturing_work_order
                                : existing_row.item_code;

                            if (existing_key === key) {
                                exists = true;
                            }
                        });

                        if (!exists) {

                            // child values
                            let api_qty = flt(child_map[key].qty);
                            let api_gross_weight = flt(child_map[key].gross_weight);

                            // stock reconciliation values
                            let current_qty = flt(stock_map[key]?.qty || 0);
                            let stock_gross_weight = flt(stock_map[key]?.custom_gross_weight || 0);

                            let row = frappe.model.add_child(
                                frm.doc,
                                "Stock Comparison",
                                "custom_stock_comparison"
                            );

                            row.item_code = child_map[key].item_code;
                            row.warehouse = child_map[key].warehouse;

                            // Child Stock Reconciliation values
                            row.qty = api_qty;
                            row.custom_gross_weight = api_gross_weight;

                            // Stock Reconciliation values
                            row.currenty_qty = current_qty;
                            row.gross_weight_in_stock_reconciliation = stock_gross_weight;

                            // ✅ diff_qty stores gross weight difference
                            row.diff_qty = stock_gross_weight - api_gross_weight;

                            row.valuation_rate = child_map[key].valuation_rate;

                            // store MWO if exists
                            if (has_mwo) {
                                row.custom_manufacturing_work_order =
                                    child_map[key].manufacturing_work_order;
                            }
                        }
                    });

                    refresh_field("custom_stock_comparison");

                    frappe.msgprint("Data fetched successfully!");
                }
            });
        }
    });

    d.show();
}
});

