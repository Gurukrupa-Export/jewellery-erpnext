// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("User Process Access", {
	setup(frm) {
		frm.set_query("department_process", () => ({ filters: { disabled: 0 } }));
		frm.set_query("user", () => ({ filters: { enabled: 1, user_type: "System User" } }));
	},
});
