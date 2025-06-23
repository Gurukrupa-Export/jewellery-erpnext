frappe.ui.form.on("Payment Entry", {

    refresh: (frm) => {

        console.log("entered");
        frm.trigger("setup")

    },
    setup: (frm) => {
        if ((frm.doc.docstatus == 1) && (frm.doc.references.length == 0)) {

            frm.add_custom_button("Reconcile Inter Branch", () => {
                var d = new frappe.ui.Dialog({
                    title: __("Reconcile Inter Branch Payment"),
                    fields: [
                        {
                            "label": __("Select Sales Invoice"),
                            "fieldtype": "Link",
                            "fieldname": "sales_invoice",
                            "options": "Sales Invoice",
                            "reqd": 1
                        }

                    ],
                    primary_action: (values) => {
                        frappe.call({
                            method: "jewellery_erpnext.jewellery_erpnext.doc_events.payment_entry.reconcile_inter_branch_payment",
                            args: {
                                company: frm.doc.company,
                                posting_date: frm.doc.posting_date,
                                doctype: frm.doc.doctype,
                                pe_name: frm.doc.name,
                                pe_branch: frm.doc.branch,
                                paid_amount: frm.doc.paid_amount,
                                receivable_account: frm.doc.paid_from,
                                party_type: frm.doc.party_type,
                                party: frm.doc.party,
                                si_name: values.sales_invoice,
                            },
                            freeze: 1,
                            freeze_msg: __("Reconciling Inter Branch Payment.."),
                            callback: (r) => {
                                if (!r.exec) {
                                    d.hide()
                                    let link_html = `\n`
                                    r.message.forEach(jv_name => {
                                        let jv_link = frappe.utils.get_form_link("Journal Entry", jv_name)
                                        link_html += `<a href="${jv_link}" class="text-muted">${jv_name}</a>\n`
                                    });
                                    frappe.msgprint(`Journal Entry has been Successfully Created ${link_html}`)
                                }

                            }

                        })

                    },
                    primary_action_label: __('Reconcile Payment')
                })

                d.show()

            }).addClass("btn-primary")
        }
    }

})