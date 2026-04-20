// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("MOP Settings", {
	refresh(frm) {
		frm.fields_dict.sync_mop_log.$input.on("click", function () {
			frappe.call({
				method: "sync_mop_log",
				doc: frm.doc,
				callback: function () {
					frappe.show_alert({
						message: __("MOP Log sync has been queued as a background job."),
						indicator: "blue",
					});
				},
			});
		});
	},
});
