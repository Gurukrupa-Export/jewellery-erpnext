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

This module does NOT touch rates, valuation or GL. Nominal valuation and the liability
posting are separate, later work.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	get_customer_gold_settings,
	is_customer_gold_enabled,
)

CUSTOMER_GOODS = "Customer Goods"


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
