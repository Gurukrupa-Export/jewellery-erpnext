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
        ``Material Transfer`` SE and owns ``issue_qty``.
      - The Casting Employee IR Receive's ``update_tree_on_receive`` (``tree_casting.py``, called
        from ``employee_ir.py``) books the CAST OUTPUT into ``receive_qty`` / ``loss_qty``.
      - Receive Material (tree button) then RETURNS the post-cast LEFTOVER: received leg =
        ``Material Transfer`` (MSL -> Dept RM); loss leg = ``Process Loss`` Repack
        (metal @ MSL -> ML variant @ Dept Scrap); ``receive_qty`` / ``loss_qty`` +=. The per-item
        ``(recv + loss) <= pending`` cap makes it structurally leftover-only, so it can never
        re-receive what the Employee IR already booked.

Notes:
  * **Ledger-invisible SEs.** ``Material Transfer`` and ``Process Loss`` are both absent from
    MOP Settings' ``Stock Entry Type To Reservation``, so ``doc_events/stock_entry.onsubmit``
    skips reservation + MOP Log. ``auto_created = 1`` also bypasses the WORK-ORDER /
    metal-property validations in ``before_validate``.
  * ``se.company`` is derived from the warehouse being moved (not ``tree.company``) so a
    multi-company tree passes ``validate_warehouse_company``; ``se.manufacturer`` is set so the
    ``before_validate`` metal branch resolves without a Main Slip link.
  * Warehouse resolution reuses existing app resolvers (``Warehouse`` custom fields
    ``employee`` / ``department``); MSL is the employee's Raw-Material warehouse.
  * **Receive returns the batches the Issue put in.** The MSL warehouse is the *employee's*, so it
    pools metal from every tree, main slip and EIR injection for that operator — warehouse-wide
    FIFO would hand back whichever batch happens to be oldest, i.e. someone else's (a Customer
    Goods batch in, company metal out). ``receive_material`` therefore resolves each batch itself
    via ``_allocate_tree_batches`` and pre-stamps the rows, which makes
    ``_apply_fifo_batches_to_stock_entry`` a no-op for them (``_expand_source_rows_for_fifo``
    early-returns on a row that already carries ``batch_no``). ``issue_material`` keeps plain FIFO
    — sourcing fresh metal from the department pool is exactly what it should do.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
	capped_auto_batch_nos,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.ownership_priority import (
	allocate_in_order,
	batch_priority_map,
	batch_sort_key,
	describe_customer_spill,
	is_customer_rank,
	loss_rank,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking import (
	validate_no_prior_period_pending,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	_apply_fifo_batches_to_stock_entry,
	_ensure_posting_datetime,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
	_pending_eps,
	_tree_status,
	lock_tree,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	_get_department_rm_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins,
	preallocate_series_for_docs,
)

MATERIAL_TRANSFER = "Material Transfer"
MATERIAL_TRANSFER_MAIN_SLIP = "Material Transfer"
# Ledger-invisible Repack type used for the receive loss leg: it converts the metal into its
# ML loss variant. NOT the plain "Repack" type (that one IS in MOP Settings' reservation list and
# would create MOP Log / reservation). "Process Loss" (purpose Repack) is the same type the
# Employee IR loss engine uses and is absent from the reservation list.
PROCESS_LOSS = "Process Loss"


# ---------------------------------------------------------------------------
# Warehouse resolution
# ---------------------------------------------------------------------------
def _is_casting_tree(tree):
	"""EIR-seeded casting trees (employee_ir set): receive is Employee-IR-driven. Standalone trees
	(employee_ir empty) use the Receive button. Both post plain Material Transfer SEs."""
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
	# Needed for the before_validate metal branch to resolve manufacturer without a Main Slip.
	se.manufacturer = tree.get("manufacturer")
	se.custom_tree_number = tree.name
	se.auto_created = 1
	return se


def _stamp_batch(row, batch_no=None, inventory_type=None, customer=None):
	"""Stamp a pre-resolved batch (and its ownership) onto a row dict.

	``inventory_type`` is a ledger-enforced Inventory Dimension and is mandatory on Stock Entry
	Detail, so a caller that resolves the batch itself MUST carry the batch's ownership across —
	``_expand_source_rows_for_fifo`` only stamps it on the branch that picks the batch by FIFO,
	which a pre-stamped row deliberately skips.
	"""
	if batch_no:
		row["batch_no"] = batch_no
	if inventory_type:
		row["inventory_type"] = inventory_type
	if customer:
		row["customer"] = customer
	return row


def _append_item(
	se,
	item_code,
	qty,
	s_warehouse,
	t_warehouse,
	batch_no=None,
	inventory_type=None,
	customer=None,
):
	se.append(
		"items",
		_stamp_batch(
			{
				"item_code": item_code,
				"qty": qty,
				"s_warehouse": s_warehouse,
				"t_warehouse": t_warehouse,
				"uom": "Gram",
				"use_serial_batch_fields": 1,
			},
			batch_no,
			inventory_type,
			customer,
		),
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


def _append_repack_loss_pair(
	se,
	metal_item,
	loss_item,
	qty,
	msl_wh,
	scrap_wh,
	batch_no=None,
	inventory_type=None,
	customer=None,
):
	"""Append a consume(metal @ MSL) + produce(ML variant @ Scrap) row pair to a Repack SE.

	Produce-row flags mirror ``loss_stock_entry._build_combined_loss_se``:
	``set_basic_rate_manually`` opts the produce row out of ERPNext's Repack rate pooling (which
	would smear every FG row's cost across one shared rate). No ``basic_rate`` is set here --
	``CustomStockEntry.set_basic_rate`` assigns it centrally from the consumed rows once ERPNext
	has resolved their outgoing rates, so the metal's value moves onto the loss item instead of
	vanishing from the ledger (see ``customization/utils/loss_valuation``).

	``batch_no`` pre-resolves the consumed batch (see ``_tree_owed_batches``); the produce row is
	left without a ``batch_no`` so its ML batch is minted on submit — but it still carries the
	consumed batch's ownership. A customer's metal stays the customer's even after it is booked as
	loss, and the minted ML batch reads inventory_type/customer straight off this row
	(``Batch.update_inventory_dimentions``), so dropping them here silently converts customer stock
	into company stock. Ownership goes through ``normalize_ownership`` rather than being copied
	verbatim: a Customer Goods batch with no customer must degrade to Regular Stock, or the minted
	batch trips the "This item is not allowed as Customer Goods" guard and fails the submit.
	"""
	inventory_type, customer = normalize_ownership(
		inventory_type, customer, batch_no=batch_no, item_code=metal_item
	)
	# Consume: metal out of MSL (pre-stamped batch, or filled later by FIFO when omitted).
	se.append(
		"items",
		_stamp_batch(
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
			batch_no,
			inventory_type,
			customer,
		),
	)
	# Produce: ML loss variant into Scrap — same ownership, no batch (minted on submit).
	se.append(
		"items",
		_stamp_batch(
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
			None,
			inventory_type,
			customer,
		),
	)


# ---------------------------------------------------------------------------
# Batch provenance — Receive returns the batches the Issue put in
# ---------------------------------------------------------------------------
def _batch_ownership(batch_nos):
	"""``{batch_no: (inventory_type, customer)}`` — one round-trip for a whole allocation.

	Thin adapter over ``ownership_priority.batch_priority_map`` (which fetches the
	same columns plus ``creation`` for the FIFO tie-break). Kept as a named module
	attribute because the Tree Number suites patch it directly.
	"""
	ranks = batch_priority_map(batch_nos)
	return {b: (m.inventory_type, m.customer) for b, m in ranks.items()}


def _tree_owed_batches(se, tree, item_code, msl_wh):
	"""``[(batch_no, qty)]`` this tree still owes back at ``msl_wh``, in FIFO order.

	The tree's own Stock Entries ARE the record — ``custom_tree_number`` is stamped on every leg
	(``_new_transfer_se``) — so the pool is derived on the fly rather than cached in a field that
	could drift: per batch, ``issued into MSL - already taken back out of MSL``. Only
	``docstatus = 1`` counts, so a reversed tree (``cancel_tree_stock_entries``) drops out of the
	netting for free.

	The netted result is then capped at what is PHYSICALLY left of each batch at MSL. That cap is
	load-bearing, not defensive: the MSL warehouse is the *employee's* (shared by every tree, main
	slip and EIR injection for that operator), and the casting Employee IR's gain injection draws
	this tree's own metal out of it without stamping ``custom_tree_number``. Casting mints no new
	batch at MSL, so the leftover genuinely IS the issued batch — only its quantity moves.

	The loss leg's produce row can never be double-counted: it carries no ``s_warehouse`` and its
	item is the ML variant, so the warehouse and ``item_code`` predicates both exclude it.
	"""
	rows = frappe.db.sql(
		"""
		SELECT sed.batch_no, sed.s_warehouse, sed.t_warehouse, sed.qty
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.custom_tree_number = %(tree)s
		  AND se.docstatus = 1
		  AND sed.item_code = %(item_code)s
		  AND IFNULL(sed.batch_no, '') != ''
		  AND (sed.t_warehouse = %(msl)s OR sed.s_warehouse = %(msl)s)
		""",
		{"tree": tree.name, "item_code": item_code, "msl": msl_wh},
		as_dict=True,
	)

	owed = {}
	for r in rows:
		qty = flt(r.qty)
		if r.t_warehouse == msl_wh:
			owed[r.batch_no] = flt(owed.get(r.batch_no)) + qty
		if r.s_warehouse == msl_wh:
			owed[r.batch_no] = flt(owed.get(r.batch_no)) - qty

	eps = _pending_eps()
	owed = {b: q for b, q in owed.items() if q > eps}
	if not owed:
		return []

	# `qty` is deliberately NOT passed: it would FIFO-truncate the availability list before the
	# owed cap below can apply. `batch_no` (honoured as a list) restricts the pick to this tree's
	# batches; capped_auto_batch_nos still orders by Batch.creation and drops orphan-SBB phantoms.
	_ensure_posting_datetime(se)
	available = (
		capped_auto_batch_nos(
			frappe._dict(
				posting_date=se.posting_date,
				posting_time=se.posting_time,
				item_code=item_code,
				warehouse=msl_wh,
				batch_no=list(owed),
				for_stock_levels=False,
				consider_negative_batches=False,
			)
		)
		or []
	)

	out = []
	for b in available:
		qty = min(flt(owed.get(b.batch_no)), flt(b.qty))
		if qty > eps:
			out.append((b.batch_no, qty))

	# Ownership tier first, then the Batch.creation FIFO order capped_auto_batch_nos
	# already produced. Consuming order: the customer's own metal comes back before
	# the company's. The loss leg re-sorts this same pool the other way round --
	# see _allocate_tree_legs.
	ranks = batch_priority_map([b for b, _q in out])
	out.sort(key=lambda bq: batch_sort_key(bq[0], ranks.get(bq[0]), "consume"))
	return out


def _allocate_tree_batches(se, tree, item_code, msl_wh, need):
	"""``[(batch_no, qty)]`` covering ``need`` from this tree's own owed batches, else throw.

	Fail-fast rather than falling back to warehouse-wide FIFO: the MSL pool holds other operators'
	and other customers' metal, so a fallback would silently hand back material this tree never
	issued — the exact defect this function exists to prevent.
	"""
	prec = _se_precision()
	eps = _pending_eps()
	pool = _tree_owed_batches(se, tree, item_code, msl_wh)

	out = []
	remaining = flt(need, prec)
	for batch_no, avail in pool:
		if remaining <= eps:
			break
		take = flt(min(flt(avail), remaining), prec)
		if take <= 0:
			continue
		out.append((batch_no, take))
		remaining = flt(remaining - take, prec)

	if remaining > eps:
		frappe.throw(
			_(
				"Tree {0}, Item {1}: need {2} but only {3} of the batches this tree issued into "
				"{4} is still available ({5}). Returning any other batch would hand back material "
				"this tree never issued — check whether it was already drawn out for another job."
			).format(
				tree.name,
				item_code,
				flt(need, prec),
				flt(flt(need, prec) - remaining, prec),
				msl_wh,
				", ".join(f"{b}: {flt(q, prec)}" for b, q in pool) or _("none"),
			)
		)
	return out


def _allocate_tree_legs(se, tree, item_code, msl_wh, recv, loss):
	"""``(recv_allocation, loss_allocation)`` — two ranked passes over ONE pool.

	The receive leg and the loss leg want OPPOSITE orderings of the same batches:
	the customer's metal should go back to them first, while wastage should be
	written off against the company's. A single ordered walk cannot satisfy both.
	Allocating ``recv + loss`` in one pass and slicing it -- which is what this
	flow used to do -- makes the loss leg inherit whatever the receive leg did not
	take, so with customer-first ordering the customer's gold ends up scrapped
	whenever the pool's customer qty covers the combined need. ``Tree Number.submit_tree``
	settles leftovers with ``recv = 0``, which is exactly that case.

	So: pass 1 takes ``recv`` from the pool in ``consume_rank`` order, pass 2 takes
	``loss`` from what is LEFT, re-sorted into ``loss_rank`` order. A shared
	``taken`` ledger keeps the two passes from double-booking a batch.
	"""
	prec = _se_precision()
	pool = _tree_owed_batches(se, tree, item_code, msl_wh)
	ranks = batch_priority_map([b for b, _q in pool], with_no_wastage=True)
	taken = {}

	recv_alloc, recv_short = allocate_in_order(pool, recv, prec, taken=taken)
	loss_pool = sorted(
		pool, key=lambda bq: batch_sort_key(bq[0], ranks.get(bq[0]), "loss")
	)
	loss_alloc, loss_short = allocate_in_order(loss_pool, loss, prec, taken=taken)

	shortfall = flt(recv_short + loss_short, prec)
	if shortfall > _pending_eps():
		need = flt(flt(recv, prec) + flt(loss, prec), prec)
		_throw_tree_shortfall(tree, item_code, msl_wh, need, shortfall, pool, prec)

	return recv_alloc, loss_alloc, ranks


def _warn_tree_customer_loss(customer_loss, prec):
	"""ONE orange warning when a tree write-off lands on customer-owned metal.

	Mirrors the Employee IR spill warning: the loss pass only reaches a customer
	batch once the company's are exhausted, which is allowed but must be visible.
	Never throws -- the single hard stop is the no-wastage check at the call site.
	"""
	if not customer_loss:
		return
	merged = {}
	for row in customer_loss:
		key = (row["customer"], row["item_code"], row["batch_no"])
		merged[key] = flt(merged.get(key, 0) + flt(row["qty"]), prec)
	total = flt(sum(merged.values()), prec)
	lines = describe_customer_spill(
		[
			{"customer": c, "item_code": i, "batch_no": b, "qty": q}
			for (c, i, b), q in sorted(merged.items(), key=lambda kv: str(kv[0]))
		],
		precision=prec,
	)
	frappe.msgprint(
		_(
			"Company metal on this tree could not absorb the whole loss, so {0} g was "
			"written off against customer-owned material:"
		).format(frappe.bold(total))
		+ "<br><br>"
		+ "<br>".join(lines),
		title=_("Customer Material Absorbed Loss"),
		indicator="orange",
	)


def _throw_tree_shortfall(tree, item_code, msl_wh, need, shortfall, pool, prec):
	"""Fail fast rather than falling back to warehouse-wide FIFO.

	The MSL pool holds other operators' and other customers' metal, so a fallback
	would silently hand back material this tree never issued.
	"""
	frappe.throw(
		_(
			"Tree {0}, Item {1}: need {2} but only {3} of the batches this tree issued into "
			"{4} is still available ({5}). Returning any other batch would hand back material "
			"this tree never issued — check whether it was already drawn out for another job."
		).format(
			tree.name,
			item_code,
			flt(need, prec),
			flt(need - shortfall, prec),
			msl_wh,
			", ".join(f"{b}: {flt(q, prec)}" for b, q in pool) or _("none"),
		)
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
	"""Delegate to the canonical ledger arithmetic (single formula for all four paths)."""
	return tree_balance.recompute_row_pending(md)


def _se_precision():
	return tree_balance.qty_precision()


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
	# Parent control row before tabSeries / tabBin (lock_order canonical sequence).
	lock_tree(tree.name)
	_reject_if_submitted(tree)

	if not item_code:
		frappe.throw(_("Select an Item to issue."))

	# One crucible melts one alloy: the item must carry the tree's own metal type, touch and
	# purity. Checked HERE, before any Stock Entry is built, because these SEs are stamped
	# auto_created=1 and that deliberately bypasses the metal-property validation in
	# doc_events/stock_entry.py -- so nothing downstream would catch a mis-picked metal. Left
	# unchecked it is worse than a bad transfer: _ledger_row would open a brand-new
	# material_details line for the foreign item.
	tree_balance.validate_item_matches_tree_metal(tree, item_code)

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

	# Month-start close: this is one of only two paths that move stock INTO an
	# employee MSL warehouse, so it is one of only two that can carry an unsettled
	# balance across a month boundary. Runs before any Series/Bin lock below, so a
	# rejection holds nothing.
	validate_no_prior_period_pending(msl_wh)

	# Both tree kinds record the Issue as a plain Material Transfer (ledger-invisible).
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

	  * **Received** metal -> ``Material Transfer``: MSL -> Dept RM, same metal item.
	  * **Loss** -> a separate ``Process Loss`` (purpose Repack) SE that CONSUMES the metal at MSL
	    and PRODUCES the resolved ML loss variant into Dept Scrap (mirrors the Employee IR loss
	    engine — the loss item is converted, not moved as-is).

	Works for both standalone and casting trees. For a casting tree the Casting Employee IR Receive
	books the cast output first; this button then returns only the post-cast leftover — the per-item
	``(recv + loss) <= pending`` cap below can never exceed what physically remains in MSL, so it
	cannot double-count the Employee-IR receive.
	"""
	frappe.has_permission("Tree Number", "write", tree, throw=True)
	# Parent control row before tabSeries / tabBin (lock_order canonical sequence).
	lock_tree(tree.name)
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

	# --- Received metal: Material Transfer MSL -> Dept RM (same item).
	if has_recv:
		se_type = (
			MATERIAL_TRANSFER_MAIN_SLIP if _is_casting_tree(tree) else MATERIAL_TRANSFER
		)
		se_recv = _new_transfer_se(tree, se_type, company_wh=msl_wh)
		se_recv.employee = employee

	# --- Loss: Process Loss Repack consuming metal @ MSL and producing the ML variant @ Scrap.
	if has_loss:
		se_loss = _new_repack_loss_se(tree, company_wh=msl_wh)
		se_loss.employee = employee

	if not se_recv and not se_loss:
		frappe.throw(
			_("Receive / Loss quantities all round to zero at precision {0}.").format(
				prec
			)
		)

	# Warehouse pairs come from the plan, not from the allocation below: every batch of an item
	# shares the item's warehouses, so the Bins to lock are known before any batch is resolved.
	loss_items = {}
	for p in plan:
		if flt(p["recv"], prec) > 0:
			recv_pairs += [(p["item"], msl_wh), (p["item"], dept_rm)]
		if flt(p["loss"], prec) > 0:
			loss_items[p["item"]] = _resolve_tree_loss_item(tree, p["item"])
			loss_pairs += [(p["item"], msl_wh), (loss_items[p["item"]], scrap_wh)]

	# Canonical lock order (lock_order.py): ALL naming counters (pos 2) before ANY Bin (pos 3),
	# across BOTH SEs, in one deterministic sequence. preallocate_series_for_docs is None-safe.
	# Locking before the allocation also keeps the batch-availability read behind it stable.
	preallocate_series_for_docs(se_recv, se_loss)
	lock_bins(recv_pairs + loss_pairs)

	# Return what this tree issued. Both legs draw from ONE pool of the tree's own
	# owed batches via a shared `taken` ledger, so they can never book the same
	# batch qty twice — but each leg is ordered for its OWN rule: the customer's
	# metal is handed back first, the company's is written off first. Every row
	# carries its batch's ownership (inventory_type / customer) across with it.
	customer_loss = []
	for p in plan:
		rem_recv = flt(p["recv"], prec)
		rem_loss = flt(p["loss"], prec)
		if flt(rem_recv + rem_loss, prec) <= 0:
			continue
		recv_alloc, loss_alloc, ranks = _allocate_tree_legs(
			se_recv or se_loss, tree, p["item"], msl_wh, rem_recv, rem_loss
		)

		for batch_no, qty in recv_alloc:
			meta = ranks.get(batch_no)
			inv, cust = normalize_ownership(
				meta.inventory_type if meta else None,
				meta.customer if meta else None,
				batch_no=batch_no,
				item_code=p["item"],
			)
			_append_item(
				se_recv,
				p["item"],
				qty,
				msl_wh,
				dept_rm,
				batch_no=batch_no,
				inventory_type=inv,
				customer=cust,
			)

		for batch_no, qty in loss_alloc:
			meta = ranks.get(batch_no)
			# No wastage for customer-supplied material. Such batches rank LAST in
			# the loss pass, so reaching one means nothing else this tree owes had
			# any capacity left — the throw is the correct outcome, not a side
			# effect of proportional spreading.
			if meta and meta.no_wastage:
				frappe.throw(
					_(
						"Tree {0}, Item {1}: no wastage is allowed for customer material "
						"(batch {2}, customer {3}). Return the full weight so no loss is "
						"booked; the unused metal goes back as raw material."
					).format(tree.name, p["item"], batch_no, meta.customer)
				)
			inv, cust = normalize_ownership(
				meta.inventory_type if meta else None,
				meta.customer if meta else None,
				batch_no=batch_no,
				item_code=p["item"],
			)
			if is_customer_rank(loss_rank(inv)):
				customer_loss.append(
					{
						"customer": cust,
						"item_code": p["item"],
						"batch_no": batch_no,
						"qty": qty,
					}
				)
			_append_repack_loss_pair(
				se_loss,
				p["item"],
				loss_items[p["item"]],
				qty,
				msl_wh,
				scrap_wh,
				batch_no=batch_no,
				inventory_type=inv,
				customer=cust,
			)

	_warn_tree_customer_loss(customer_loss, prec)

	# Post the transfer FIRST so both legs' consumption lands in a stable order (all Bins stay
	# locked across both submits). The FIFO helper is a structural no-op now that every source row
	# carries a batch, but it still normalises each SE's posting datetime.
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
