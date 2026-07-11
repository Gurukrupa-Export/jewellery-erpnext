"""Button-driven Issue / Receive Material for a Tree Number.

Implements the documented *Tree Number Material Tracking* flow. Two tree kinds:

  Receive posts up to TWO SEs — a received-metal transfer and, when loss is booked, a separate
  ``Process Loss`` Repack that converts the metal into its ML loss variant.

  * **Standalone** tree (``employee_ir`` empty): both buttons post physical stock.
      - Issue Material   -> plain ``Material Transfer`` SE: Source WH -> MSL WH; ``issue_qty`` +=.
      - Receive Material -> received leg = plain ``Material Transfer`` SE (MSL -> Dept RM);
                            loss leg = ``Process Loss`` Repack (metal @ MSL -> ML variant @ Dept
                            Scrap); ``receive_qty`` / ``loss_qty`` +=.
  * **Casting** tree (``employee_ir`` set): dual receive model.
      - Issue Material posts the physical Source -> MSL transfer as a
        ``Material Transfer (MAIN SLIP)`` SE and owns ``issue_qty``.
      - The Casting Employee IR Receive's ``update_tree_on_receive`` (``tree_casting.py``, called
        from ``employee_ir.py``) books the CAST OUTPUT into ``receive_qty`` / ``loss_qty``.
      - Receive Material (tree button) then RETURNS the post-cast LEFTOVER: received leg =
        ``Material Transfer (MAIN SLIP)`` (MSL -> Dept RM); loss leg = ``Process Loss`` Repack
        (metal @ MSL -> ML variant @ Dept Scrap); ``receive_qty`` / ``loss_qty`` +=. The per-item
        ``(recv + loss) <= pending`` cap makes it structurally leftover-only, so it can never
        re-receive what the Employee IR already booked.

Notes:
  * **Ledger-invisible SEs.** ``Material Transfer``, ``Material Transfer (MAIN SLIP)`` and
    ``Process Loss`` are all absent from MOP Settings' ``Stock Entry Type To Reservation``, so
    ``doc_events/stock_entry.onsubmit`` skips reservation + MOP Log. ``auto_created = 1`` also
    bypasses the WORK-ORDER / metal-property validations in ``before_validate``.
  * ``se.company`` is derived from the warehouse being moved (not ``tree.company``) so a
    multi-company tree passes ``validate_warehouse_company``; ``se.manufacturer`` is set so the
    MAIN SLIP ``before_validate`` branch resolves without a Main Slip link.
  * Warehouse resolution reuses existing app resolvers (``Warehouse`` custom fields
    ``employee`` / ``department``); MSL is the employee's Raw-Material warehouse.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	_apply_fifo_batches_to_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
	_pending_eps,
	_tree_status,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	_get_department_rm_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins,
	preallocate_series_for_docs,
)

MATERIAL_TRANSFER = "Material Transfer"
MATERIAL_TRANSFER_MAIN_SLIP = "Material Transfer (MAIN SLIP)"
# Ledger-invisible Repack type used for the receive loss leg: it converts the metal into its
# ML loss variant. NOT the plain "Repack" type (that one IS in MOP Settings' reservation list and
# would create MOP Log / reservation). "Process Loss" (purpose Repack) is the same type the
# Employee IR loss engine uses and is absent from the reservation list.
PROCESS_LOSS = "Process Loss"


# ---------------------------------------------------------------------------
# Warehouse resolution
# ---------------------------------------------------------------------------
def _is_casting_tree(tree):
	"""EIR-seeded casting trees (employee_ir set): Issue uses the MAIN SLIP SE type and receive is
	Employee-IR-driven. Standalone trees (employee_ir empty) use plain Material Transfer + the
	Receive button."""
	return bool(tree.get("employee_ir"))


def _resolve_msl_warehouse(tree):
	"""The tree's MSL warehouse = the employee's Raw-Material warehouse."""
	if not tree.employee:
		frappe.throw(
			_(
				"Set Employee on Tree {0} to resolve its MSL (Raw Material) warehouse."
			).format(tree.name)
		)
	wh = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "employee": tree.employee, "warehouse_type": "Raw Material"},
		"name",
	)
	if not wh:
		frappe.throw(
			_("No Raw Material (MSL) warehouse found for Employee {0}.").format(
				tree.employee
			)
		)
	return wh


def _resolve_source_warehouse(tree, source_warehouse=None):
	"""Issue source: explicit arg -> tree.source_warehouse -> dept Raw Material WH."""
	source_wh = source_warehouse or tree.get("source_warehouse")
	if not source_wh:
		if not tree.department:
			frappe.throw(
				_("Set Department on Tree {0} to resolve a Source warehouse.").format(
					tree.name
				)
			)
		source_wh = _get_department_rm_warehouse(tree.department)
	if not source_wh:
		frappe.throw(
			_(
				"Could not resolve a Source warehouse (Department {0} Raw Material)."
			).format(tree.department)
		)
	return source_wh


def _resolve_scrap_warehouse(department):
	"""Dept Scrap warehouse for the loss leg; require exactly one per department."""
	if not department:
		frappe.throw(_("Department is required to resolve a Scrap warehouse."))
	rows = frappe.db.get_all(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Scrap"},
		["name"],
	)
	if not rows:
		frappe.throw(
			_("No Scrap warehouse found for Department {0}.").format(department)
		)
	if len(rows) > 1:
		frappe.throw(
			_(
				"Multiple Scrap warehouses found for Department {0}: {1}. "
				"Configure exactly one Scrap warehouse per department."
			).format(department, ", ".join(r.name for r in rows))
		)
	return rows[0].name


# ---------------------------------------------------------------------------
# Stock Entry / ledger helpers
# ---------------------------------------------------------------------------
def _new_transfer_se(tree, se_type=MATERIAL_TRANSFER, company_wh=None):
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = se_type
	se.purpose = "Material Transfer"
	# Derive company from the warehouse being moved, not the tree, so a tree whose warehouses
	# belong to a different company than tree.company still passes validate_warehouse_company.
	se.company = (
		frappe.get_cached_value("Warehouse", company_wh, "company")
		if company_wh
		else tree.company
	)
	# Needed for the MAIN SLIP before_validate branch to resolve manufacturer without a Main Slip.
	se.manufacturer = tree.get("manufacturer")
	se.custom_tree_number = tree.name
	se.auto_created = 1
	return se


def _append_item(se, item_code, qty, s_warehouse, t_warehouse):
	se.append(
		"items",
		{
			"item_code": item_code,
			"qty": qty,
			"s_warehouse": s_warehouse,
			"t_warehouse": t_warehouse,
			"uom": "Gram",
			"use_serial_batch_fields": 1,
		},
	)


def _new_repack_loss_se(tree, company_wh=None):
	"""A ledger-invisible ``Process Loss`` (purpose Repack) SE for the receive loss leg.

	Reuses ``_new_transfer_se`` (so manufacturer / custom_tree_number / auto_created / the
	warehouse-derived company are all set identically) and flips the purpose to Repack so the
	metal item can be consumed and the ML loss variant produced in its place.
	"""
	se = _new_transfer_se(tree, PROCESS_LOSS, company_wh=company_wh)
	se.purpose = "Repack"
	return se


def _resolve_tree_loss_item(tree, item_code, loss_type="Loss"):
	"""Resolve (creating if needed) the ML loss variant for a tree metal item.

	Mirrors ``loss_stock_entry._resolve_loss_item``: derive ``variant_of`` from the Item, then
	delegate to ``get_item_loss_item`` which looks up the Variant Loss Table mapping and throws a
	clear "configure the Variant Loss Table" error when none exists (desired fail-fast).
	"""
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if not variant_of:
		frappe.throw(
			_(
				"Item {0}: variant_of is required to resolve its loss (ML) variant."
			).format(item_code)
		)
	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	return get_item_loss_item(tree.company, item_code, variant_of, loss_type)


def _append_repack_loss_pair(se, metal_item, loss_item, qty, msl_wh, scrap_wh):
	"""Append a consume(metal @ MSL) + produce(ML variant @ Scrap) row pair to a Repack SE.

	Produce-row flags mirror ``loss_stock_entry._build_combined_loss_se`` so the metal value is
	written off as loss (``set_basic_rate_manually`` opts the produce row out of the
	valuation-rate requirement; no ``basic_rate`` is set).
	"""
	# Consume: metal out of MSL (batch filled later by FIFO).
	se.append(
		"items",
		{
			"item_code": metal_item,
			"qty": qty,
			"transfer_qty": qty,
			"conversion_factor": 1,
			"s_warehouse": msl_wh,
			"t_warehouse": None,
			"uom": "Gram",
			"stock_uom": "Gram",
			"pcs": "1",
			"use_serial_batch_fields": 1,
		},
	)
	# Produce: ML loss variant into Scrap.
	se.append(
		"items",
		{
			"item_code": loss_item,
			"qty": qty,
			"transfer_qty": qty,
			"conversion_factor": 1,
			"s_warehouse": None,
			"t_warehouse": scrap_wh,
			"uom": "Gram",
			"stock_uom": "Gram",
			"pcs": "1",
			"is_finished_item": 1,
			"set_basic_rate_manually": 1,
			"use_serial_batch_fields": 1,
		},
	)


def _ledger_row(tree, item_code):
	for md in tree.material_details:
		if md.item_code == item_code:
			return md
	return tree.append(
		"material_details",
		{
			"item_code": item_code,
			"issue_qty": 0,
			"receive_qty": 0,
			"loss_qty": 0,
			"pending_qty": 0,
		},
	)


def _recompute_pending(md):
	md.pending_qty = flt(md.issue_qty) - flt(md.receive_qty) - flt(md.loss_qty)


def _se_precision():
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def _reject_if_submitted(tree):
	"""A manually-submitted tree is locked: no further Issue / Receive is allowed."""
	if tree.get("status") == "Submitted":
		frappe.throw(
			_("Tree {0} is submitted (locked); no further Issue/Receive.").format(
				tree.name
			)
		)


# ---------------------------------------------------------------------------
# Public operations (called from the Tree Number controller methods)
# ---------------------------------------------------------------------------
def issue_material(tree, item_code, qty, source_warehouse=None):
	"""Issue `qty` of `item_code` from the Source WH into the tree's MSL WH."""
	frappe.has_permission("Tree Number", "write", tree, throw=True)
	_reject_if_submitted(tree)

	if not item_code:
		frappe.throw(_("Select an Item to issue."))
	qty = flt(qty, _se_precision())
	if qty <= 0:
		frappe.throw(
			_("Issue Qty must be greater than zero (precision {0}).").format(
				_se_precision()
			)
		)

	source_wh = _resolve_source_warehouse(tree, source_warehouse)
	msl_wh = tree.get("msl_warehouse") or _resolve_msl_warehouse(tree)
	if source_wh == msl_wh:
		frappe.throw(
			_("Source and MSL warehouse cannot be the same ({0}).").format(source_wh)
		)

	# Casting trees record the Issue as a MAIN SLIP transfer (relabel only — still ledger-invisible).
	se_type = (
		MATERIAL_TRANSFER_MAIN_SLIP if _is_casting_tree(tree) else MATERIAL_TRANSFER
	)
	se = _new_transfer_se(tree, se_type, company_wh=source_wh)
	_append_item(se, item_code, qty, source_wh, msl_wh)

	# Canonical lock order: series (pos 2) -> bins (pos 3) before the stock posting.
	preallocate_series_for_docs(se)
	lock_bins([(item_code, source_wh), (item_code, msl_wh)])

	_apply_fifo_batches_to_stock_entry(se)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	md = _ledger_row(tree, item_code)
	md.issue_qty = flt(md.issue_qty) + qty
	_recompute_pending(md)
	# Back-fill the resolved warehouses (None-safe: the fields may be unmigrated on a site
	# that hasn't run `bench migrate` yet; frappe ignores unknown-field sets on save).
	if not tree.get("source_warehouse"):
		tree.source_warehouse = source_wh
	if not tree.get("msl_warehouse"):
		tree.msl_warehouse = msl_wh
	tree.status = _tree_status(tree)
	tree.save(ignore_permissions=True)
	return se.name


def receive_material(tree, rows):
	"""Receive / book loss for a tree (`rows` = [{item_code, receive_qty, loss_qty}]).

	Posts up to TWO ledger-invisible Stock Entries (returns their names as a list):

	  * **Received** metal -> ``Material Transfer (MAIN SLIP)`` (casting) / ``Material Transfer``
	    (standalone): MSL -> Dept RM, same metal item.
	  * **Loss** -> a separate ``Process Loss`` (purpose Repack) SE that CONSUMES the metal at MSL
	    and PRODUCES the resolved ML loss variant into Dept Scrap (mirrors the Employee IR loss
	    engine — the loss item is converted, not moved as-is).

	Works for both standalone and casting trees. For a casting tree the Casting Employee IR Receive
	books the cast output first; this button then returns only the post-cast leftover — the per-item
	``(recv + loss) <= pending`` cap below can never exceed what physically remains in MSL, so it
	cannot double-count the Employee-IR receive.
	"""
	frappe.has_permission("Tree Number", "write", tree, throw=True)
	_reject_if_submitted(tree)

	if isinstance(rows, str):
		rows = json.loads(rows)
	if not rows:
		frappe.throw(_("Nothing to receive."))

	prec = _se_precision()
	eps = _pending_eps()
	by_item = {md.item_code: md for md in tree.material_details}

	# Validate + collect intent (no side effects yet).
	plan = []
	for r in rows:
		item = r.get("item_code")
		recv = flt(r.get("receive_qty"))
		loss = flt(r.get("loss_qty"))
		if not item or (recv <= 0 and loss <= 0):
			continue
		md = by_item.get(item)
		if not md:
			frappe.throw(
				_("Item {0} is not on this tree's Material Details.").format(item)
			)
		if (recv + loss) - flt(md.pending_qty) > eps:
			frappe.throw(
				_("Item {0}: Receive ({1}) + Loss ({2}) exceeds Pending ({3}).").format(
					item, recv, loss, md.pending_qty
				)
			)
		plan.append({"md": md, "item": item, "recv": recv, "loss": loss})

	if not plan:
		frappe.throw(_("Enter a Receive Qty or Loss Qty on at least one item."))

	msl_wh = tree.get("msl_warehouse") or _resolve_msl_warehouse(tree)
	has_recv = any(flt(p["recv"], prec) > 0 for p in plan)
	has_loss = any(flt(p["loss"], prec) > 0 for p in plan)
	dept_rm = _get_department_rm_warehouse(tree.department) if has_recv else None
	scrap_wh = _resolve_scrap_warehouse(tree.department) if has_loss else None

	# tree.employee: for casting trees a static copy of the Issue Employee IR's employee
	# (tree_casting.create_tree_on_issue); standalone trees carry the operator holding the MSL.
	# None-safe: an unset employee leaves se.employee blank.
	employee = tree.get("employee")

	se_recv = se_loss = None
	recv_pairs, loss_pairs = [], []

	# --- Received metal: Material Transfer [(MAIN SLIP) for casting] MSL -> Dept RM (same item).
	if has_recv:
		se_type = (
			MATERIAL_TRANSFER_MAIN_SLIP if _is_casting_tree(tree) else MATERIAL_TRANSFER
		)
		se_recv = _new_transfer_se(tree, se_type, company_wh=msl_wh)
		se_recv.employee = employee
		for p in plan:
			recv = flt(p["recv"], prec)
			if recv > 0:
				_append_item(se_recv, p["item"], recv, msl_wh, dept_rm)
				recv_pairs += [(p["item"], msl_wh), (p["item"], dept_rm)]

	# --- Loss: Process Loss Repack consuming metal @ MSL and producing the ML variant @ Scrap.
	if has_loss:
		se_loss = _new_repack_loss_se(tree, company_wh=msl_wh)
		se_loss.employee = employee
		for p in plan:
			loss = flt(p["loss"], prec)
			if loss > 0:
				loss_item = _resolve_tree_loss_item(tree, p["item"])
				_append_repack_loss_pair(
					se_loss, p["item"], loss_item, loss, msl_wh, scrap_wh
				)
				loss_pairs += [(p["item"], msl_wh), (loss_item, scrap_wh)]

	if not se_recv and not se_loss:
		frappe.throw(
			_("Receive / Loss quantities all round to zero at precision {0}.").format(
				prec
			)
		)

	# Canonical lock order (lock_order.py): ALL naming counters (pos 2) before ANY Bin (pos 3),
	# across BOTH SEs, in one deterministic sequence. preallocate_series_for_docs is None-safe.
	preallocate_series_for_docs(se_recv, se_loss)
	lock_bins(recv_pairs + loss_pairs)

	# Post the transfer FIRST, then compute the loss FIFO against post-transfer MSL stock so the
	# two legs never double-pick the same batch (all Bins stay locked across both submits).
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

	for p in plan:
		md = p["md"]
		md.receive_qty = flt(md.receive_qty) + p["recv"]
		md.loss_qty = flt(md.loss_qty) + p["loss"]
		_recompute_pending(md)
	if not tree.get("msl_warehouse"):
		tree.msl_warehouse = msl_wh
	tree.status = _tree_status(tree)
	tree.save(ignore_permissions=True)
	return [
		n for n in (getattr(se_recv, "name", None), getattr(se_loss, "name", None)) if n
	]


def cancel_tree_stock_entries(tree):
	"""Cancel every submitted tree-created SE (reversal/cleanup). Runs privileged (the whitelisted
	callers gate on Tree Number 'write', not Stock Entry 'cancel')."""
	tree_name = tree.name if hasattr(tree, "name") else tree
	names = frappe.db.get_all(
		"Stock Entry",
		{"custom_tree_number": tree_name, "auto_created": 1, "docstatus": 1},
		pluck="name",
	)
	for name in names:
		se = frappe.get_doc("Stock Entry", name)
		se.flags.ignore_permissions = True
		se.cancel()
	return names
