import frappe
import json
from frappe.utils import get_link_to_form, nowdate, flt
from pypika import Order


def create_inter_branch_journal_entries(args, reconcile_type):
	"""
	Create two Journal Entries: one for PE branch, one for SI branch.
	Enqueue reconciliation job after commit.
	"""
	pe_branch_account = get_branch_account(args.pe_branch)
	args.pe_branch_account = pe_branch_account
	receivable_branch = args.branch

	if reconcile_type == "Supplier Payment":
		receivable_branch = args.supplier_branch
	elif reconcile_type == "Customer Advance":
		receivable_branch = args.customer_branch

	receivable_branch_acc = get_branch_account(receivable_branch)

	args.receivable_branch = receivable_branch
	args.receivable_branch_acc = receivable_branch_acc

	res = []
	args.pe_jv_name = None
	args.si_jv_name = None

	jv_accounts_details = construct_jv_args(args, reconcile_type)

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

	if reconcile_type == "Supplier Payment":
		frappe.msgprint(f"Supplier Payment Inter Branch JVs created: {', '.join(res)}")
		return res

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

def construct_jv_args(args, reconcile_type):
	jv_pe_branch = []
	jv_receivable_branch = []

	if reconcile_type == "Supplier Payment":
		jv_pe_branch.extend([
			{
				"account": args.receivable_branch_acc,
				"debit_in_account_currency": args.allocated_amount,
				"branch": args.pe_branch,
			},
			{
				"account": args.paid_to,
				"credit_in_account_currency": args.allocated_amount,
				"branch": args.pe_branch,
				"party_type": args.party_type,
				"party": args.party
			}
		])

		jv_receivable_branch.extend([
			{
				"account": args.paid_to,
				"debit_in_account_currency": args.allocated_amount,
				"branch": args.receivable_branch,
				"party_type": args.party_type,
				"party": args.party
			},
			{
				"account": args.pe_branch_account,
				"credit_in_account_currency": args.allocated_amount,
				"branch": args.receivable_branch,
			}

		])

	elif reconcile_type == "Customer Advance":
		jv_pe_branch = [
			{
				"account": args.paid_from,
				"debit_in_account_currency": args.allocated_amount,
				"party_type": args.party_type,
				"party": args.party,
				"branch": args.pe_branch
			},
			{
				"account": args.receivable_branch_acc,
				"credit_in_account_currency": args.allocated_amount,
				"branch": args.pe_branch
			}
		]

		jv_receivable_branch = [
			{
				"account": args.paid_from,
				"credit_in_account_currency": args.allocated_amount,
				"party_type": args.party_type,
				"party": args.party,
				"branch": args.receivable_branch
			},
			{
				"account": args.pe_branch_account,
				"debit_in_account_currency": args.allocated_amount,
				"branch": args.receivable_branch
			}
		]

	elif reconcile_type == "Against Invoices" and args.party_type == "Customer":
		jv_pe_branch = [
			{
				"account": args.paid_from,
				"debit_in_account_currency": args.allocated_amount,
				"party_type": args.party_type,
				"party": args.party,
				"branch": args.pe_branch
			},
			{
				"account": args.receivable_branch_acc,
				"credit_in_account_currency": args.allocated_amount,
				"branch": args.pe_branch
			}
		]

		receivabe_account_details = {
			"account": args.paid_from,
			"credit_in_account_currency": args.allocated_amount,
			"party_type": args.party_type,
			"party": args.party,
			"reference_type": "Sales Invoice",
			"reference_name": args.invoice_name,
			"branch": args.branch,

		}

		jv_receivable_branch = [
			{
				"account": args.pe_branch_account,
				"debit_in_account_currency": args.allocated_amount,
				"branch": args.receivable_branch
			},
			receivabe_account_details
		]

	else:
		jv_pe_branch = [
			{
				"account": args.receivable_branch_acc,
				"debit_in_account_currency": args.allocated_amount,
				"branch": args.pe_branch
			},
			{
				"account": args.paid_to,
				"party_type": args.party_type,
				"party": args.party,
				"credit_in_account_currency": args.allocated_amount,
				"branch": args.pe_branch
			}
		]

		receivable_account_details = {
			"account": args.pe_branch_account,
			"credit_in_account_currency": args.allocated_amount,
			"branch": args.branch,

		}

		jv_receivable_branch = [
			{
				"account": args.paid_to,
				"party_type": args.party_type,
				"party": args.party,
				"reference_type": "Purchase Invoice",
				"reference_name": args.invoice_name,
				"debit_in_account_currency": args.allocated_amount,
				"branch": args.receivable_branch
			},
			receivable_account_details
		]

	return {
		args.pe_branch: jv_pe_branch,
		args.receivable_branch: jv_receivable_branch
	}


def reconcile_pe_with_inter_branch_jv(args=None, reconcile_type=None):
	args = frappe._dict(args or {})
	pe_doc = frappe.get_doc("Payment Entry", args.pe_name)
	try:
		pe_reconc_doc = frappe.get_single("Payment Reconciliation")
		pe_reconc_doc.update({
			"company": args.company,
			"party_type": args.party_type,
			"party": args.party,
			"receivable_payable_account": args.paid_from if args.part_type == "Customer" else args.paid_to,
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
			{get_link_to_form(error_log.doctype, error_log.name)}""")

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
	# if branch_account := frappe.db.get_value("Branch", branch_name, "branch_account"):
	# 	return branch_account
	if branch_account := frappe.db.exists("Account", {"account_name": branch_name}):
		return branch_account

	frappe.throw(f"Branch account for {branch_name} does not exist. Please create it first.")


@frappe.whitelist()
def reconcile_inter_branch_payment(data, reconcile_type):
	if isinstance(data, str):
		data = json.loads(data)

	jv_list = []

	for row in data:
		jv_data = frappe._dict(row)

		validate_allocated_amount(jv_data, reconcile_type)
		validate_inter_branch(jv_data, reconcile_type)

		jv = create_inter_branch_journal_entries(jv_data, reconcile_type)

		jv_list.extend(jv)

	return jv_list


@frappe.whitelist()
def get_unreconciled_invoices(company, party_type, party):
	"""
	Get unreconciled Sales Invoices for given company and customer.
	"""
	doctype = {
		"Customer": "Sales Invoice",
		"Supplier": "Purchase Invoice"
	}.get(party_type)
	print("party type", party_type)
	print("doctypeeee ----", doctype)
	d = frappe.qb.DocType(doctype)

	query = (
		frappe.qb.from_(d)
		.select(d.name, d.posting_date, d.outstanding_amount, d.grand_total, d.branch)
		.where(
			(d.docstatus == 1) &
			(d.company == company) &
			(d.outstanding_amount > 0)
		)
		.orderby(d.posting_date, order=Order.desc)
	)

	if party_type == "Customer":
		query.where(d.customer == party)
	else:
		query.where(d.supplier == party)

	data = query.run(debug=True,as_dict=True)

	return data

def validate_allocated_amount(jv_data, reconcile_type):
	"""
	Validate that allocated amount.
	"""
	msg_value = jv_data.invoice_name if reconcile_type not in ["Customer Advance", "Supplier Payment"] else jv_data.party
	if not jv_data.allocated_amount:
		frappe.throw(f"Allocated amount must be greater than zero for <b>{msg_value}<b>")

	if reconcile_type in ["Customer Advance", "Supplier Payment"]: # for advance amount don't want outstanding amount validation
		return

	if jv_data.allocated_amount > jv_data.outstanding_amount:
		frappe.throw(f"Allocated amount {jv_data.allocated_amount} cannot be greater than outstanding amount {jv_data.outstanding_amount}.")


# get customer branch from user
# def get_customer_branch(customer_name):
# 	return frappe.db.get_value("Branch", {"custom_customer": customer_name}, "name")

def validate_inter_branch(jv_data, reconcile_type):
	receivable_branch = ""

	if reconcile_type == "Customer Advance":
		receivable_branch = jv_data.customer_branch
	elif reconcile_type == "Supplier Payment":
		receivable_branch = jv_data.supplier_branch
	else:
		receivable_branch = jv_data.branch

	if jv_data.pe_branch == receivable_branch:
		receivable_branch_label = "Invoice" if reconcile_type == "Against Invoices" else reconcile_type.split()[0]
		frappe.throw(f"{receivable_branch_label} branch and Payment Entry branch are same.")


@frappe.whitelist()
def create_inter_branch_contra_entry(**kwargs):
	"""
	Create inter-branch Internal Transfer Payment Entries
	"""
	args = frappe._dict(kwargs)

	# --- Validations ---
	if not args.source_branch or not args.target_branch:
		frappe.throw("Both Source Branch and Target Branch are required.")

	if args.source_branch == args.target_branch:
		frappe.throw("Source and Target Branch cannot be the same.")

	if not args.source_bank or not args.target_bank:
		frappe.throw("Both Source Bank and Target Bank accounts are required.")

	if not args.amount or float(args.amount) <= 0:
		frappe.throw("Amount must be greater than zero.")

	# Validate bank accounts
	for acc in (args.source_bank, args.target_bank):
		atype = frappe.db.get_value("Account", acc, "account_type")
		if atype != "Bank":
			frappe.throw(f"Account {acc} is not a Bank type account.")

	# --- Get division accounts ---
	source_division_acc = get_branch_account(args.source_branch)
	target_division_acc = get_branch_account(args.target_branch)

	res = []

	company = args.company or frappe.defaults.get_user_default("Company")
	posting_date = args.posting_date or nowdate()

	def _create_payment_entry(args, from_acc, to_acc, branch):
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Internal Transfer"
		pe.company = company
		pe.posting_date = posting_date
		pe.branch = branch

		pe.paid_from = from_acc
		pe.paid_to = to_acc
		pe.paid_amount = flt(args.amount)
		pe.received_amount = flt(args.amount)
		pe.reference_no = args.reference_no
		pe.reference_date = args.reference_date

		if args.remarks:
			pe.custom_remarks = 1
			pe.remarks = args.remarks

		pe.insert()
		pe.submit()

		return pe.name

	pe1 = _create_payment_entry(args, args.source_bank, target_division_acc, args.source_branch)
	pe2 = _create_payment_entry(args, source_division_acc, args.target_bank, args.target_branch)

	res.extend([pe1, pe2])

	frappe.msgprint(
		f"""
		✅ Created Inter-Branch Contra Entries:
		<b>{', <br>'.join(res)}</b><br>
		"""
	)

	return res