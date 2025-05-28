import frappe

def on_submit(doc, method):
	"""
	Create a journal entry after submission.
	"""
	if not doc.references:
		return

	pe_branch = doc.branch
	paid_from_account = doc.paid_from
	paid_to_account = doc.paid_to

	if not pe_branch:
		return

	for row in doc.references:
		if (not row.reference_doctype == "Sales Invoice"
			and not row.reference_name
			and row.ref_journal_entry):
			continue

		si_name = row.reference_name
		si_branch = is_different_branch_ad(si_name, pe_branch)

		if si_branch:
			jv = create_journal_entry_for_different_branch(
				doc,
				paid_from_account,
				paid_to_account,
				si_branch,
				pe_branch,
				row.allocated_amount
			)
			row.db_set("ref_journal_entry", jv)


def on_cancel(doc, method):
	"""
	Cancel the journal entry on payment entry cancellation.
	"""
	if not doc.references:
		return

	for row in doc.references:
		if row.ref_journal_entry:
			try:
				jv = frappe.get_doc("Journal Entry", row.ref_journal_entry)
				if jv.docstatus == 1:
					jv.cancel()
			except frappe.DoesNotExistError:
				pass

			row.db_set("ref_journal_entry", None)


def is_different_branch_ad(si_name, pe_branch):
	si_branch = frappe.db.get_value("Sales Invoice", si_name, "branch")
	if si_branch and si_branch != pe_branch:
		return si_branch

	return False


def create_journal_entry_for_different_branch(doc, paid_from_account, paid_to_account, si_branch, pe_branch, amount):
	"""
	Create a journal entry for the different branch.
	"""
	jv = frappe.new_doc("Journal Entry")
	jv.voucher_type = "Journal Entry"
	jv.company = doc.company
	jv.posting_date = doc.posting_date

	jv.set("accounts", [
		{
			"account": paid_to_account,
			"debit_in_account_currency": amount,
			"branch": si_branch,
		},
		{
			"account": paid_to_account,
			"credit_in_account_currency": amount,
			"branch": pe_branch
		},
		{
			"account": paid_from_account,
			"debit_in_account_currency": amount,
			"party_type": doc.party_type,
			"party": doc.party,
			"branch": pe_branch,
		},
		{
			"account": paid_from_account,
			"credit_in_account_currency": amount,
			"party_type": doc.party_type,
			"party": doc.party,
			"branch": si_branch
		}
	])

	jv.insert()
	jv.submit()

	return jv.name
