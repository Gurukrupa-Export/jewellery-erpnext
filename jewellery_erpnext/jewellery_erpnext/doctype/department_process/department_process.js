// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Department Process", {
	setup(frm) {
		frm.set_query("department", () => ({ filters: { is_group: 0 } }));
	},
	department(frm) {
		if (!frm.doc.department) return;
		frappe.db.get_value("Department", frm.doc.department, "manufacturer").then((r) => {
			const manufacturer = r && r.message && r.message.manufacturer;
			if (manufacturer) frm.set_value("manufacturer", manufacturer);
		});
	},
});
