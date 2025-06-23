import frappe
import frappe.utils


def create_inter_branch_journal_entries(args):
	"""
	Create two Journal Entries: one for PE branch, one for SI branch.
	Enqueue reconciliation job after commit.
	"""
	si_branch_account = get_branch_account(args.si_branch)
	pe_branch_account = get_branch_account(args.pe_branch)

	res = []
	args.pe_jv_name = None
	args.si_jv_name = None

	jv_pe_branch = [
		{
			"account": args.receivable_account,
			"debit_in_account_currency": args.amount,
			"party_type": args.party_type,
			"party": args.party,
			"branch": args.pe_branch
		},
		{
			"account": si_branch_account,
			"credit_in_account_currency": args.amount,
			"branch": args.pe_branch
		}
	]

	jv_si_branch = [
		{
			"account": args.receivable_account,
			"credit_in_account_currency": args.amount,
			"party_type": args.party_type,
			"party": args.party,
			"reference_type": "Sales Invoice",
			"reference_name": args.si_name,
			"branch": args.si_branch
		},
		{
			"account": pe_branch_account,
			"debit_in_account_currency": args.amount,
			"branch": args.si_branch
		}
	]

	jv_accounts_details = {
		args.pe_branch: jv_pe_branch,
		args.si_branch: jv_si_branch
	}

	for branch, accounts in jv_accounts_details.items():
		jv = frappe.new_doc("Journal Entry")
		jv.voucher_type = "Journal Entry"
		jv.company = args.company
		jv.posting_date = args.posting_date
		jv.custom_branch = branch
		jv.ref_payment_entry = args.pe_name

		for row in accounts:
			jv.append("accounts", row)

		jv.insert()
		jv.submit()

		if branch == args.pe_branch:
			args.pe_jv_name = jv.name
		else:
			args.si_jv_name = jv.name

		res.append(jv.name)

	# Enqueue reconciliation
	job_name = f"reconcile_{args.pe_jv_name}_invoice_with_payment_{args.pe_name}"
	frappe.enqueue(
		method=reconcile_pe_with_inter_branch_jv,
		args=args,
		enqueue_after_commit=True,
		job_name=job_name,
	)

	frappe.msgprint(
		f"""
		✅ Created Inter-Branch Journal Entries: <b>{', '.join(res)}</b><br>
		⏳ Reconciliation job <b>{job_name}</b> has been scheduled.<br>
		Please check the Payment Entry comments for final status.
		"""
	)

	return res


def reconcile_pe_with_inter_branch_jv(args=None):
	args = frappe._dict(args or {})
	pe_doc = frappe.get_doc("Payment Entry", args.pe_name)
	try:
		pe_reconc_doc = frappe.get_single("Payment Reconciliation")
		pe_reconc_doc.update({
			"company": args.company,
			"party_type": args.party_type,
			"party": args.party,
			"receivable_payable_account": args.receivable_account,
			"invoice_name": args.pe_jv_name,
			"payment_name": args.pe_name
		})

		pe_reconc_doc.get_unreconciled_entries()
		pe_reconc_dict = pe_reconc_doc.as_dict()
		entries = {
			"payments": pe_reconc_dict.get("payments"),
			"invoices": pe_reconc_dict.get("invoices")
		}

		pe_reconc_doc.allocate_entries(entries)
		pe_reconc_doc.reconcile()

		pe_doc.add_comment("Comment", "Inter Branch Payment Has Been Reconciled Successfully")

	except Exception:
		error_trace = frappe.get_traceback()
		error_log = frappe.log_error("Reconciliation Failed", error_trace)
		pe_doc.add_comment("Comment", f"""❌ Reconciliation failed.\n\n Error Log Reference:
			{frappe.utils.get_link_to_form(error_log.doctype, error_log.name)}""")

		# Cancel PE JV
		try:
			jv_pe = frappe.get_doc("Journal Entry", args.pe_jv_name)
			if jv_pe.docstatus == 1:
				jv_pe.cancel()
				jv_pe.add_comment("Comment", "❌ Cancelled due to failed reconciliation.")
		except Exception:
			pass

		# Cancel SI JV
		try:
			jv_si = frappe.get_doc("Journal Entry", args.si_jv_name)
			if jv_si.docstatus == 1:
				jv_si.cancel()
				jv_si.add_comment("Comment", "❌ Cancelled due to linked PE JV reconciliation failure.")
		except Exception:
			pass


def get_branch_account(branch_name):
	if branch_account := frappe.db.get_value("Branch", branch_name, "branch_account"):
		return branch_account
	if branch_account := frappe.db.exists("Account", {"account_name": branch_name}):
		return branch_account


	frappe.throw(f"Branch account for {branch_name} does not exist. Please create it first.")


@frappe.whitelist()
def reconcile_inter_branch_payment(**kwargs):
	kwargs = frappe._dict(kwargs)

	si_name = kwargs.si_name
	receivable_account = kwargs.receivable_account
	amount = kwargs.paid_amount

	pe_branch = kwargs.pe_branch
	si_branch = frappe.db.get_value("Sales Invoice", si_name, "branch")

	if pe_branch == si_branch:
		frappe.throw("Sales Invoice branch and Payment Entry branch are the same.")

	jv_data = frappe._dict({
		"company": kwargs.company,
		"posting_date": kwargs.posting_date,
		"doctype": kwargs.doctype,
		"pe_name": kwargs.pe_name,
		"party_type": kwargs.party_type,
		"party": kwargs.party,
		"receivable_account": receivable_account,
		"si_branch": si_branch,
		"si_name": si_name,
		"pe_branch": pe_branch,
		"amount": amount
	})

	jv = create_inter_branch_journal_entries(jv_data)
	return jv
