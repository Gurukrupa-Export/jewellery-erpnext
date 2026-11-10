// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

// Day 1: process pickers and a scan box that adds a row. The server reads each row's active
// Manufacturing Operation and current process on save; the client never decides them.

frappe.ui.form.on("Process Transfer", {
	setup(frm) {
		const process_filters = () => ({
			filters: { department: frm.doc.department, disabled: 0 },
		});
		frm.set_query("current_process", process_filters);
		frm.set_query("next_process", process_filters);
		frm.set_query("department", () => ({
			filters: { company: frm.doc.company, custom_enable_process_control: 1 },
		}));
		frm.set_query("manufacturing_work_order", "operations", () => ({
			filters: { docstatus: 1, company: frm.doc.company, manufacturer: frm.doc.manufacturer },
		}));
	},
	department(frm) {
		frm.set_value("current_process", null);
		frm.set_value("next_process", null);
	},
	initial_assignment(frm) {
		if (frm.doc.initial_assignment) frm.set_value("current_process", null);
	},
	scan_mwo(frm) {
		const mwo = (frm.doc.scan_mwo || "").trim();
		if (!mwo) return;
		const exists = (frm.doc.operations || []).some((r) => r.manufacturing_work_order === mwo);
		if (exists) {
			frappe.show_alert({
				message: __("{0} is already listed.", [frappe.utils.escape_html(mwo)]),
				indicator: "orange",
			});
		} else {
			frm.add_child("operations", { manufacturing_work_order: mwo });
			frm.refresh_field("operations");
		}
		frm.set_value("scan_mwo", "");
	},
});
