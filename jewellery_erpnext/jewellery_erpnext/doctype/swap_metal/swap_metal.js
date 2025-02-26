// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Swap Metal", {
	refresh(frm) {
		set_child_table_batch_filter(frm, "target_table");
	},
	validate(frm) {
		if (frm.doc.target_table) {
			set_purity_calculation(frm);
		}
	},
	setup(frm) {
		frappe.call({
			doc: frm.doc,
			method: "get_warehouse",
			callback: function (r) {
				console.log(r.message);
				if (frm.doc.employee) {
					frm.set_value("source_warehouse", r.message[0]);
					frm.refresh_field("source_warehouse");
					frm.refresh();
				}
				if (!frm.doc.employee) {
					frm.set_value("source_warehouse", r.message[1]);
					frm.refresh_field("source_warehouse");
					frm.refresh();
				}
			},
		});
	},
});
frappe.ui.form.on("SM Target Table", {
	item_code(frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		set_missing_value(frm, d);
	},
	batch_no(frm, cdt, cdn) {
		get_selected_batch_qty(frm, cdt, cdn);
	},
});

function get_selected_batch_qty(frm, cdt, cdn) {
	var d = locals[cdt][cdn];
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.swap_metal.swap_metal.get_selected_batch_qty",
		args: { batch: d.batch_no },
		callback: function (r) {
			if (r.message) {
				frappe.model.set_value(d.doctype, d.name, "batch_available_qty", r.message);
			}
		},
	});
}
function set_purity_calculation(frm) {
	frm.call({
		doc: frm.doc,
		method: "target_qty_calculation",
		callback: function (r) {
			if (r.message) {
				frm.set_value("purity_wise_allowed_qty", r.message);
				frm.refresh_field("purity_wise_allowed_qty");
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
					frappe.model.set_value(d.doctype, d.name, "inventory_type", "Customer Goods");
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
function set_child_table_batch_filter(frm, child_table_name) {
	frm.fields_dict[child_table_name].grid.get_field("batch_no").get_query = function (
		doc,
		cdt,
		cdn
	) {
		var child = locals[cdt][cdn];
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.swap_metal.swap_metal.from_standard_get_batch_no",
			filters: {
				item_code: child.item_code,
				warehouse: child.s_warehouse,
				company: frm.doc.company,
			},
		};
	};
}
