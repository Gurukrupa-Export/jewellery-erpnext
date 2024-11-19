// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt
// cur_frm.cscript.refresh = cur_frm.cscript.inspection_type;
frappe.ui.form.on("QC", {
	refresh: function (frm) {
		// Ignore cancellation of reference doctype on cancel all.
		frm.ignore_doctypes_on_cancel_all = [
			"Manufacturing Work Order",
			"Manufacturing Operation",
		];
		if (frm.doc.docstatus == 0 && frm.doc.status == "Pending") {
			frm.add_custom_button(__("Start QC"), function () {
				frm.set_value({ status: "WIP", start_time: frappe.datetime.now_datetime() });
				frm.save();
			});
		}
		if (frm.doc.status == "Rejected" && frm.doc.docstatus == 1) {
			frm.add_custom_button(__("Force Approve"), function () {
				frm.call({
					method: "force_approve",
					doc: frm.doc,
					callback: function () {
						frm.reload_doc();
						frappe.msgprint(__("Status updated successfully"));
					},
				});
			});
		}
	},
	setup: function (frm) {
		frm.set_query("manufacturing_operation", function (doc) {
			return {
				filters: {
					manufacturing_work_order: frm.doc.manufacturing_work_order,
				},
			};
		});
	},
	quality_inspection_template: function (frm) {
		if (frm.doc.quality_inspection_template) {
			return frm.call({
				method: "get_specification_details",
				doc: frm.doc,
				callback: function () {
					refresh_field("readings");
				},
			});
		}
	},
	received_gross_wt: function (frm) {
		var mwo = frm.doc.manufacturing_work_order;
		var mnf_opt = frm.doc.manufacturing_operation;
		var eir = frm.doc.employee_ir;
		var g_wt = frm.doc.gross_wt;
		var r_gwt = frm.doc.received_gross_wt;
		receive_gross_wt(frm, mwo, mnf_opt, eir, g_wt, r_gwt);
	},
});
function receive_gross_wt(frm, mwo, mnf_opt, eir, g_wt, r_gwt) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.qc.qc.receive_gross_wt_from_qc",
		args: {
			doc_name: frm.doc.name,
			mwo: mwo,
			mnf_opt: mnf_opt,
			eir: eir,
			g_wt: g_wt,
			r_gwt: r_gwt,
		},
		callback: function (r) {
			if (r.message) {
				console.log(r.message);
				frm.clear_table("employee_loss_details");
				var r_data = r.message;
				// for (var i = 0; i < r_data.length; i++) {
				//     var child = frm.add_child("employee_loss_details");
				//     child.item_code = r_data[i].item_code;
				//     child.net_weight = r_data[i].qty;
				// 	child.stock_uom = r_data[i].stock_uom;
				// 	child.manufacturing_work_order = r_data[i].manufacturing_work_order;
				// 	child.proportionally_loss = r_data[i].proportionally_loss;
				// 	child.received_gross_weight = r_data[i].received_gross_weight;
				// 	child.main_slip_consumption = r_data[i].main_slip_consumption;
				// }
				// frm.set_value("mop_loss_details_total",r.message[1])
				frm.refresh_field("employee_loss_details"); //,"mop_loss_details_total");
				// frm.save()
			} else {
				frm.clear_table("employee_loss_details");
				frm.refresh_field("employee_loss_details");
				// frm.save()
			}
		},
	});
}
