"""
EOD MOP Log Sync — converts unsynced MOP Log entries into a single consolidating Stock Entry
per Manufacturing Work Order.

MOP Logs with ``is_synced = 0`` represent virtual warehouse movements recorded by
Department IR and Employee IR. This module groups those logs by Manufacturing Work Order
and creates exactly ONE **Material Transfer to Department** Stock Entry per MWO that moves
items from the Stock Reservation Entry warehouse (where stock is physically reserved) to
the last Manufacturing Operation's final ``to_warehouse``.

**Last operation detection**: uses the MOP Log ``creation`` timestamp to determine which
operation is last — ``flow_index`` is per-MOP-scoped and unreliable for cross-MOP comparison.

**Loss entries**: Process Loss Stock Entries are NOT created here. Loss handling is owned
by the Employee IR Process Loss feature (``doc_events/loss_stock_entry.py``).

**Syncing**: after SE submit, ALL unsynced non-cancelled MOP Logs for the MWO are marked
synced atomically within the same savepoint.

**Stock Reservation:** ``Material Transfer to Department`` is not in the configured
``Stock Entry Type To Reservation`` list, so submitting the EOD SE does NOT auto-create
new SREs. Source warehouse is resolved from the existing SRE for each item/batch/MWO.

Before submit, batch lines are checked with ``get_batch_qty(..., ignore_reserved_stock=True)``
at the **source** warehouse so MOP Log totals cannot exceed **physical** batch stock (SLE).
"""

import frappe
from frappe import _
from frappe.utils import cint, flt


def sync_mop_logs():
	"""Main entry point. Returns a summary dict for the UI."""
	unsynced_groups = _get_unsynced_mop_groups()
	processed = 0
	stock_entries = []

	for group_key, mop_data_list in unsynced_groups.items():
		frappe.db.savepoint("mop_eod_sync_hop")
		try:
			se_names, count = _sync_mwo_group(group_key, mop_data_list)
			stock_entries.extend(se_names)
			processed += count
			now_ts = frappe.utils.now()
			for d in mop_data_list:
				frappe.db.set_value(
					"Manufacturing Operation",
					d["mop_name"],
					"last_eod_sync_on",
					now_ts,
					update_modified=False,
				)
			frappe.db.release_savepoint("mop_eod_sync_hop")
		except Exception:
			frappe.db.rollback(save_point="mop_eod_sync_hop")
			company, mwo = group_key
			frappe.log_error(
				title=f"MOP EOD Sync failed for MWO {mwo}",
				message=f"Company {company}\n{frappe.get_traceback()}",
			)

	processed_mwos = {key[1] for key in unsynced_groups.keys()}
	for mwo in processed_mwos:
		try:
			_reconcile_reservations_for_mwo(mwo, dry_run=True)
		except Exception:
			frappe.log_error(
				title=f"MOP EOD SRE reconcile failed for MWO {mwo}",
				message=frappe.get_traceback(),
			)

	return {"processed": processed, "stock_entries": stock_entries}


def _reconcile_reservations_for_mwo(mwo, dry_run=True):
	"""Audit-first Stock Reservation Entry reconciliation.

	Iterates active SREs for a MWO and finds rows where:
	  - SRE has remaining qty (reserved - delivered > 0), AND
	  - the latest non-cancelled MOP movement balance for the same
	    (item_code, warehouse) is zero.

	When dry_run=True (default), only logs which SREs would be cancelled.
	When dry_run=False, cancels them under per-SRE savepoints.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	sres = frappe.db.get_all(
		"Stock Reservation Entry",
		filters={"manufacturing_work_order": mwo, "docstatus": 1},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"manufacturing_operation",
		],
	)
	for sre in sres:
		remaining = flt(sre.reserved_qty) - flt(sre.delivered_qty)
		if remaining <= 0:
			continue
		if not sre.manufacturing_operation:
			continue
		balance_rows = get_current_mop_balance_rows(
			sre.manufacturing_operation,
			include_fields=[
				"item_code",
				"batch_no",
				"qty_after_transaction_batch_based as qty",
				"to_warehouse",
			],
		)
		matched = [
			b
			for b in balance_rows
			if b.get("item_code") == sre.item_code
			and b.get("to_warehouse") == sre.warehouse
		]
		balance_qty = sum(flt(b.get("qty")) for b in matched)
		if balance_qty <= 0:
			msg = (
				f"EOD SRE reconcile: {sre.name} (MWO {mwo}, item "
				f"{sre.item_code}, wh {sre.warehouse}) has no MOP balance "
				f"(remaining={remaining}, balance=0)."
			)
			if dry_run:
				frappe.logger().info(f"{msg} DRY-RUN -- would cancel.")
			else:
				frappe.db.savepoint("eod_sre_reconcile")
				try:
					frappe.get_doc("Stock Reservation Entry", sre.name).cancel()
					frappe.logger().info(f"{msg} CANCELLED.")
					frappe.db.release_savepoint("eod_sre_reconcile")
				except Exception:
					frappe.db.rollback(save_point="eod_sre_reconcile")
					frappe.log_error(
						title=f"EOD SRE reconcile cancel failed: {sre.name}",
						message=frappe.get_traceback(),
					)


def _get_unsynced_mop_groups():
	"""
	Return a dict of {(company, mwo): [{'mop_name': ..., 'mop_doc': ..., 'logs': ...}]}
	for all unsynced logs grouped by Manufacturing Work Order.
	"""
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
			"creation",
			"voucher_type",
			"voucher_no",
		],
		order_by="manufacturing_operation, flow_index asc, creation asc",
	)

	mop_logs = {}
	for log in logs:
		mop_logs.setdefault(log.manufacturing_operation, []).append(log)

	mop_cache = {}
	groups = {}

	for mop_name, op_logs in mop_logs.items():
		if mop_name not in mop_cache:
			mop_doc_dict = frappe.db.get_value(
				"Manufacturing Operation",
				mop_name,
				[
					"company",
					"manufacturer",
					"manufacturing_work_order",
					"manufacturing_order",
					"department",
					"loss_wt",
				],
				as_dict=True,
			)
			mop_cache[mop_name] = mop_doc_dict

		mop = mop_cache[mop_name]
		if not mop:
			continue

		group_key = (mop.company, mop.manufacturing_work_order)
		groups.setdefault(group_key, []).append(
			{"mop_name": mop_name, "mop_doc": mop, "logs": op_logs}
		)

	return groups


def _find_last_operation(mop_data_list):
	"""Return the mop_data entry whose logs have the highest creation timestamp.

	flow_index is per-MOP-scoped and unreliable for cross-MOP ordering, so
	creation timestamp is used to determine which operation came last.
	"""
	if not mop_data_list:
		return None
	best = None
	best_creation = None
	for mop_data in mop_data_list:
		for log in mop_data.get("logs") or []:
			log_creation = str(log.get("creation") or "")
			if best_creation is None or log_creation > best_creation:
				best_creation = log_creation
				best = mop_data
	return best


def _get_last_logs_per_item_batch(logs):
	"""Return one log per (item_code, batch_no) — the one with the highest
	(flow_index, creation) composite key, representing the latest balance snapshot.
	"""
	latest = {}
	for log in logs:
		key = (log.item_code, log.batch_no)
		cur = latest.get(key)
		cur_sort = (cur.flow_index, str(cur.get("creation") or "")) if cur else None
		new_sort = (log.flow_index, str(log.get("creation") or ""))
		if cur_sort is None or new_sort > cur_sort:
			latest[key] = log
	return list(latest.values())


def _get_t_warehouse_from_logs(logs):
	"""Return the first non-null to_warehouse found in a set of logs."""
	for log in logs:
		if log.to_warehouse:
			return log.to_warehouse
	return None


def _get_sre_source_warehouse(mwo, item_code, batch_no):
	"""Resolve the source warehouse for an EOD SE row from Stock Reservation Entry.

	Prefers a batch-level SRE match via Serial and Batch Entry child table.
	Falls back to a Qty-based SRE (no sb_entries) when no batch SRE exists.
	Returns None when no active SRE is found — caller logs and skips the row.
	"""
	rows = frappe.db.sql(
		"""
		SELECT sre.warehouse
		FROM `tabStock Reservation Entry` sre
		INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
		WHERE sre.manufacturing_work_order = %s
		  AND sre.item_code = %s
		  AND sbe.batch_no = %s
		  AND sre.docstatus = 1
		LIMIT 1
		""",
		(mwo, item_code, batch_no),
		as_dict=True,
	)
	if rows:
		return rows[0]["warehouse"]
	return frappe.db.get_value(
		"Stock Reservation Entry",
		{"manufacturing_work_order": mwo, "item_code": item_code, "docstatus": 1},
		"warehouse",
	)


def _build_eod_se_rows(mwo, last_mop_name, last_logs, t_warehouse):
	"""Build Stock Entry item rows for the EOD material transfer.

	Source warehouse comes from the active SRE for each item/batch/MWO.
	Rows where s_warehouse == t_warehouse (no movement needed) are silently skipped.
	Rows with no SRE are skipped with a logged error so one missing reservation
	does not block the rest of the MWO's items.
	"""
	rows = []
	for log in last_logs:
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue

		s_warehouse = _get_sre_source_warehouse(mwo, log.item_code, log.batch_no)
		if not s_warehouse:
			frappe.log_error(
				title=f"MOP EOD Sync: no SRE warehouse for {log.item_code} batch {log.batch_no}",
				message=f"MWO {mwo} — skipping row; fix reservation before next EOD.",
			)
			continue

		if s_warehouse == t_warehouse:
			continue

		row = {
			"item_code": log.item_code,
			"qty": qty,
			"s_warehouse": s_warehouse,
			"t_warehouse": t_warehouse,
			"manufacturing_operation": last_mop_name,
			"custom_manufacturing_work_order": mwo,
			"use_serial_batch_fields": 1,
		}
		if log.batch_no:
			row["batch_no"] = log.batch_no
		if getattr(log, "serial_no", None):
			row["serial_no"] = log.serial_no
		rows.append(row)
	return rows


def _sync_mwo_group(group_key, mop_data_list):
	"""Create ONE Material Transfer to Department SE for a complete MWO batch.

	1. Identifies the last Manufacturing Operation by latest MOP Log creation timestamp.
	2. Builds SE rows using SRE source warehouse and last-op's to_warehouse as target.
	3. Submits a single SE for all items in the batch.
	4. Marks ALL unsynced non-cancelled MOP Logs for the MWO as synced.

	Returns (list_of_se_names, log_count).
	"""
	company, mwo = group_key
	se_names = []
	all_logs = [log for md in mop_data_list for log in md.get("logs") or []]

	last_mop_data = _find_last_operation(mop_data_list)
	if not last_mop_data:
		_mark_all_mwo_mop_logs_synced([mwo])
		return [], len(all_logs)

	last_mop_name = last_mop_data["mop_name"]
	last_logs = _get_last_logs_per_item_batch(last_mop_data["logs"])

	t_warehouse = _get_t_warehouse_from_logs(last_logs)
	if not t_warehouse:
		frappe.log_error(
			title=f"MOP EOD Sync: no target warehouse for MWO {mwo}",
			message=(
				f"Last operation {last_mop_name} logs have no to_warehouse. "
				"Logs remain unsynced until to_warehouse is available."
			),
		)
		return [], 0

	items = _build_eod_se_rows(mwo, last_mop_name, last_logs, t_warehouse)

	if items:
		_validate_eod_items_for_mwo_reservation(items)
		_validate_eod_source_batch_stock(
			items,
			manufacturing_work_order=mwo,
			mop_data_list=mop_data_list,
			company=company,
		)
		manufacturing_order = last_mop_data["mop_doc"].get("manufacturing_order")
		se_name = _submit_eod_material_transfer_se(
			company,
			mwo,
			manufacturing_order,
			items,
			header_mop_name=last_mop_name,
			header_manufacturer=_mop_manufacturer_label(last_mop_data["mop_doc"]),
		)
		se_names.append(se_name)

	_mark_all_mwo_mop_logs_synced([mwo])
	return se_names, len(all_logs)


def _mark_all_mwo_mop_logs_synced(manufacturing_work_orders, stock_entry_name=None):
	"""Mark ALL unsynced non-cancelled MOP Logs for the given MWOs as synced.

	Called AFTER SE submit so the savepoint can roll back both the SE and the
	mark-synced together if anything fails.
	``stock_entry_name`` is accepted for future audit trail use.
	"""
	if not manufacturing_work_orders:
		return
	frappe.db.set_value(
		"MOP Log",
		{
			"manufacturing_work_order": ["in", manufacturing_work_orders],
			"is_synced": 0,
			"is_cancelled": 0,
		},
		"is_synced",
		1,
	)


def _mop_manufacturer_label(mop):
	"""Manufacturer from cached Manufacturing Operation row (dict or document-like object)."""
	if mop is None:
		return None
	if isinstance(mop, dict):
		return mop.get("manufacturer")
	return getattr(mop, "manufacturer", None)


def _submit_eod_material_transfer_se(
	company,
	mwo,
	manufacturing_order,
	items,
	header_mop_name=None,
	header_manufacturer=None,
):
	"""Create, save, and submit one Material Transfer to Department Stock Entry; return name."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer to Department"
	se.company = company
	se.manufacturing_order = manufacturing_order
	se.manufacturing_work_order = mwo
	se.auto_created = 1
	if header_mop_name:
		se.manufacturing_operation = header_mop_name
	if header_manufacturer:
		se.manufacturer = header_manufacturer
	for item in items:
		se.append("items", item)
	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return se.name


def _validate_eod_items_for_mwo_reservation(items_to_transfer):
	"""
	Ensure lines carry batch/serial data required by ``stock_reservation_entry_for_mwo``
	on submit (Serial/Batch SRE needs ``row.batch_no`` on the Stock Entry row when the
	item is batch-tracked).
	"""
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
					"MOP EOD Sync: item {0} is batch-tracked but the MOP Log line has no Batch No "
					"(Manufacturing Operation {1}). Stock Reservation on submit cannot build "
					"sb_entries — fix the source MOP Log / vouchers, then retry."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)
		if cint(has_serial_no) and not item.get("serial_no"):
			frappe.throw(
				_(
					"MOP EOD Sync: item {0} is serialized but the MOP Log line has no Serial No "
					"(Manufacturing Operation {1})."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)


def _resolve_eod_manufacturer_label(mop_data_list, manufacturing_work_order):
	"""Manufacturer from Manufacturing Operation rows; fallback to Manufacturing Work Order."""
	if not mop_data_list:
		if manufacturing_work_order:
			return frappe.db.get_value(
				"Manufacturing Work Order", manufacturing_work_order, "manufacturer"
			)
		return None
	mfrs = set()
	for md in mop_data_list:
		mdoc = md.get("mop_doc")
		if not mdoc:
			continue
		m = mdoc.get("manufacturer")
		if m:
			mfrs.add(m)
	if mfrs:
		return ", ".join(sorted(mfrs))
	if manufacturing_work_order:
		return frappe.db.get_value(
			"Manufacturing Work Order", manufacturing_work_order, "manufacturer"
		)
	return None


def _collect_mop_names(mop_data_list):
	if not mop_data_list:
		return ""
	names = sorted({md.get("mop_name") for md in mop_data_list if md.get("mop_name")})
	return ", ".join(names)


def _list_open_sre_for_batch(
	item_code, warehouse, batch_no, manufacturing_work_order=None
):
	"""Submitted Serial/Batch SRE rows with undelivered qty at ``warehouse`` (diagnostics)."""
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


def _list_open_sre_other_warehouses(
	item_code, batch_no, manufacturing_work_order=None, exclude_warehouse=None, limit=5
):
	"""Open SRE lines for same item/batch but different warehouse (wrong-WH hint)."""
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
		.where(sb.batch_no == batch_no)
		.where(sre.reserved_qty >= sre.delivered_qty)
		.where(sre.status.notin(["Delivered", "Cancelled"]))
		.where(sre.reservation_based_on == "Serial and Batch")
		.groupby(sre.name, sre.warehouse)
		.limit(limit)
	)
	if exclude_warehouse:
		q = q.where(sre.warehouse != exclude_warehouse)
	if manufacturing_work_order:
		q = q.where(sre.manufacturing_work_order == manufacturing_work_order)
	return q.run(as_dict=True)


def _format_batch_short_diagnostics(
	item_code,
	warehouse,
	batch_no,
	req_qty,
	physical,
	manufacturing_work_order,
	mop_data_list,
	company,
):
	"""Extra lines for ValidationError when physical batch qty is insufficient."""
	lines = []
	if company:
		lines.append(_("Company: {0}").format(company))
	if manufacturing_work_order:
		lines.append(
			_("Manufacturing Work Order: {0}").format(manufacturing_work_order)
		)
	mops = _collect_mop_names(mop_data_list)
	if mops:
		lines.append(_("Manufacturing Operation(s): {0}").format(mops))
	mfr = _resolve_eod_manufacturer_label(mop_data_list, manufacturing_work_order)
	if mfr:
		lines.append(_("Manufacturer: {0}").format(mfr))
	else:
		lines.append(_("Manufacturer: (not set on Operation / Work Order)"))

	sre_here = _list_open_sre_for_batch(
		item_code,
		warehouse,
		batch_no,
		manufacturing_work_order=manufacturing_work_order,
	)
	if not sre_here and manufacturing_work_order:
		sre_here = _list_open_sre_for_batch(
			item_code, warehouse, batch_no, manufacturing_work_order=None
		)

	total_open_here = 0.0
	for row in sre_here:
		oq = flt(row.get("open_qty"), 3)
		if oq <= 0:
			continue
		total_open_here += oq
		lines.append(
			_("Open Stock Reservation Entry {0} @ {1}: undelivered {2}").format(
				row.get("name"), row.get("warehouse"), oq
			)
		)

	if physical <= 1e-6 and total_open_here > 1e-6:
		lines.append(
			_(
				"Hint: physical batch qty is 0 but open reservation(s) exist at this warehouse — "
				"likely stale SRE or stock moved without updating reservation; cancel/amend SRE or restore stock."
			)
		)
	elif not sre_here:
		other = _list_open_sre_other_warehouses(
			item_code,
			batch_no,
			manufacturing_work_order=manufacturing_work_order,
			exclude_warehouse=warehouse,
		)
		if other:
			parts = [
				_("{0} @ {1} (open {2})").format(
					r.get("name"), r.get("warehouse"), flt(r.get("open_qty"), 3)
				)
				for r in other
				if flt(r.get("open_qty"), 3) > 0
			]
			if parts:
				lines.append(
					_("Open reservations on other warehouse(s) (sample): {0}").format(
						"; ".join(parts)
					)
				)

	return "\n".join(lines)


def _get_sre_undelivered_batch_qty(
	item_code, warehouse, batch_no, manufacturing_work_order=None
):
	"""Sum undelivered qty on submitted Serial/Batch Stock Reservation Entry rows (audit helper)."""
	from frappe.query_builder.functions import Sum

	sb = frappe.qb.DocType("Serial and Batch Entry")
	sre = frappe.qb.DocType("Stock Reservation Entry")
	q = (
		frappe.qb.from_(sre)
		.inner_join(sb)
		.on(sre.name == sb.parent)
		.select(Sum(sb.qty - sb.delivered_qty).as_("qty"))
		.where(sre.docstatus == 1)
		.where(sre.item_code == item_code)
		.where(sre.warehouse == warehouse)
		.where(sb.batch_no == batch_no)
		.where(sre.reserved_qty >= sre.delivered_qty)
		.where(sre.status.notin(["Delivered", "Cancelled"]))
		.where(sre.reservation_based_on == "Serial and Batch")
	)
	if manufacturing_work_order:
		q = q.where(sre.manufacturing_work_order == manufacturing_work_order)
	rows = q.run(as_list=True)
	if not rows or rows[0][0] is None:
		return 0.0
	return flt(rows[0][0], 3)


def _validate_eod_source_batch_stock(
	items_to_transfer,
	manufacturing_work_order=None,
	mop_data_list=None,
	company=None,
):
	"""
	Ensure aggregated transfer qty per (source warehouse, item, batch) does not exceed
	**physical** batch balance (SLE / serial-batch ledger), ignoring ERPNext's net
	"pickable" qty that subtracts undelivered Stock Reservation Entry rows.
	"""
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
			detail = _format_batch_short_diagnostics(
				item_code,
				wh,
				batch_no,
				req_qty,
				physical,
				manufacturing_work_order,
				mop_data_list,
				company,
			)
			main = _(
				"MOP EOD Sync: cannot move {0} of item {1}, batch {2} from {3}: "
				"MOP Log(s) require {4} but only {5} physical qty exists for this batch in that warehouse "
				"(short by {6}; SLE / batch ledger — reservation does not create stock). "
				"Reconcile vouchers, MOP Log, or cancel stale reservations."
			).format(
				frappe.bold(req_qty),
				frappe.bold(item_code),
				frappe.bold(batch_no),
				frappe.bold(wh),
				req_qty,
				physical,
				short,
			)
			frappe.throw(
				main + "\n\n" + detail,
				title=_("MOP EOD Sync — insufficient batch stock"),
			)

		if manufacturing_work_order:
			sre_open = _get_sre_undelivered_batch_qty(
				item_code,
				wh,
				batch_no,
				manufacturing_work_order=manufacturing_work_order,
			)
			if sre_open + 1e-6 < req_qty:
				frappe.log_error(
					title=_("MOP EOD Sync — reservation audit"),
					message=_(
						"Transfer {0} {1} batch {2} from {3} (MWO {4}): physical qty {5} allows the move, "
						"but undelivered Stock Reservation Entry qty for this item/batch/warehouse/MWO is only {6}. "
						"Verify SO reservation vs physical issue rules."
					).format(
						req_qty,
						item_code,
						batch_no,
						wh,
						manufacturing_work_order,
						physical,
						sre_open,
					),
				)
