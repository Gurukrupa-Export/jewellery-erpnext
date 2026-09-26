# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Return unused customer gold at its BOOKED carrying value -- the SOP's Example D.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
The SOP states it twice, once as a business rule and once inside the worked example:

    "If unused gold is returned, use its booked carrying nominal value; do not fetch a new rate."
    "When giving unused gold back, use the booked carrying nominal rate. Do not use the latest
     market rate."

So this module deliberately does **not** call ``resolve_customer_gold_rate_for_date``. That
resolver is correct for a receipt, where today's quote is the thing being booked, and wrong here,
where the obligation was fixed the day the metal arrived. Returning 2 g booked at Rs.7,164.83/g
clears Rs.14,329.66 whatever gold is worth this morning -- and if the market has moved to
Rs.7,500 the difference belongs to the customer's next order through an explicit revaluation, not
silently to the return.

WHY THE ACCOUNTING NEEDS NO JOURNAL ENTRY
-----------------------------------------
The receipt credits the liability by putting it on the row's ``expense_account`` and letting
``StockController`` post the contra leg (``customer_gold_receipt.apply_valuation_policy``). An
outgoing Stock Entry with the same account posts the same pair in reverse -- **Cr Stock /
Dr Customer Gold Liability** -- which is exactly the SOP's required net effect. Adding a JE on top
would be the duplicate posting S07 Sec 7.1 forbids.

That symmetry is the reason this is a Stock Entry and not a bespoke document.

WHERE THE BOOKED RATE COMES FROM
--------------------------------
From the custody ledger's own ``Receipt`` event -- ``cg_carrying_value_delta / cg_gross_qty_delta``
for the batch being returned. Not from the Stock Entry's ``custom_gold_rate_per_gram``, even though
that field holds the same number, because the ledger row is the record that the liability was
actually created against and is what a reconciliation reads. One source of truth, and it is the one
the accounts were posted from.

WHAT IS GUARDED, AND WHAT IS NOT YET RECORDABLE
-----------------------------------------------
A return is refused unless the metal is genuinely free:

* the customer must still own that quantity in the ledger, and
* the metal must physically still be in the custody warehouse.

The second test is what currently answers "is it allocated, in WIP, or in FG" -- because metal in
any of those states has physically left the custody warehouse. It is an honest guard, not the
intended one: ``Allocation``, ``Release`` and ``Production`` events are still never written, so
``cg_stage`` cannot yet distinguish reserved-but-present metal from free metal. When those events
land, this check tightens rather than changes shape.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
	EVENT_RECEIPT,
	EVENT_RETURN,
	LEDGER_DOCTYPE,
	STAGE_RM,
	_write_event,
	build_event_key,
	is_ledger_schema_ready,
	quantity_basis,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_customer_gold_company_settings,
	get_customer_gold_settings,
	get_customer_gold_valuation_policy,
	is_customer_gold_enabled,
)


def get_booked_rate(company, customer, batch_no):
	"""The rate this batch's metal was booked at, from the Receipt events that created it.

	Returns ``None`` when the batch has no valued Receipt event -- a batch received under Zero
	Value, or one that predates the custody ledger. The caller blocks rather than guessing: a
	return valued at a rate nobody booked is worse than a return that does not happen.

	Averaged across Receipt events rather than taking the latest, because one batch can legitimately
	carry metal from two receipts at different rates and the obligation is their sum, not the more
	recent of them.
	"""
	rows = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={
			"company": company,
			"customer": customer,
			"batch_no": batch_no,
			"cg_event_kind": EVENT_RECEIPT,
		},
		fields=["cg_gross_qty_delta", "cg_carrying_value_delta"],
	)

	qty = flt(sum(flt(r.cg_gross_qty_delta) for r in rows))
	value = flt(
		sum(flt(r.cg_carrying_value_delta) for r in rows if r.cg_carrying_value_delta)
	)

	if qty <= 0 or not value:
		return None

	return value / qty


def get_returnable_qty(company, customer, batch_no, warehouse, item_code):
	"""How much of this batch may be handed back: owned AND physically present.

	The minimum of the two, because either alone is wrong. Ledger position alone would allow
	returning metal already issued to the shop floor; physical stock alone would allow returning
	another customer's metal that happens to sit in the same warehouse.
	"""
	# ``get_batch_qty``, not ``get_stock_balance``: the latter has no batch parameter in v16
	# (``erpnext/stock/utils.py:96-104``) and would return the warehouse total for the item --
	# which on a shared custody warehouse is every customer's metal at once.
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	owned = flt(
		sum(
			flt(r.cg_gross_qty_delta)
			for r in frappe.get_all(
				LEDGER_DOCTYPE,
				filters={
					"company": company,
					"customer": customer,
					"batch_no": batch_no,
				},
				fields=["cg_gross_qty_delta"],
			)
		)
	)

	physical = flt(
		get_batch_qty(batch_no=batch_no, warehouse=warehouse, item_code=item_code)
	)

	return max(min(owned, physical), 0.0)


# POST-only: this SUBMITS a Stock Entry. See ``revalue_customer_gold`` for the reasoning --
# both are state-changing and neither should be reachable by a GET.
@frappe.whitelist(methods=["POST"])
def make_customer_gold_return(
	company, customer, batch_no, qty, warehouse, item_code, posting_date=None
):
	"""Hand unused customer gold back. Returns the submitted Stock Entry name.

	Whitelisted, so every check here is a server-side check: the caller's permission on Stock
	Entry, the ownership of the batch, the eligibility of the quantity and the existence of a
	booked rate. None of it may be assumed from the UI having offered the action.
	"""
	qty = flt(qty)

	if not is_customer_gold_enabled():
		frappe.throw(
			frappe._("The Customer Gold flow is not enabled for this site."),
			title=frappe._("Customer Gold Disabled"),
		)

	if not is_ledger_schema_ready():
		frappe.throw(
			frappe._(
				"This site's Customer Gold Ledger Entry schema is incomplete, so a return "
				"cannot be recorded. Complete the schema migration first."
			),
			title=frappe._("Customer Gold Schema Incomplete"),
		)

	frappe.has_permission("Stock Entry", ptype="create", throw=True)

	if qty <= 0:
		frappe.throw(frappe._("Return quantity must be greater than zero."))

	_validate_batch_owner(batch_no, customer)

	available = get_returnable_qty(company, customer, batch_no, warehouse, item_code)
	if qty > available:
		frappe.throw(
			frappe._(
				"Only {0} of batch {1} is free to return -- the rest is either already "
				"delivered or has left {2} for manufacturing. Requested {3}."
			).format(
				frappe.bold(available),
				frappe.bold(batch_no),
				frappe.bold(warehouse),
				frappe.bold(qty),
			),
			title=frappe._("Insufficient Free Customer Gold"),
		)

	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL
	booked_rate = get_booked_rate(company, customer, batch_no) if nominal else None

	if nominal and booked_rate is None:
		frappe.throw(
			frappe._(
				"Batch {0} has no booked carrying value, so the amount to release from the "
				"Customer Gold Liability cannot be established. Returning it at a current "
				"market rate is not permitted."
			).format(frappe.bold(batch_no)),
			title=frappe._("No Booked Carrying Value"),
		)

	se = _build_return_entry(
		company,
		customer,
		batch_no,
		qty,
		warehouse,
		item_code,
		booked_rate,
		posting_date,
	)
	_record_return_event(se, customer, batch_no, item_code, qty, booked_rate, nominal)
	return se.name


def _validate_batch_owner(batch_no, customer):
	"""Refuse outright to hand one customer's metal to another."""
	owner = frappe.db.get_value("Batch", batch_no, "custom_customer")
	if owner != customer:
		# NEITHER the owner NOR THE BATCH may be named. ``batch_rename`` builds batch ids as
		# ``{customer}-{...}-{item}-{seq}``, so the batch id CONTAINS the owning customer's code
		# -- printing it discloses the owner just as surely as printing the owner. The same trap
		# caught the delivery entitlement guard, and the same assertion caught it again here.
		frappe.throw(
			frappe._(
				"The selected batch is not held for {0}, so it cannot be returned to them."
			).format(frappe.bold(customer)),
			title=frappe._("Customer Gold Entitlement"),
		)


def _build_return_entry(
	company, customer, batch_no, qty, warehouse, item_code, booked_rate, posting_date
):
	"""The outgoing Stock Entry, valued at the booked rate.

	``set_basic_rate_manually`` is what makes the booked rate survive: erpnext's loop at
	``stock_entry.py:1615-1619`` takes ``continue`` for such a row, so the rate is not
	recalculated from the batch's current valuation. Same mechanism the nominal receipt relies on,
	used in the opposite direction.
	"""
	settings = get_customer_gold_settings()
	entry_type = settings.get("customer_gold_return_stock_entry_type")
	if not entry_type:
		frappe.throw(
			frappe._(
				"Set the Customer Gold Return Stock Entry Type in {0} before returning "
				"customer gold."
			).format(frappe.bold(frappe._("Subcontracting Settings"))),
			title=frappe._("Customer Gold Configuration Missing"),
		)

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = entry_type
	se.company = company
	se._customer = customer
	if posting_date:
		se.posting_date = posting_date
		se.set_posting_time = 1

	row = {
		"item_code": item_code,
		"qty": qty,
		"s_warehouse": warehouse,
		"batch_no": batch_no,
		"use_serial_batch_fields": 1,
		"inventory_type": "Customer Goods",
		"customer": customer,
	}

	if booked_rate is not None:
		row["basic_rate"] = booked_rate
		row["set_basic_rate_manually"] = 1
		row["allow_zero_valuation_rate"] = 0
		# THE CONTRA LEG, and the reason no Journal Entry is needed. An outgoing Stock Entry
		# credits stock and debits the row's expense_account -- so naming the liability account
		# here posts Cr Stock / Dr Customer Gold Liability, the SOP's required net effect.
		row["expense_account"] = get_customer_gold_company_settings(
			company
		).liability_account
	else:
		row["allow_zero_valuation_rate"] = 1

	se.append("items", row)
	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return se


def _record_return_event(se, customer, batch_no, item_code, qty, booked_rate, nominal):
	"""One custody event, negative -- the metal has left the customer's holding with us."""
	row = se.items[0]
	_write_event(
		cg_event_key=build_event_key(
			se.company, se.doctype, row.name, None, EVENT_RETURN
		),
		cg_event_kind=EVENT_RETURN,
		cg_stage=STAGE_RM,
		company=se.company,
		customer=customer,
		reference_doctype=se.doctype,
		reference_docname=se.name,
		cg_source_row=row.name,
		item_code=item_code,
		batch_no=batch_no,
		stock_uom=row.stock_uom or row.uom,
		cg_gross_qty_delta=-flt(qty),
		**quantity_basis(item_code, -flt(qty), se.company),
		# Negative: the booked obligation this return discharges. Computed from the BOOKED rate,
		# never from the outgoing SLE -- the SLE reflects the batch's current valuation, which is
		# precisely the number the SOP forbids using here.
		cg_carrying_value_delta=-flt(booked_rate) * flt(qty) if nominal else None,
		cg_currency=frappe.get_cached_value("Company", se.company, "default_currency")
		if nominal
		else None,
	)
