"""
SRE-Based MWO Sync — converts unsynced MOP Log entries into a single
consolidating Stock Entry per MWO using the active Stock Reservation Entry
as the ground truth for the source warehouse.

Algorithm per MWO:
1. Find the last Manufacturing Operation (by creation timestamp) that has
   unsynced MOP Logs.
2. Get all active SREs for that MOP → each SRE.warehouse is the s_warehouse.
3. From the last MOP's latest flow-index logs → derive t_warehouse per item/batch.
4. Create one Material Transfer to Department SE per unique (s_wh, t_wh) pair.
5. Cancel each moved SRE and recreate it at t_warehouse (same qty, same batches).
6. Mark every unsynced MOP Log for the MWO (all intermediate hops) as is_synced=1.

Loss entries are NOT created here — Employee IR books process loss then-and-there
via employee_loss_se.py; those MOP Log rows are already written with is_synced=1
by the EIR bridge and therefore excluded automatically (is_synced=0 filter).

The Sunday delete_cancelled_stock_reservations() function is registered as a
daily scheduler event and self-gates to run only on Sundays.
"""

import datetime

import frappe
from frappe import _
from frappe.utils import cint, flt

# ---------------------------------------------------------------------------
# Sync lock helpers
# ---------------------------------------------------------------------------


def _set_sync_lock(running):
	frappe.db.set_value(
		"MOP Settings",
		"MOP Settings",
		{
			"sync_running": 1 if running else 0,
			"sync_started_at": frappe.utils.now() if running else None,
		},
		update_modified=False,
	)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def sync_mop_logs():
	"""Main entry point called by the MOP Settings button and hourly scheduler."""
	_set_sync_lock(True)
	try:
		return _run_sync()
	finally:
		_set_sync_lock(False)


def maybe_sync_mop_logs():
	"""Hourly scheduler hook. Fires sync_mop_logs only when the current hour
	matches the hour configured in MOP Settings → Scheduled Sync Time."""
	sync_time = frappe.db.get_single_value("MOP Settings", "sync_time")
	if not sync_time:
		return

	target_hour = int(str(sync_time).split(":")[0])
	if datetime.datetime.now().hour != target_hour:
		return

	sync_mop_logs()


# ---------------------------------------------------------------------------
# Core sync loop
# ---------------------------------------------------------------------------


def _run_sync():
	frappe.flags.mop_sync_in_progress = True
	mwo_groups = _get_mwos_with_unsynced_logs()
	processed = 0
	stock_entries = []

	for mwo, logs_by_mop in mwo_groups.items():
		company = _resolve_company_for_mwo(logs_by_mop)
		frappe.db.savepoint("mop_sre_sync")
		try:
			se_names = _sync_mwo_via_sre(mwo, logs_by_mop, company)
			stock_entries.extend(se_names)
			all_logs = [log for logs in logs_by_mop.values() for log in logs]
			_mark_synced(all_logs)
			processed += len(all_logs)
			frappe.db.release_savepoint("mop_sre_sync")
		except Exception:
			frappe.db.rollback(save_point="mop_sre_sync")
			frappe.log_error(
				title=f"MOP SRE Sync failed for MWO {mwo}",
				message=frappe.get_traceback(),
			)

	return {"processed": processed, "stock_entries": stock_entries}


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------


def _get_mwos_with_unsynced_logs():
	"""Return {mwo: {mop_name: [log_dict, ...]}} for all unsynced, non-cancelled logs."""
	logs = frappe.db.get_all(
		"MOP Log",
		filters={"is_synced": 0, "is_cancelled": 0},
		fields=[
			"name",
			"manufacturing_operation",
			"manufacturing_work_order",
			"item_code",
			"batch_no",
			"serial_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"from_warehouse",
			"to_warehouse",
			"flow_index",
			"voucher_type",
			"voucher_no",
		],
		order_by="manufacturing_work_order, manufacturing_operation, flow_index asc, creation asc",
	)

	result = {}
	for log in logs:
		mwo = log.manufacturing_work_order
		mop = log.manufacturing_operation
		if not mwo or not mop:
			continue
		result.setdefault(mwo, {}).setdefault(mop, []).append(log)
	return result


def _resolve_company_for_mwo(logs_by_mop):
	"""Fetch company from the first Manufacturing Operation found in the group."""
	for mop_name in logs_by_mop:
		company = frappe.db.get_value("Manufacturing Operation", mop_name, "company")
		if company:
			return company
	return None


def _get_last_mop_for_mwo(mop_names):
	"""Return the MOP name with the latest creation timestamp."""
	if not mop_names:
		return None
	if len(mop_names) == 1:
		return mop_names[0]
	rows = frappe.db.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", mop_names]},
		fields=["name", "creation"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0].name if rows else mop_names[0]


def _get_active_sres_for_mop(mwo, mop_name):
	"""Return all submitted SREs linked to this MWO + MOP."""
	sre_cols = frappe.db.get_table_columns("Stock Reservation Entry")
	filters = {"docstatus": 1, "manufacturing_work_order": mwo}
	if "manufacturing_operation" in sre_cols:
		filters["manufacturing_operation"] = mop_name
	return frappe.db.get_all(
		"Stock Reservation Entry",
		filters=filters,
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"reservation_based_on",
			"manufacturing_work_order",
			"manufacturing_operation",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"voucher_qty",
			"company",
			"stock_uom",
		],
	)


def _build_item_t_warehouse_map(logs):
	"""From the highest flow_index logs, build {(item_code, batch_no): to_warehouse}."""
	latest = _latest_flow_logs(logs)
	mapping = {}
	for log in latest:
		if log.to_warehouse:
			mapping[(log.item_code, log.batch_no or "")] = log.to_warehouse
	return mapping


def _latest_flow_logs(logs):
	if not logs:
		return []
	max_idx = max(log.flow_index for log in logs)
	return [log for log in logs if log.flow_index == max_idx]


# ---------------------------------------------------------------------------
# Per-MWO sync orchestrator
# ---------------------------------------------------------------------------


def _sync_mwo_via_sre(mwo, logs_by_mop, company):
	"""
	For one MWO:
	  - Resolve last MOP, active SREs, and per-item t_warehouse from MOP logs.
	  - Create one Material Transfer SE per (s_wh, t_wh) pair.
	  - Cancel each moved SRE and recreate it at t_warehouse.
	Returns list of created SE names.
	"""
	last_mop_name = _get_last_mop_for_mwo(list(logs_by_mop.keys()))
	if not last_mop_name:
		return []

	mop_doc = frappe.db.get_value(
		"Manufacturing Operation",
		last_mop_name,
		["company", "manufacturer", "manufacturing_work_order", "manufacturing_order"],
		as_dict=True,
	)
	if not mop_doc:
		return []

	active_sres = _get_active_sres_for_mop(mwo, last_mop_name)
	if not active_sres:
		return []

	t_wh_map = _build_item_t_warehouse_map(logs_by_mop[last_mop_name])
	last_logs = _latest_flow_logs(logs_by_mop[last_mop_name])

	# Determine (s_wh, t_wh) for each SRE
	# Group items by (s_wh, t_wh) → one SE per pair
	groups = {}  # {(s_wh, t_wh): [(sre, log_rows)]}
	for sre in active_sres:
		s_wh = sre.warehouse
		# Batch-aware lookup first, then item-only fallback
		t_wh = t_wh_map.get((sre.item_code, "")) or next(
			(v for (ic, _), v in t_wh_map.items() if ic == sre.item_code), None
		)
		if not t_wh or t_wh == s_wh:
			continue
		item_logs = [
			log
			for log in last_logs
			if log.item_code == sre.item_code
			and flt(log.qty_after_transaction_batch_based) > 0
		]
		if not item_logs:
			continue
		groups.setdefault((s_wh, t_wh), []).append((sre, item_logs))

	if not groups:
		return []

	se_names = []
	sres_to_relocate = []

	for (s_wh, t_wh), entries in groups.items():
		se_items = []
		for sre, item_logs in entries:
			for log in item_logs:
				qty = flt(log.qty_after_transaction_batch_based, 3)
				if qty <= 0:
					continue
				row = {
					"item_code": log.item_code,
					"qty": qty,
					"s_warehouse": s_wh,
					"t_warehouse": t_wh,
					"manufacturing_operation": last_mop_name,
					"custom_manufacturing_work_order": mwo,
					"use_serial_batch_fields": 1,
				}
				if log.batch_no:
					row["batch_no"] = log.batch_no
				if log.serial_no:
					row["serial_no"] = log.serial_no
				se_items.append(row)
			sres_to_relocate.append((sre, t_wh))

		if not se_items:
			continue

		_validate_eod_items_for_mwo_reservation(se_items)
		_validate_eod_source_batch_stock(
			se_items, manufacturing_work_order=mwo, company=company
		)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer to Department"
		se.company = company or mop_doc.company
		se.manufacturing_order = mop_doc.manufacturing_order
		se.manufacturing_work_order = mwo
		se.manufacturing_operation = last_mop_name
		se.manufacturer = mop_doc.manufacturer
		se.auto_created = 1
		for item in se_items:
			se.append("items", item)
		se.flags.ignore_permissions = True
		se.save()
		se.submit()
		se_names.append(se.name)

	# After SE(s) submitted, relocate SREs
	for sre, t_wh in sres_to_relocate:
		_relocate_sre(sre, t_wh)

	return se_names


# ---------------------------------------------------------------------------
# SRE relocation: cancel old, recreate at new warehouse
# ---------------------------------------------------------------------------


def _relocate_sre(sre, new_warehouse):
	"""Cancel the existing SRE and recreate it at new_warehouse (same qty/batches/voucher)."""
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	sre_doc = frappe.get_doc("Stock Reservation Entry", sre.name)
	if sre_doc.docstatus != 1:
		return

	reserved_qty = flt(sre_doc.reserved_qty) - flt(sre_doc.delivered_qty)
	if reserved_qty <= 1e-9:
		sre_doc.ignore_permissions = True
		sre_doc.cancel()
		return

	has_batch_no, has_serial_no = frappe.get_cached_value(
		"Item", sre_doc.item_code, ["has_batch_no", "has_serial_no"]
	)

	sb_entries = []
	if cint(has_batch_no):
		old_sb = frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": sre_doc.name},
			fields=["batch_no", "qty", "delivered_qty"],
		)
		for sb in old_sb:
			remaining = flt(sb.qty) - flt(sb.delivered_qty)
			if remaining > 1e-9:
				sb_entries.append({"batch_no": sb.batch_no, "qty": remaining})

	sre_doc.ignore_permissions = True
	sre_doc.cancel()

	if cint(has_batch_no) and not sb_entries:
		return

	if sb_entries:
		available_qty = get_available_qty_to_reserve(
			sre_doc.item_code, new_warehouse, batch_no=sb_entries[0]["batch_no"]
		)
	else:
		available_qty = get_available_qty_to_reserve(sre_doc.item_code, new_warehouse)

	new_sre = frappe.new_doc("Stock Reservation Entry")
	new_sre.voucher_type = sre_doc.voucher_type
	new_sre.voucher_no = sre_doc.voucher_no
	new_sre.voucher_detail_no = sre_doc.voucher_detail_no
	new_sre.voucher_qty = sre_doc.voucher_qty
	new_sre.item_code = sre_doc.item_code
	new_sre.warehouse = new_warehouse
	new_sre.reserved_qty = reserved_qty
	new_sre.company = sre_doc.company
	new_sre.stock_uom = sre_doc.stock_uom
	new_sre.reservation_based_on = sre_doc.reservation_based_on
	new_sre.has_batch_no = cint(has_batch_no)
	new_sre.has_serial_no = cint(has_serial_no)
	new_sre.available_qty = max(flt(available_qty), reserved_qty)
	new_sre.manufacturing_work_order = sre_doc.manufacturing_work_order
	new_sre.manufacturing_operation = sre_doc.manufacturing_operation

	for sb in sb_entries:
		new_sre.append("sb_entries", {"batch_no": sb["batch_no"], "qty": sb["qty"]})

	new_sre.flags.ignore_permissions = True
	new_sre.insert(ignore_links=1)
	new_sre.submit()


# ---------------------------------------------------------------------------
# Mark synced
# ---------------------------------------------------------------------------


def _mark_synced(logs):
	log_names = [log.name for log in logs]
	if log_names:
		frappe.db.set_value("MOP Log", {"name": ["in", log_names]}, "is_synced", 1)


# ---------------------------------------------------------------------------
# Batch and reservation validation (kept from original)
# ---------------------------------------------------------------------------


def _validate_eod_items_for_mwo_reservation(items_to_transfer):
	"""Ensure batch/serial data is present for items that need it (SRE build guard)."""
	for item in items_to_transfer:
		item_code = item.get("item_code")
		if not item_code or flt(item.get("qty")) <= 0:
			continue
		item_flags = frappe.db.get_value(
			"Item", item_code, ["has_batch_no", "has_serial_no"]
		)
		if not item_flags:
			frappe.throw(
				_("MOP EOD Sync: Item {0} not found.").format(frappe.bold(item_code))
			)
		has_batch_no, has_serial_no = item_flags
		mop_label = item.get("manufacturing_operation") or "?"
		if cint(has_batch_no) and not item.get("batch_no"):
			frappe.throw(
				_(
					"MOP SRE Sync: item {0} is batch-tracked but the MOP Log line has no Batch No "
					"(Manufacturing Operation {1}). Stock Reservation on submit cannot build "
					"sb_entries — fix the source MOP Log / vouchers, then retry."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)
		if cint(has_serial_no) and not item.get("serial_no"):
			frappe.throw(
				_(
					"MOP SRE Sync: item {0} is serialized but the MOP Log line has no Serial No "
					"(Manufacturing Operation {1})."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)


def _validate_eod_source_batch_stock(
	items_to_transfer, manufacturing_work_order=None, company=None
):
	"""Ensure aggregated transfer qty per (s_wh, item, batch) does not exceed physical batch stock."""
	from erpnext.stock.doctype.batch.batch import get_batch_qty
	from frappe.utils import nowtime, today

	posting_date = today()
	posting_time = nowtime()
	needed = {}
	for item in items_to_transfer:
		wh = item.get("s_warehouse")
		item_code = item.get("item_code")
		batch_no = item.get("batch_no")
		qty = flt(item.get("qty"), 3)
		if not wh or not item_code or qty <= 0 or not batch_no:
			continue
		key = (wh, item_code, batch_no)
		needed[key] = flt(needed.get(key, 0) + qty, 3)

	for (wh, item_code, batch_no), req_qty in needed.items():
		try:
			physical_raw = get_batch_qty(
				batch_no=batch_no,
				warehouse=wh,
				item_code=item_code,
				posting_date=posting_date,
				posting_time=posting_time,
				ignore_reserved_stock=True,
			)
		except Exception:
			physical_raw = None
		physical = flt(physical_raw, 3) if physical_raw is not None else 0.0
		if req_qty > physical + 1e-6:
			short = flt(req_qty - physical, 3)
			detail_lines = []
			if company:
				detail_lines.append(_("Company: {0}").format(company))
			if manufacturing_work_order:
				detail_lines.append(
					_("Manufacturing Work Order: {0}").format(manufacturing_work_order)
				)
			sre_here = _list_open_sre_for_batch(
				item_code,
				wh,
				batch_no,
				manufacturing_work_order=manufacturing_work_order,
			)
			if not sre_here and manufacturing_work_order:
				sre_here = _list_open_sre_for_batch(item_code, wh, batch_no)
			total_open = 0.0
			for row in sre_here:
				oq = flt(row.get("open_qty"), 3)
				if oq > 0:
					total_open += oq
					detail_lines.append(
						_("Open SRE {0} @ {1}: undelivered {2}").format(
							row.get("name"), row.get("warehouse"), oq
						)
					)
			if physical <= 1e-6 and total_open > 1e-6:
				detail_lines.append(
					_(
						"Hint: physical batch qty is 0 but open reservation(s) exist — "
						"likely stale SRE; cancel/amend SRE or restore stock."
					)
				)
			frappe.throw(
				_(
					"MOP SRE Sync: cannot move {0} of item {1}, batch {2} from {3}: "
					"MOP Log(s) require {4} but only {5} physical qty exists "
					"(short by {6}; SLE / batch ledger).\n\n{7}"
				).format(
					frappe.bold(req_qty),
					frappe.bold(item_code),
					frappe.bold(batch_no),
					frappe.bold(wh),
					req_qty,
					physical,
					short,
					"\n".join(detail_lines),
				),
				title=_("MOP SRE Sync — insufficient batch stock"),
			)


# ---------------------------------------------------------------------------
# SRE diagnostic helpers (kept for troubleshooting)
# ---------------------------------------------------------------------------


def _list_open_sre_for_batch(
	item_code, warehouse, batch_no, manufacturing_work_order=None
):
	from frappe.query_builder.functions import Sum

	sb = frappe.qb.DocType("Serial and Batch Entry")
	sre = frappe.qb.DocType("Stock Reservation Entry")
	q = (
		frappe.qb.from_(sre)
		.inner_join(sb)
		.on(sre.name == sb.parent)
		.select(sre.name, sre.warehouse, Sum(sb.qty - sb.delivered_qty).as_("open_qty"))
		.where(sre.docstatus == 1)
		.where(sre.item_code == item_code)
		.where(sre.warehouse == warehouse)
		.where(sb.batch_no == batch_no)
		.where(sre.reserved_qty >= sre.delivered_qty)
		.where(sre.status.notin(["Delivered", "Cancelled"]))
		.where(sre.reservation_based_on == "Serial and Batch")
		.groupby(sre.name, sre.warehouse)
	)
	if manufacturing_work_order:
		q = q.where(sre.manufacturing_work_order == manufacturing_work_order)
	return q.run(as_dict=True)


# ---------------------------------------------------------------------------
# Sunday cron: delete all cancelled SREs
# ---------------------------------------------------------------------------


def delete_cancelled_stock_reservations():
	"""Registered as a daily scheduler event; self-gates to run only on Sundays."""
	if datetime.datetime.now().weekday() != 6:  # 6 = Sunday
		return

	cancelled = frappe.db.get_all(
		"Stock Reservation Entry", filters={"docstatus": 2}, pluck="name"
	)
	for name in cancelled:
		frappe.db.savepoint("del_cancelled_sre")
		try:
			frappe.delete_doc(
				"Stock Reservation Entry", name, ignore_permissions=True, force=True
			)
			frappe.db.release_savepoint("del_cancelled_sre")
		except Exception:
			frappe.db.rollback(save_point="del_cancelled_sre")
			frappe.log_error(
				title=f"Failed to delete cancelled SRE {name}",
				message=frappe.get_traceback(),
			)
