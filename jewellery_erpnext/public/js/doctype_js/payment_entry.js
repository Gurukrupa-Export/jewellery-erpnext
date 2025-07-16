frappe.ui.form.on("Payment Entry", {

	refresh: (frm) => {
		frm.trigger("setup");
	},
	setup: (frm) => {
		if ((frm.doc.docstatus == 1) && (frm.doc.unallocated_amount > 0) && (frm.doc.party_type == "Customer")){

			frm.add_custom_button("Reconcile Inter Branch", async () => {
				this.data = await frm.events.get_unreconciled_sales_invoices(frm, frm.doc.company, frm.doc.party);

				var d = new frappe.ui.Dialog({
					title: __("Reconcile Inter Branch Payment"),
					size: "extra-large",
					fields: [
						{
							label: __("Paid Amount"),
							fieldtype: "Currency",
							fieldname: "paid_amount",
							default: frm.doc.paid_amount,
							read_only: 1,
						},
						{
							fieldtype: "Column Break",
							fieldname: "column_break_1"
						},
												{
							label: __("Total Allocated Amount"),
							fieldtype: "Currency",
							fieldname: "total_allocated_amount",
							onchange: () => {
								let total_allocated_amount = d.get_value("total_allocated_amount")
								let paid_amount = d.get_value("paid_amount")
								if (total_allocated_amount > paid_amount) {
									frappe.throw(__("Total Allocated Amount cannot be greater than Paid Amount"));
								}
							},
							default: 0,
							read_only: 1,
						},
						{
							fieldtype: "Section Break",
							fieldname: "section_break_1",
						},
						{
							label: __("Unreconciled Invoices"),
							fieldtype: "Table",
							fieldname: "invoices",
							data: this.data,
							get_data: () => {
								return this.data;
							},
							fields: [
								{
									"label": __("Sales Invoice"),
									"fieldtype": "Link",
									"fieldname": "sales_invoice",
									"options": "Sales Invoice",
									"in_list_view": 1,
									"reqd": 1,
									"read_only": 1,
									 get_query: function () {
										return {
											filters: {
												company: frm.doc.company,
												customer: frm.doc.party,
											},
										};
									}
								},
								{
									"label": __("Posting Date"),
									"fieldtype": "Date",
									"fieldname": "posting_date",
									"in_list_view": 1,
									"reqd": 1,
									"read_only": 1
								},
								{
									"label": __("Branch"),
									"fieldtype": "Link",
									"fieldname": "branch",
									"options": "Branch",
									"in_list_view": 1,
									"reqd": 1,
									"read_only": 1
								},
								{
									"label": __("Outstanding Amount"),
									"fieldtype": "Currency",
									"fieldname": "outstanding_amount",
									"in_list_view": 1,
									"reqd": 1,
									"read_only": 1
								},
								{
									"label": __("Allocated Amount"),
									"fieldtype": "Currency",
									"fieldname": "allocated_amount",
									"default": 0,
									"onchange": function () {
										let si_list = d.fields_dict.invoices.grid.get_selected_children();
										let total_allocated_amount = 0;

										if (!si_list.length) {
											si_list = d.get_values()["invoices"];
										}

										si_list.forEach((invoice) => {
											total_allocated_amount += invoice.allocated_amount || 0;
										})

										d.set_value("total_allocated_amount", total_allocated_amount);
									},
									"in_list_view": 1,
									"reqd": 1,
								}
							]
						}

					],
					primary_action: (values) => {
						const selected_invoices = d.fields_dict.invoices.grid.get_selected_children();

						const invoice_data = selected_invoices.map((invoice) => {
							return {
								company: frm.doc.company,
								posting_date: frm.doc.posting_date,
								doctype: frm.doc.doctype,
								pe_name: frm.doc.name,
								pe_branch: frm.doc.branch,
								paid_amount: frm.doc.paid_amount,
								receivable_account: frm.doc.paid_from,
								party_type: frm.doc.party_type,
								party: frm.doc.party,
								si_name: invoice.sales_invoice,
								si_branch: invoice.branch,
								allocated_amount: invoice.allocated_amount,
								outstanding_amount: invoice.outstanding_amount,

							}
						})

						if (!invoice_data.length) {
							frappe.msgprint("Please select the invoice you need to reconcile.")
						}

						frappe.call({
							method: "jewellery_erpnext.jewellery_erpnext.doc_events.payment_entry.reconcile_inter_branch_payment",
							args: {
								invoice_data: invoice_data
							},
							freeze: 1,
							freeze_msg: __("Reconciling Inter Branch Payment.."),
							callback: (r) => {
								if (!r.exec && r.message) {
									d.hide()
									if (r.message.length) {
										let link_html = `\n`
										r.message.forEach(jv_name => {
											let jv_link = frappe.utils.get_form_link("Journal Entry", jv_name)
											link_html += `<a href="${jv_link}" class="text-muted">${jv_name}</a>\n`
										});
										frappe.msgprint(`Journal Entry has been Successfully Created ${link_html}`)
									}
								}

							}

						})

					},
					primary_action_label: __('Reconcile Payment')
				})
				d.show()

			}).addClass("btn-primary")
		}
	},
	get_unreconciled_sales_invoices: async (frm, company, customer) => {
		let si_list = []
		await frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.payment_entry.get_unreconciled_sales_invoices",
			args: {
				company: company,
				customer: customer,
			},
			callback: (r) => {
				if (r.message) {
					si_list = r.message.map(si => {
						return {
							"sales_invoice": si.name,
							"posting_date": si.posting_date,
							"branch": si.branch,
							"outstanding_amount": si.outstanding_amount,
						}
					})

				} else {
					frappe.msgprint(__("No unreconciled sales invoices found for this customer."));
				}
			}
		});

		return si_list;
	}
})