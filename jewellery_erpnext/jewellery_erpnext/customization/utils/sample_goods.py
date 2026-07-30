# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Single source of truth for the "Customer Sample Goods" block.

Stock received as a Customer Sample -- a ``Customer Goods Received`` Stock Entry whose
``customer_voucher_type == "Customer Sample Goods"`` -- is stamped onto the created Batch
as ``custom_customer_voucher_type`` (see
``customization/batch/doc_events/utils.py::update_inventory_dimentions``). The **Batch** is
therefore the authoritative runtime marker; a later consuming Stock Entry does not itself
carry ``customer_voucher_type``.

Such sample stock must never be used in a Work Order, issued to Production/Floor, or
consumed in manufacturing -- it may only move via the three Customer Goods movements
(Received / Issue / Transfer). Every enforcement layer (the Stock Entry consumption
backstop, the FIFO auto-allocation exclusion, and the Employee/Department IR "Issue"
fail-fast guards) imports the constant, the allow-list and the "is it a sample" test from
here so there is exactly one definition and no import cycle.
"""

import frappe
from frappe import _
from frappe.utils import flt

SAMPLE_VOUCHER_TYPE = "Customer Sample Goods"

# The only Stock Entry types on which a sample batch may legitimately appear as a source
# row: receive it, return it to the customer, or relocate it between warehouses.
# Block-by-default -- every other stock_entry_type that draws a sample source row is a
# Work-Order use / issue-to-floor / manufacturing consumption and is blocked.
SAMPLE_ALLOWED_SE_TYPES = {
	"Customer Goods Received",
	"Customer Goods Issue",
	"Customer Goods Transfer",
}


def is_customer_sample_batch(batch_no):
	"""True when ``batch_no`` was received as Customer Sample Goods.

	Uses ``frappe.get_cached_value`` -- the voucher type is set once at batch creation and
	never changes, so the request/Redis cache is always safe and avoids repeated reads when
	the same batch appears on several rows.
	"""
	if not batch_no:
		return False
	return (
		frappe.get_cached_value("Batch", batch_no, "custom_customer_voucher_type")
		== SAMPLE_VOUCHER_TYPE
	)


def get_sample_batches(batch_nos):
	"""Return the subset of ``batch_nos`` that are Customer Sample Goods batches.

	One bulk query so a per-row guard does not fan out into N ``get_value`` calls.
	"""
	batch_nos = {b for b in batch_nos if b}
	if not batch_nos:
		return set()
	return set(
		frappe.get_all(
			"Batch",
			filters={
				"name": ["in", list(batch_nos)],
				"custom_customer_voucher_type": SAMPLE_VOUCHER_TYPE,
			},
			pluck="name",
		)
	)


def sample_batches_in_operation(manufacturing_operation):
	"""Sample batches currently sitting (positive balance) in a Manufacturing Operation.

	Reads the operation's running MOP Log balance -- the exact rows the Employee/Department
	IR "Issue" paths clone -- keeps rows with a positive batch-based balance, and returns
	the offending balance rows (each carrying ``item_code`` and ``batch_no``). Returns an
	empty list when the operation is empty or holds no sample stock.
	"""
	if not manufacturing_operation:
		return []

	# Local import avoids a module-load cycle (mop_log imports jewellery_erpnext.utils).
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	rows = [
		row
		for row in get_current_mop_balance_rows(manufacturing_operation)
		if row.get("batch_no") and flt(row.get("qty_after_transaction_batch_based")) > 0
	]
	if not rows:
		return []

	sample = get_sample_batches({row.get("batch_no") for row in rows})
	return [row for row in rows if row.get("batch_no") in sample]


def assert_no_sample_in_operations(operation_rows, doc=None):
	"""Throw if any operation about to be issued holds Customer Sample Goods stock.

	Shared by the Employee IR and Department IR "Issue" fail-fast guards. ``operation_rows``
	are the IR child rows, each carrying ``manufacturing_operation`` and
	``manufacturing_work_order``.
	"""
	for row in operation_rows or []:
		operation = row.get("manufacturing_operation")
		offenders = sample_batches_in_operation(operation)
		if not offenders:
			continue
		first = offenders[0]
		frappe.throw(
			_(
				"Manufacturing Operation {0} (Work Order {1}) holds Customer Sample Goods "
				"batch {2} (item {3}). Sample stock cannot be issued to Production or used "
				"in manufacturing. Remove it before issuing."
			).format(
				frappe.bold(operation),
				row.get("manufacturing_work_order"),
				frappe.bold(first.get("batch_no")),
				first.get("item_code"),
			)
		)
