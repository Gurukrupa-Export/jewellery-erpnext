# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Server-side eligibility rules for a Customer Gold receipt.

Everything here is gated twice: the ``enable_customer_gold_flow`` master switch, and the
Stock Entry Type configured on ``Subcontracting Settings``. With the switch off, or on any
other Stock Entry Type, these functions return immediately and existing behaviour is
untouched.

The rules are deliberately split across two lifecycle points, because ``batch_no`` does not
exist during validation:

``before_validate``  customer, inventory type, item and quantity -- everything the caller
                    supplies directly.
``before_submit``    batch ownership -- runs AFTER ``batch_rename.create_child_batches`` so
                    that batches minted by ``create_parent_batches`` are visible. Placing a
                    batch check any earlier would reject every legitimate receipt, since the
                    rows carry no batch until then.

``before_validate`` additionally freezes the Customer Gold rate snapshot -- see
``set_customer_gold_rate_snapshot`` -- so the receipt permanently records which rate was
resolved for its posting date.

``before_validate`` finally derives the NOMINAL VALUATION from that frozen snapshot -- see
``set_customer_gold_nominal_valuation`` -- which is what makes the receipt post a non-zero
Stock Ledger Entry and a ``Dr Stock / Cr Customer Gold Liability`` GL pair.

WARNING -- this module writes to the Stock Ledger and the General Ledger. A wrong line here
produces a wrong valuation on a precious-metal ledger, not a failed save.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.customer_gold_rate import (
	resolve_customer_gold_rate_for_date,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	get_customer_gold_company_settings,
	get_customer_gold_settings,
	is_customer_gold_enabled,
)

CUSTOMER_GOODS = "Customer Goods"

#: Field holding the value this module derived itself, kept alongside ERPNext's own
#: ``basic_amount`` precisely so the two can be compared. ``basic_amount`` is recomputed by
#: reposts; this one is not. A divergence between them IS the audit signal.
NOMINAL_VALUE_FIELD = "custom_gold_nominal_value"

#: Snapshot fields written by ``set_customer_gold_rate_snapshot``. Provisioned by
#: ``patches.add_customer_gold_rate_snapshot_fields``.
RATE_SNAPSHOT_FIELDS = (
	"custom_gold_rate_reference",
	"custom_gold_rate_date",
	"custom_gold_rate_source",
	"custom_gold_rate_field",
	"custom_gold_rate_raw",
	"custom_gold_rate_unit",
	"custom_gold_rate_per_gram",
)


def _receipt_settings(doc):
	"""Return the settings when ``doc`` is a Customer Gold receipt, else ``None``.

	Fetched once per document -- never per row.
	"""
	if doc.doctype != "Stock Entry":
		return None
	if not is_customer_gold_enabled():
		return None

	settings = get_customer_gold_settings()
	configured_type = settings.get("customer_goods_stock_entry_type")
	if not configured_type or doc.get("stock_entry_type") != configured_type:
		return None

	return settings


def validate_customer_gold_receipt(doc, method=None):
	"""Customer, inventory type, item and quantity rules for a Customer Gold receipt."""
	settings = _receipt_settings(doc)
	if not settings:
		return

	_validate_receipt_purpose(settings)
	customer = _validate_customer(doc)
	_validate_rows(doc, settings, customer)
	set_customer_gold_rate_snapshot(doc, settings)
	set_customer_gold_nominal_valuation(doc, settings)


def _validate_receipt_purpose(settings):
	"""Re-check the configured type at runtime, in case the master was edited later."""
	purpose = frappe.db.get_value(
		"Stock Entry Type", settings.customer_goods_stock_entry_type, "purpose"
	)
	if purpose != "Material Receipt":
		frappe.throw(
			_(
				"Stock Entry Type {0} is configured for Customer Gold receipts but its purpose is {1}, not {2}."
			).format(
				frappe.bold(settings.customer_goods_stock_entry_type),
				frappe.bold(purpose or _("not set")),
				frappe.bold("Material Receipt"),
			),
			title=_("Customer Gold Configuration Invalid"),
		)


def _validate_customer(doc):
	"""``Stock Entry._customer`` is the authoritative header customer for this flow."""
	customer = doc.get("_customer")
	if not customer:
		frappe.throw(
			_("Customer is mandatory for a Customer Gold receipt."),
			title=_("Customer Missing"),
		)

	for row in doc.get("items") or []:
		if not row.get("customer"):
			# Only the browser fills this today, so an API-created receipt would arrive
			# blank. Backfill from the authoritative header rather than reject.
			row.customer = customer
		elif row.customer != customer:
			frappe.throw(
				_(
					"Row #{0}: Customer {1} does not match the receipt Customer {2}."
				).format(row.idx, frappe.bold(row.customer), frappe.bold(customer)),
				title=_("Customer Mismatch"),
			)

	return customer


def _validate_rows(doc, settings, customer):
	configured_item = settings.customer_24kt_item

	for row in doc.get("items") or []:
		if row.get("inventory_type") and row.inventory_type != CUSTOMER_GOODS:
			frappe.throw(
				_(
					"Row #{0}: Customer Gold receipt requires Inventory Type {1}, but {2} was supplied."
				).format(
					row.idx,
					frappe.bold(CUSTOMER_GOODS),
					frappe.bold(row.inventory_type),
				),
				title=_("Invalid Inventory Type"),
			)
		# Set server-side so an API-created receipt cannot bypass ownership tagging;
		# today only the client sets this.
		row.inventory_type = CUSTOMER_GOODS

		if row.item_code != configured_item:
			frappe.throw(
				_(
					"Row #{0}: Customer Gold receipt accepts only the configured Customer 24KT Item {1}."
				).format(row.idx, frappe.bold(configured_item)),
				title=_("Invalid Item"),
			)

		if flt(row.qty) <= 0:
			frappe.throw(
				_(
					"Row #{0}: Quantity must be greater than zero for a Customer Gold receipt."
				).format(row.idx),
				title=_("Invalid Quantity"),
			)

		# custom_pure_qty is computed by doc_events.stock_entry.before_validate, which is
		# FIRST in the ordered before_validate list -- so by the time this runs it is already
		# populated. Assert it, because a silent zero here is a gold-accountability defect:
		# it is what let 22,140 g of customer metal sit unrecorded, and it would propagate
		# into the balance, allocation and per-serial-ownership work that reads it.
		if flt(row.get("custom_pure_qty")) <= 0:
			frappe.throw(
				_(
					"Row #{0}: Pure Quantity could not be computed for item {1}. Check that the item carries a Metal Purity attribute and that Manufacturing Setting defines a pure gold item for this company."
				).format(row.idx, frappe.bold(row.item_code)),
				title=_("Pure Quantity Missing"),
			)


def validate_customer_gold_batches(doc, method=None):
	"""Batch ownership rules, run after the batch creators have minted batches."""
	settings = _receipt_settings(doc)
	if not settings:
		return

	customer = doc.get("_customer")

	for row in doc.get("items") or []:
		if not row.get("batch_no"):
			frappe.throw(
				_(
					"Row #{0}: Customer gold must be batch tracked, but no batch could be determined for item {1}."
				).format(row.idx, frappe.bold(row.item_code)),
				title=_("Batch Missing"),
			)

		batch = frappe.db.get_value(
			"Batch",
			row.batch_no,
			["custom_customer", "custom_inventory_type"],
			as_dict=True,
		)
		if not batch:
			frappe.throw(
				_("Row #{0}: Batch {1} does not exist.").format(
					row.idx, frappe.bold(row.batch_no)
				)
			)

		if batch.custom_customer and batch.custom_customer != customer:
			frappe.throw(
				_(
					"Row #{0}: Batch {1} belongs to Customer {2} and cannot be used for a receipt from Customer {3}."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(batch.custom_customer),
					frappe.bold(customer),
				),
				title=_("Batch Belongs To Another Customer"),
			)

		if (
			batch.custom_inventory_type
			and batch.custom_inventory_type != CUSTOMER_GOODS
		):
			frappe.throw(
				_(
					"Row #{0}: Batch {1} is {2}, so it cannot hold customer gold."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(batch.custom_inventory_type),
				),
				title=_("Invalid Batch Inventory Type"),
			)


def set_customer_gold_rate_snapshot(doc, settings):
	"""Freeze the Customer Gold rate evidence on the receipt.

	Runs on every validate of a DRAFT and overwrites unconditionally. That is deliberate
	and serves two purposes at once:

	* the snapshot re-resolves whenever ``posting_date`` or the configured source / field /
	  unit changes, so a corrected posting date cannot leave yesterday's rate behind; and
	* a client-supplied value can never become financial truth -- whatever arrives over the
	  API is replaced by the server's own resolution.

	CAREFUL -- ``before_validate`` DOES run on the submit transition. ``run_before_save_methods``
	calls it for ``_action in ("save", "submit")`` (``frappe/model/document.py:1396-1397``) and
	``check_docstatus_transition`` sets ``_action = "submit"`` for 0 -> 1 (``document.py:1126``).
	So the snapshot is re-resolved ONE FINAL TIME at submit and is frozen only afterwards,
	because ordinary saves are then blocked. The practical consequence: if the Gold Rates row
	for this posting date is corrected between drafting and submitting, the submitted document
	carries the corrected rate. That matches the agreed policy (resolve on draft, re-resolve on
	change, freeze at submit) -- but it is a final re-resolve AT submit, not a stop-running-at-submit.

	Cancellation does not clear it: a cancelled receipt must still show the rate it originally
	used. An amendment carries no snapshot (the fields are ``no_copy``) and resolves afresh
	against its own posting date.

	Sets snapshot fields ONLY. The valuation that consumes them lives in
	``set_customer_gold_nominal_valuation``, which reads ``custom_gold_rate_per_gram`` from
	here and must never resolve a rate again.
	"""
	rate = resolve_customer_gold_rate_for_date(doc.get("posting_date"), settings)

	doc.custom_gold_rate_reference = rate.gold_rate_reference
	doc.custom_gold_rate_date = rate.gold_rate_date
	doc.custom_gold_rate_source = rate.rate_source
	doc.custom_gold_rate_field = rate.rate_field
	doc.custom_gold_rate_raw = rate.raw_rate
	doc.custom_gold_rate_unit = rate.rate_unit
	doc.custom_gold_rate_per_gram = rate.per_gram_rate

	return rate


def set_customer_gold_nominal_valuation(doc, settings):
	"""Value the receipt at the frozen nominal rate and route the credit to the liability.

	This is the function that makes a Customer Gold receipt post value. Before it existed the
	receipt landed at ``incoming_rate = 0`` and produced no GL at all -- ERPNext drops a GL map
	whose every leg nets to zero (``accounts/general_ledger.py:315-328``), which is why 2,682
	historical receipt SLEs carry only 12 GL rows between them.

	Two writes per row do the whole job:

	``set_basic_rate_manually`` + ``basic_rate``
	    ``jewellery_erpnext.doc_events.stock_entry.allow_zero_valuation`` stamps
	    ``allow_zero_valuation_rate = 1`` on every Customer Goods row, and ERPNext then wipes
	    ``basic_rate`` for exactly such rows (``stock_entry.py:1444-1446``). Rather than fight
	    that -- clearing the flag would mean running after a hook that is already first in the
	    ordered list, i.e. editing shared code -- we take the escape hatch ERPNext itself
	    provides: ``set_basic_rate_manually`` makes ``set_basic_rate`` ``continue`` past the row
	    before the wipe (``stock_entry.py:1435-1436``). It is also the repost-safe choice, since
	    a repost reloads the document from the database and hits the same ``continue``.

	``expense_account``
	    For a Material Receipt the credit counter-leg IS the row's Difference Account
	    (``controllers/stock_controller.py:709-716`` -- Stock Entry Detail has no
	    ``target_warehouse``, so the ``else`` branch always applies). Pointing it at the
	    configured liability account yields ``Dr Warehouse Stock / Cr Customer Gold Liability``
	    from ERPNext's own posting engine, so debit and credit are generated from the same
	    ``stock_value_difference`` and tie out by construction. Cancellation and repost reversal
	    come free. No Journal Entry, no manual GL, no window where the books are unbalanced.

	Nothing here validates root type -- ``Subcontracting Settings`` already enforces
	``root_type == "Liability"`` on this account at configuration time. What IS re-checked is the
	company, because a Single's child table can be edited after a receipt is drafted and posting
	to another company's ledger is unrecoverable without a cancel.

	Runs on every draft validate and overwrites unconditionally, exactly like the snapshot: a
	changed posting date or quantity re-derives the value, and a ``basic_rate`` supplied over the
	API is replaced by the server's own derivation before anything is stored.
	"""
	per_gram = flt(doc.get("custom_gold_rate_per_gram"))
	if per_gram <= 0:
		# Never fall back to a lookup. The snapshot is the single source of truth for this
		# document's rate, and resolving again would reintroduce the historical-drift hazard
		# the snapshot exists to remove -- and would fail outright on a posting date whose
		# Gold Rates record has since been deleted.
		frappe.throw(
			_(
				"Customer Gold receipt {0} has no frozen gold rate for Posting Date {1}, so it cannot be valued. Re-save the document to resolve the rate."
			).format(
				frappe.bold(doc.get("name") or _("(new)")),
				frappe.bold(doc.get("posting_date")),
			),
			title=_("Nominal Rate Missing"),
		)

	liability_account = _customer_gold_liability_account(doc)

	total_nominal_value = 0.0
	for row in doc.get("items") or []:
		_validate_row_uom(row)

		# transfer_qty is qty in the STOCK uom; the rate is per gram and the stock uom is
		# Gram at conversion factor 1, so the two are directly compatible. Fall back to the
		# product when set_transfer_qty has not run yet on this pass.
		stock_qty = flt(row.get("transfer_qty")) or flt(row.get("qty")) * flt(
			row.get("conversion_factor") or 1
		)

		row.set_basic_rate_manually = 1
		row.basic_rate = per_gram
		# MUST be set explicitly. The same ``continue`` that protects basic_rate also skips
		# ERPNext's own basic_amount computation (``stock_entry.py:1485``), so leaving this
		# out gives a correct ledger under a Stock Entry whose header total reads zero.
		row.basic_amount = flt(stock_qty * per_gram, row.precision("basic_amount"))
		row.expense_account = liability_account

		total_nominal_value += flt(row.basic_amount)

	doc.set(
		NOMINAL_VALUE_FIELD,
		flt(total_nominal_value, doc.precision(NOMINAL_VALUE_FIELD)),
	)


def _customer_gold_liability_account(doc):
	"""Resolve the liability account ONCE per document, never per row."""
	if not doc.get("company"):
		frappe.throw(
			_("Company is mandatory for a Customer Gold receipt."),
			title=_("Company Missing"),
		)

	# Deliberately unwrapped: this throws when the company has no configured row, and that
	# is the behaviour we want -- silently posting to a default account would be worse.
	accounts = get_customer_gold_company_settings(doc.company)
	account = accounts.liability_account

	owning_company = frappe.db.get_value("Account", account, "company")
	if owning_company != doc.company:
		frappe.throw(
			_(
				"Customer Gold Liability Account {0} belongs to Company {1}, but this receipt is for Company {2}. The configuration has changed since this receipt was drafted."
			).format(
				frappe.bold(account),
				frappe.bold(owning_company),
				frappe.bold(doc.company),
			),
			title=_("Liability Account Company Mismatch"),
		)

	return account


def _validate_row_uom(row):
	"""The frozen rate is per GRAM, so a row at any other conversion factor would be wrong.

	Blocked rather than converted. The configured 24KT item carries a single UOM conversion
	row at factor 1, so this cannot fire on a correctly configured receipt -- and if it ever
	does, the rate is off by exactly the conversion factor, which is not something to guess at.
	"""
	conversion_factor = flt(row.get("conversion_factor") or 1)
	if conversion_factor != 1:
		frappe.throw(
			_(
				"Row #{0}: Customer Gold receipts must be entered in the stock UOM. Item {1} is on UOM {2} at conversion factor {3}, but the frozen gold rate is per gram."
			).format(
				row.idx,
				frappe.bold(row.get("item_code")),
				frappe.bold(row.get("uom")),
				frappe.bold(conversion_factor),
			),
			title=_("Unsupported UOM"),
		)
