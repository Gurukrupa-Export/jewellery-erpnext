import frappe

def before_submit(doc, method):
    """
    Cancel a Journal Entry if it exists, when unreconciling a Payment Entry.
    """
    if doc.voucher_type != "Payment Entry":
        return

    existing_jv_list = get_journal_entry_from_pe(doc.voucher_no)

    for je_name in existing_jv_list:
        je = frappe.get_doc("Journal Entry", je_name)
        je.cancel()


def get_journal_entry_from_pe(pe_name):
    jv_list = frappe.db.get_all("Journal Entry", {
        "ref_payment_entry":pe_name,
        "docstatus":1
    }, pluck="name")

    return jv_list