"""Process Loss Stock Entry creation for Employee IR.

On Receive submit: for each row in employee_loss_details and
manually_book_loss_details with proportionally_loss > 0, creates a
"Process Loss" (Repack purpose) Stock Entry that moves the loss quantity
from the SRE source warehouse to either:
  - Scrap warehouse by department  (is_main_slip_required = 0)
  - Employee / Subcontractor Raw Material warehouse  (is_main_slip_required = 1)

After each SE is submitted the matching Stock Reservation Entry is cancelled
and recreated with reduced reserved_qty.

Cancel path: cancels all Process Loss SEs owned by this EIR and restores the
original SRE reserved quantities via custom_replaced_sre_snapshot (JSON).
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, nowtime, today

PROCESS_LOSS_SE_TYPE = "Process Loss"
CHILD_TABLE_EMPLOYEE = "employee_loss_details"
CHILD_TABLE_MANUAL = "manually_book_loss_details"


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

	# Reduce all SREs first so stock is free for the ledger entry.
	for entry in pending:
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
	for se_name in se_names:
		frappe.get_doc("Stock Entry", se_name).cancel()

	_restore_reduced_sres(eir)


# ---------------------------------------------------------------------------
# Per-row preparation (validate + resolve, no side effects)
# ---------------------------------------------------------------------------


def _prepare_loss_row(eir, row, table_name):
	"""Validate a single loss row and return a dict of resolved data.

	Returns None when proportionally_loss <= 0.
	Raises on any missing mandatory field or unresolvable reference.
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

	return {
		"row": row,
		"table_name": table_name,
		"qty": qty,
		"mwo": mwo,
		"sre_doc": sre_doc,
		"s_warehouse": sre_doc.warehouse,
		"t_warehouse": t_warehouse,
		"loss_item": loss_item,
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
	  * Restrict candidates to a SINGLE warehouse so we never deduct across
	    physical locations: the warehouse of the operation-matched SRE if one
	    exists, else the warehouse holding the largest reserved_qty.
	  * Within that warehouse pick the SRE that can COVER the loss, preferring
	    the current operation's SRE, then the largest. If none individually
	    covers the loss, return the largest so ``_validate_sre_qty`` raises with
	    an accurate aggregate message.

	``candidates`` (the ordered, single-warehouse list) is returned alongside so
	the validation can report the batch's aggregate reservation on failure.
	"""
	rows = frappe.db.sql(
		"""
        SELECT
            sre.name,
            sre.warehouse,
            sre.reserved_qty,
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
		(mwo, row.item_code, row.batch_no),
		as_dict=True,
	)

	if not rows:
		# Fallback: Qty-based reservation (reservation_based_on = "Qty")
		rows = frappe.db.get_all(
			"Stock Reservation Entry",
			{
				"manufacturing_work_order": mwo,
				"item_code": row.item_code,
				"docstatus": 1,
			},
			[
				"name",
				"warehouse",
				"reserved_qty",
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
			],
		)

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

	# Confine candidates to a single warehouse so deduction never spans physical
	# locations: the operation-matched SRE's warehouse, else the warehouse with
	# the largest reserved_qty.
	row_mop = getattr(row, "manufacturing_operation", None)
	op_matched = [
		r for r in rows if row_mop and r.get("manufacturing_operation") == row_mop
	]
	chosen_wh = (
		op_matched[0]["warehouse"]
		if op_matched
		else max(rows, key=lambda r: flt(r.get("reserved_qty"), 3))["warehouse"]
	)
	candidates = [r for r in rows if r.get("warehouse") == chosen_wh]

	# Order: current operation's SRE first, then by reserved_qty descending.
	candidates.sort(
		key=lambda r: (
			not (bool(row_mop) and r.get("manufacturing_operation") == row_mop),
			-flt(r.get("reserved_qty"), 3),
		)
	)

	qty = flt(qty, 3)
	covering = next(
		(c for c in candidates if flt(c.get("reserved_qty"), 3) >= qty), None
	)
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


def _resolve_t_warehouse(eir, table_name):
	"""Resolve target warehouse based on is_main_slip_required."""
	if cint(eir.is_main_slip_required):
		return _resolve_raw_material_warehouse(eir)
	return _resolve_scrap_warehouse(eir)


def _resolve_raw_material_warehouse(eir):
	if eir.subcontracting == "Yes":
		if not eir.subcontractor:
			frappe.throw(
				_(
					"Employee IR {0}: subcontractor is required when "
					"is_main_slip_required is enabled"
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
					"is_main_slip_required is enabled"
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
	if cint(eir.is_main_slip_required):
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
	"""Guard that the chosen SRE can cover the loss.

	``sre_doc`` is the covering SRE picked by ``_find_sre`` (or the largest when
	none covers). This fires only when no single SRE in the batch's warehouse
	can absorb the loss; the message reports the batch's aggregate reservation
	and per-SRE breakdown so a genuine shortfall is diagnosable.
	"""
	reserved = flt(sre_doc.reserved_qty, 3)
	if qty > reserved:
		total = flt(sum(flt(c.get("reserved_qty"), 3) for c in candidates), 3)
		breakdown = ", ".join(
			f"{c['name']}={flt(c.get('reserved_qty'), 3)}" for c in candidates
		)
		frappe.throw(
			_(
				"Employee IR {0}, {1} row {2}: loss qty {3} cannot be covered by "
				"any single Stock Reservation Entry for batch {4} in warehouse {5} "
				"(largest is {6}={7}; batch totals {8} across [{9}])."
			).format(
				eir.name,
				table_name,
				row.idx,
				qty,
				row.batch_no,
				sre_doc.warehouse,
				sre_doc.name,
				reserved,
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
				"inventory_type": row.inventory_type,
				"customer": row.customer,
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
		if getattr(row, "inventory_type", None):
			produce_row["inventory_type"] = row.inventory_type
		if getattr(row, "customer", None):
			produce_row["customer"] = row.customer
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

	Stores original_reserved_qty and employee_ir in custom_replaced_sre_snapshot
	(JSON) so the cancel path can restore the original quantity.
	"""
	original_reserved_qty = flt(sre_doc.reserved_qty, 3)
	new_qty = flt(original_reserved_qty - loss_qty, 3)

	sre_doc.cancel()

	if new_qty <= 0:
		# Entire reservation consumed — no new SRE needed.
		return

	new_sre = frappe.copy_doc(sre_doc)
	new_sre.docstatus = 0
	new_sre.name = None
	new_sre.amended_from = None
	new_sre.status = "Draft"
	new_sre.reserved_qty = new_qty
	new_sre.voucher_qty = _reservation_voucher_qty(sre_doc, new_qty)
	new_sre.available_qty = max(flt(sre_doc.available_qty), new_qty)
	new_sre.custom_replaced_sre_snapshot = json.dumps(
		{
			"employee_ir": eir.name,
			"original_reserved_qty": original_reserved_qty,
		}
	)

	if sre_doc.reservation_based_on == "Serial and Batch":
		for sb in new_sre.sb_entries:
			if sb.batch_no == row.batch_no:
				sb.qty = new_qty
				break

	new_sre.flags.ignore_permissions = True
	new_sre.insert(ignore_links=True)
	new_sre.submit()


def _restore_reduced_sres(eir):
	"""On EIR cancel: cancel reduced SREs and restore original reserved qty."""
	# Find SREs created by this EIR via the snapshot field (no dedicated column).
	rows = frappe.db.sql(
		"""
        SELECT name, custom_replaced_sre_snapshot
        FROM `tabStock Reservation Entry`
        WHERE docstatus = 1
          AND custom_replaced_sre_snapshot LIKE %s
        """,
		(f'%"employee_ir": "{eir.name}"%',),
		as_dict=True,
	)

	for sre_row in rows:
		snapshot = {}
		try:
			snapshot = json.loads(sre_row.custom_replaced_sre_snapshot or "{}")
		except Exception:
			pass

		orig_qty = flt(snapshot.get("original_reserved_qty", 0), 3)
		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_row.name)
		sre_doc.cancel()

		if orig_qty <= 0:
			continue

		restored = frappe.copy_doc(sre_doc)
		restored.docstatus = 0
		restored.name = None
		restored.amended_from = None
		restored.status = "Draft"
		restored.reserved_qty = orig_qty
		restored.voucher_qty = _reservation_voucher_qty(sre_doc, orig_qty)
		restored.available_qty = max(flt(sre_doc.available_qty), orig_qty)
		restored.custom_replaced_sre_snapshot = None

		for sb in restored.sb_entries:
			sb.qty = orig_qty

		restored.flags.ignore_permissions = True
		restored.insert(ignore_links=True)
		restored.submit()
