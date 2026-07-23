"""Melting Loss handling for Metal Conversions.

When ``is_melting_loss`` is checked, a Metal Conversions document records ONLY a
melting loss: NO purity-conversion Stock Entry is created. On submit exactly one
"Process Loss" (Repack purpose) Stock Entry moves the Loss Quantity from the
department Raw Material warehouse (``source_warehouse``) to the department Scrap
warehouse; the remaining quantity is untouched in the Raw Material warehouse.

Single-mode only (blocked when ``multiple_metal_converter = 1``).

Validation placement:
  * ``validate_melting_loss``  (save-time, re-runs on submit): V1-V6, V12
  * ``update_source_betch``    (save-time, re-runs on submit): V7 (availability)
  * ``make_melting_loss_stock_entry`` (submit-time only): V8 (allocation sum),
    V9/V10 (loss-item resolution), V11 (scrap warehouse)

Mirrors the Employee IR loss engine
(``employee_ir/doc_events/loss_stock_entry.py``): a "Process Loss" Repack SE that
consumes the source metal from its batch(es) and produces a mapped loss-item
variant into the Scrap warehouse.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

MELTING_LOSS_SE_TYPE = "Process Loss"
MELTING_LOSS_LOSS_TYPE = "Loss"


def _loss_precision():
	# ERPNext recomputes flt(qty * conversion_factor, precision("transfer_qty"));
	# read the live precision instead of hardcoding 3.
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


# ---------------------------------------------------------------------------
# Validation (save-time; re-runs on submit)
# ---------------------------------------------------------------------------


def validate_melting_loss(doc):
	"""V1-V6 + V12 and defensive clearing of conversion fields.

	Runs FIRST in Metal Conversions.validate so the clearing takes effect before
	``update_alloy_betch`` / ``update_source_betch`` read the alloy / customer
	fields. No-op unless ``is_melting_loss`` is checked.
	"""
	if not cint(doc.get("is_melting_loss")):
		return

	prec = _loss_precision()

	# V1 — single mode only
	if cint(doc.multiple_metal_converter):
		frappe.throw(
			_(
				"Melting Loss can only be recorded in single Metal Converter mode. "
				"Uncheck Multiple Metal Converter to record a melting loss."
			)
		)

	# V2 — source item mandatory
	if not doc.source_item:
		frappe.throw(_("Source Item is mandatory when Is Melting Loss is checked."))

	# V3 — source qty mandatory and > 0 (covers zero and negative)
	if not flt(doc.source_qty) > 0:
		frappe.throw(
			_(
				"Source Qty is mandatory and must be greater than zero when Is Melting "
				"Loss is checked."
			)
		)

	# V4 — loss qty mandatory
	if not doc.loss_qty:
		frappe.throw(_("Loss Quantity is mandatory when Is Melting Loss is checked."))

	# V5 — loss qty positive at stock precision (zero, negative, sub-precision)
	loss = flt(doc.loss_qty, prec)
	if loss <= 0:
		frappe.throw(
			_("Loss Quantity {0} is not a positive quantity at precision {1}.").format(
				doc.loss_qty, prec
			)
		)

	# V6 — loss qty cannot exceed source qty (equality allowed = full loss)
	if loss > flt(doc.source_qty, prec):
		frappe.throw(
			_("Loss Quantity {0} cannot exceed Source Qty {1}.").format(
				doc.loss_qty, doc.source_qty
			)
		)

	# V12 — customer mandatory when customer metal; otherwise clear a stale customer
	# (mandatory_depends_on is client-only; enforce server-side).
	if cint(doc.get("is_customer_metal")):
		if not doc.customer:
			frappe.throw(_("Customer is mandatory when Is Customer Metal is checked."))
	else:
		doc.customer = None

	_clear_conversion_fields(doc)


def _clear_conversion_fields(doc):
	"""Blank every conversion-only field so a loss document carries no target/alloy
	state, even when created via the API (where ``depends_on`` does not clear values).
	"""
	doc.target_item = None
	doc.target_qty = 0
	doc.source_alloy_check = 0
	doc.source_alloy = None
	doc.source_alloy_qty = None
	doc.source_alloy_batch = None
	doc.target_alloy_check = 0
	doc.target_alloy = None
	doc.target_alloy_qty = 0
	doc.alloy_batch_details = []


# ---------------------------------------------------------------------------
# Stock Entry creation (submit-time)
# ---------------------------------------------------------------------------


def make_melting_loss_stock_entry(doc):
	"""Build + submit ONE "Process Loss" Repack SE moving ``loss_qty`` from the
	department Raw Material warehouse to the department Scrap warehouse.

	Carries the submit-only guards V8 (allocation sum), V9/V10 (loss item) and
	V11 (scrap warehouse). Idempotent.
	"""
	# Idempotency: never mint a second loss SE for the same document.
	if frappe.db.exists(
		"Stock Entry",
		{
			"custom_metal_conversion_reference": doc.name,
			"stock_entry_type": MELTING_LOSS_SE_TYPE,
			"auto_created": 1,
			"docstatus": ["!=", 2],
		},
	):
		return

	prec = _loss_precision()
	loss_qty = flt(doc.loss_qty, prec)

	# V11 — department Scrap warehouse (imported resolver throws when unconfigured).
	from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
		get_scrap_warehouse,
	)

	scrap_wh = get_scrap_warehouse(doc.department)

	# V9 / V10 — mapped loss item variant.
	loss_item = _resolve_loss_item(doc)

	source_wh = doc.source_warehouse
	inventory_type = doc.inventory_type or "Regular Stock"

	# Consume rows: FIFO batches already allocated to exactly loss_qty in
	# update_source_betch (loss mode passes required_qty = loss_qty).
	rows = [r for r in doc.source_batch_details if flt(r.qty, prec) > 0]

	# V8 — allocation-sum defensive invariant. Normally UNREACHABLE from the UI:
	# submit re-runs validate, which rebuilds source_batch_details to exactly
	# loss_qty (or throws V7). Guards against direct/programmatic tampering.
	allocated = flt(sum(flt(r.qty) for r in rows), prec)
	if allocated != loss_qty:
		frappe.throw(
			_(
				"Allocated batch quantity {0} does not match Loss Quantity {1}. "
				"Please save the document again."
			).format(allocated, loss_qty)
		)

	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		stock_lock_key,
	)

	# RULE A — deterministic consume order (by batch) to avoid 1213 lock cycles.
	rows = sorted(
		rows, key=lambda r: stock_lock_key(doc.source_item, source_wh, r.batch)
	)
	# Canonical lock order: pin the Stock Entry naming-series row (position 2) BEFORE
	# the Bins so this flow is Series-then-Bin like every conformant SE submit -- fixes
	# the Bin-before-Series inversion behind F-002 1213 cycles. Additive: SELECT ... FOR
	# UPDATE only, re-entrant with the real naming at insert.
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = doc.company
	_series_stub.stock_entry_type = MELTING_LOSS_SE_TYPE
	preallocate_series_for_docs(_series_stub)
	# RULE B — pre-lock the source + scrap Bins up front, in canonical order.
	lock_bins([(doc.source_item, source_wh), (loss_item, scrap_wh)])

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": MELTING_LOSS_SE_TYPE,
			"purpose": "Repack",
			"company": doc.company,
			"branch": doc.branch,
			"department": doc.department,
			# Header manufacturer is required: the global M/F pure-metal block
			# (doc_events/stock_entry.py) runs on the consume rows at submit and
			# reads self.manufacturer, else falls back to a module-default and throws.
			"manufacturer": doc.manufacturer,
			"custom_metal_conversion_reference": doc.name,
			"inventory_type": inventory_type,
			"auto_created": 1,
			# _customer is deliberately NOT set on the header: setting it would engage
			# customer_subcontracting.create_child_batches on this auto-created loss SE,
			# which its own guard explicitly scopes out (A-Z child-suffix exhaustion).
		}
	)

	for r in rows:
		se.append(
			"items",
			{
				"item_code": doc.source_item,
				"qty": flt(r.qty, prec),
				"s_warehouse": source_wh,
				"batch_no": r.batch,
				"inventory_type": inventory_type,
				"customer": doc.customer,
				"department": doc.department,
				"employee": doc.employee,
				"manufacturer": doc.manufacturer,
				"use_serial_batch_fields": 1,
			},
		)

	# ONE produce row into the Scrap warehouse. No batch_no: the loss variant has
	# create_new_batch = 1, so the Serial and Batch Bundle mints a NEW batch on
	# submit. Scrap is booked as Regular Stock by policy -- the melted metal is written
	# off to the company; the customer is recorded on the row for traceability only.
	# basic_rate is left unset here: CustomStockEntry.set_basic_rate assigns it from the
	# consumed rows' value (customization/utils/loss_valuation), so the scrap carries the
	# metal's valuation instead of entering stock at rate 0.
	se.append(
		"items",
		{
			"item_code": loss_item,
			"qty": loss_qty,
			"t_warehouse": scrap_wh,
			"is_finished_item": 1,
			"set_basic_rate_manually": 1,
			"inventory_type": "Regular Stock",
			"customer": doc.customer,
			"department": doc.department,
			"employee": doc.employee,
			"manufacturer": doc.manufacturer,
			"use_serial_batch_fields": 1,
		},
	)

	se.save()
	se.submit()
	# db_set persists post-save; a plain attribute assignment would not.
	doc.db_set("stock_entry", se.name)


def _resolve_loss_item(doc):
	"""Resolve the mapped loss item variant (V9 / V10).

	Uses the same ``(variant, loss_type)`` Variant Loss Table lookup that
	``get_item_loss_item`` performs, so the pre-check and the resolver cannot
	diverge.
	"""
	variant_of = frappe.db.get_value("Item", doc.source_item, "variant_of")
	if not variant_of:
		# V9
		frappe.throw(
			_("Item {0} has no variant template; cannot resolve a loss item.").format(
				doc.source_item
			)
		)

	loss_variant = frappe.db.get_value(
		"Variant Loss Table",
		{"variant": variant_of, "loss_type": MELTING_LOSS_LOSS_TYPE},
		"loss_variant",
	)
	if not loss_variant:
		# V10
		frappe.throw(
			_(
				"No Variant Loss Table entry found for variant {0}, loss type {1}. "
				"Configure it on the Manufacturer."
			).format(variant_of, MELTING_LOSS_LOSS_TYPE)
		)

	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	loss_item = get_item_loss_item(
		doc.company, doc.source_item, variant_of, MELTING_LOSS_LOSS_TYPE
	)
	if not loss_item:
		frappe.throw(
			_("Could not resolve a loss item for {0}.").format(doc.source_item)
		)
	return loss_item


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def cancel_melting_loss_stock_entries(doc):
	"""Scoped cancel cascade: cancel ONLY the auto-created "Process Loss" SEs owned
	by this document. Legacy conversion SEs (Repack-Metal Conversion) are left
	untouched, preserving today's behaviour. A no-op for conversion-mode documents.
	"""
	for se_name in frappe.db.get_all(
		"Stock Entry",
		{
			"custom_metal_conversion_reference": doc.name,
			"stock_entry_type": MELTING_LOSS_SE_TYPE,
			"auto_created": 1,
			"docstatus": 1,
		},
		pluck="name",
	):
		frappe.get_doc("Stock Entry", se_name).cancel()
