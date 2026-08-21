# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The monetary ledger for customer-owned gold.

One row per item row of a submitted Customer Gold receipt, recording who the metal belongs
to, what it is, how much of it there is, what it was valued at, and which document put it
there. Day 6's balance calculation reads this; so, later, do allocation and delivery
settlement.

WHY THIS IS NOT AN EXTENSION OF ``Subcontracting Log``
------------------------------------------------------
``Subcontracting Log`` is a WEIGHT ledger and stays one. Three properties make it unfit to
hold money, and none of them is cosmetic:

1. It has no monetary dimension at all -- not one Currency field across ~40 fields -- and
   90,712 existing rows would carry NULL for any column added now, permanently
   unreconcilable against the GL.
2. Its Float columns are ``NOT NULL DEFAULT 0``, so "not computed" and "computed as zero"
   are the same value. That ambiguity is precisely how 22,140 g of customer metal sat at
   ``pure_qty = 0`` unnoticed.

   CORRECTION, measured after this doctype was created: a new doctype does NOT escape that.
   Frappe renders every ``Float`` as ``decimal(21,3) NOT NULL DEFAULT 0.000``, so
   ``pure_qty`` here is NOT NULL too and NULL is simply unavailable. An earlier draft of this
   docstring claimed otherwise and was wrong. The ambiguity is instead removed at the source:
   ``customer_gold_receipt._validate_rows`` REFUSES to validate a receipt whose row would land
   at ``custom_pure_qty <= 0``, so no row can reach this ledger with an uncomputed zero. A
   guarantee enforced on the way in beats a nullable column nobody checks.
3. Its writer inserts through ``frappe.db.bulk_insert``, which SILENTLY DROPS any key not
   already present as a table column (``subcontracting_log.py:249-251``). A liability figure
   that must tie to the General Ledger cannot be written through a path that can discard it
   without raising.

There is also a trigger conflict that extending it would inherit: ``create_subcontracting_log``
keys off a HARDCODED ``ENTRY_TYPE`` dict, while this flow resolves its Stock Entry Type from
``Subcontracting Settings``. On every site today the configured type IS
``"Customer Goods Received"``, so a second writer on ``on_submit`` would double-write, and
nothing at the database level would stop it -- there is no unique key on
``(reference_docname, batch)``.

Writing through ``doc.insert()`` rather than a bulk path is a deliberate trade: a receipt has
a handful of rows, not thousands, and the lifecycle guarantees are worth more here than the
throughput.
"""

import frappe
from frappe.model.document import Document


class CustomerGoldLedger(Document):
	pass


def create_customer_gold_ledger(doc, method=None):
	"""Write one ledger row per item row. Registered on Stock Entry ``on_submit``."""
	from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
		_receipt_settings,
	)

	settings = _receipt_settings(doc)
	if not settings:
		return

	existing = frappe.get_all(
		"Customer Gold Ledger",
		filters={"reference_doctype": doc.doctype, "reference_docname": doc.name},
		limit=1,
	)
	if existing:
		# A resubmit or a repost must never add a second set of rows. Reposts do not call
		# on_submit, but an amended-and-resubmitted document carries a NEW name, so this
		# guard is about genuine double-fires rather than amendments.
		return

	liability_account = frappe.db.get_value(
		"Stock Entry Detail", {"parent": doc.name}, "expense_account"
	)

	for row in doc.get("items") or []:
		ledger = frappe.new_doc("Customer Gold Ledger")
		ledger.customer = row.get("customer") or doc.get("_customer")
		ledger.company = doc.company
		ledger.item = row.item_code
		ledger.batch = row.get("batch_no")
		ledger.reference_doctype = doc.doctype
		ledger.reference_docname = doc.name
		ledger.posting_date = doc.posting_date
		ledger.quantity = row.qty
		# Cannot be an uncomputed zero: the receipt validator refuses to save one. The column
		# is NOT NULL regardless (Frappe Float), so there is nothing to guard against here.
		ledger.pure_qty = row.get("custom_pure_qty")
		ledger.stock_uom = row.get("stock_uom")
		ledger.nominal_rate_per_gram = doc.get("custom_gold_rate_per_gram")
		ledger.nominal_value = row.get("basic_amount")
		ledger.gold_rate_reference = doc.get("custom_gold_rate_reference")
		ledger.gold_rate_date = doc.get("custom_gold_rate_date")
		ledger.liability_account = row.get("expense_account") or liability_account
		ledger.insert(ignore_permissions=True)


def cancel_customer_gold_ledger(doc, method=None):
	"""Flag the rows cancelled. Never delete -- the audit trail is the point.

	Balance calculations must filter on ``is_cancelled = 0``. Flagging rather than deleting
	also means a cancelled receipt still shows what it was valued at, matching how the rate
	snapshot survives cancellation on the Stock Entry itself.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
		_receipt_settings,
	)

	if not _receipt_settings(doc):
		return

	for name in frappe.get_all(
		"Customer Gold Ledger",
		filters={"reference_doctype": doc.doctype, "reference_docname": doc.name},
		pluck="name",
	):
		frappe.db.set_value("Customer Gold Ledger", name, "is_cancelled", 1)
