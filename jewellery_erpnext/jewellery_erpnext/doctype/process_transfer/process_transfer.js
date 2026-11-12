// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

// The server reads each work order's active operation and current process and enforces every
// rule on save, submit and cancel; this script only offers filters and the scan/fetch entry.

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
		frm.set_value("scan_mwo", "");
		add_work_orders(frm, [mwo]);
	},
	get_operations(frm) {
		frappe.prompt(
			{
				fieldname: "work_orders",
				fieldtype: "Small Text",
				label: __("Manufacturing Work Orders"),
				description: __("One per line."),
				reqd: 1,
			},
			(values) => add_work_orders(frm, values.work_orders.split(/[\n,]+/)),
			__("Get Manufacturing Operations"),
			__("Add")
		);
	},
});

// Work orders the server cannot add are listed in a message; the form stays usable.
function add_work_orders(frm, work_orders) {
	const names = work_orders.map((w) => (w || "").trim()).filter(Boolean);
	if (!names.length) return;
	return frm.call("add_work_orders", { work_orders: names }).then(() => {
		frm.refresh_field("operations");
		frm.dirty();
	});
}
