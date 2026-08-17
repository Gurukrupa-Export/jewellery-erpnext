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
					frm.add_custom_button(__("Create SNC (All Materials)"), function () {
						frappe.call({
							method: "jewellery_erpnext.customer_subcontracting.sub_utils.snc.create_snc",
							args: {
								mwo: frm.doc.name,
								include_all_items: 1,
							},
							freeze: true,
							freeze_message: __("Creating SNC (All Items)"),
							callback: function (response) {
								if (!response.message) return;
								const make_receive =
									response.message.make_receive && response.message.make_receive.docname
										? response.message.make_receive.docname
										: __("Not created");
								frappe.msgprint({
									title: __("Full SNC Created"),
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
		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(
				__("Customer Goods Issue"),
				function () {
					frappe.call({
						method: "make_customer_goods_issue",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Creating Stock Entry"),
						callback: function (r) {
							if (r.message) {
								frappe.model.sync(r.message);
								frappe.set_route("Form", r.message.doctype, r.message.name);
							}
						},
					});
				}
			);
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

		// Show "Upload Missing Images" on Draft FG MWOs: either the mandatory
		// Front/Left pair is missing (blocking) or optional slots are still open.
		if (frm.doc.docstatus == 0 && frm.doc.item_code && !frm.doc.__islocal && frm.doc.for_fg) {
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_missing_photoshop_images",
				args: {
					item_code: frm.doc.item_code,
					master_bom: frm.doc.master_bom || "",
				},
				callback: function (r) {
					if (!r.message || !r.message.check_required) return;
					const blocking = Object.keys(r.message.missing || {}).length;
					const optional = (r.message.optional_item || []).length;
					if (blocking || optional) {
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

		set_html(frm);
	},
	before_submit: function (frm) {
		// Client-side intercept: on FG work orders whose Item is flagged
		// 'Is Photoshop Images', the Finish Front View AND Left View images are
		// mandatory on the Item (and mirrored onto the Master BOM). Catch it here
		// so the user gets the upload dialog instead of a bare server throw.
		if (!frm.doc.item_code || !frm.doc.for_fg) return;

		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_missing_photoshop_images",
			args: {
				item_code: frm.doc.item_code,
				master_bom: frm.doc.master_bom || "",
			},
			async: false,
			callback: function (r) {
				if (!r.message || !r.message.check_required) return;

				const missing = r.message.missing || {};
				if (!Object.keys(missing).length) return;

				const item_fields_map = r.message.item_image_fields || {};
				const bom_fields_map = r.message.bom_image_fields || {};
				const labels = (missing.item || [])
					.map((f) => __(item_fields_map[f] || f))
					.concat((missing.bom || []).map((f) => __(bom_fields_map[f] || f)));

				frappe.validated = false;
				frappe.msgprint({
					title: __("Missing Photoshop Images"),
					indicator: "orange",
					message: __(
						"MWO cannot be submitted. Finish <b>Front View</b> and <b>Left View</b> " +
							"images are mandatory on the Finished Item and its Master BOM.<br>" +
							"Missing: <b>{0}</b><br>" +
							"Click <b>Upload Missing Images</b> to upload them now.",
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

function open_upload_images_dialog(frm) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.get_missing_photoshop_images",
		args: {
			item_code: frm.doc.item_code,
			master_bom: frm.doc.master_bom || "",
		},
		callback: function (r) {
			if (!r.message || !r.message.check_required) {
				frappe.msgprint(__("Photoshop images are not required for this Item."));
				return;
			}

			const missing = r.message.missing || {};
			const item_fields_map = r.message.item_image_fields || {};
			const bom_fields_map = r.message.bom_image_fields || {};
			const required_item = missing.item || [];
			const optional_item = r.message.optional_item || [];

			// Only Finished Item slots are offered — the Item is the master and its
			// images are mirrored onto the Master BOM (on upload and on submit).
			// So a surviving BOM gap means no Master BOM is linked: no upload here
			// can fix that, say so instead of showing a useless dialog.
			if (!required_item.length && (missing.bom || []).length) {
				frappe.msgprint({
					title: __("Master BOM Not Linked"),
					indicator: "orange",
					message: __(
						"Finished Item {0} already carries the mandatory finish images, but the " +
							"Master BOM is missing {1}. Set the <b>Design Code BOM</b> on this work " +
							"order, then submit again.",
						[
							frm.doc.item_code,
							(missing.bom || []).map((f) => __(bom_fields_map[f] || f)).join(", "),
						]
					),
				});
				return;
			}

			if (!required_item.length && !optional_item.length) {
				frappe.msgprint(__("All images are already uploaded."));
				return;
			}

			let dialog_fields = [];

			if (required_item.length) {
				dialog_fields.push({
					fieldtype: "Section Break",
					label: __("Mandatory Finish Images ({0})", [frm.doc.item_code]),
				});
				for (const fieldname of required_item) {
					dialog_fields.push({
						fieldname: "item__" + fieldname,
						fieldtype: "Attach Image",
						label: __(item_fields_map[fieldname] || fieldname),
						reqd: 1,
					});
				}
			}

			if (optional_item.length) {
				dialog_fields.push({
					fieldtype: "Section Break",
					label: __("Other Finish Images (Optional)"),
					collapsible: 1,
				});
				for (const fieldname of optional_item) {
					dialog_fields.push({
						fieldname: "item__" + fieldname,
						fieldtype: "Attach Image",
						label: __(item_fields_map[fieldname] || fieldname),
					});
				}
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

					let item_images = {};
					for (const [key, val] of Object.entries(values)) {
						if (key.startsWith("item__") && val) {
							item_images[key.replace("item__", "")] = val;
						}
					}

					if (!Object.keys(item_images).length) {
						frappe.msgprint(__("Please upload at least one image."));
						return;
					}

					frappe.call({
						method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.update_photoshop_images",
						args: {
							item_code: frm.doc.item_code,
							master_bom: frm.doc.master_bom || "",
							item_images: JSON.stringify(item_images),
						},
						freeze: true,
						freeze_message: __("Saving images..."),
						callback: function (res) {
							if (res.message && res.message.success) {
								frappe.show_alert({
									message: __(
										"Images updated on Item Master and mirrored onto the Master BOM."
									),
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
