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

This module does NOT set ``basic_rate``, ``valuation_rate`` or any stock/GL value. Nominal
valuation and the liability posting are separate, later work.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.customer_gold_rate import (
	resolve_customer_gold_rate_for_date,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	get_customer_gold_settings,
	is_customer_gold_enabled,
)

CUSTOMER_GOODS = "Customer Goods"

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

	Sets snapshot fields ONLY -- never ``basic_rate``, ``valuation_rate`` or an expense
	account. Consuming the frozen rate for stock valuation is later work, and that work
	must read ``custom_gold_rate_per_gram`` from here rather than resolving a rate again.
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
