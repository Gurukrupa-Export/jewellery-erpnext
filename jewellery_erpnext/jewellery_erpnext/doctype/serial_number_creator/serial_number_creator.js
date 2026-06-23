// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Serial Number Creator", {
	refresh: function (frm) {
		set_html(frm);
	},
});

frappe.ui.form.on("SNC FG Details", {
	refresh: function (frm, cdt, cdn) {
		var child = locals[cdt][cdn];
	},
	row_material: function (frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		frappe.db.get_value("Item", d.row_material, "item_group", function (r) {
			if (r.item_group === "Metal - V") {
				d.pcs = 1;
				frm.refresh_field("fg_details");
			}
		});
	},
});

function set_html(frm) {
	frappe.call({
		method: "run_doc_method",
		args: {
			dt: frm.doc.doctype,
			dn: frm.doc.name,
			method: "get_serial_summary",
		},
		callback: function (r) {
			if (r.message) {
				frm.get_field("serial_summery").$wrapper.html(r.message);
			}
		},
	});
	frappe.call({
		method: "run_doc_method",
		args: {
			dt: frm.doc.doctype,
			dn: frm.doc.name,
			method: "get_bom_summary",
		},
		callback: function (r) {
			if (r.message) {
				frm.get_field("bom_summery").$wrapper.html(r.message);
			}
		},
	});
}
