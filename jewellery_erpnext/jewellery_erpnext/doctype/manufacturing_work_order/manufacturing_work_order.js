// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Manufacturing Work Order", {
	refresh: function (frm) {
		if (
			frm.doc.docstatus == 1 &&
			frm.doc.qty < 2 &&
			["In Process", "Not Started"].includes(frm.doc.status)
		) {
			frm.add_custom_button(__("Split Work Order"), function () {
				frm.trigger("split_work_order");
			});
		}
		if (frm.doc.docstatus == 1 && frm.doc.serial_no) {
			frm.add_custom_button(__("Unpack Raw Material"), function () {
				frm.trigger("unpack_raw_material");
			});
		}
		if (frm.doc.docstatus == 1 && frm.doc.is_finding_mwo == 1) {
			if (!frm.doc.final_transfer_entry) {
				frm.add_custom_button(__("Finish PMO"), function () {
					frm.trigger("transfer_to_raw");
				});
			}
			if (!frm.doc.finding_transfer_entry) {
				frm.add_custom_button(__("Transfer Finding"), function () {
					frm.trigger("transfer_finding");
				});
			}
		}

		if (frm.doc.multicolour && !frm.doc.merged_mwo) {
			frm.events.setup_merge_mwo_button(frm)
		}
		set_html(frm);
	},
	transfer_to_raw: function (frm) {
		frm.call({
			doc: frm.doc,
			method: "create_mfg_entry",
			freeze: true,
			freeze_message: __("Manufacturing...."),
			callback: (r) => {
				if (!r.exc) {
					frappe.msgprint(__("Manufacturing Entry has been generated."));
					frm.refresh();
				}
			},
		});
	},
	transfer_finding: function (frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Transfer to another MWO"),
			fields: [
				{
					fieldname: "mwo",
					fieldtype: "Link",
					options: "Manufacturing Work Order",
					label: "Manufacturing Work Order",
					reqd: 1,
					get_query: () => {
						return {
							filters: {
								manufacturing_order: frm.doc.manufacturing_order,
								docstatus: 1,
								is_finding_mwo: 0,
							},
						};
					},
				},
			],
			primary_action: function () {
				frm.doc.transfer_mwo = dialog.get_values()["mwo"];
				frm.call({
					doc: frm.doc,
					method: "transfer_to_mwo",
					freeze: true,
					freeze_message: __("Transfering...."),
					callback: (r) => {
						if (!r.exc) {
							frappe.msgprint(__("Material Tranfered to the MWO."));
							frm.refresh();
						}
					},
				});
				dialog.hide();
			},
			primary_action_label: __("Submit"),
		});
		dialog.show();
	},
	unpack_raw_material: function (frm) {
		frm.call({
			doc: frm.doc,
			method: "create_repair_un_pack_stock_entry",
			freeze: true,
			freeze_message: __("Unpacking...."),
			callback: (r) => {
				if (!r.exc) {
					frappe.msgprint(__("Item Unpacking done."));
					frm.refresh();
				}
			},
		});
	},
	split_work_order: function (frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Update"),
			fields: [
				{
					fieldname: "split_count",
					fieldtype: "Int",
					label: "Split Into",
				},
			],
			primary_action: function () {
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.create_split_work_order",
					freeze: true,
					args: {
						docname: frm.doc.name,
						company: frm.doc.company,
						manufacturer: frm.doc.manufacturer,
						count: dialog.get_values()["split_count"],
					},
					callback: function (r) {
						frm.reload_doc();
					},
				});
				dialog.hide();
			},
			primary_action_label: __("Submit"),
		});
		dialog.show();
		// dialog.$wrapper.find('.modal-dialog').css("max-width", "90%");
	},
	setup_merge_mwo_button: async (frm) => {
		console.log("asdasdasdsd");

		this.data = await frm.events.get_merge_mwo_list(frm)
		console.log(this.data)
		if (this.data.length) {

			frm.add_custom_button("Merge MWO", () => {
				var d = new frappe.ui.Dialog({
					title: __("Merge MWO Operation"),
					size: "extra-large",
					fields: [
						{
							fieldname: "mwo_list",
							fieldtype: "Table",
							label: "MWO List",
							data: this.data,
							get_data: () => {
								return this.data
							},
							fields: [
								{
									fieldname: "mwo",
									fieldtype: "Link",
									label: "Manufacturing Work Order",
									options: "Manufacturing Work Order",
									reqd: 1,
									read_only: 1,
									in_list_view: 1
								},
								{
									fieldname: "mop",
									fieldtype: "Link",
									label: "Manufacturing Operation",
									options: "Manufacturing Operation",
									reqd: 1,
									read_only: 1,
									in_list_view: 1
								}
							]
						}
					],
					primary_action: (values) => {
						const selected_mwo = d.fields_dict.mwo_list.grid.get_selected_children();

						let target_mop_list = selected_mwo.map((e) => {
							return e.mop
						})

						frappe.call({
							method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.merge_multicolor_mwo",
							args: {
								source_mop: frm.doc.manufacturing_operation,
								target_mop_list: target_mop_list
							},
							freeze: true,
							freeze_msg: __("Merging Multicolor MWO MOP Balance Table"),
							callback: (r) => {
								if (!r.exec) {
									d.hide()
									frappe.msgprint("MWO MOP Merged Successfully")
								}
							}
						})
					},
					primary_action_label: __("Merge MOP Operation")
				})
				d.show();

			}).addClass("btn-primary")
		}
	},
	get_merge_mwo_list: async (frm) => {
		let mwo_list = []
		await frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_merge_mwo_list",
			args: {
				pmo: frm.doc.manufacturing_order,
				mwo: frm.doc.name,
				mop: frm.doc.manufacturing_operation
			},
			freeze: true,
			callback: (r) => {
				if (!r.exec && r.message.length) {
					r.message.map((e) => {
						mwo_list.push({
							"mwo": e.name,
							"mop": e.manufacturing_operation
						})
					})
				}
			}
		})

		return mwo_list
	}
});

function set_html(frm) {
	if (frm.doc.__islocal && !frm.doc.is_last_operation) {
		frm.get_field("stock_entry_details").$wrapper.html("");
	}
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_linked_stock_entries",
		args: {
			mwo_name: frm.doc.name,
		},
		callback: function (r) {
			frm.get_field("stock_entry_details").$wrapper.html(r.message);
		},
	});
	if (frm.doc.for_fg) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_bom_summary",
			args: {
				design_id_bom: frm.doc.master_bom,
			},
			callback: function (r) {
				frm.get_field("bom_summary").$wrapper.html(r.message);
			},
		});
	}
}
