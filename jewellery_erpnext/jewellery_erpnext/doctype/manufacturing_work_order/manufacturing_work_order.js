// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Manufacturing Work Order", {
	refresh: function (frm) {
		if (frm.doc.docstatus == 1) {
			frappe.call({
				method: "jewellery_erpnext.customer_subcontracting.sub_utils.snc.validate_button_visibility",
				args: {
					mwo: frm.doc.name,
				},
				callback: function (r) {
					if (!r.message) return;
					frm.add_custom_button(__("Create SNC"), function () {
						frappe.call({
							method: "jewellery_erpnext.customer_subcontracting.sub_utils.snc.create_snc",
							args: {
								mwo: frm.doc.name,
							},
							freeze: true,
							freeze_message: __("Creating SNC"),
							callback: function (response) {
								if (!response.message) return;
								const make_receive =
									response.message.make_receive && response.message.make_receive.docname
										? response.message.make_receive.docname
										: __("Not created");
								frappe.msgprint({
									title: __("SNC Created"),
									indicator: "green",
									message: __(
										"Material Receive: {0}<br>Metal Conversion: {1}<br>Material Transfer: {2}",
										[
											make_receive,
											(response.message.conversions || []).join(", ") || __("None"),
											(response.message.transfers || []).join(", ") || __("None"),
										]
									),
								});
								frm.reload_doc();
							},
						});
					});
				},
			});
		}
		if (
			frm.doc.docstatus == 1 &&
			frm.doc.qty < 2 &&
			["In Process", "Not Started"].includes(frm.doc.status)
		) {
			frm.add_custom_button(__("Split Work Order"), function () {
				frm.trigger("split_work_order");
			});
		}
		// Single unpack button, Repair work orders only: unpacks the repaired serial
		// into the linked Repair Order's BOM raw materials as Customer Goods. PMO type
		// is not on the form, so resolve it before adding the button.
		if (frm.doc.docstatus == 1 && frm.doc.serial_no) {
			frappe.db
				.get_value("Parent Manufacturing Order", frm.doc.manufacturing_order, "type")
				.then((r) => {
					if (r.message && r.message.type === "Repair") {
						frm.add_custom_button(__("Unpack Raw Material"), function () {
							frm.trigger("unpack_raw_material");
						});
					}
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
		// if (frm.doc.docstatus == 0 && frm.doc.department && frm.doc.department.startsWith("Serial Number")) {
		// 	frm.add_custom_button(__("Create Repack"), function () {
		// 		frappe.call({
		// 			method: "jewellery_erpnext.customer_subcontracting.sub_utils.repack.create_pending_repack",
		// 			args: {
		// 				mwo_name: frm.doc.name,
		// 			},
		// 			freeze: true,
		// 			freeze_message: __("Creating Repack..."),
		// 			callback: function (r) {
		// 				if (!r.exc) {
		// 					frappe.msgprint(__("Customer Gold Repack Created"));

		// 					frm.reload_doc();
		// 				}
		// 			},
		// 		});
		// 	});
		// }

		// Photoshop images live on THIS work order (never on the Item or the BOM).
		// Hide the section outright when the Design Code Item is not flagged, and
		// offer "Upload Missing Images" while either slot is still empty.
		toggle_photoshop_section(frm);

		set_html(frm);
	},
	before_submit: function (frm) {
		// Client-side intercept: on FG work orders whose Design Code Item is
		// flagged 'Is Photoshop Images', the Finish Front View AND Left View
		// images are mandatory ON THIS WORK ORDER. Catch it here so the user gets
		// the upload dialog instead of a bare server throw.
		if (!frm.doc.item_code || !frm.doc.for_fg) return;

		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_missing_photoshop_images",
			args: {
				manufacturing_work_order: frm.doc.name,
			},
			async: false,
			callback: function (r) {
				if (!r.message || !r.message.check_required) return;

				const missing = r.message.missing || [];
				if (!missing.length) return;

				const image_fields = r.message.image_fields || {};
				const labels = missing.map((f) => __(image_fields[f] || f));

				frappe.validated = false;
				frappe.msgprint({
					title: __("Missing Photoshop Images"),
					indicator: "orange",
					message: __(
						"MWO cannot be submitted. Finish <b>Front View</b> and <b>Left View</b> " +
							"images are mandatory on this work order.<br>" +
							"Missing: <b>{0}</b><br>" +
							"Attach them in the <b>Photoshop Images</b> section, or click " +
							"<b>Upload Missing Images</b> to upload them now.",
						[labels.join(", ")]
					),
				});
				// Open the upload dialog automatically
				setTimeout(function () {
					open_upload_images_dialog(frm);
				}, 500);
			},
		});
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
			method: "create_unpack_serial_no_stock_entry",
			freeze: true,
			freeze_message: __("Unpacking...."),
			callback: (r) => {
				if (!r.exc) {
					frappe.msgprint(__("Serial No unpacked into raw materials."));
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
	on_submit: function (frm) {
		let attempts = 0;
		function try_fetch_snc() {
			frappe.call({
				method: "frappe.client.get_list",
				args: {
					doctype: "Serial Number Creator",
					filters: { manufacturing_work_order: frm.doc.name, docstatus: ["!=", 2] },
					fields: ["name"],
					limit_page_length: 1,
				},
				callback: function (r) {
					if (r.message && r.message.length) {
						frappe.set_route("Form", "Serial Number Creator", r.message[0].name);
					} else {
						attempts++;
						if (attempts < 5) {
							setTimeout(try_fetch_snc, 2000); // retry after 2 seconds
						} else {
							frappe.msgprint("Serial Number Creator is not ready yet. Please refresh later.");
						}
					}
				},
			});
		}
		try_fetch_snc();
	},
});

// -------- Upload Missing Images Dialog --------

const PHOTOSHOP_IMAGE_SECTION = ["photoshop_images_section", "finish_front_view", "finish_left_view"];

// Show the Photoshop Images section (and the upload shortcut) only for FG work
// orders whose Design Code Item is flagged. The section is visible by default in
// the doctype, so a failed call leaves the fields reachable rather than stranded.
function toggle_photoshop_section(frm) {
	if (frm.doc.__islocal || !frm.doc.item_code || !frm.doc.for_fg) return;

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_missing_photoshop_images",
		args: {
			manufacturing_work_order: frm.doc.name,
		},
		callback: function (r) {
			if (!r.message) return;

			const required = !!r.message.check_required;
			frm.toggle_display(PHOTOSHOP_IMAGE_SECTION, required);
			if (!required) return;

			if (frm.doc.docstatus == 0 && (r.message.missing || []).length) {
				frm.add_custom_button(
					__("Upload Missing Images"),
					function () {
						open_upload_images_dialog(frm);
					},
					__("Actions")
				);
			}
		},
	});
}

function open_upload_images_dialog(frm) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_missing_photoshop_images",
		args: {
			manufacturing_work_order: frm.doc.name,
		},
		callback: function (r) {
			if (!r.message || !r.message.check_required) {
				frappe.msgprint(__("Photoshop images are not required for this work order."));
				return;
			}

			const image_fields = r.message.image_fields || {};
			const missing = r.message.missing || [];

			if (!missing.length) {
				frappe.msgprint(__("All images are already uploaded."));
				return;
			}

			// Only the two mandatory slots exist, and they are stored on this work
			// order alone - nothing is written to the Item or the Master BOM.
			let dialog_fields = [
				{
					fieldtype: "Section Break",
					label: __("Mandatory Finish Images ({0})", [frm.doc.name]),
				},
			];
			for (const fieldname of missing) {
				dialog_fields.push({
					fieldname: fieldname,
					fieldtype: "Attach Image",
					label: __(image_fields[fieldname] || fieldname),
					reqd: 1,
				});
			}

			const dlg = new frappe.ui.Dialog({
				title: __("Upload Missing Images"),
				fields: dialog_fields,
				size: "large",
				primary_action_label: __("Upload & Save"),
				primary_action: function () {
					// get_values() returns null when a mandatory slot is empty and has
					// already told the user which one.
					const values = dlg.get_values();
					if (!values) return;

					let images = {};
					for (const fieldname of missing) {
						if (values[fieldname]) images[fieldname] = values[fieldname];
					}

					if (!Object.keys(images).length) {
						frappe.msgprint(__("Please upload at least one image."));
						return;
					}

					frappe.call({
						method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.update_photoshop_images",
						args: {
							manufacturing_work_order: frm.doc.name,
							images: JSON.stringify(images),
						},
						freeze: true,
						freeze_message: __("Saving images..."),
						callback: function (res) {
							if (res.message && res.message.success) {
								frappe.show_alert({
									message: __("Images saved on this work order."),
									indicator: "green",
								});
								dlg.hide();
								frm.reload_doc();
							}
						},
					});
				},
			});
			dlg.show();
		},
	});
}

// -------- HTML helpers --------

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
