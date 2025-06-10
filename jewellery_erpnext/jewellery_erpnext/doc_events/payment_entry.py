import frappe

def on_submit(doc, method):
	"""
	Create a journal entry after submission.
	"""
	if not doc.references:
		return

	pe_branch = doc.branch
	receivable_account = doc.paid_from

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
				receivable_account,
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
	if si_branch and (si_branch != pe_branch):
		return si_branch

	return False


def create_journal_entry_for_different_branch(doc, receivable_account, si_branch, pe_branch, amount):
	"""
	Create a journal entry for the different branch.
	"""
	# get branch accounts if not raise error
	si_branch_account = get_branch_account(si_branch)
	pe_branch_account = get_branch_account(pe_branch)

	jv = frappe.new_doc("Journal Entry")
	jv.voucher_type = "Journal Entry"
	jv.company = doc.company
	jv.posting_date = doc.posting_date


	jv.set("accounts", [
		{
			"account": receivable_account,
			"credit_in_account_currency": amount,
			"party_type": doc.party_type,
			"party": doc.party,
		},
		{
			"account": si_branch_account,
			"branch": si_branch,
			"debit_in_account_currency": amount,
		},
		{
			"account": pe_branch_account,
			"branch": pe_branch,
			"credit_in_account_currency": amount,
		},
		{
			"account": receivable_account,
			"debit_in_account_currency": amount,
			"party_type": doc.party_type,
			"party": doc.party,
		}

	])

	jv.insert()
	jv.submit()

	return jv.name


def get_branch_account(branch_name):
	"""
	Get the branch account for the given branch name.
	"""
	if branch_account:= frappe.db.get_value("Branch", branch_name, "branch_account"):
		return branch_account

	if branch_account:= frappe.db.exists("Account", {"account_name":branch_name}):
		return branch_account

	frappe.throw(f"Branch account for {branch_name} does not exist. Please create it first.")