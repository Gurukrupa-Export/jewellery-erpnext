# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Batch form Repack: move stock of one batch into a new batch at an entered rate.

The Batch form's Actions > Repack button posts one ``Repack`` Stock Entry that consumes
``qty`` of the batch from a warehouse and produces the same item, in the same warehouse,
into a NEW batch whose valuation is the entered target rate. When the entry leaves a net
balance on the company's Stock Adjustment account, a Journal Entry moves that net into
Stock Received But Not Billed.

Adapted from the source line's Batch Repack tool (3ce93808, d74e7f3e, 93223a69 and
59c6a4f8), with these differences:

* It is gated to ``REPACK_COMPANIES``. The batch's company is resolved on the server
  (``custom_company``, else the company of the batch's reference voucher), because
  ``custom_company`` is blank on most batches and the client value cannot be trusted.
* Every rule is re-checked on the server, and a refused request posts nothing. Disabled
  and customer-owned batches, group or other-company warehouses, quantities above the
  warehouse's batch balance, non-positive quantities or rates, missing Company accounts
  and items that are not batch-tracked are all refused.
* No permission bypass: the user needs Stock Entry and Journal Entry create + submit,
  Batch create + write (Batch is not submittable; the bundle hooks save the new batch
  during submit), the ``Repack`` Stock Entry Type grant, and access to the warehouse
  under the Warehouse permission rules.
* The target batch is inserted only after the Stock Entry exists as a draft, with
  ``reference_doctype``/``reference_name`` pointing at it and ``custom_voucher_detail_no``
  at the produce row. The app's Batch hooks then stamp inventory type and Batch Rate from
  that row, and a site Batch script that opens the reference document finds a real one.
* The Branch accounting dimension, when the site has one, is set on the Stock Entry, its
  rows and both Journal Entry rows. Its field name and whether it is mandatory are read
  from the Accounting Dimension metadata.
* The GL net is summed with the query builder (v16 rejects string SQL aggregates).
* The Journal Entry records its Stock Entry in ``custom_repack_stock_entry``, and
  cancelling that Stock Entry cancels the Journal Entry in the same transaction
  (``cancel_linked_journal_entries``, a Stock Entry ``on_cancel`` hook), so a Repack is
  reversed as one unit. A site without that field refuses the Repack.

The entry never carries manufacturing references, so the Stock Entry submit hooks skip
reservations and the MOP Log for it.
"""

import math

import frappe
from erpnext.stock.doctype.batch.batch import (
	batch_uses_naming_series,
	get_batch_qty,
	get_name_from_hash,
)
from frappe import _
from frappe.model.naming import make_autoname
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt, get_datetime, nowtime, today

from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry_type import (
	is_type_allowed,
)

REPACK_COMPANIES = ("Gurukrupa Export Private Limited",)
# Journal Entry field that holds the Repack Stock Entry; added by the
# add_journal_entry_repack_stock_entry_field patch.
JE_LINK_FIELD = "custom_repack_stock_entry"
REPACK_STOCK_ENTRY_TYPE = "Repack"
CUSTOMER_INVENTORY_TYPES = ("Customer Goods", "Customer Stock")

# The accounting dimension this tool fills, identified by its document type. Its field
# name, label and per-company rules come from the Accounting Dimension records.
BRANCH_DIMENSION_DOCTYPE = "Branch"

# Batch is not submittable, and frappe.has_permission(<non-submittable>, "submit") is
# always False, so Batch is checked for create and write: the Serial and Batch Bundle
# hooks save the new batch while the Stock Entry is submitted.
REQUIRED_PERMISSIONS = (
	("Stock Entry", "create"),
	("Stock Entry", "submit"),
	("Journal Entry", "create"),
	("Journal Entry", "submit"),
	("Batch", "create"),
	("Batch", "write"),
)

COMPANY_ACCOUNT_FIELDS = {
	"stock_adjustment_account": "Stock Adjustment Account",
	"cost_center": "Default Cost Center",
	"stock_received_but_not_billed": "Stock Received But Not Billed",
}


@frappe.whitelist()
def get_batch_repack_context(batch_no):
	"""What the Batch form needs to offer Repack. Read-only.

	Returns ``{"allowed": False}`` unless the batch can be repacked by this user.
	Otherwise returns the company, the item, the company's non-group warehouses holding a
	positive balance of the batch, and the Branch dimension (or None) for the dialog.
	"""
	frappe.has_permission("Batch", "read", doc=batch_no, throw=True)
	batch = _get_batch(batch_no)
	company = _resolve_batch_company(batch)

	if _batch_refusal(batch, company) or _missing_permissions():
		return {"allowed": False}
	if not is_type_allowed(REPACK_STOCK_ENTRY_TYPE):
		return {"allowed": False}

	item = _get_item(batch.item)
	if _item_refusal(item):
		return {"allowed": False}

	dimension = _branch_dimension(company)
	return {
		"allowed": True,
		"company": company,
		"item_code": batch.item,
		"warehouses": _warehouse_balances(batch.name, batch.item, company),
		"branch": _branch_context(batch, dimension),
	}


@frappe.whitelist(methods=["POST"])
def create_repack_stock_entry(
	batch_no: str,
	warehouse: str,
	qty: float,
	target_rate: float,
	posting_datetime: str | None = None,
	branch: str | None = None,
):
	"""Repack ``qty`` of ``batch_no`` in ``warehouse`` into a new batch at ``target_rate``.

	Every check runs before anything is created. Returns
	``{"stock_entry": <name>, "journal_entry": <name or None>}``.
	"""
	qty = flt(qty)
	target_rate = flt(target_rate)

	frappe.has_permission("Batch", "read", doc=batch_no, throw=True)
	batch = _get_batch(batch_no)
	company = _resolve_batch_company(batch)

	refusal = _batch_refusal(batch, company)
	if refusal:
		frappe.throw(refusal)

	missing = _missing_permissions()
	if missing:
		frappe.throw(
			_("You need {0} permission to repack a batch.").format(", ".join(missing)),
			frappe.PermissionError,
		)
	if not is_type_allowed(REPACK_STOCK_ENTRY_TYPE):
		frappe.throw(
			_("You are not allowed to create {0} Stock Entries.").format(
				REPACK_STOCK_ENTRY_TYPE
			),
			frappe.PermissionError,
		)

	item = _get_item(batch.item)
	refusal = _item_refusal(item)
	if refusal:
		frappe.throw(refusal)

	if not _journal_entry_link_ready():
		frappe.throw(
			_(
				"Batch Repack needs the Repack Stock Entry field on Journal Entry, so that "
				"cancelling the entry also cancels its Journal Entry. Run bench migrate on "
				"this site first."
			)
		)

	if not (math.isfinite(qty) and math.isfinite(target_rate)):
		frappe.throw(_("Qty and Target Rate must be finite numbers."))
	if qty <= 0:
		frappe.throw(_("Qty must be greater than zero."))
	if target_rate <= 0:
		frappe.throw(_("Target Rate must be greater than zero."))
	posted_at = _parse_posting_datetime(posting_datetime)

	refusal = _warehouse_refusal(_get_warehouse(warehouse), warehouse, company)
	if refusal:
		frappe.throw(refusal)

	precision = _qty_precision()
	available = flt(_available_qty(batch, warehouse), precision)
	if flt(qty, precision) > available:
		frappe.throw(
			_("Qty {0} is more than the {1} available in batch {2} at {3}.").format(
				qty, available, batch.name, warehouse
			)
		)

	accounts = _company_accounts(company)

	dimension = _branch_dimension(company)
	if dimension:
		branch = branch or _default_branch(batch, dimension)
		if dimension.mandatory and not branch:
			frappe.throw(
				_("{0} is mandatory for company {1}.").format(dimension.label, company)
			)
	else:
		branch = None

	se = _build_repack_stock_entry(
		batch, item, company, warehouse, qty, target_rate, accounts, posted_at
	)
	if branch:
		_apply_dimension([se, *se.items], dimension.fieldname, branch)

	# Draft first: the target batch must reference an existing Stock Entry and row.
	se.insert()
	target_row = _target_row(se)
	target_batch = _make_target_batch(item, se, target_row, company)
	target_row.batch_no = target_batch.name
	se.save()
	se.submit()

	journal_entry = None
	net = flt(
		_net_stock_adjustment(se.name, accounts.stock_adjustment_account),
		_amount_precision(),
	)
	if net:
		journal_entry = _make_repack_journal_entry(se, accounts, net, dimension, branch)

	return {"stock_entry": se.name, "journal_entry": journal_entry}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _get_batch(batch_no):
	return frappe.get_doc("Batch", batch_no)


def _resolve_batch_company(batch):
	"""The batch's own ``custom_company``, else the company of its reference voucher.

	Batches minted through a Serial and Batch Bundle carry no ``custom_company``, so the
	voucher that created the batch is the only reliable source for most of them.
	"""
	company = batch.get("custom_company")
	if company:
		return company

	reference_doctype = batch.get("reference_doctype")
	reference_name = batch.get("reference_name")
	if not (reference_doctype and reference_name):
		return None
	if not frappe.get_meta(reference_doctype).has_field("company"):
		return None
	return frappe.db.get_value(reference_doctype, reference_name, "company")


def _is_customer_owned(batch):
	return bool(batch.get("custom_customer")) or (
		batch.get("custom_inventory_type") in CUSTOMER_INVENTORY_TYPES
	)


def _batch_refusal(batch, company):
	"""Why this batch cannot be repacked here, or None."""
	if not company or company not in REPACK_COMPANIES:
		return _(
			"Repack from the Batch form is only available for batches of {0}."
		).format(", ".join(REPACK_COMPANIES))
	if cint(batch.get("disabled")):
		return _("Batch {0} is disabled.").format(batch.name)
	if _is_customer_owned(batch):
		return _(
			"Batch {0} holds customer-owned stock and cannot be repacked from the Batch form."
		).format(batch.name)
	return None


def _missing_permissions():
	"""The ``Doctype: ptype`` rights the session user lacks for the repack."""
	return [
		f"{doctype}: {ptype}"
		for doctype, ptype in REQUIRED_PERMISSIONS
		if not frappe.has_permission(doctype, ptype)
	]


def _get_item(item_code):
	fields = [
		"name",
		"has_batch_no",
		"create_new_batch",
		"batch_number_series",
		"stock_uom",
		"item_name",
	]
	# The HSN code is an India Compliance field; read it only where the site has it.
	if frappe.get_meta("Item").has_field("gst_hsn_code"):
		fields.append("gst_hsn_code")
	return frappe.db.get_value("Item", item_code, fields, as_dict=True)


def _item_refusal(item):
	if not item:
		return _("The batch's item does not exist.")
	if not cint(item.has_batch_no):
		return _(
			"Item {0} is not batch-tracked, so it cannot be repacked into a batch."
		).format(item.name)
	return None


def _get_warehouse(warehouse):
	"""The warehouse's company and group flag; None when it is missing or not visible.

	Visibility goes through ``frappe.get_list``, so the Warehouse permission query
	conditions and user permissions apply, the same rule the Batch form's list uses.
	"""
	if not warehouse or not isinstance(warehouse, str):
		return None
	if not frappe.get_list(
		"Warehouse", filters={"name": warehouse}, pluck="name", limit=1
	):
		return None
	return frappe.db.get_value(
		"Warehouse", warehouse, ["name", "company", "is_group"], as_dict=True
	)


def _warehouse_refusal(details, warehouse, company):
	if not details:
		return _("Warehouse {0} does not exist or is not accessible.").format(warehouse)
	if details.company != company:
		return _("Warehouse {0} does not belong to company {1}.").format(
			warehouse, company
		)
	if cint(details.is_group):
		return _("Warehouse {0} is a group warehouse.").format(warehouse)
	return None


def _qty_precision():
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def _amount_precision():
	return frappe.get_precision("GL Entry", "debit") or 2


def _parse_posting_datetime(posting_datetime):
	"""The entered posting date and time as a datetime, or None to post at submit time."""
	if not posting_datetime:
		return None
	try:
		return get_datetime(posting_datetime)
	except Exception:
		frappe.throw(
			_("Posting Date and Time {0} is not a valid date and time.").format(
				frappe.bold(posting_datetime)
			)
		)


def _available_qty(batch, warehouse):
	return get_batch_qty(batch_no=batch.name, warehouse=warehouse, item_code=batch.item)


def _warehouse_balances(batch_no, item_code, company):
	"""``[{"warehouse", "qty"}]`` for the company's non-group warehouses with a positive balance."""
	precision = _qty_precision()
	totals = {}
	for row in get_batch_qty(batch_no=batch_no, item_code=item_code) or []:
		warehouse = row.get("warehouse")
		if warehouse:
			totals[warehouse] = totals.get(warehouse, 0) + flt(row.get("qty"))

	candidates = [w for w, qty in totals.items() if flt(qty, precision) > 0]
	if not candidates:
		return []

	# get_list, not get_all: the Warehouse permission rules decide what the user sees.
	allowed = set(
		frappe.get_list(
			"Warehouse",
			filters={"name": ["in", candidates], "company": company, "is_group": 0},
			pluck="name",
		)
	)
	return [
		{"warehouse": warehouse, "qty": flt(totals[warehouse], precision)}
		for warehouse in sorted(allowed)
	]


def _company_accounts(company):
	"""The Company's Stock Adjustment, Cost Center and SRBNB accounts; all are required."""
	accounts = (
		frappe.db.get_value(
			"Company", company, list(COMPANY_ACCOUNT_FIELDS), as_dict=True
		)
		or frappe._dict()
	)
	missing = [
		_(label)
		for fieldname, label in COMPANY_ACCOUNT_FIELDS.items()
		if not accounts.get(fieldname)
	]
	if missing:
		frappe.throw(
			_("Set {0} on company {1} before repacking.").format(
				", ".join(missing), company
			)
		)
	return accounts


# ---------------------------------------------------------------------------
# Branch accounting dimension
# ---------------------------------------------------------------------------


def _branch_dimension(company):
	"""The site's enabled Branch accounting dimension for ``company``, or None.

	``fieldname`` and ``label`` come from the Accounting Dimension. ``mandatory`` is set
	when the company requires it on Profit and Loss or Balance Sheet entries; the repack
	posts both.
	"""
	dimension = frappe.db.get_value(
		"Accounting Dimension",
		{"document_type": BRANCH_DIMENSION_DOCTYPE, "disabled": 0},
		["name", "fieldname", "label"],
		as_dict=True,
	)
	if not dimension or not dimension.fieldname:
		return None

	details = frappe.get_all(
		"Accounting Dimension Detail",
		filters={
			"parent": dimension.name,
			"parenttype": "Accounting Dimension",
			"company": company,
		},
		fields=["mandatory_for_pl", "mandatory_for_bs", "default_dimension"],
	)
	return frappe._dict(
		fieldname=dimension.fieldname,
		label=dimension.label or BRANCH_DIMENSION_DOCTYPE,
		document_type=BRANCH_DIMENSION_DOCTYPE,
		mandatory=any(
			cint(d.mandatory_for_pl) or cint(d.mandatory_for_bs) for d in details
		),
		company_default=next(
			(d.default_dimension for d in details if d.default_dimension), None
		),
	)


def _inward_voucher_value(batch, fieldname):
	"""``fieldname`` on the voucher row that created the batch, else on that voucher."""
	reference_doctype = batch.get("reference_doctype")
	reference_name = batch.get("reference_name")
	if not (reference_doctype and reference_name):
		return None

	meta = frappe.get_meta(reference_doctype)
	row_name = batch.get("custom_voucher_detail_no")
	if row_name:
		for table in meta.get_table_fields():
			if not frappe.get_meta(table.options).has_field(fieldname):
				continue
			value = frappe.db.get_value(
				table.options, {"name": row_name, "parent": reference_name}, fieldname
			)
			if value:
				return value

	if meta.has_field(fieldname):
		return frappe.db.get_value(reference_doctype, reference_name, fieldname)
	return None


def _default_branch(batch, dimension):
	"""The batch's inward voucher row, else the user's default, else the company default."""
	return (
		_inward_voucher_value(batch, dimension.fieldname)
		or frappe.defaults.get_user_default(dimension.document_type)
		or dimension.company_default
	)


def _branch_context(batch, dimension):
	if not dimension:
		return None
	return {
		"fieldname": dimension.fieldname,
		"label": dimension.label,
		"document_type": dimension.document_type,
		"default": _default_branch(batch, dimension),
		"reqd": 1 if dimension.mandatory else 0,
	}


def _apply_dimension(docs, fieldname, value):
	"""Set the dimension on every doc or row whose doctype carries the field."""
	for doc in docs:
		if doc.meta.has_field(fieldname):
			doc.set(fieldname, value)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _build_repack_stock_entry(
	batch, item, company, warehouse, qty, target_rate, accounts, posted_at
):
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK_STOCK_ENTRY_TYPE
	se.purpose = "Repack"
	se.company = company
	se.auto_created = 0
	if posted_at:
		se.set_posting_time = 1
		se.posting_date = posted_at.date()
		se.posting_time = posted_at.strftime("%H:%M:%S")
	else:
		se.set_posting_time = 0
		se.posting_date = today()
		se.posting_time = nowtime()

	common = {
		"item_code": item.name,
		"item_name": item.item_name or item.name,
		"qty": qty,
		"transfer_qty": qty,
		"uom": item.stock_uom,
		"stock_uom": item.stock_uom,
		"conversion_factor": 1,
		"gst_hsn_code": item.get("gst_hsn_code"),
		"use_serial_batch_fields": 1,
		"expense_account": accounts.stock_adjustment_account,
		"cost_center": accounts.cost_center,
	}
	# Consumed: the source batch, valued by the ledger.
	se.append(
		"items",
		{
			**common,
			"s_warehouse": warehouse,
			"batch_no": batch.name,
			"is_finished_item": 0,
		},
	)
	# Produced: the same item into a new batch, at the entered rate.
	se.append(
		"items",
		{
			**common,
			"t_warehouse": warehouse,
			"basic_rate": target_rate,
			"set_basic_rate_manually": 1,
			"is_finished_item": 1,
		},
	)
	return se


def _target_row(se):
	for row in se.items:
		if row.t_warehouse and not row.s_warehouse:
			return row
	frappe.throw(_("The Repack entry has no target row."))


def _make_target_batch(item, se, target_row, company):
	"""Insert the produce row's batch, referencing the draft Stock Entry and that row."""
	batch = frappe.new_doc("Batch")
	batch.item = item.name
	batch.reference_doctype = "Stock Entry"
	batch.reference_name = se.name
	batch.custom_voucher_detail_no = target_row.name
	batch.custom_company = company
	if not cint(item.create_new_batch):
		# ERPNext's Batch.autoname raises "Batch ID is mandatory" when the item does not
		# create batches itself, so name it here with ERPNext's own order: the item's
		# series, then the Stock Settings naming series, then a hash, skipping taken IDs.
		while not batch.batch_id:
			if item.batch_number_series:
				batch.batch_id = make_autoname(item.batch_number_series, doc=batch)
			elif batch_uses_naming_series():
				batch.batch_id = batch.get_name_from_naming_series()
			else:
				batch.batch_id = get_name_from_hash()
			if frappe.db.exists("Batch", batch.batch_id):
				batch.batch_id = None
	batch.insert()
	return batch


def _net_stock_adjustment(se_name, account):
	"""Debit minus credit the Stock Entry posted to ``account``."""
	gle = frappe.qb.DocType("GL Entry")
	rows = (
		frappe.qb.from_(gle)
		.select(Sum(gle.debit).as_("debit"), Sum(gle.credit).as_("credit"))
		.where(
			(gle.voucher_type == "Stock Entry")
			& (gle.voucher_no == se_name)
			& (gle.account == account)
			& (gle.is_cancelled == 0)
		)
	).run(as_dict=True)
	totals = rows[0] if rows else {}
	return flt(totals.get("debit")) - flt(totals.get("credit"))


def _journal_entry_link_ready():
	return bool(frappe.db.has_column("Journal Entry", JE_LINK_FIELD))


def cancel_linked_journal_entries(doc, method=None):
	"""Stock Entry ``on_cancel``: cancel the Journal Entries a Batch Repack posted for it.

	Runs after the Stock Entry's own cancel hooks and before Frappe's back-link check, so
	the submitted Journal Entry that links to the entry is cancelled first and does not
	block the cancel. Only ``Repack`` entries are looked at. The user needs Journal Entry
	cancel permission; without it the whole cancel is refused and nothing changes.
	"""
	if doc.get("stock_entry_type") != REPACK_STOCK_ENTRY_TYPE:
		return
	if not _journal_entry_link_ready():
		return

	for name in frappe.get_all(
		"Journal Entry",
		filters={JE_LINK_FIELD: doc.name, "docstatus": 1},
		pluck="name",
		order_by="creation asc",
	):
		if not frappe.has_permission("Journal Entry", "cancel", doc=name):
			frappe.throw(
				_(
					"Cancelling {0} also cancels its Journal Entry {1}, which needs "
					"Journal Entry cancel permission."
				).format(doc.name, name),
				frappe.PermissionError,
			)
		frappe.get_doc("Journal Entry", name).cancel()


def _make_repack_journal_entry(se, accounts, net, dimension=None, branch=None):
	"""Move the Stock Entry's net Stock Adjustment into Stock Received But Not Billed.

	The Stock Adjustment row takes the opposite side of ``net`` and the SRBNB row takes
	``net``'s side, so the adjustment account ends at zero for this repack.
	"""
	amount = flt(abs(net), _amount_precision())
	if net > 0:
		adjustment_debit, adjustment_credit = 0, amount
	else:
		adjustment_debit, adjustment_credit = amount, 0
	adjustment_account = accounts.stock_adjustment_account
	srbnb_account = accounts.stock_received_but_not_billed

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = se.company
	je.posting_date = se.posting_date
	je.title = adjustment_account
	je.user_remark = _("Stock Adjustment Entry | Repack: {0}").format(se.name)
	je.remark = _("Note: Stock Adjustment Entry | Ref: {0}").format(se.name)
	je.set(JE_LINK_FIELD, se.name)

	je.append(
		"accounts",
		{
			"account": adjustment_account,
			"cost_center": accounts.cost_center,
			"debit_in_account_currency": adjustment_debit,
			"debit": adjustment_debit,
			"credit_in_account_currency": adjustment_credit,
			"credit": adjustment_credit,
			"exchange_rate": 1,
			"against_account": srbnb_account,
		},
	)
	je.append(
		"accounts",
		{
			"account": srbnb_account,
			"cost_center": accounts.cost_center,
			"debit_in_account_currency": adjustment_credit,
			"debit": adjustment_credit,
			"credit_in_account_currency": adjustment_debit,
			"credit": adjustment_debit,
			"exchange_rate": 1,
			"against_account": adjustment_account,
		},
	)
	if dimension and branch:
		_apply_dimension(je.accounts, dimension.fieldname, branch)

	je.insert()
	je.submit()
	return je.name
