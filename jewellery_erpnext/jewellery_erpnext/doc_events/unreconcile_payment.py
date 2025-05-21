import frappe
from frappe import qb

def before_submit(doc, method):
    """
    Cancel a Journal Entry if it exists, when unreconciling a Payment Entry.
    """
    if doc.voucher_type != "Payment Entry":
        return

    for alloc in doc.allocations:
        if not alloc.reference_doctype == "Sales Invoice" and not alloc.reference_name:
            continue

        if je_name := check_journal_entry_exists(doc, alloc.reference_doctype, alloc.reference_name):
            try:
                je = frappe.get_doc("Journal Entry", je_name)
                if je.docstatus == 1:
                    je.cancel()
            except frappe.DoesNotExistError:
                pass

            alloc.ref_journal_entry = None


def check_journal_entry_exists(doc, ref_doctype, ref_name):
    pe = qb.DocType("Payment Entry")
    per = qb.DocType("Payment Entry Reference")

    query = (
        qb.from_(per)
        .left_join(pe)
        .on(per.parent == pe.name)
        .select(per.ref_journal_entry)
        .where(
            (per.reference_doctype == ref_doctype)
            & (per.reference_name == ref_name)
            & (per.ref_journal_entry.isnotnull())
            & (pe.name == doc.voucher_no)
        )
    )

    res = query.run(as_dict=True)

    return res[0].ref_journal_entry if res else None