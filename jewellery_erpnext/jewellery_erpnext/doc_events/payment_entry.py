import frappe
import json
import frappe.utils
from pypika import Order


def create_inter_branch_journal_entries(args, is_adv=False):
	"""
	Create two Journal Entries: one for PE branch, one for SI branch.
	Enqueue reconciliation job after commit.
	"""
	pe_branch_account = get_branch_account(args.pe_branch)
	receivable_branch = args.customer_branch if is_adv else args.si_branch
	receivable_branch_acc = get_branch_account(receivable_branch)

	res = []
	args.pe_jv_name = None
	args.si_jv_name = None

	jv_pe_branch = [
		{
			"account": args.receivable_account,
			"debit_in_account_currency": args.allocated_amount,
			"party_type": args.party_type,
			"party": args.party,
			"branch": args.pe_branch
		},
		{
			"account": receivable_branch_acc,
			"credit_in_account_currency": args.allocated_amount,
			"branch": args.pe_branch
		}
	]

	receivabe_account_details = {
		"account": args.receivable_account,
		"credit_in_account_currency": args.allocated_amount,
		"party_type": args.party_type,
		"party": args.party,
		"branch": receivable_branch
	}

	if not is_adv:
		receivabe_account_details.update({
			"reference_type": "Sales Invoice",
			"reference_name": args.si_name,
			"branch": args.si_branch,
		})

	jv_receivable_branch = [
		receivabe_account_details,
		{
			"account": pe_branch_account,
			"debit_in_account_currency": args.allocated_amount,
			"branch": receivable_branch
		}
	]

	jv_accounts_details = {
		args.pe_branch: jv_pe_branch,
		receivable_branch: jv_receivable_branch
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
def reconcile_inter_branch_payment(data, is_adv=False):
	if isinstance(data, str):
		data = json.loads(data)

	if isinstance(is_adv, str):
		is_adv = json.loads(is_adv)

	jv_list = []

	for row in data:
		jv_data = frappe._dict(row)
		validate_allocated_amount(jv_data, is_adv)
		# if is_adv:
		# 	jv_data.customer_branch = get_customer_branch(jv_data.party)

		validate_inter_branch(jv_data, is_adv)

		jv = create_inter_branch_journal_entries(jv_data, is_adv)

		jv_list.extend(jv)

	return jv_list


@frappe.whitelist()
def get_unreconciled_sales_invoices(company, customer):
	"""
	Get unreconciled Sales Invoices for given company and customer.
	"""
	SI = frappe.qb.DocType("Sales Invoice")

	query = (
		frappe.qb.from_(SI)
		.select(SI.name, SI.posting_date, SI.outstanding_amount, SI.grand_total, SI.branch)
		.where(
			(SI.docstatus == 1) &
			(SI.company == company) &
			(SI.customer == customer) &
			(SI.outstanding_amount > 0)
		)
		.orderby(SI.posting_date, order=Order.desc)
	)

	data = query.run(as_dict=True)

	return data

def validate_allocated_amount(jv_data, is_adv):
	"""
	Validate that allocated amount.
	"""
	msg_value = jv_data.si_name if not is_adv else jv_data.party
	if not jv_data.allocated_amount:
		frappe.throw(f"Allocated amount must be greater than zero for <b>{msg_value}<b>")

	if is_adv: # for advance amount don't want outstanding amount validation
		return

	if jv_data.allocated_amount > jv_data.outstanding_amount:
		frappe.throw(f"Allocated amount {jv_data.allocated_amount} cannot be greater than outstanding amount {jv_data.outstanding_amount}.")


# get customer branch from user
# def get_customer_branch(customer_name):
# 	return frappe.db.get_value("Branch", {"custom_customer": customer_name}, "name")

def validate_inter_branch(jv_data, is_adv=False):
	receivable_branch = ""

	if is_adv:
		receivable_branch = jv_data.customer_branch
	else:
		receivable_branch = jv_data.si_branch

	if jv_data.pe_branch == receivable_branch:
		receivable_branch_label = "Sales Invoice" if not is_adv else "Customer"
		frappe.throw(f"{receivable_branch_label} branch and Payment Entry branch are same.")