# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Single source of truth for a Stock Entry row's inventory ownership.

Ownership is never inherited batch-to-batch. The chain is strictly

    source batch -> Stock Entry Detail row -> newly minted batch

because ``Batch.update_inventory_dimentions`` reads ``custom_inventory_type`` /
``custom_customer`` off the row that minted the batch (via
``Batch.custom_voucher_detail_no``). Populating that row is therefore the SE
*builder's* job, and any builder that mints a batch from customer-owned stock
must carry the ownership across itself -- see ``_stamp_batch`` in
``tree_number/doc_events/tree_stock_entry.py``.

This module holds the resolution rules that every builder must share. They were
first written (and are still pinned by tests) for the Employee IR loss engine;
they live here so the tree and warehouse loss builders cannot drift from them.

Three rules, all load-bearing:

1. **The batch wins over the row.** The batch is the physical truth; a stale
   value on the row must not override it.
2. **A non-customer inventory type never carries a customer.** Defensive
   coherence -- a Regular Stock row with a stray customer would mint a
   mislabelled batch.
3. **A customer type with no resolvable customer downgrades to Regular Stock.**
   This is not a nicety. ``Batch.update_inventory_dimentions`` throws "This item
   is not allowed as Customer Goods" for items without
   ``custom_inventory_type_can_be_customer_goods``, and its ``Process Loss``
   escape hatch (``is_process_loss_repack``) bails when the batch has no
   customer. Emitting a customer type without a customer would therefore hard
   fail the submit. Production data contains such batches (Customer Goods with a
   NULL customer), so this path is live, not theoretical.
"""

import frappe
from frappe import _

CUSTOMER_INVENTORY_TYPES = ("Customer Goods", "Customer Stock")
DEFAULT_INVENTORY_TYPE = "Regular Stock"
PROCESS_LOSS_SE_TYPE = "Process Loss"


def _get(row, fieldname):
	"""Read ``fieldname`` off a row that may be a dict or a Document/namespace."""
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def normalize_ownership(inventory_type, customer, batch_no=None, item_code=None):
	"""Apply the coherence rules to an already-resolved ``(inventory_type, customer)``.

	Use this when the caller has already read the batch (e.g. via a bulk
	``_batch_ownership`` round-trip) and only needs the rules applied, so the
	batch is not fetched twice.
	"""
	inventory_type = inventory_type or DEFAULT_INVENTORY_TYPE

	# Rule 2: customer only ever travels with customer-owned inventory.
	if inventory_type not in CUSTOMER_INVENTORY_TYPES:
		return inventory_type, None

	# Rule 3: a customer type with no customer is malformed -- downgrade rather
	# than mint a batch that trips the Customer Goods guard and fails the submit.
	if not customer:
		frappe.logger().warning(
			"row_ownership: batch {0} (item {1}) is {2} with no customer; "
			"booking row as {3}".format(
				batch_no, item_code, inventory_type, DEFAULT_INVENTORY_TYPE
			)
		)
		return DEFAULT_INVENTORY_TYPE, None

	return inventory_type, customer


def resolve_batch_ownership(row):
	"""Resolve ``(inventory_type, customer)`` for a row from its SOURCE batch.

	The batch wins (rule 1); anything already on the row is only a fallback for
	fields the batch does not carry.
	"""
	batch_no = _get(row, "batch_no")
	batch_inv = {}
	if batch_no:
		batch_inv = (
			frappe.db.get_value(
				"Batch",
				batch_no,
				["custom_inventory_type", "custom_customer"],
				as_dict=True,
			)
			or {}
		)

	# Rule 1: batch first, row second.
	inventory_type = (
		batch_inv.get("custom_inventory_type") or _get(row, "inventory_type") or None
	)
	customer = batch_inv.get("custom_customer") or _get(row, "customer")

	return normalize_ownership(
		inventory_type, customer, batch_no=batch_no, item_code=_get(row, "item_code")
	)


def validate_loss_ownership_carried(se):
	"""Fail loudly when a Process Loss builder forgets to carry ownership to its produce row.

	This is the bug class this module exists to prevent: the tree and warehouse loss builders
	consumed a Customer Goods batch and left the ML produce row bare, so
	``doc_events/stock_entry.py``'s blanket "default blank to Regular Stock" quietly booked a
	customer's metal as company scrap, and the batch minted off that row inherited it.

	The signature is precise, which is why this can throw rather than warn: **every** legitimate
	Process Loss builder stamps its produce row explicitly, whether it preserves ownership (Employee
	IR ``loss_stock_entry``, ``main_slip.create_process_loss``, and now the tree/warehouse builders)
	or deliberately writes it off to the company (``melting_loss`` books the ML row "Regular Stock"
	by policy). Only a builder that never stamped at all leaves the row blank. So a blank produce row
	on a Process Loss SE whose consumption is customer-owned is always a builder bug, never a policy
	choice -- and this must run BEFORE the blanket default, which destroys the distinction between
	"deliberately Regular Stock" and "never set".

	Deliberately does NOT compare consume vs produce inventory_type: ``melting_loss`` legitimately
	consumes Customer Goods and produces Regular Stock, and a naive mismatch check would break it.
	"""
	if se.get("stock_entry_type") != PROCESS_LOSS_SE_TYPE:
		return

	customer_sources = [
		row
		for row in (se.get("items") or [])
		if row.get("s_warehouse")
		and not row.get("t_warehouse")
		and row.get("inventory_type") in CUSTOMER_INVENTORY_TYPES
	]
	if not customer_sources:
		return

	for row in se.get("items") or []:
		if row.get("s_warehouse") or not row.get("t_warehouse"):
			continue
		if row.get("inventory_type"):
			continue
		src = customer_sources[0]
		frappe.throw(
			_(
				"Row #{0} ({1}) produces stock from {2} batch {3} owned by {4}, but its "
				"inventory type was never set. A Process Loss entry must carry the consumed "
				"batch's ownership onto the produced row, or the customer's material is "
				"silently booked as company stock. This is a bug in whichever flow built this "
				"Stock Entry."
			).format(
				row.get("idx"),
				row.get("item_code"),
				src.get("inventory_type"),
				frappe.bold(src.get("batch_no")),
				frappe.bold(src.get("customer")),
			)
		)
