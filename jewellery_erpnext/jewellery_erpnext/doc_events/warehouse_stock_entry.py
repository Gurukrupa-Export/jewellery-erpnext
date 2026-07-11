# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Button-driven Issue / Receive Material for an employee MSL warehouse.

Mirrors the Tree Number material flow
(``doctype/tree_number/doc_events/tree_stock_entry.py``) but keyed on the **Warehouse
itself** instead of a Tree Number. An "MSL" warehouse = a ``Warehouse`` with ``employee``
set and ``warehouse_type == "Raw Material"``; its department is taken from the employee
(``employee`` / ``department`` are mutually exclusive on a warehouse, so the warehouse's own
``department`` is NULL — ``doc_events/warehouse.py``).

  * **Issue Material**   -> plain ``Material Transfer`` SE: Dept RM WH -> this MSL WH.
  * **Receive Material** -> received leg  = ``Material Transfer`` (this MSL -> Dept RM);
                            loss leg      = ``Process Loss`` Repack (metal @ MSL -> ML loss
                            variant @ Dept Scrap). Loss is **auto-computed** per item:
                            ``loss = pending - returned`` (each settled item is fully drained).

Notes:
  * **Ledger-invisible SEs.** ``Material Transfer`` and ``Process Loss`` are absent from MOP
    Settings' ``Stock Entry Type To Reservation``, so ``doc_events/stock_entry.onsubmit`` skips
    reservation + MOP Log. ``auto_created = 1`` also bypasses the WORK-ORDER / metal-property
    validations in ``before_validate`` (``se.manufacturer`` is set so the M/F pure-metal block
    still resolves a ``Manufacturing Setting``).
  * ``se.company`` is derived from the warehouse being moved (not the MSL warehouse blindly) so a
    multi-company MSL/department pair still passes ``validate_warehouse_company``.
  * "Pending" is always read live from the ledger (``get_warehouse_item_tracking``) so the
    Receive maths cannot drift from the maintained ``custom_msl_tracking`` copy; the copy is
    recomputed after every action.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

# --- Reused, stable helpers (do not re-implement) --------------------------------------------
from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking import (
	get_warehouse_item_tracking,
	recalculate_msl_tracking,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	_apply_fifo_batches_to_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
	get_scrap_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	_get_department_rm_warehouse,
)

# Doc-agnostic SE row builders (take only `se` + primitives — no Tree coupling), reused from the
# tree flow so the subtle row flags (use_serial_batch_fields / is_finished_item /
# set_basic_rate_manually) stay defined in exactly one place.
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
	_append_item,
	_append_repack_loss_pair,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins,
	preallocate_series_for_docs,
)

MATERIAL_TRANSFER = "Material Transfer"
# Ledger-invisible Repack type used for the receive loss leg (converts the metal into its ML loss
# variant). NOT the plain "Repack" type — that one IS in MOP Settings' reservation list. This is
# the same type the Employee IR loss engine uses and is absent from the reservation list.
PROCESS_LOSS = "Process Loss"
LOSS_TYPE = "Loss"


def _se_precision():
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def _eps(prec):
	return (10**-prec) / 2


# ---------------------------------------------------------------------------
# Warehouse resolution
# ---------------------------------------------------------------------------
def _validate_msl_warehouse(warehouse):
	"""Resolve + validate an employee (MSL) warehouse and its department context.

	Returns a ``frappe._dict`` with ``msl_wh / employee / department / company / manufacturer``.
	Throws for a missing / non-employee / non-Raw-Material / disabled warehouse, or an employee
	with no department.
	"""
	if not warehouse:
		frappe.throw(_("Warehouse is required."))
	wh = frappe.db.get_value(
		"Warehouse",
		warehouse,
		["name", "employee", "warehouse_type", "disabled", "company"],
		as_dict=True,
	)
	if not wh:
		frappe.throw(_("Warehouse {0} not found.").format(warehouse))
	if not wh.employee:
		frappe.throw(
			_("Warehouse {0} is not an employee (MSL) warehouse.").format(warehouse)
		)
	if wh.warehouse_type != "Raw Material":
		frappe.throw(
			_("Warehouse {0} is not a Raw Material (MSL) warehouse.").format(warehouse)
		)
	if wh.disabled:
		frappe.throw(_("Warehouse {0} is disabled.").format(warehouse))

	department = frappe.db.get_value("Employee", wh.employee, "department")
	if not department:
		frappe.throw(
			_("Employee {0} on warehouse {1} has no Department set.").format(
				wh.employee, warehouse
			)
		)
	# Header manufacturer is required by the M/F pure-metal block in
	# doc_events/stock_entry.before_validate (it reads self.manufacturer to find the
	# Manufacturing Setting's pure_gold_item). Prefer the Department's manufacturer, then the
	# session default (mirrors Employee Loss Entry).
	manufacturer = frappe.db.get_value(
		"Department", department, "manufacturer"
	) or frappe.defaults.get_user_default("manufacturer")
	return frappe._dict(
		msl_wh=wh.name,
		employee=wh.employee,
		department=department,
		company=wh.company,
		manufacturer=manufacturer,
	)


def _pending_by_item(warehouse):
	"""Live per-item pending (Issue - Receive - Loss) from the ledger."""
	rows = get_warehouse_item_tracking({"warehouse": warehouse})
	return {r["item_code"]: flt(r["pending_qty"]) for r in rows}


def _resolve_loss_item(ctx, item_code):
	"""Resolve (creating if needed) the ML loss variant for a metal item.

	Mirrors ``tree_stock_entry._resolve_tree_loss_item`` / ``employee_loss_entry._resolve_loss_item``:
	derive ``variant_of`` from the Item, then delegate to ``get_item_loss_item`` which looks up the
	Variant Loss Table mapping and throws a clear "configure the Variant Loss Table" error when none
	exists (desired fail-fast).
	"""
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if not variant_of:
		frappe.throw(
			_(
				"Item {0}: variant_of is required to resolve its loss (ML) variant."
			).format(item_code)
		)
	return get_item_loss_item(ctx.company, item_code, variant_of, LOSS_TYPE)


# ---------------------------------------------------------------------------
# Stock Entry header builders
# ---------------------------------------------------------------------------
def _new_transfer_se(ctx, company_wh, se_type=MATERIAL_TRANSFER):
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = se_type
	se.purpose = "Material Transfer"
	# Derive company from the warehouse being moved so a multi-company MSL/department pair passes
	# validate_warehouse_company.
	se.company = (
		frappe.get_cached_value("Warehouse", company_wh, "company") or ctx.company
	)
	se.department = ctx.department
	se.employee = ctx.employee
	se.manufacturer = ctx.manufacturer
	se.auto_created = 1
	return se


def _new_repack_loss_se(ctx, company_wh):
	"""A ledger-invisible ``Process Loss`` (purpose Repack) SE for the receive loss leg. Reuses the
	transfer header builder and flips the purpose so the metal can be consumed and the ML loss
	variant produced in its place."""
	se = _new_transfer_se(ctx, company_wh, PROCESS_LOSS)
	se.purpose = "Repack"
	return se


def _refresh_tracking(warehouse):
	"""Recompute the maintained ``custom_msl_tracking`` table after a move. A tracking-refresh
	failure must not roll back the stock posting, so failures are logged, not raised."""
	try:
		recalculate_msl_tracking(warehouse)
	except Exception:
		frappe.log_error(
			title="Warehouse Issue/Receive: MSL tracking refresh failed",
			message=frappe.get_traceback(),
		)


# ---------------------------------------------------------------------------
# Whitelisted operations (called from warehouse.js)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_receivable_items(warehouse):
	"""Live ``[{item_code, pending_qty}]`` (pending > 0) to seed the Receive dialog grid."""
	_validate_msl_warehouse(warehouse)
	prec = _se_precision()
	rows = get_warehouse_item_tracking({"warehouse": warehouse})
	return [
		{"item_code": r["item_code"], "pending_qty": flt(r["pending_qty"], prec)}
		for r in rows
		if flt(r["pending_qty"], prec) > 0
	]


@frappe.whitelist()
def issue_material(warehouse, item_code, qty, source_warehouse=None):
	"""Issue ``qty`` of ``item_code`` from the department Raw Material WH into this MSL WH.

	Posts one ledger-invisible ``Material Transfer`` SE and returns its name.
	"""
	frappe.has_permission("Stock Entry", "create", throw=True)
	ctx = _validate_msl_warehouse(warehouse)

	if not item_code:
		frappe.throw(_("Select an Item to issue."))
	prec = _se_precision()
	qty = flt(qty, prec)
	if qty <= 0:
		frappe.throw(
			_("Issue Qty must be greater than zero (precision {0}).").format(prec)
		)

	source_wh = source_warehouse or _get_department_rm_warehouse(ctx.department)
	if not source_wh:
		frappe.throw(
			_(
				"Could not resolve a Source warehouse (Department {0} Raw Material)."
			).format(ctx.department)
		)
	if source_wh == ctx.msl_wh:
		frappe.throw(
			_("Source and this (MSL) warehouse cannot be the same ({0}).").format(
				source_wh
			)
		)

	se = _new_transfer_se(ctx, company_wh=source_wh)
	_append_item(se, item_code, qty, source_wh, ctx.msl_wh)

	# Canonical lock order (lock_order.py): naming series (pos 2) before Bins (pos 3).
	preallocate_series_for_docs(se)
	lock_bins([(item_code, source_wh), (item_code, ctx.msl_wh)])

	_apply_fifo_batches_to_stock_entry(se)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	_refresh_tracking(ctx.msl_wh)
	return se.name


@frappe.whitelist()
def receive_material(warehouse, rows):
	"""Receive metal back to the department RM warehouse and auto-scrap the difference.

	``rows`` = ``[{item_code, return_qty}]`` — only the items the operator chose to settle. For each
	item ``loss = pending - return_qty`` (pending read live from the ledger). Posts up to TWO
	ledger-invisible Stock Entries (returns their names as a list):

	  * **Received** metal -> ``Material Transfer``: this MSL -> Dept RM, same item.
	  * **Loss** -> a ``Process Loss`` (purpose Repack) SE that CONSUMES the metal at MSL and
	    PRODUCES the resolved ML loss variant into Dept Scrap.
	"""
	frappe.has_permission("Stock Entry", "create", throw=True)
	ctx = _validate_msl_warehouse(warehouse)

	if isinstance(rows, str):
		rows = json.loads(rows)
	if not rows:
		frappe.throw(_("Nothing to receive."))

	prec = _se_precision()
	eps = _eps(prec)
	pending_by_item = _pending_by_item(ctx.msl_wh)

	# Validate + collect intent (no side effects yet). loss = pending - return per item.
	plan = []
	for r in rows:
		item = r.get("item_code")
		if not item:
			continue
		pending = flt(pending_by_item.get(item), prec)
		if pending <= 0:
			frappe.throw(
				_("Item {0} has no pending qty in {1}.").format(item, ctx.msl_wh)
			)
		ret = flt(r.get("return_qty"), prec)
		if ret < 0:
			frappe.throw(_("Item {0}: Return Qty cannot be negative.").format(item))
		if ret - pending > eps:
			frappe.throw(
				_("Item {0}: Return Qty ({1}) exceeds Pending ({2}).").format(
					item, ret, pending
				)
			)
		ret = min(ret, pending)
		loss = flt(pending - ret, prec)
		if ret <= 0 and loss <= 0:
			continue
		plan.append(frappe._dict(item=item, ret=ret, loss=loss))

	if not plan:
		frappe.throw(_("Select at least one item to settle."))

	has_recv = any(p.ret > 0 for p in plan)
	has_loss = any(p.loss > 0 for p in plan)
	dept_rm = _get_department_rm_warehouse(ctx.department) if has_recv else None
	scrap_wh = get_scrap_warehouse(ctx.department) if has_loss else None

	se_recv = se_loss = None
	recv_pairs, loss_pairs = [], []

	# --- Received metal: Material Transfer MSL -> Dept RM (same item).
	if has_recv:
		se_recv = _new_transfer_se(ctx, company_wh=ctx.msl_wh)
		for p in plan:
			if p.ret > 0:
				_append_item(se_recv, p.item, p.ret, ctx.msl_wh, dept_rm)
				recv_pairs += [(p.item, ctx.msl_wh), (p.item, dept_rm)]

	# --- Loss: Process Loss Repack consuming metal @ MSL and producing the ML variant @ Scrap.
	if has_loss:
		se_loss = _new_repack_loss_se(ctx, company_wh=ctx.msl_wh)
		for p in plan:
			if p.loss > 0:
				loss_item = _resolve_loss_item(ctx, p.item)
				_append_repack_loss_pair(
					se_loss, p.item, loss_item, p.loss, ctx.msl_wh, scrap_wh
				)
				loss_pairs += [(p.item, ctx.msl_wh), (loss_item, scrap_wh)]

	# Canonical lock order: ALL naming counters (pos 2) before ANY Bin (pos 3), across BOTH SEs, in
	# one deterministic sequence. preallocate_series_for_docs is None-safe.
	preallocate_series_for_docs(se_recv, se_loss)
	lock_bins(recv_pairs + loss_pairs)

	# Post the transfer FIRST, then compute the loss FIFO against post-transfer MSL stock so the two
	# legs never double-pick the same batch (all Bins stay locked across both submits).
	if se_recv:
		_apply_fifo_batches_to_stock_entry(se_recv)
		se_recv.flags.ignore_permissions = True
		se_recv.insert()
		se_recv.submit()
	if se_loss:
		_apply_fifo_batches_to_stock_entry(se_loss)
		se_loss.flags.ignore_permissions = True
		se_loss.insert()
		se_loss.submit()

	_refresh_tracking(ctx.msl_wh)
	return [
		n for n in (getattr(se_recv, "name", None), getattr(se_loss, "name", None)) if n
	]
