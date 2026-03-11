// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

// frappe.ui.form.on("MOP Settings", {
// 	refresh(frm) {
// 		// Bind the Sync MOP Log button
// 		frm.fields_dict.sync_mop_log.$input.on("click", function () {
// 			frappe.call({
// 				method: "sync_mop_log",
// 				doc: frm.doc,
// 				freeze: true,
// 				freeze_message: __("Processing EOD MOP Log Sync..."),
// 				callback: function (r) {
// 					if (r.message) {
// 						frappe.show_alert({
// 							message: __(
// 								"{0} logs processed, {1} Stock Entries created",
// 								[r.message.processed, r.message.stock_entries.length]
// 							),
// 							indicator: "green",
// 						});
// 					}
// 				},
// 			});
// 		});
// 	},
// });
