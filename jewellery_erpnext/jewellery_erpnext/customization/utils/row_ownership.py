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
from frappe.utils import cint

#: The only customer-owned Inventory Type that exists on the real sites. "Customer Stock" is a
#: test-site fixture, so nothing may WRITE it -- readers still accept both.
CUSTOMER_GOODS_INVENTORY_TYPE = "Customer Goods"
CUSTOMER_INVENTORY_TYPES = (CUSTOMER_GOODS_INVENTORY_TYPE, "Customer Stock")
DEFAULT_INVENTORY_TYPE = "Regular Stock"
PROCESS_LOSS_SE_TYPE = "Process Loss"
REPACK_SE_TYPE = "Repack"
METAL_CONVERSION_SE_TYPE = "Repack-Metal Conversion"

# Stock Entry types that consume one item and produce another, so the produced
# batch's ownership can only come from the consumed one. Both are guarded by
# ``validate_loss_ownership_carried``.
OWNERSHIP_CARRYING_SE_TYPES = (PROCESS_LOSS_SE_TYPE, REPACK_SE_TYPE)

# The Item variant letter a Stock Entry row carries (``Item.variant_of``, mirrored onto the row as
# ``custom_variant_of``) -> the Parent Manufacturing Order checkbox that says the customer supplied
# that kind of material. Metal and findings are both gold, so both read ``is_customer_gold``.
#
# ONE COPY, THREE READERS. This map was written out by hand in ``se_utils.validate_inventory_dimention``
# and again in ``se_utils.get_fifo_batches``, and the Material Request builder in
# ``parent_manufacturing_order.create_material_requests`` needed a third. Three copies of one policy
# is how the enforcement and the allocator drift apart, so they all come here. ``_ITEM_TYPE_PREFIX``
# on the PMO maps its own item-type buckets onto these same letters.
VARIANT_CUSTOMER_FLAG = {
	"M": "is_customer_gold",
	"F": "is_customer_gold",
	"D": "is_customer_diamond",
	"G": "is_customer_gemstone",
	"O": "is_customer_material",
}

#: Variant letters whose owner is never substituted, in either direction. The order's own
#: checkbox decides: customer diamond / gemstone ticked -> only that customer's Customer Goods,
#: not ticked -> only company Regular Stock. The manufacturer's "Allow Regular Goods Instead Of
#: Customer Goods" and the staged warn-only rollout both stop at these two. Metal, findings and
#: other material keep the manufacturer's allowance.
STRICT_CUSTOMER_GOODS_VARIANTS = ("D", "G")


def pmo_requires_customer_goods(pmo_data, variant_of):
	"""True when a row of this variant letter may draw ONLY the order customer's own goods.

	:func:`pmo_expects_customer_goods` narrowed to :data:`STRICT_CUSTOMER_GOODS_VARIANTS`: where
	that one says "the customer supplied this", this one says "and nothing else will do".
	"""
	return variant_of in STRICT_CUSTOMER_GOODS_VARIANTS and pmo_expects_customer_goods(
		pmo_data, variant_of
	)


def pmo_requires_company_stock(pmo_data, variant_of):
	"""True when a row of this variant letter may draw ONLY company (Regular Stock) batches.

	The other half of :func:`pmo_requires_customer_goods`: a diamond or gemstone row on an order
	that does NOT say the customer supplied it. ``pmo_data`` must be a real order -- a row that
	names none is not judged, so an ordinary transfer of a customer's stones still finds them.
	"""
	return (
		bool(pmo_data)
		and variant_of in STRICT_CUSTOMER_GOODS_VARIANTS
		and not pmo_expects_customer_goods(pmo_data, variant_of)
	)


def pmo_expects_customer_goods(pmo_data, variant_of):
	"""True when this order's flags make a row of this variant letter the CUSTOMER's material.

	Capability, never possession: a True here says the order was placed on the customer's own
	stones or metal, not that any particular batch is theirs. What a row actually draws is still
	decided by the batch (:func:`resolve_batch_ownership`), which is why ``get_fifo_batches``
	re-stamps each allocation from the batch it took rather than trusting this answer.

	``pmo_data`` is whatever ``frappe.db.get_value("Parent Manufacturing Order", ..., as_dict=1)``
	returned, so None (the PMO does not exist) is a legitimate input and answers False rather than
	raising -- a Stock Entry pointing at a deleted order must not 500 the save.
	"""
	if not pmo_data or not variant_of:
		return False

	flag = VARIANT_CUSTOMER_FLAG.get(variant_of)
	if not flag:
		return False

	return bool(cint(pmo_data.get(flag)))


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


def resolve_batch_ownership(row, batch=None):
	"""Resolve ``(inventory_type, customer)`` for a row from its SOURCE batch.

	The batch wins (rule 1); anything already on the row is only a fallback for
	fields the batch does not carry.

	``batch`` is the row's Batch record when the caller already holds it -- a validator
	looking at every row of a document reads them all in one ``bulk_map`` rather than
	one ``get_value`` per row. Pass ``{}`` for "this batch has no ownership of its own";
	leaving it None means "not read yet, fetch it". Either way the precedence rule lives
	here and only here, so a bulk caller cannot drift from a single-row one.
	"""
	batch_no = _get(row, "batch_no")
	batch_inv = batch or {}
	if batch is None and batch_no:
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
	"""Fail loudly when a consume/produce builder forgets to carry ownership to its produce row.

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

	Covers **Repack** as well as **Process Loss**. A purity Repack (pure metal -> alloy) mints a new
	batch from consumed metal exactly as a loss write-off does, and the EIR gain injection's Repack
	leg used to leave its produce row bare -- laundering a customer's pure metal into company alloy.
	Widening the guard is safe by its own construction: it fires only on a **blank** produce row, so
	a builder that deliberately writes company ownership still passes untouched.
	"""
	if se.get("stock_entry_type") not in OWNERSHIP_CARRYING_SE_TYPES:
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
				"inventory type was never set. A {5} entry must carry the consumed "
				"batch's ownership onto the produced row, or the customer's material is "
				"silently booked as company stock. This is a bug in whichever flow built this "
				"Stock Entry."
			).format(
				row.get("idx"),
				row.get("item_code"),
				src.get("inventory_type"),
				frappe.bold(src.get("batch_no")),
				frappe.bold(src.get("customer")),
				se.get("stock_entry_type"),
			)
		)
