"""Process Loss Stock Entry creation for Employee IR.

On Receive submit: for each row in employee_loss_details and
manually_book_loss_details with proportionally_loss > 0, creates a
"Process Loss" (Repack purpose) Stock Entry that moves the loss quantity
from the SRE source warehouse to either:
  - Scrap warehouse by department  (is_raw_material = 0)
  - Employee / Subcontractor Raw Material warehouse  (is_raw_material = 1)

Before the SE is submitted, each matching Stock Reservation Entry that still holds a
reservation is cancelled and recreated with reduced reserved_qty, so the loss quantity
is free for the ledger entry. Reservations already spent upstream (delivered/consumed/
transferred == reserved, e.g. consumed by a Product Certification) are left alone: they
hold no stock in ERPNext's Bin formula, so there is nothing to release.

Cancel path: cancels all Process Loss SEs owned by this EIR and restores the
original SRE reserved quantities via custom_replaced_sre_snapshot (JSON).
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, nowtime, today

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	resolve_batch_ownership,
)

PROCESS_LOSS_SE_TYPE = "Process Loss"
CHILD_TABLE_EMPLOYEE = "employee_loss_details"
CHILD_TABLE_MANUAL = "manually_book_loss_details"

# Float noise guard for reservation/stock comparisons. Same value and intent as
# pc_tagging_stock_sync.TOLERANCE / serial_number_creator.TOLERANCE.
TOLERANCE = 0.0001


def _sre_remaining(sre):
	"""Reservation an SRE still holds, in stock UOM.

	Mirrors ERPNext's own Bin formula
	(``reserved_qty - delivered_qty - transferred_qty - consumed_qty``, see
	``erpnext/stock/doctype/bin/bin.py`` update_reserved_stock and
	``stock_ledger.get_reserved_stock``). A submitted-but-Delivered SRE therefore holds
	ZERO reserved stock: it blocks nothing and there is nothing left to release.

	Accepts either a dict row (from ``_query_batch_and_qty_sres``) or a loaded Document.
	"""
	get = sre.get if hasattr(sre, "get") else lambda key: getattr(sre, key, 0)
	return flt(
		flt(get("reserved_qty"))
		- flt(get("delivered_qty"))
		- flt(get("transferred_qty"))
		- flt(get("consumed_qty")),
		3,
	)


def _physical_batch_qty(item_code, batch_no, warehouse):
	"""Physical SBB qty of (item, batch) at ``warehouse``, ignoring reservations.

	Thin re-export of the shared helper in ``pc_tagging_stock_sync`` so this module has a
	single import site. In v16 real per-batch stock lives in Serial and Batch Bundle, not
	``tabStock Ledger Entry.batch_no`` (which is NULL) — ``get_batch_qty`` handles that.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
		_physical_batch_qty as _shared_physical_batch_qty,
	)

	return _shared_physical_batch_qty(item_code, batch_no, warehouse)


def _warehouses_with_physical_batch(item_code, batch_no):
	"""``[(warehouse, qty)]`` for warehouses physically holding the batch, qty desc.

	Shared with ``pc_tagging_stock_sync``; used for fail-fast diagnostics only.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
		_warehouses_with_physical_batch as _shared_warehouses_with_physical_batch,
	)

	return _shared_warehouses_with_physical_batch(item_code, batch_no)


def is_no_wastage_customer(customer):
	"""True when ``customer`` is flagged ``custom_no_wastage`` — customer-supplied
	material must not be charged manufacturing loss (the unused weight is returned as
	raw material instead of becoming a customer-owned scrap batch)."""
	return bool(customer) and bool(
		frappe.db.get_value("Customer", customer, "custom_no_wastage")
	)


def batch_owner_no_wastage(batch_no):
	"""True when ``batch_no`` is owned by a no-wastage customer (keyed on the batch's
	``custom_customer``, the same ownership source as ``_resolve_batch_inventory``)."""
	if not batch_no:
		return False
	return is_no_wastage_customer(
		frappe.db.get_value("Batch", batch_no, "custom_customer")
	)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def create_loss_stock_entries(eir):
	"""Create ONE Process Loss SE covering ALL loss rows across the entire EIR.

	Called once after the operations loop in on_submit_receive.
	All SREs are reduced before the SE is submitted so ERPNext does not
	block consumption due to reserved stock.
	"""
	# Idempotency: skip if a Process Loss SE already exists for this EIR.
	if frappe.db.exists(
		"Stock Entry",
		{
			"employee_ir": eir.name,
			"stock_entry_type": PROCESS_LOSS_SE_TYPE,
			"auto_created": 1,
			"docstatus": ["!=", 2],
		},
	):
		return

	# Collect and validate all loss rows across both child tables.
	pending = []

	for row in eir.employee_loss_details:
		entry = _prepare_loss_row(eir, row, CHILD_TABLE_EMPLOYEE)
		if entry:
			pending.append(entry)

	for row in eir.manually_book_loss_details:
		entry = _prepare_loss_row(eir, row, CHILD_TABLE_MANUAL)
		if entry:
			pending.append(entry)

	if not pending:
		return

	# RULE A (canonical lock order): process losses in (item_code, warehouse, batch_no)
	# order so both the SRE reductions below and the combined SE's consume/produce rows
	# lock shared Bins in the same deterministic sequence across concurrent Process-Loss
	# submits. Each loss row is independent (own SRE + own scrap produce), so reordering
	# does not change the net stock effect.
	from jewellery_erpnext.jewellery_erpnext.lock_order import stock_lock_key

	pending.sort(
		key=lambda e: stock_lock_key(
			e["row"].item_code,
			getattr(e["sre_doc"], "warehouse", None),
			e["row"].batch_no,
		)
	)

	# Reduce all SREs first so stock is free for the ledger entry. Rows whose
	# reservation was already spent upstream (Product Certification consumes SREs via
	# consume_stock_reservation_entry) hold no reserved stock at all, so nothing blocks
	# the loss SE and there is nothing to release -- reducing them would only rebuild a
	# reservation ERPNext then rejects ("Reserved Qty should be greater than Delivered
	# Qty", stock_reservation_entry.validate_with_allowed_qty).
	for entry in pending:
		if not entry.get("needs_sre_reduction"):
			continue
		_reduce_sre(
			eir, entry["row"], entry["sre_doc"], entry["qty"], entry["table_name"]
		)

	# Build and submit ONE SE with all consume/produce row pairs.
	se = _build_combined_loss_se(eir, pending)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()


def cancel_loss_stock_entries(eir):
	"""Cancel all Process Loss SEs created by this EIR and restore SREs.

	Called at the start of on_submit_receive(cancel=True) before MOP Log flip.
	"""
	se_names = frappe.db.get_all(
		"Stock Entry",
		{
			"employee_ir": eir.name,
			"stock_entry_type": PROCESS_LOSS_SE_TYPE,
			"auto_created": 1,
			"docstatus": 1,
		},
		pluck="name",
	)
	if se_names:
		_assert_no_orphaned_reductions(eir, se_names)

	for se_name in se_names:
		frappe.get_doc("Stock Entry", se_name).cancel()

	_restore_reduced_sres(eir)


def _assert_no_orphaned_reductions(eir, se_names):
	"""Refuse to cancel when this EIR's reductions can no longer be undone.

	``custom_replaced_sre_snapshot`` was referenced by ``_reduce_sre`` long before any
	patch created the column. Assigning an unknown fieldname to a Document is not an
	error -- ``get_valid_dict()`` filters it out -- so on every site but ``gk`` the
	snapshot was silently dropped and the reduction left no trace.

	Cancelling such an EIR would cancel its Process Loss Stock Entries (returning the
	loss quantity to stock) while the reservations stay permanently reduced. Stopping is
	the honest outcome: the reservation has to be corrected by hand.

	The check is narrow on purpose. Finding no markers is *legitimate* when every SRE was
	already spent (``_sre_remaining <= TOLERANCE``) or fully consumed by the loss
	(``new_qty <= TOLERANCE``) -- in both cases ``_reduce_sre`` returns without creating a
	replacement, so there is genuinely nothing to restore. Only the real orphan signature
	throws: a cancelled reservation replaced, within the same instant, by a still-live one
	that carries neither marker.
	"""
	if _restore_marker_count(eir):
		return

	window = frappe.db.sql(
		"""
        SELECT MIN(creation), MAX(creation)
        FROM `tabStock Entry`
        WHERE name IN %(se_names)s
        """,
		{"se_names": tuple(se_names)},
	)
	if not window or not window[0][0]:
		return
	start, end = window[0]

	orphan = frappe.db.sql(
		"""
        SELECT c.name, n.name
        FROM `tabStock Reservation Entry` c
        JOIN `tabStock Reservation Entry` n
          ON n.item_code = c.item_code
         AND n.warehouse = c.warehouse
         AND n.voucher_no = c.voucher_no
         AND n.voucher_detail_no = c.voucher_detail_no
         AND n.docstatus = 1
         AND n.creation BETWEEN c.modified AND c.modified + INTERVAL 5 SECOND
         AND COALESCE(n.employee_ir, '') = ''
         AND COALESCE(n.custom_replaced_sre_snapshot, '') = ''
        WHERE c.docstatus = 2
          AND c.modified BETWEEN %(start)s AND %(end)s
        LIMIT 1
        """,
		{"start": start, "end": end},
	)
	if not orphan:
		return

	frappe.throw(
		_(
			"Employee IR {0} cannot be cancelled automatically. Its Process Loss entries "
			"reduced Stock Reservation Entries before this site recorded how to restore "
			"them (for example {1}, replaced by {2}), so cancelling would release the "
			"loss quantity while leaving the reservation permanently short. Restore the "
			"affected reservations manually, then cancel."
		).format(eir.name, orphan[0][0], orphan[0][1])
	)


def _restore_marker_count(eir):
	"""Count reservations this EIR can still be unwound from (see _restore_reduced_sres)."""
	return frappe.db.sql(
		"""
        SELECT COUNT(*)
        FROM `tabStock Reservation Entry`
        WHERE docstatus = 1
          AND (
            employee_ir = %(eir)s
            OR custom_replaced_sre_snapshot LIKE %(legacy)s
          )
        """,
		{"eir": eir.name, "legacy": f'%"employee_ir": "{eir.name}"%'},
	)[0][0]


# ---------------------------------------------------------------------------
# Per-row preparation (validate + resolve, no side effects)
# ---------------------------------------------------------------------------


def _prepare_loss_row(eir, row, table_name):
	"""Validate a single loss row and return a dict of resolved data.

	Returns None when proportionally_loss <= 0.
	Raises on any missing mandatory field or unresolvable reference.

	Mostly read-only, with two documented edge-case side effects: it logs an Error Log for
	a sub-precision loss row, and ``_find_sre`` may self-heal an EOD-orphaned WIP reservation
	(re-creating the missing Stock Reservation Entry at the batch's physical warehouse) before
	the batch can be resolved.
	"""
	# Gate at the Stock Entry Detail's ACTUAL transfer_qty precision so a loss that survives
	# here is guaranteed representable when ERPNext's set_transfer_qty() recomputes
	# flt(qty * conversion_factor, precision("transfer_qty")). The intended precision is 3
	# (property_setter/stock_entry_detail.json, provisioned by property_setter_guard); we read
	# it live rather than hardcoding 3 so this stays correct if float_precision / the property
	# setter ever change. A row below that precision would round to 0 in the SE and hard-crash
	# the WHOLE Employee IR submit on se.insert() ("Qty in Stock UOM can not be zero.") -- skip
	# it loudly instead. Once precision == 3, flt(0.001, 3) = 0.001 > 0, so real sub-0.01 g
	# losses are kept; this only fires for genuinely sub-representable rows.
	se_precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3
	qty = flt(row.proportionally_loss, se_precision)
	if qty <= 0:
		if flt(row.proportionally_loss) > 0:
			# Dropping is consistent: no pending entry -> no SRE reduction, no SE row (each loss
			# row is independent). Log (operational + Error Log) so it is never silent.
			msg = (
				"Employee IR {0} {1} row {2}: proportionally_loss={3} g rounds to 0 at "
				"Stock Entry Detail transfer_qty precision {4}; row skipped so se.insert() does "
				"not abort the EIR submit. item={5} batch={6}".format(
					eir.name,
					table_name,
					row.idx,
					row.proportionally_loss,
					se_precision,
					row.item_code,
					row.batch_no,
				)
			)
			frappe.logger().warning("create_loss_stock_entries: " + msg)
			frappe.log_error(
				title="Employee IR sub-precision loss row skipped", message=msg
			)
		return None

	# No wastage for customer-supplied material: a representable loss on a no-wastage
	# customer's batch must never be booked (it would mint a customer-owned scrap
	# batch). Blocks any path — including programmatic/API — that reaches SE creation.
	if batch_owner_no_wastage(row.batch_no):
		frappe.throw(
			_(
				"Employee IR {0}: no wastage is allowed for customer material "
				"(batch {1}, item {2}). Received weight must equal issued weight so no "
				"loss is booked; the unused metal is returned as raw material."
			).format(eir.name, row.batch_no, row.item_code)
		)

	if not row.item_code:
		frappe.throw(
			_("Employee IR {0}, {1} row {2}: item_code is required").format(
				eir.name, table_name, row.idx
			)
		)
	if not row.batch_no:
		frappe.throw(
			_(
				"Employee IR {0}, {1} row {2}: batch_no is required for Process Loss"
			).format(eir.name, table_name, row.idx)
		)

	mwo = _resolve_mwo(eir, row, table_name)
	sre_doc, candidates = _find_sre(eir, row, mwo, table_name, qty)
	t_warehouse = _resolve_t_warehouse(eir, table_name)
	loss_item = _resolve_loss_item(eir, row, table_name)

	_validate_sre_qty(eir, row, sre_doc, candidates, qty, table_name)

	inventory_type, customer = _resolve_batch_inventory(row)

	return {
		"row": row,
		"table_name": table_name,
		"qty": qty,
		"mwo": mwo,
		"sre_doc": sre_doc,
		# False when the reservation was already spent upstream (e.g. consumed by a
		# Product Certification): it holds no stock, so there is nothing to release.
		"needs_sre_reduction": _sre_remaining(sre_doc) > TOLERANCE,
		"s_warehouse": sre_doc.warehouse,
		"t_warehouse": t_warehouse,
		"loss_item": loss_item,
		"inventory_type": inventory_type,
		"customer": customer,
	}


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _resolve_mwo(eir, row, table_name):
	mwo = getattr(row, "manufacturing_work_order", None)
	if not mwo:
		frappe.throw(
			_(
				"Employee IR {0}, {1} row {2}: manufacturing_work_order is required"
			).format(eir.name, table_name, row.idx)
		)
	return mwo


def _resolve_batch_inventory(row):
	"""Resolve ``(inventory_type, customer)`` from the loss row's SOURCE batch.

	The Process Loss SE is built with ``auto_created = 1``, so neither
	``CustomStockEntry.update_batches`` (the interactive Batch backfill,
	customization/stock_entry/stock_entry.py) nor the FIFO resolver runs for it.
	The only thing that touches inventory_type on this SE is the fill-if-empty
	stamp in Stock Entry ``before_validate`` (doc_events/stock_entry.py), which
	defaults any *blank* row to "Regular Stock". The loss rows arrive blank
	(validate_process_loss appends employee_loss_details with inventory_type
	commented out), so every row was being booked as "Regular Stock" even when
	the metal came out of a Customer Goods batch.

	The loss physically leaves ``row.batch_no``, so the ledger row -- and the
	scrap batch minted from the produce row (Batch.update_inventory_dimentions
	copies inventory_type/customer off the SE Detail row) -- must carry THAT
	batch's inventory type. Setting the value here (before ``se.insert()``)
	pre-empts the before_validate stamp, which only fires
	``if not row.inventory_type``.

	The rules themselves live in ``customization/utils/row_ownership`` so the
	tree and warehouse loss builders resolve ownership identically.
	"""
	return resolve_batch_ownership(row)


_SRE_LOOKUP_FIELDS = [
	"name",
	"warehouse",
	"reserved_qty",
	"delivered_qty",
	"transferred_qty",
	"consumed_qty",
	"available_qty",
	"voucher_qty",
	"reservation_based_on",
	"has_batch_no",
	"company",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"stock_uom",
	"manufacturing_operation",
]


def _query_batch_and_qty_sres(mwo, item_code, batch_no):
	"""Return submitted (docstatus=1) SREs for (mwo, item, batch).

	Prefer a batch-level match via the Serial and Batch Entry child table; fall back to a
	Qty-based reservation (no sb_entries) if the batch join returns nothing. Shared by the
	initial lookup and the post-heal re-query in ``_find_sre``.
	"""
	rows = frappe.db.sql(
		"""
        SELECT
            sre.name,
            sre.warehouse,
            sre.reserved_qty,
            sre.delivered_qty,
            sre.transferred_qty,
            sre.consumed_qty,
            sre.available_qty,
            sre.voucher_qty,
            sre.reservation_based_on,
            sre.has_batch_no,
            sre.company,
            sre.voucher_type,
            sre.voucher_no,
            sre.voucher_detail_no,
            sre.stock_uom,
            sre.manufacturing_operation
        FROM `tabStock Reservation Entry` sre
        INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
        WHERE sre.manufacturing_work_order = %s
          AND sre.item_code = %s
          AND sbe.batch_no = %s
          AND sre.docstatus = 1
        """,
		(mwo, item_code, batch_no),
		as_dict=True,
	)

	if not rows:
		# Fallback: Qty-based reservation (reservation_based_on = "Qty")
		rows = frappe.db.get_all(
			"Stock Reservation Entry",
			{
				"manufacturing_work_order": mwo,
				"item_code": item_code,
				"docstatus": 1,
			},
			_SRE_LOOKUP_FIELDS,
		)

	return rows


def get_batch_sre_headroom(mwo, batch_nos):
	"""``{(item_code, batch_no): largest single remaining SRE}`` for ``mwo``.

	The most loss ``_validate_sre_qty`` will let a single row book against a batch.
	That guard deliberately does NOT aggregate across the batch's SREs -- it throws
	when the row's qty exceeds the largest *one* of them -- so this is the real
	per-row ceiling, and the loss waterfall needs it up front.

	Why it matters: the old flat split spread loss thinly over every batch, so a
	row's share was almost always well under its reservation. The waterfall
	concentrates the whole loss on the Regular Stock tier, which can push one row
	past a reservation that is legitimately split across several operation-tagged
	SREs (see ``_find_sre``). Capping the tier's capacity here makes the excess
	spill to the next tier instead of failing an Employee IR that submits today.

	One round-trip for the whole document. A batch with no batch-level SRE (the
	Qty-based fallback) is absent from the result, and the caller then applies no
	cap -- preserving today's behaviour rather than guessing.
	"""
	batch_nos = sorted({b for b in (batch_nos or []) if b})
	if not mwo or not batch_nos:
		return {}

	rows = frappe.db.sql(
		"""
        SELECT
            sre.item_code AS item_code,
            sbe.batch_no AS batch_no,
            MAX(
                sre.reserved_qty
                - IFNULL(sre.delivered_qty, 0)
                - IFNULL(sre.transferred_qty, 0)
                - IFNULL(sre.consumed_qty, 0)
            ) AS headroom
        FROM `tabStock Reservation Entry` sre
        INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
        WHERE sre.manufacturing_work_order = %(mwo)s
          AND sre.docstatus = 1
          AND sbe.batch_no IN %(batches)s
        GROUP BY sre.item_code, sbe.batch_no
        """,
		{"mwo": mwo, "batches": batch_nos},
		as_dict=True,
	)
	return {
		(r["item_code"], r["batch_no"]): flt(r["headroom"], 3)
		for r in rows
		if flt(r["headroom"]) > TOLERANCE
	}


def _find_sre(eir, row, mwo, table_name, qty):
	"""Return ``(sre_doc, candidates)`` for this loss row.

	A batch's reservation legitimately spans multiple operation-tagged SREs in
	the SAME warehouse (SRE = physical truth; the bulk metal arrives via one
	operation-tagged Stock Entry while small increments arrive via others, and
	Employee IR logical moves never re-tag reservations). Loss must therefore be
	deducted against the batch's reservation as a whole, not the SRE that merely
	carries the current operation's tag.

	Selection:
	  * Prefer a batch-level match via the Serial and Batch Entry child table;
	    fall back to a Qty-based reservation (no sb_entries) if the batch join
	    returns nothing.
	  * Prefer ACTIVE reservations (``_sre_remaining`` > TOLERANCE). A submitted SRE
	    whose delivered/consumed/transferred qty already covers reserved_qty holds no
	    stock (ERPNext's Bin formula nets to zero), so it neither blocks the loss SE
	    nor has anything left to release.
	  * Restrict candidates to a SINGLE warehouse so we never deduct across
	    physical locations: the warehouse of the operation-matched SRE if one
	    exists, else the warehouse holding the largest reserved_qty.
	  * Within that warehouse pick the SRE that can COVER the loss, preferring
	    the current operation's SRE, then the largest. If none individually
	    covers the loss, return the largest so ``_validate_sre_qty`` raises with
	    an accurate aggregate message.
	  * When NO active reservation remains (e.g. a Product Certification already
	    consumed it) the SRE is still needed for source-warehouse resolution, but a
	    spent SRE is not physical truth — pick the spent SRE whose warehouse
	    PHYSICALLY holds the batch in the required qty, and fail fast listing the
	    warehouses that do if none qualifies.

	``candidates`` (the ordered, single-warehouse list) is returned alongside so
	the validation can report the batch's aggregate reservation on failure.

	Self-heal: if no submitted SRE is found, EOD sync may have orphaned this MWO's WIP
	reservation (source SREs cancelled, re-reservation silently skipped because the batch
	is physically at a warehouse other than the EOD row target — see
	``mop_eod_sync._reserve_batch_at_physical_warehouse``). We re-create the missing SRE at
	the batch's physical warehouse and re-query, throwing only if it is still unreservable.
	"""
	rows = _query_batch_and_qty_sres(mwo, row.item_code, row.batch_no)

	if not rows:
		# Orphaned reservation: heal it at the warehouse where the batch physically sits, then
		# re-run BOTH queries. Reserving at the physical warehouse (not the current-op dept WH)
		# is required for correctness — _prepare_loss_row consumes the loss from sre.warehouse,
		# so a wrong warehouse would trade "no SRE" for negative batch stock.
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_reserve_batch_at_physical_warehouse,
		)

		operation = getattr(
			row, "manufacturing_operation", None
		) or frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_operation"
		)
		healed = _reserve_batch_at_physical_warehouse(
			mwo, row.item_code, row.batch_no, qty, operation, eir.company
		)
		if healed:
			rows = _query_batch_and_qty_sres(mwo, row.item_code, row.batch_no)

	if not rows:
		frappe.throw(
			_(
				"Employee IR {0}: No active Stock Reservation Entry found for "
				"MWO {1}, Item {2}, Batch {3} ({4} row {5})."
			).format(
				eir.name,
				mwo,
				row.item_code,
				row.batch_no,
				table_name,
				row.idx,
			)
		)

	qty = flt(qty, 3)
	row_mop = getattr(row, "manufacturing_operation", None)

	# Prefer reservations that still hold stock. A spent SRE (delivered/consumed/
	# transferred == reserved) nets to zero in ERPNext's Bin formula, so it neither
	# blocks the loss Stock Entry nor has anything left for _reduce_sre to release.
	active = [r for r in rows if _sre_remaining(r) > TOLERANCE]

	if not active:
		# Every reservation for this batch was already consumed upstream (a Product
		# Certification consumes SREs via consume_stock_reservation_entry, which sets
		# delivered_qty = reserved_qty and leaves docstatus = 1). Nothing is reserved,
		# so the loss can be booked directly against physical stock -- but a spent SRE
		# is NOT physical truth, so resolve the source warehouse by actual SBB stock
		# rather than trusting sre.warehouse.
		return _pick_spent_sre_by_physical_stock(eir, row, rows, qty, table_name)

	# Confine candidates to a single warehouse so deduction never spans physical
	# locations: the operation-matched SRE's warehouse, else the warehouse with
	# the largest remaining reservation.
	op_matched = [
		r for r in active if row_mop and r.get("manufacturing_operation") == row_mop
	]
	chosen_wh = (
		op_matched[0]["warehouse"]
		if op_matched
		else max(active, key=lambda r: _sre_remaining(r))["warehouse"]
	)
	candidates = [r for r in active if r.get("warehouse") == chosen_wh]

	# Order: current operation's SRE first, then by remaining reservation descending.
	candidates.sort(
		key=lambda r: (
			not (bool(row_mop) and r.get("manufacturing_operation") == row_mop),
			-_sre_remaining(r),
		)
	)

	covering = next((c for c in candidates if _sre_remaining(c) >= qty), None)
	chosen = covering or (candidates[0] if candidates else None)
	if not chosen:
		# Defensive: candidates derives from `rows` (already guarded as non-empty above),
		# so this cannot trigger today — it hardens against future filter changes silencing
		# the loss into an IndexError instead of an actionable message.
		frappe.throw(
			_("No Stock Reservation Entry candidate found for {0} in batch {1}").format(
				row.item_code, getattr(row, "batch_no", None)
			)
		)

	return frappe.get_doc("Stock Reservation Entry", chosen["name"]), candidates


def _pick_spent_sre_by_physical_stock(eir, row, rows, qty, table_name):
	"""``(sre_doc, candidates)`` when every SRE for this batch is already spent.

	A spent SRE (delivered/consumed/transferred covers reserved_qty) releases no stock
	and needs no reduction — but ``_prepare_loss_row`` still takes ``s_warehouse`` from
	it, so the warehouse must be re-validated. Per the rule this codebase already applies
	in ``pc_tagging_stock_sync`` and ``serial_number_creator``: an SRE is physical truth
	only while it is active, so pick the spent SRE whose warehouse actually holds the
	batch. Fail fast (naming the warehouses that DO hold it) rather than booking a loss
	out of a warehouse the metal has left — that would only trade this error for a
	BatchNegativeStockError deeper in the Stock Entry.

	We deliberately do NOT self-heal via ``_reserve_batch_at_physical_warehouse`` here:
	that path exists for ORPHANED reservations (no SRE at all). Re-reserving stock that
	an upstream step deliberately released would resurrect a closed reservation and can
	block other flows.
	"""
	ordered = sorted(rows, key=lambda r: -flt(r.get("reserved_qty"), 3))

	seen_wh = set()
	for candidate in ordered:
		wh = candidate.get("warehouse")
		if not wh or wh in seen_wh:
			continue
		seen_wh.add(wh)
		physical = flt(_physical_batch_qty(row.item_code, row.batch_no, wh) or 0)
		if physical + TOLERANCE >= qty:
			candidates = [r for r in rows if r.get("warehouse") == wh]
			return (
				frappe.get_doc("Stock Reservation Entry", candidate["name"]),
				candidates,
			)

	holders = _warehouses_with_physical_batch(row.item_code, row.batch_no)
	holders_txt = ", ".join(f"{wh}={q}" for wh, q in holders) if holders else _("none")
	frappe.throw(
		_(
			"Employee IR {0}, {1} row {2}: the reservation for batch {3} (item {4}) was "
			"already fully consumed upstream, and none of its warehouses [{5}] still "
			"physically holds the {6} required for the loss. Warehouses currently "
			"holding this batch: {7}."
		).format(
			eir.name,
			table_name,
			row.idx,
			row.batch_no,
			row.item_code,
			", ".join(sorted(seen_wh)) or _("none"),
			qty,
			holders_txt,
		)
	)


def _resolve_t_warehouse(eir, table_name):
	"""Resolve target warehouse based on is_raw_material."""
	if cint(eir.is_raw_material):
		return _resolve_raw_material_warehouse(eir)
	return _resolve_scrap_warehouse(eir)


def _resolve_raw_material_warehouse(eir):
	if eir.subcontracting == "Yes":
		if not eir.subcontractor:
			frappe.throw(
				_(
					"Employee IR {0}: subcontractor is required when "
					"is_raw_material is enabled"
				).format(eir.name)
			)
		wh = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": eir.company,
				"subcontractor": eir.subcontractor,
				"warehouse_type": "Raw Material",
			},
		)
		if not wh:
			frappe.throw(
				_(
					"Employee IR {0}: No Raw Material warehouse found for "
					"subcontractor {1}"
				).format(eir.name, eir.subcontractor)
			)
	else:
		if not eir.employee:
			frappe.throw(
				_(
					"Employee IR {0}: employee is required when "
					"is_raw_material is enabled"
				).format(eir.name)
			)
		wh = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"employee": eir.employee,
				"warehouse_type": "Raw Material",
			},
		)
		if not wh:
			frappe.throw(
				_(
					"Employee IR {0}: No Raw Material warehouse found for employee {1}"
				).format(eir.name, eir.employee)
			)
	return wh


def _resolve_scrap_warehouse(eir):
	if not eir.department:
		frappe.throw(
			_(
				"Employee IR {0}: department is required to resolve Scrap warehouse"
			).format(eir.name)
		)
	results = frappe.db.get_all(
		"Warehouse",
		{"disabled": 0, "department": eir.department, "warehouse_type": "Scrap"},
		["name"],
	)
	if not results:
		frappe.throw(
			_("Employee IR {0}: No Scrap warehouse found for department {1}").format(
				eir.name, eir.department
			)
		)
	if len(results) > 1:
		frappe.throw(
			_(
				"Employee IR {0}: Multiple Scrap warehouses found for department "
				"{1}: {2}. Configure one unique Scrap warehouse per department."
			).format(
				eir.name,
				eir.department,
				", ".join(r.name for r in results),
			)
		)
	return results[0].name


def _resolve_loss_item(eir, row, table_name):
	"""Return the item_code to use on the produce row of the Process Loss SE."""
	if cint(eir.is_raw_material):
		# Same item — loss moves to employee/subcontractor raw-material warehouse.
		return row.item_code

	# Scrap path: resolve the dust/loss variant via the manufacturer's mapping.
	if not row.variant_of:
		frappe.throw(
			_(
				"Employee IR {0}, {1} row {2}: variant_of is required to "
				"resolve loss item"
			).format(eir.name, table_name, row.idx)
		)
	# loss_type defaults to "Loss" when not explicitly set on the child row.
	loss_type = row.loss_type or "Loss"
	if not eir.manufacturer:
		frappe.throw(
			_("Employee IR {0}: manufacturer is required to look up loss item").format(
				eir.name
			)
		)

	loss_variant_template = frappe.db.get_value(
		"Variant Loss Table",
		{
			"parent": eir.manufacturer,
			"parenttype": "Manufacturer",
			"parentfield": "custom_variant_loss_table",
			"variant": row.variant_of,
			"loss_type": loss_type,
		},
		"loss_variant",
	)
	if not loss_variant_template:
		frappe.throw(
			_(
				"Employee IR {0}: No Variant Loss Table entry found for "
				"Manufacturer {1}, variant {2}, loss_type {3} "
				"({4} row {5})"
			).format(
				eir.name,
				eir.manufacturer,
				row.variant_of,
				loss_type,
				table_name,
				row.idx,
			)
		)

	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	loss_item = get_item_loss_item(
		eir.company, row.item_code, row.variant_of, loss_type
	)
	if not loss_item:
		frappe.throw(
			_(
				"Employee IR {0}: Could not resolve or create loss item for "
				"variant_of={1}, loss_type={2} ({3} row {4})"
			).format(eir.name, row.variant_of, loss_type, table_name, row.idx)
		)
	return loss_item


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_sre_qty(eir, row, sre_doc, candidates, qty, table_name):
	"""Guard that the loss can actually be sourced.

	``sre_doc`` is the covering SRE picked by ``_find_sre`` (or the largest when
	none covers). Two regimes:

	  * The SRE still holds a reservation — the loss must fit inside what REMAINS
	    (``reserved_qty`` minus delivered/transferred/consumed), because that is all
	    ``_reduce_sre`` can release. This fires only when no single SRE in the batch's
	    warehouse can absorb the loss; the message reports the batch's aggregate
	    remaining reservation and per-SRE breakdown so a genuine shortfall is
	    diagnosable.
	  * The reservation is spent — nothing is reserved, so the only constraint is
	    physical stock at the source warehouse. ``_find_sre`` already picked a
	    warehouse that covers it; re-assert here so the guarantee is local.
	"""
	remaining = _sre_remaining(sre_doc)

	if remaining <= TOLERANCE:
		physical = flt(
			_physical_batch_qty(row.item_code, row.batch_no, sre_doc.warehouse) or 0
		)
		if physical + TOLERANCE < qty:
			frappe.throw(
				_(
					"Employee IR {0}, {1} row {2}: loss qty {3} exceeds the physical "
					"stock of batch {4} in warehouse {5} ({6}). The reservation for this "
					"batch was already fully consumed upstream, so the loss must be "
					"covered by physical stock."
				).format(
					eir.name,
					table_name,
					row.idx,
					qty,
					row.batch_no,
					sre_doc.warehouse,
					physical,
				)
			)
		return

	if qty > remaining:
		total = flt(sum(_sre_remaining(c) for c in candidates), 3)
		breakdown = ", ".join(f"{c['name']}={_sre_remaining(c)}" for c in candidates)
		frappe.throw(
			_(
				"Employee IR {0}, {1} row {2}: loss qty {3} cannot be covered by "
				"any single Stock Reservation Entry for batch {4} in warehouse {5} "
				"(largest remaining is {6}={7}; batch totals {8} remaining across [{9}])."
			).format(
				eir.name,
				table_name,
				row.idx,
				qty,
				row.batch_no,
				sre_doc.warehouse,
				sre_doc.name,
				remaining,
				total,
				breakdown,
			)
		)


# ---------------------------------------------------------------------------
# SE builder
# ---------------------------------------------------------------------------


def _build_combined_loss_se(eir, pending):
	"""Build ONE Repack SE with consume+produce row pairs for all loss entries."""
	first = pending[0]
	mwo = first["mwo"]

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = PROCESS_LOSS_SE_TYPE
	se.purpose = "Repack"
	se.company = eir.company
	se.posting_date = today()
	se.posting_time = nowtime()
	se.employee_ir = eir.name
	se.auto_created = 1
	se.manufacturing_work_order = mwo
	se.department = eir.department
	se.to_department = eir.department

	if eir.subcontracting == "Yes":
		se.subcontractor = eir.subcontractor
	else:
		se.employee = eir.employee

	manufacturing_order = frappe.db.get_value(
		"Manufacturing Work Order", mwo, "manufacturing_order"
	)
	if manufacturing_order:
		se.manufacturing_order = manufacturing_order

	# Carry the source batch's customer voucher type onto the SE header so the
	# scrap batches minted from the produce rows inherit it
	# (Batch.update_inventory_dimentions reads customer_voucher_type off the SE).
	# One EIR loss is a single customer context in practice, so the first
	# customer-owned loss row's batch is authoritative.
	for entry in pending:
		if entry["customer"]:
			voucher_type = frappe.db.get_value(
				"Batch", entry["row"].batch_no, "custom_customer_voucher_type"
			)
			if voucher_type:
				se.customer_voucher_type = voucher_type
			break

	for entry in pending:
		row = entry["row"]
		qty = entry["qty"]
		entry_mwo = entry["mwo"]
		mop = getattr(row, "manufacturing_operation", None)
		# Carry the loss row's pcs onto both SE rows; fall back to "1" so a
		# missing value preserves the field default and stays valid (reqd).
		pcs = getattr(row, "pcs", None) or "1"

		# Consume row: source item out of SRE warehouse.
		se.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": qty,
				"transfer_qty": qty,
				"pcs": pcs,
				"s_warehouse": entry["s_warehouse"],
				"t_warehouse": None,
				"batch_no": row.batch_no,
				"uom": row.stock_uom or "Gram",
				"stock_uom": row.stock_uom or "Gram",
				"conversion_factor": 1,
				"manufacturing_operation": mop,
				"custom_manufacturing_work_order": entry_mwo,
				"inventory_type": entry["inventory_type"],
				"customer": entry["customer"],
				"use_serial_batch_fields": 1,
			},
		)
		# Produce row: loss item into target (scrap) warehouse.
		produce_row = {
			"item_code": entry["loss_item"],
			"qty": qty,
			"transfer_qty": qty,
			"s_warehouse": None,
			"t_warehouse": entry["t_warehouse"],
			"uom": row.stock_uom or "Gram",
			"stock_uom": row.stock_uom or "Gram",
			"conversion_factor": 1,
			"is_finished_item": 1,
			"set_basic_rate_manually": 1,
			"manufacturing_operation": mop,
			"custom_manufacturing_work_order": entry_mwo,
			"use_serial_batch_fields": 1,
		}
		# Scrap/loss inherits the source batch's inventory type: a customer's
		# metal stays the customer's even after it is booked as loss. The minted
		# scrap batch picks this up via Batch.update_inventory_dimentions.
		produce_row["inventory_type"] = entry["inventory_type"]
		if entry["customer"]:
			produce_row["customer"] = entry["customer"]
		se.append("items", produce_row)

	return se


# ---------------------------------------------------------------------------
# SRE reduction and restoration
# ---------------------------------------------------------------------------


def _reservation_voucher_qty(sre_doc, reserved_qty):
	"""voucher_qty that lets reserved_qty clear validate_with_allowed_qty.

	SO lines are routinely over-reserved by sibling MWO reservations, so
	recreating even a reduced reservation trips ERPNext's allowed-qty guard.
	Mirror stock_reservation_entry_for_mwo (doc_events/stock_entry.py): lift
	voucher_qty to cover already-reserved qty + this entry's qty.
	"""
	base = flt(sre_doc.voucher_qty)
	if (
		sre_doc.voucher_type != "Sales Order"
		or not sre_doc.voucher_no
		or not sre_doc.voucher_detail_no
	):
		return base

	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_sre_reserved_qty_for_voucher_detail_no,
	)

	total_so_reserved = get_sre_reserved_qty_for_voucher_detail_no(
		sre_doc.item_code,
		"Sales Order",
		sre_doc.voucher_no,
		sre_doc.voucher_detail_no,
		ignore_sre=sre_doc.name,
	)
	return max(base, flt(total_so_reserved) + flt(reserved_qty))


def _reduce_sre(eir, row, sre_doc, loss_qty, table_name):
	"""Cancel the existing SRE and recreate it with reduced reserved_qty.

	Stores original_reserved_qty (plus the per-batch original qty) and employee_ir in
	custom_replaced_sre_snapshot (JSON) so the cancel path can restore exactly what was
	taken away.

	No-ops when the reservation is already spent: a Delivered/consumed SRE nets to zero
	in ERPNext's Bin formula, so it blocks nothing and there is nothing to release.
	Recreating it would carry ``delivered_qty`` forward (``frappe.copy_doc`` defaults to
	``ignore_no_copy=True``) and trip
	``StockReservationEntry.validate_with_allowed_qty``'s
	"Reserved Qty should be greater than Delivered Qty".
	"""
	if _sre_remaining(sre_doc) <= TOLERANCE:
		return

	# The replacement carries delivered_qty = 0, so it must be sized by what the old entry
	# still RESERVED, not by its gross reserved_qty -- otherwise a partially-delivered SRE
	# (reserved 5, delivered 4, net 1) would come back reserving 5 - loss and silently
	# re-reserve the 4 already handed over.
	original_reserved_qty = flt(sre_doc.reserved_qty, 3)
	original_delivered_qty = flt(sre_doc.delivered_qty, 3)
	remaining_qty = _sre_remaining(sre_doc)
	is_batched = sre_doc.reservation_based_on == "Serial and Batch"

	original_sb_qty = None
	if is_batched:
		matching = [sb for sb in sre_doc.sb_entries if sb.batch_no == row.batch_no]
		if not matching:
			# Reachable via the batch-agnostic Qty fallback in _query_batch_and_qty_sres:
			# with no matching row the loop below would change nothing and ERPNext would
			# reset reserved_qty to sum(sb_entries.qty) on submit
			# (stock_reservation_entry.py validate_reservation_based_on_serial_and_batch),
			# silently discarding the reduction. Fail loudly instead.
			frappe.throw(
				_(
					"Employee IR {0}, {1} row {2}: Stock Reservation Entry {3} reserves "
					"batches [{4}] but not {5}, so the loss cannot be deducted from it."
				).format(
					eir.name,
					table_name,
					row.idx,
					sre_doc.name,
					", ".join(
						sorted(
							{sb.batch_no for sb in sre_doc.sb_entries if sb.batch_no}
						)
					),
					row.batch_no,
				)
			)
		# Per-batch remaining, for the same reason as the header figure above.
		original_sb_qty = flt(
			flt(matching[0].qty, 3) - flt(matching[0].delivered_qty, 3), 3
		)

	new_qty = flt(remaining_qty - loss_qty, 3)

	sre_doc.cancel()

	if new_qty <= TOLERANCE:
		# Entire reservation consumed — no new SRE needed.
		return

	new_sre = frappe.copy_doc(sre_doc)
	new_sre.docstatus = 0
	new_sre.name = None
	new_sre.amended_from = None
	new_sre.status = "Draft"
	# copy_doc keeps no_copy fields (frappe/model/document.py copy_doc: "No_copy fields
	# also get copied"), so the consumption counters would ride along onto a brand-new
	# reservation. Zero them explicitly: this replacement has delivered nothing.
	new_sre.delivered_qty = 0
	new_sre.transferred_qty = 0
	new_sre.consumed_qty = 0
	new_sre.reserved_qty = new_qty
	new_sre.voucher_qty = _reservation_voucher_qty(sre_doc, new_qty)
	new_sre.available_qty = max(flt(sre_doc.available_qty), new_qty)
	# Two markers, deliberately: `employee_ir` is an exact-match key so the cancel path
	# does not have to LIKE-scan every reservation in the table, while the snapshot
	# carries the payload needed to rebuild the reservation.
	new_sre.employee_ir = eir.name
	new_sre.custom_replaced_sre_snapshot = json.dumps(
		{
			"employee_ir": eir.name,
			"original_reserved_qty": original_reserved_qty,
			"original_delivered_qty": original_delivered_qty,
			"batch_no": row.batch_no if is_batched else None,
			"original_sb_qty": original_sb_qty,
		}
	)

	if is_batched:
		# DECREMENT the affected batch row only. Assigning the whole new header qty
		# would corrupt multi-batch reservations: ERPNext recomputes
		# reserved_qty = sum(sb_entries.qty) on submit. Every row is rebased on its own
		# remaining qty, since the replacement's delivered_qty starts at 0.
		kept = []
		for sb in new_sre.sb_entries:
			sb.qty = flt(flt(sb.qty, 3) - flt(sb.delivered_qty, 3), 3)
			sb.delivered_qty = 0
			if sb.batch_no == row.batch_no:
				sb.qty = flt(sb.qty - loss_qty, 3)
			if sb.qty <= TOLERANCE:
				continue
			kept.append(sb)
		new_sre.sb_entries = kept
		for idx, sb in enumerate(new_sre.sb_entries, start=1):
			sb.idx = idx
		new_sre.reserved_qty = flt(sum(flt(sb.qty, 3) for sb in new_sre.sb_entries), 3)
		if new_sre.reserved_qty <= TOLERANCE:
			return

	new_sre.flags.ignore_permissions = True
	new_sre.insert(ignore_links=True)
	new_sre.submit()


def _restore_reduced_sres(eir):
	"""On EIR cancel: cancel reduced SREs and restore original reserved qty.

	Returns the number of reservations found for this EIR, so the caller can tell
	"nothing to restore" apart from "the markers were never written" (see
	``_assert_no_orphaned_reductions``).
	"""
	# `employee_ir` is the current marker and matches exactly. The snapshot LIKE is the
	# legacy arm: reservations reduced before `employee_ir` was stamped carry only the
	# JSON, and they must stay restorable.
	rows = frappe.db.sql(
		"""
        SELECT name, custom_replaced_sre_snapshot
        FROM `tabStock Reservation Entry`
        WHERE docstatus = 1
          AND (
            employee_ir = %(eir)s
            OR custom_replaced_sre_snapshot LIKE %(legacy)s
          )
        """,
		{"eir": eir.name, "legacy": f'%"employee_ir": "{eir.name}"%'},
		as_dict=True,
	)

	for sre_row in rows:
		snapshot = {}
		try:
			snapshot = json.loads(sre_row.custom_replaced_sre_snapshot or "{}")
		except Exception:
			pass

		# Restore the reservation the EIR actually took away: the pre-loss REMAINING qty,
		# since the restored entry (like the reduced one) carries delivered_qty = 0.
		# original_delivered_qty is absent on snapshots written before this change, where
		# it was always effectively 0.
		orig_qty = flt(
			flt(snapshot.get("original_reserved_qty", 0), 3)
			- flt(snapshot.get("original_delivered_qty", 0), 3),
			3,
		)
		snap_batch = snapshot.get("batch_no")
		snap_sb_qty = snapshot.get("original_sb_qty")
		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_row.name)
		sre_doc.cancel()

		if orig_qty <= 0:
			continue

		restored = frappe.copy_doc(sre_doc)
		restored.docstatus = 0
		restored.name = None
		restored.amended_from = None
		restored.status = "Draft"
		# Mirror _reduce_sre: copy_doc carries no_copy counters forward.
		restored.delivered_qty = 0
		restored.transferred_qty = 0
		restored.consumed_qty = 0
		restored.reserved_qty = orig_qty
		restored.voucher_qty = _reservation_voucher_qty(sre_doc, orig_qty)
		restored.available_qty = max(flt(sre_doc.available_qty), orig_qty)
		# copy_doc carries no_copy fields, so both markers ride along from the reduced
		# entry. Clear them: this reservation is whole again and must not be picked up
		# by a second cancel of the same EIR.
		restored.employee_ir = None
		restored.custom_replaced_sre_snapshot = None

		if restored.sb_entries:
			if snap_batch and snap_sb_qty is not None:
				# Restore ONLY the batch row the loss was taken from. Blanket-assigning
				# orig_qty to every row would multiply a multi-batch reservation, since
				# ERPNext recomputes reserved_qty = sum(sb_entries.qty) on submit.
				matched = next(
					(sb for sb in restored.sb_entries if sb.batch_no == snap_batch),
					None,
				)
				if matched:
					matched.qty = flt(snap_sb_qty, 3)
				else:
					restored.append(
						"sb_entries",
						{
							"batch_no": snap_batch,
							"qty": flt(snap_sb_qty, 3),
							"warehouse": restored.warehouse,
						},
					)
			elif len(restored.sb_entries) == 1:
				# Legacy snapshot (pre per-batch tracking): unambiguous only when the
				# reservation covers a single batch.
				restored.sb_entries[0].qty = orig_qty

			for sb in restored.sb_entries:
				sb.delivered_qty = 0
			restored.reserved_qty = flt(
				sum(flt(sb.qty, 3) for sb in restored.sb_entries), 3
			)

		restored.flags.ignore_permissions = True
		restored.insert(ignore_links=True)
		restored.submit()

	return len(rows)
