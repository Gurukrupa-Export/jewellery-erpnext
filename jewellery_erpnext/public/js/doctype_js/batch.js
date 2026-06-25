frappe.ui.form.on("Batch", {
refresh: (frm) => {
		if (!frm.is_new()) {
			frm.add_custom_button(__("View Ledger"), () => {
				frappe.route_options = {
					batch_no: frm.doc.name,
				};
				frappe.set_route("query-report", "Stock Ledger");
			});
			frm.trigger("make_dashboard");
		}
		if (frm.doc.custom_company !== "Sadguru Diamond") return;
        // Only on saved (not new) docs
        if (frm.doc.__islocal) return;

        frm.add_custom_button(__("Repack"), function () {
            make_repack_dialog(frm);
        }, __("Actions"));
	},
});



function make_repack_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __("Create Repack Entry"),
        fields: [
            {
                fieldname: "qty",
                fieldtype: "Float",
                label: __("Qty"),
                reqd: 1
            },
            {
                fieldname: "rate",
                fieldtype: "Currency",
                label: __("Target Rate"),
                reqd: 1,
                description: __("Rate for the new (target) batch")
            },
            {
                fieldname: "warehouse",
                fieldtype: "Link",
                label: __("Warehouse"),
                options: "Warehouse",
                reqd: 1,
                get_query: function () {
                    return {
                        filters: {
                            "company": frm.doc.custom_company,
                            "is_group": 0
                        }
                    };
                }
            }
        ],
        primary_action_label: __("Submit"),
        primary_action(values) {
            dialog.hide();

            frappe.confirm(
                __(`Create Repack Stock Entry for <b>${values.qty}</b> qty at rate <b>${values.rate}</b> from warehouse <b>${values.warehouse}</b>?`),
                () => {
                    frappe.call({
                        method: "jewellery_erpnext.jewellery_erpnext.customization.batch.batch.create_repack_stock_entry",
                        args: {
                            batch_no: frm.doc.name,
                            item_code: frm.doc.item,
                            qty: values.qty,
                            target_rate: values.rate,
                            company: frm.doc.custom_company,
                            warehouse: values.warehouse
                        },
                        freeze: true,
                        freeze_message: __("Creating Repack Entry..."),
                        callback(r) {
							if (r.message) {
								const { stock_entry, journal_entry } = r.message;

								let msg = `Stock Entry <a href="/app/stock-entry/${stock_entry}" target="_blank"><b>${stock_entry}</b></a> created and submitted successfully.`;

								if (journal_entry) {
									msg += `<br>Journal Entry <a href="/app/journal-entry/${journal_entry}" target="_blank"><b>${journal_entry}</b></a> also created for value difference.`;
								}

								frappe.msgprint({
									title: __("Repack Completed"),
									message: __(msg),
									indicator: "green"
								});

								frm.reload_doc();
							}
						}
                    });
                }
            );
        }
    });

    // Pre-fill qty from batch if available
    if (frm.doc.batch_qty) {
        dialog.set_value("qty", frm.doc.batch_qty);
    }

    dialog.show();
}
