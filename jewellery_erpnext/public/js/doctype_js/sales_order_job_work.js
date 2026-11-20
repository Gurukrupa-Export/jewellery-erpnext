// KGGK job work (spec step 14): invoice a Subcontracting Income Sales Order one PMO at a time.
// Kept apart from sales_order.js, which is already past the repository's file-size limit.

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || ["Closed", "Completed"].includes(frm.doc.status)) {
			return;
		}
		const job_work = "jewellery_erpnext.jewellery_erpnext.job_work";
		frappe.call({
			method: `${job_work}.has_job_work_rows`,
			args: { sales_order: frm.doc.name },
			callback(r) {
				if (!r.message) return;
				frm.add_custom_button(
					__("Job Work Sales Invoices"),
					() => {
						frappe.call({
							method: `${job_work}.make_job_work_sales_invoices`,
							args: { sales_order: frm.doc.name },
							freeze: true,
							freeze_message: __("Creating one Sales Invoice per PMO"),
							callback(res) {
								const invoices = res.message || [];
								if (!invoices.length) return;
								if (invoices.length === 1) {
									frappe.set_route("Form", "Sales Invoice", invoices[0]);
									return;
								}
								frappe.msgprint({
									title: __("Draft Sales Invoices (one per PMO)"),
									indicator: "green",
									message: invoices
										.map((name) =>
											frappe.utils.get_form_link("Sales Invoice", name, true)
										)
										.join("<br>"),
								});
								frm.reload_doc();
							},
						});
					},
					__("Create")
				);
			},
		});
	},
});
