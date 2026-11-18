# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Connections-tab links to ``Customer Gold Allocation``: the receipt-by-receipt breakup.

The standard Connections tab, not a custom panel. Each hook adds a "Customer Gold" group whose
count opens the allocation list filtered to this document; the list's Report view groups and
totals it. It is what Customize Form > Links would give, kept in code so every site gets it.

* Stock Entry: ``receipt_voucher`` -- what a receipt's gold went out as, and when.
* Journal Entry: ``settlement_voucher`` -- the receipts a settlement JE released.
* Delivery Note / Sales Invoice: ``reference_docname`` -- the receipts a delivery drew on.
"""

import frappe
from frappe import _

ALLOCATION_DOCTYPE = "Customer Gold Allocation"


def _with_allocations(data, fieldname):
	data = frappe._dict(data or {})
	non_standard = dict(data.get("non_standard_fieldnames") or {})
	non_standard[ALLOCATION_DOCTYPE] = fieldname
	data.non_standard_fieldnames = non_standard
	transactions = list(data.get("transactions") or [])
	if not any(
		ALLOCATION_DOCTYPE in (group.get("items") or []) for group in transactions
	):
		transactions.append(
			{"label": _("Customer Gold"), "items": [ALLOCATION_DOCTYPE]}
		)
	data.transactions = transactions
	return data


def stock_entry(data):
	return _with_allocations(data, "receipt_voucher")


def journal_entry(data):
	return _with_allocations(data, "settlement_voucher")


def delivery_note(data):
	return _with_allocations(data, "reference_docname")


def sales_invoice(data):
	return _with_allocations(data, "reference_docname")
