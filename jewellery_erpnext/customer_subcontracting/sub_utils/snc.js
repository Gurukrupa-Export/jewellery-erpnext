frappe.ui.form.on("Stock Entry", {
	refresh(frm) {
		if (frm.doc.stock_entry_type === "Material Transfer (WORK ORDER)" && frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Create SNC"), () => {
				frappe.call({
					method: "jewellery_erpnext.customer_subcontracting.sub_utils.snc.create_snc",
					args: {
						stock_entry: frm.doc.name,
					},
					freeze: true,
					freeze_message: __("Creating SNC"),
					callback(r) {
						if (!r.message) return;

						const transfers = (r.message.transfers || []).join(", ") || __("None");
						const conversions = (r.message.conversions || []).join(", ") || __("None");
						const make_receive =
							r.message.make_receive && r.message.make_receive.docname
								? r.message.make_receive.docname
								: __("Not created");

						frappe.msgprint({
							title: __("SNC Created"),
							indicator: "green",
							message: __(
								"Material Receive: {0}<br>Metal Conversion: {1}<br>Work Order Transfer: {2}",
								[make_receive, conversions, transfers]
							),
						});
						frm.reload_doc();
					},
				});
			});
		}
	},
});
