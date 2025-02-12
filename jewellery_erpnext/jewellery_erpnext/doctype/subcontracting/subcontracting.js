// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

// frappe.ui.form.on("Subcontracting", {
// 	refresh(frm) {
// 		set_sum(frm);
// 	},
// 	validate(frm) {
// 		if (frm.doc.target_table) {
// 			set_purity_calculation(frm);
// 		}
// 	},
// });
// frappe.ui.form.on("SM Target Table", {
// 	item_code(frm, cdt, cdn) {
// 		var d = locals[cdt][cdn];
// 		set_missing_value(frm, d);
// 	},
// 	// batch_no(frm, cdt, cdn) {
// 	// 	get_selected_batch_qty(frm, cdt, cdn);
// 	// },
// });

function set_sum(frm) {
	let qty_sum_source = 0;
	// let qty_sum_target = 0;

	if (frm.doc.source_table && frm.doc.source_table.length > 0) {
		frm.doc.source_table.forEach((row) => {
			qty_sum_source += row.qty;
		});
	} else {
		qty_sum_source = 0;
	}

	// if (frm.doc.target_table && frm.doc.target_table.length > 0) {
	// 	frm.doc.target_table.forEach((row) => {
	// 		qty_sum_target += row.qty;
	// 	});
	// } else {
	// 	qty_sum_target = 0;
	// }

	frm.set_value("sum_source_table", qty_sum_source);
	// frm.set_value("sum_target_table", qty_sum_target);

	frm.refresh_field("sum_source_table");
	// frm.refresh_field("sum_target_table");
}

function set_purity_calculation(frm) {
	frm.call({
		doc: frm.doc,
		method: "set_purity_wise_allowed_qty",
		callback: function (r) {
			if (r.message) {
				frm.set_value("purity_wise_allowed_target_qty", r.message);
				frm.refresh_field("purity_wise_allowed_target_qty");
			}
		},
	});
}

function set_missing_value(frm, d) {
	if (d.item_code) {
		var args = {
			item_code: d.item_code,
			warehouse: cstr(d.s_warehouse) || cstr(d.t_warehouse),
			transfer_qty: d.transfer_qty,
			serial_no: d.serial_no,
			batch_no: d.batch_no,
			bom_no: d.bom_no,
			expense_account: d.expense_account,
			cost_center: d.cost_center,
			company: frm.doc.company,
			qty: d.qty,
			voucher_type: frm.doc.doctype,
			voucher_no: d.name,
			allow_zero_valuation: 1,
		};
		frappe.call({
			doc: frm.doc,
			method: "custom_get_item_details",
			args: args,
			callback: function (r) {
				if (r.message) {
					$.each(r.message, function (k, v) {
						if (v) {
							frappe.model.set_value(d.doctype, d.name, k, v);
						}
					});
					frappe.model.set_value(d.doctype, d.name, "use_serial_batch_fields", 1);
					frappe.model.set_value(d.doctype, d.name, "inventory_type", "Regular Stock");
					frappe.model.set_value(
						d.doctype,
						d.name,
						"manufacturer",
						frm.doc.manufacturer
					);
					frappe.model.set_value(d.doctype, d.name, "department", frm.doc.department);
					frappe.model.set_value(
						d.doctype,
						d.name,
						"parent_manufacturing_order",
						frm.doc.manufacturing_order
					);
					frappe.model.set_value(
						d.doctype,
						d.name,
						"manufacturing_work_order",
						frm.doc.work_order
					);
					frappe.model.set_value(
						d.doctype,
						d.name,
						"manufacturing_operation",
						frm.doc.operation
					);

					refresh_field("target_table");
				}
			},
		});
	}
}
