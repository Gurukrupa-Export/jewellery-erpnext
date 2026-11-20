"""KGGK job work for Gurukrupa.

A Manufacturing Work Order issued to a subcontractor that is one of our own companies (its
Supplier Code is set on a Company) goes out on an Employee IR Issue, which raises the job-work
Purchase Order (``Purchase Order.employee_ir``). KGGK quotes and plans against that PO, issues
the material back with a Customer Goods Issue, and Gurukrupa receives it on an Employee IR
Receive once the Purchase Invoice is in. The helpers here are the job-work-only checks those
documents call; every other flow passes through them untouched.
"""

import frappe
from frappe import _
from frappe.utils import flt, get_link_to_form

# KGGK bills the job-work charge on this item (Subcontracting Income Sales Order rows).
SUBCONTRACTING_INCOME_ITEM = "Subcontracting Income"


@frappe.whitelist()
def is_job_work_order(mwo):
	"""True when the work order is KGGK job work for Gurukrupa: its PMO's Sales Order row was
	raised from a Purchase Order that an Employee IR Issue created (``employee_ir`` set)."""
	pmo = frappe.db.get_value("Manufacturing Work Order", mwo, "manufacturing_order")
	if not pmo:
		return False
	so_item = frappe.db.get_value("Parent Manufacturing Order", pmo, "sales_order_item")
	if not so_item:
		return False
	po_no = frappe.db.get_value("Sales Order Item", so_item, "po_no")
	return bool(po_no and frappe.db.get_value("Purchase Order", po_no, "employee_ir"))


def _job_work_mwos(doc):
	"""Job-work Manufacturing Work Orders a Customer Goods Issue returns material for."""
	if doc.get("stock_entry_type") != "Customer Goods Issue":
		return []
	mwos = {
		row.custom_manufacturing_work_order
		for row in doc.get("items") or []
		if row.get("custom_manufacturing_work_order")
	}
	return [mwo for mwo in mwos if is_job_work_order(mwo)]


def complete_job_work_mwo(doc, cancel=False):
	"""Job work (KGGK): issuing the material back to Gurukrupa finishes the work order.

	Only for work orders that trace back to an Employee IR Purchase Order, so the other
	Customer Goods Issue flows (sample goods, customer returns) never touch MWO status.
	Cancelling the issue re-opens a work order this completed.
	"""
	for mwo in _job_work_mwos(doc):
		status = frappe.db.get_value("Manufacturing Work Order", mwo, "status")
		if not cancel:
			frappe.db.set_value("Manufacturing Work Order", mwo, "status", "Completed")
		elif status == "Completed":
			frappe.db.set_value("Manufacturing Work Order", mwo, "status", "In Process")


def validate_job_work_purchase_invoice(doc):
	"""Job work (KGGK): a Receive needs the Purchase Invoice for its Issue's PO first.

	Only for a subcontractor that is one of our own companies (its Supplier Code is set on
	a Company) -- outside karigars and subcontractors receive exactly as before. Checked at
	submit, so a Receive can still be drafted while the invoice is pending, and every work
	order on the Receive is checked against the Issue that sent it out.
	"""
	if doc.get("type") != "Receive" or doc.get("subcontracting") != "Yes":
		return
	if not frappe.db.exists("Company", {"supplier_code": doc.subcontractor}):
		return

	mwos = {
		row.manufacturing_work_order
		for row in doc.employee_ir_operations
		if row.manufacturing_work_order
	}
	missing = []
	for mwo in sorted(mwos):
		issue_ir = frappe.db.sql(
			"""
			SELECT eir.name
			FROM `tabEmployee IR` eir
			JOIN `tabEmployee IR Operation` op ON op.parent = eir.name
			WHERE op.manufacturing_work_order = %s
				AND eir.type = 'Issue'
				AND eir.subcontracting = 'Yes'
				AND eir.subcontractor = %s
				AND eir.department = %s
				AND eir.docstatus = 1
			ORDER BY eir.creation DESC
			LIMIT 1
			""",
			(mwo, doc.subcontractor, doc.department),
		)
		if not issue_ir:
			continue
		issue_name = issue_ir[0][0]
		po = frappe.db.get_value(
			"Purchase Order", {"employee_ir": issue_name, "docstatus": ["<", 2]}, "name"
		)
		if not po:
			missing.append(
				_("{0}: no Purchase Order found for Issue {1}").format(
					mwo, get_link_to_form("Employee IR", issue_name)
				)
			)
			continue
		has_invoice = frappe.db.sql(
			"""
			SELECT 1 FROM `tabPurchase Invoice Item`
			WHERE purchase_order = %s AND docstatus = 1
			LIMIT 1
			""",
			(po,),
		)
		if not has_invoice:
			missing.append(
				_("{0}: submit a Purchase Invoice for {1}").format(
					mwo, get_link_to_form("Purchase Order", po)
				)
			)
	if missing:
		frappe.throw(
			"<br>".join(missing),
			title=_("Purchase Invoice required before Receive"),
		)


# Marks the additional-cost rows this module owns on a Manufacture Stock Entry, so they
# can be rebuilt on every save without touching rows a user added by hand.
JOB_WORK_COST_PREFIX = "Job Work:"


def add_job_work_additional_costs(doc):
	"""Job work (KGGK): carry the KGGK charge into the finished goods' valuation.

	Gurukrupa's FG Manufacture entry (Serial Number Creator) is made after the Employee IR
	Receive, and a job-work Receive cannot submit before its Purchase Invoice
	(``validate_job_work_purchase_invoice``), so every invoice for the PMO already exists
	when this runs. Each submitted invoice row for a Purchase Order raised by Employee IR
	against this PMO becomes one Additional Cost row: its net amount (GST is input credit,
	not cost) on the invoice row's own expense account. ERPNext credits that account and
	debits stock, so the charge leaves Subcontracting Expense and becomes FG value instead of
	being counted twice. The description records the Purchase Invoice, the KGGK Sales
	Invoice, the PMO and the MWO.
	"""
	if doc.get("docstatus") != 0 or doc.get("stock_entry_type") != "Manufacture":
		return
	pmo = doc.get("manufacturing_order")
	if not pmo:
		return

	doc.set(
		"additional_costs",
		[
			row
			for row in doc.get("additional_costs") or []
			if not (
				row.get("custom_purchase_invoice")
				or (row.description or "").startswith(JOB_WORK_COST_PREFIX)
			)
		],
	)

	invoice_rows = frappe.db.sql(
		"""
		SELECT
			pii.parent AS purchase_invoice,
			pii.po_detail,
			pii.base_net_amount AS amount,
			pii.expense_account,
			pii.cost_center,
			poi.manufacturing_operation
		FROM `tabPurchase Invoice Item` pii
		JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
		JOIN `tabPurchase Order Item` poi ON poi.name = pii.po_detail
		JOIN `tabPurchase Order` po ON po.name = poi.parent
		WHERE poi.custom_pmo = %(pmo)s
			AND IFNULL(po.employee_ir, '') != ''
			AND pi.docstatus = 1
			AND pi.is_return = 0
			AND pi.company = %(company)s
		ORDER BY pi.posting_date, pii.idx
		""",
		{"pmo": pmo, "company": doc.company},
		as_dict=True,
	)

	for row in invoice_rows:
		if not row.amount:
			continue
		mwo = (
			frappe.db.get_value(
				"Manufacturing Operation",
				row.manufacturing_operation,
				"manufacturing_work_order",
			)
			if row.manufacturing_operation
			else None
		)
		sales_invoice = _kggk_sales_invoice(row.po_detail)
		doc.append(
			"additional_costs",
			{
				"expense_account": row.expense_account,
				"cost_center": row.cost_center,
				"amount": row.amount,
				"base_amount": row.amount,
				"exchange_rate": 1,
				"custom_purchase_invoice": row.purchase_invoice,
				"custom_sales_invoice": sales_invoice,
				"custom_parent_manufacturing_order": pmo,
				"custom_manufacturing_work_order": mwo,
				"description": "{0} Purchase Invoice {1} | Sales Invoice {2} | PMO {3} | MWO {4}".format(
					JOB_WORK_COST_PREFIX,
					row.purchase_invoice,
					sales_invoice or "-",
					pmo,
					mwo or "-",
				),
			},
		)


def _kggk_sales_invoice(po_detail):
	"""The submitted KGGK Sales Invoice that billed this job-work Purchase Order row."""
	if not po_detail:
		return None
	result = frappe.db.sql(
		"""
		SELECT si.name
		FROM `tabSales Invoice Item` sii
		JOIN `tabSales Invoice` si ON si.name = sii.parent
		JOIN `tabSales Order Item` soi ON soi.name = sii.so_detail
		WHERE soi.custom_po_details = %s
			AND si.docstatus = 1
			AND si.is_return = 0
		ORDER BY si.posting_date DESC, si.creation DESC
		LIMIT 1
		""",
		(po_detail,),
	)
	return result[0][0] if result else None


# ============================================================================
# GURUKRUPA: GST ON THE JOB-WORK PURCHASE ORDER (spec step 13)
# ============================================================================


def apply_job_work_gst(po):
	"""Put GST on the KGGK job-work Purchase Order, so the Purchase Invoice made from it
	carries the tax the spec asks for instead of being entered by hand.

	The template is the one India Compliance would pick on the form for this supplier and
	place of supply (in-state / out-state, reverse charge), not a hard-coded name. India
	Compliance does not skip disabled templates, so a disabled pick is swapped for the
	enabled template of the same Tax Category. When no template applies (no GSTIN, same
	GSTIN, composition supplier, nothing enabled) the PO stays untaxed -- and taxing never
	blocks the Employee IR Issue: if the taxed PO cannot be saved, the PO is left as it was
	and the user is told to add the tax by hand.
	"""
	from erpnext.controllers.accounts_controller import get_taxes_and_charges
	from frappe.utils.messages import clear_last_message
	from india_compliance.gst_india.overrides.transaction import get_gst_details

	master_doctype = "Purchase Taxes and Charges Template"
	details = get_gst_details(
		po.as_dict(), po.doctype, po.company, update_place_of_supply=True
	)
	template = (details or {}).get("taxes_and_charges")
	if not template:
		return
	taxes = details.get("taxes") or []
	if frappe.db.get_value(master_doctype, template, "disabled"):
		tax_category = frappe.db.get_value(master_doctype, template, "tax_category")
		template = tax_category and frappe.db.get_value(
			master_doctype,
			{"company": po.company, "tax_category": tax_category, "disabled": 0},
			"name",
		)
		if not template:
			return
		taxes = get_taxes_and_charges(master_doctype, template) or []

	for field in ("place_of_supply", "is_reverse_charge"):
		if details.get(field) is not None:
			po.set(field, details.get(field))
	po.taxes_and_charges = template
	po.set("taxes", [])
	for tax in taxes:
		po.append("taxes", tax)
	frappe.db.savepoint("job_work_gst")
	try:
		po.save()
	except frappe.ValidationError:
		frappe.db.rollback(save_point="job_work_gst")
		clear_last_message()
		po.reload()
		frappe.msgprint(
			_(
				"GST could not be applied to Purchase Order {0} ({1}). Add the tax on it by hand."
			).format(get_link_to_form("Purchase Order", po.name), template),
			indicator="orange",
		)


# ============================================================================
# KGGK: SALES INVOICE PER PMO (spec step 14)
# ============================================================================


def _job_work_so_items(so_detail_names):
	"""``{Sales Order Item: row}`` for the Subcontracting Income rows among ``so_detail_names``
	that bill KGGK job work: rows carrying their KGGK PMO and the Gurukrupa PO row they were
	quoted from, where that PO was raised by an Employee IR. ``job_work_mwo`` is Gurukrupa's
	Manufacturing Work Order on that PO row."""
	names = [name for name in so_detail_names if name]
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT
			soi.name,
			soi.custom_parent_manufacturing_order AS pmo,
			mop.manufacturing_work_order AS job_work_mwo
		FROM `tabSales Order Item` soi
		JOIN `tabPurchase Order Item` poi ON poi.name = soi.custom_po_details
		JOIN `tabPurchase Order` po ON po.name = poi.parent
		LEFT JOIN `tabManufacturing Operation` mop ON mop.name = poi.manufacturing_operation
		WHERE soi.name IN %(names)s
			AND soi.item_code = %(item)s
			AND IFNULL(po.employee_ir, '') != ''
		""",
		{"names": tuple(names), "item": SUBCONTRACTING_INCOME_ITEM},
		as_dict=True,
	)
	return {row.name: row for row in rows}


def set_job_work_sales_invoice_links(doc, method=None):
	"""KGGK Sales Invoice: stamp each job-work row with its PMO and Gurukrupa's MWO, and keep
	the invoice to ONE PMO (spec step 14: invoices are made PMO-wise).

	The Sales Order link is the row's own ``sales_order`` / ``so_detail``. Rows that do not
	bill job work are untouched, and an invoice without job-work rows is not checked.
	"""
	if doc.get("is_return"):
		return
	job_work = _job_work_so_items([row.get("so_detail") for row in doc.items])
	if not job_work:
		return

	pmos = set()
	for row in doc.items:
		info = job_work.get(row.get("so_detail"))
		if not info:
			frappe.throw(
				_(
					"Row #{0}: a job-work Sales Invoice can only bill Subcontracting Income rows "
					"of its Sales Order. Make a separate invoice for {1}."
				).format(row.idx, row.item_code)
			)
		row.custom_parent_manufacturing_order = info.pmo
		row.custom_job_work_mwo = info.job_work_mwo
		pmos.add(info.pmo)

	if len(pmos) > 1:
		frappe.throw(
			_(
				"A job-work Sales Invoice bills one Parent Manufacturing Order. This invoice has "
				"{0}: {1}. Use Create > Job Work Sales Invoices on the Sales Order to make one "
				"invoice per PMO."
			).format(len(pmos), ", ".join(sorted(p or "-" for p in pmos)))
		)


@frappe.whitelist()
def has_job_work_rows(sales_order):
	"""True when a submitted Sales Order still has job-work rows to invoice (drives the
	Sales Order button)."""
	rows = frappe.get_all(
		"Sales Order Item",
		filters={"parent": sales_order, "item_code": SUBCONTRACTING_INCOME_ITEM},
		fields=["name", "amount", "billed_amt"],
	)
	pending = [row.name for row in rows if flt(row.billed_amt) < flt(row.amount)]
	return bool(_job_work_so_items(pending))


@frappe.whitelist()
def make_job_work_sales_invoices(sales_order):
	"""Draft one Sales Invoice per PMO for a KGGK Subcontracting Income Sales Order.

	Uses ERPNext's own Sales Order -> Sales Invoice mapping (so billed quantities, taxes and
	accounting defaults behave exactly as with Create > Sales Invoice), then keeps one PMO's
	rows per invoice. A PMO that already has a draft invoice is not drafted again; the
	existing draft is returned instead. Returns the invoice names.
	"""
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	so = frappe.get_doc("Sales Order", sales_order)
	so.check_permission("read")
	frappe.has_permission("Sales Invoice", "create", throw=True)

	template = make_sales_invoice(sales_order)
	job_work = _job_work_so_items([row.so_detail for row in template.items])
	by_pmo = {}
	for row in template.items:
		info = job_work.get(row.so_detail)
		if info and info.pmo:
			by_pmo.setdefault(info.pmo, []).append(row.so_detail)
	if not by_pmo:
		frappe.throw(
			_("Sales Order {0} has no job-work rows left to invoice.").format(
				sales_order
			)
		)

	invoices = []
	for pmo in sorted(by_pmo):
		existing = frappe.db.sql(
			"""
			SELECT DISTINCT sii.parent
			FROM `tabSales Invoice Item` sii
			JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE si.docstatus = 0 AND sii.sales_order = %s AND sii.so_detail IN %s
			LIMIT 1
			""",
			(sales_order, tuple(by_pmo[pmo])),
		)
		if existing:
			invoices.append(existing[0][0])
			continue
		si = make_sales_invoice(sales_order)
		si.set("items", [row for row in si.items if row.so_detail in by_pmo[pmo]])
		# The order's own branch and addresses, set explicitly ("" for no branch): left empty,
		# insert fills the user's default Branch, which on this shared site can be a Gurukrupa
		# branch whose Gurukrupa address then fails as KGGK's company address.
		si.branch = so.get("branch") or ""
		for field in ("company_address", "customer_address", "shipping_address_name"):
			if so.get(field):
				si.set(field, so.get(field))
		si.run_method("set_missing_values")
		si.run_method("calculate_taxes_and_totals")
		si.insert()
		invoices.append(si.name)
	return invoices
