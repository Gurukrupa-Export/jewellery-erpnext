"""
EOD MOP Log Sync — converts unsynced MOP Log entries into a single consolidating Stock Entry
per Manufacturing Work Order.

MOP Logs with ``is_synced = 0`` represent virtual warehouse movements recorded by
Department IR and Employee IR. This module groups those logs by Manufacturing Work Order
and creates exactly ONE **Material Transfer to Department** Stock Entry per MWO that moves
items from the Stock Reservation Entry warehouse (where stock is physically reserved) to
the last Manufacturing Operation's final ``to_warehouse``.

**Today-only filter (scheduled mode)**: With no enabled MWO filter rows, only MOP
Logs created today (creation >= today 00:00:00 and < tomorrow 00:00:00) are processed.
Old unsynced logs are counted and reported but never marked synced or used for SEs.

**Selective mode (MWO filter)**: If MOP Settings has enabled rows in
``eod_sync_work_order_filter``, the run switches to selective mode: only the listed
MWOs are processed, but their FULL unsynced history is synced (the today-only window
and the per-row ``sync_from_datetime`` are both ignored). An optional
``manufacturing_operation`` on a row still narrows which logs are collected; all other
MWOs are excluded and logged in the Sync Log as "Excluded".

**Serial Number Creator artifact**: If a submitted ``Manufacture`` Stock Entry already
exists for an MWO, the Serial Number Creator has physically consumed the reserved stock.
EOD sync then skips the Material Transfer for that MWO and only marks its leftover MOP
Logs synced, attributing them to the artifact SE.

**Last operation detection**: uses the MOP Log ``creation`` timestamp to determine which
operation is last — ``flow_index`` is per-MOP-scoped and unreliable for cross-MOP comparison.

**Loss entries**: Process Loss Stock Entries are NOT created here. Loss handling is owned
by the Employee IR Process Loss feature (``doc_events/loss_stock_entry.py``).

**Syncing**: after SE submit, ALL unsynced non-cancelled MOP Logs for the MWO (created today)
are marked synced atomically within the same savepoint.

**Transaction phases per MWO**:
  Phase 1 (savepoint "eod_draft_phase"): validate + save SE as draft.
  Phase 2 (savepoint "eod_submit_phase"): cancel the MWO's source-warehouse SREs
    (releasing the reservation so the transfer can consume the stock), submit the SE,
    re-reserve at the target warehouse where the stock lands, then mark MOP Logs synced.
  On Phase 2 failure: rollback to submit savepoint; the cancelled SREs are restored and
    the draft SE survives for manual recovery.

**MOP EOD Sync Log**: every run creates/updates a MOP EOD Sync Log document with full
progress tracking. Every item/batch/MWO decision is recorded in the child table.

**Error reporting**: all failures are collected in-memory; ONE consolidated Error Log is
created at the end of the full sync — no per-row or per-MWO log_error calls in loops.
"""

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	cint,
	flt,
	now_datetime,
	nowdate,
)

from .eod_lock import release_eod_sync_lock, set_eod_sync_running

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def sync_mop_logs(sync_log_name=None):
	"""Main entry point called by scheduler or manual trigger. Returns a summary dict."""
	# If no sync log provided (legacy path), create one now
	if not sync_log_name:
		try:
			sync_log = frappe.new_doc("MOP EOD Sync Log")
			sync_log.status = "Queued"
			sync_log.trigger_type = "Manual"
			sync_log.started_by = frappe.session.user or "Administrator"
			sync_log.posting_date = nowdate()
			sync_log.mop_settings = "MOP Settings"
			sync_log.flags.ignore_permissions = True
			sync_log.insert()
			sync_log_name = sync_log.name
			frappe.db.set_value(
				"MOP Settings", "MOP Settings", "eod_sync_last_sync_log", sync_log_name
			)
		except Exception:
			frappe.logger().exception("MOP EOD Sync: could not create MOP EOD Sync Log")
			sync_log_name = None

	frappe.flags.in_eod_mop_sync = True
	set_eod_sync_running(sync_log_name=sync_log_name)

	failures = []
	stats = {
		"total_mwos": 0,
		"processed_mwos": 0,
		"failed_mwos": 0,
		"submitted_ses": [],
		"draft_ses": [],
		"artifact_skipped": [],
		"started_on": now_datetime(),
	}

	try:
		settings = frappe.get_doc("MOP Settings")
		# Enabled MWO filter rows switch the whole run into selective full-history mode.
		selective = any(
			cint(r.enabled) and r.manufacturing_work_order
			for r in (settings.eod_sync_work_order_filter or [])
		)
		unsynced_groups = _get_unsynced_mop_groups(
			settings=settings, sync_log_name=sync_log_name
		)
		stats["total_mwos"] = len(unsynced_groups)

		if sync_log_name:
			frappe.db.set_value(
				"MOP EOD Sync Log",
				sync_log_name,
				{
					"total_mwos": stats["total_mwos"],
					"progress_message": f"Processing {stats['total_mwos']} Manufacturing Work Orders...",
				},
				update_modified=False,
			)
			frappe.db.commit()

		for group_key, mop_data_list in unsynced_groups.items():
			_process_mwo_group(
				group_key,
				mop_data_list,
				failures,
				stats,
				sync_log_name,
				selective=selective,
			)
			# Update progress after each MWO
			if sync_log_name:
				recalculate_sync_log_totals(sync_log_name)
				frappe.db.set_value(
					"MOP EOD Sync Log",
					sync_log_name,
					"progress_message",
					f"Processed {stats['processed_mwos']} / {stats['total_mwos']} MWOs...",
					update_modified=False,
				)
				frappe.db.commit()

		# SRE reconciliation audit
		for mwo in {k[1] for k in unsynced_groups}:
			try:
				_reconcile_reservations_for_mwo(mwo, dry_run=True)
			except Exception:
				failures.append(
					{
						"step": "sre_reconcile",
						"mwo": mwo,
						"error_message": "SRE reconciliation check raised an exception.",
						"traceback": frappe.get_traceback(),
						"suggested_fix": (
							"Investigate the SRE reconcile error for MWO "
							f"{mwo}. This does not affect SE or MOP Log sync status."
						),
					}
				)

		error_log_name = None
		if failures:
			error_log_name = _create_consolidated_error_log(
				failures, stats, sync_log_name
			)
			release_eod_sync_lock(
				success=False,
				error_log_name=error_log_name,
				sync_log_name=sync_log_name,
			)
		else:
			release_eod_sync_lock(success=True, sync_log_name=sync_log_name)

		# Finalize sync log
		if sync_log_name:
			recalculate_sync_log_totals(sync_log_name)
			now = now_datetime()
			started = stats["started_on"]
			duration = (now - started).total_seconds() if started else 0
			final_status = (
				"Completed"
				if not failures
				else "Partially Completed"
				if stats["processed_mwos"] > 0
				else "Failed"
			)
			artifact_count = len(stats.get("artifact_skipped") or [])
			progress_message = f"Sync {final_status}."
			if artifact_count:
				progress_message += (
					f" {artifact_count} MWO(s) already realized by Serial Number Creator "
					"product artifact — marked synced, no transfer."
				)
			frappe.db.set_value(
				"MOP EOD Sync Log",
				sync_log_name,
				{
					"status": final_status,
					"completed_on": now,
					"duration_seconds": flt(duration, 1),
					"submitted_stock_entries": ", ".join(stats["submitted_ses"]) or "",
					"draft_stock_entries": ", ".join(stats["draft_ses"]) or "",
					"error_log": error_log_name or "",
					"progress_message": progress_message,
					"synced_mwos": stats["processed_mwos"],
					"failed_mwos": stats["failed_mwos"],
					"processed_mwos": stats["processed_mwos"] + stats["failed_mwos"],
				},
				update_modified=False,
			)
			frappe.db.commit()

	except Exception:
		tb = frappe.get_traceback()
		failures.append(
			{
				"step": "top_level",
				"error_message": "Unexpected top-level exception in sync_mop_logs.",
				"traceback": tb,
				"suggested_fix": "Investigate the traceback below and fix the root cause before retrying.",
			}
		)
		error_log_name = _create_consolidated_error_log(failures, stats, sync_log_name)
		release_eod_sync_lock(
			success=False, error_log_name=error_log_name, sync_log_name=sync_log_name
		)
		if sync_log_name:
			frappe.db.set_value(
				"MOP EOD Sync Log",
				sync_log_name,
				{
					"status": "Failed",
					"completed_on": now_datetime(),
					"error_log": error_log_name or "",
					"last_error": tb[:2000] if tb else "",
					"progress_message": "Sync failed with unexpected error.",
				},
				update_modified=False,
			)
			frappe.db.commit()

	finally:
		frappe.flags.in_eod_mop_sync = False

	return {
		"processed": stats["processed_mwos"],
		"stock_entries": stats["submitted_ses"],
	}


# ---------------------------------------------------------------------------
# Per-MWO processing
# ---------------------------------------------------------------------------


def _process_mwo_group(
	group_key, mop_data_list, failures, stats, sync_log_name=None, selective=False
):
	"""Process one (company, mwo) group through two savepoint phases.

	When ``selective`` is True (an enabled MWO filter row exists for this MWO),
	the full unsynced history is synced and marked — not just today's logs.
	"""
	company, mwo = group_key
	all_mop_names = [md["mop_name"] for md in mop_data_list]

	# If the Serial Number Creator already produced this MWO's finished product
	# (submitted Manufacture Stock Entry), the reserved stock is already physically
	# consumed. A Material Transfer here would fail against missing stock, so skip
	# the transfer and just mark the leftover MOP Logs synced, attributed to the artifact.
	artifact_se = _mwo_realized_by_artifact(mwo)
	if artifact_se:
		_mark_all_mwo_mop_logs_synced([mwo], selective=selective)
		_stamp_last_eod_sync(mop_data_list)
		stats["processed_mwos"] += 1
		stats.setdefault("artifact_skipped", []).append(mwo)
		if sync_log_name:
			for md in mop_data_list:
				for log in md.get("logs") or []:
					_insert_sync_log_item(
						sync_log_name,
						{
							"manufacturing_work_order": mwo,
							"manufacturing_operation": md["mop_name"],
							"company": company,
							"item_code": log.item_code,
							"batch_no": log.batch_no,
							"serial_no": getattr(log, "serial_no", None),
							"qty": flt(log.qty_after_transaction_batch_based, 3),
							"status": "Synced",
							"is_synced": 1,
							"stock_entry": artifact_se,
							"sync_stage": "Completed",
							"error_message": (
								f"Realized by Serial Number Creator product artifact {artifact_se}; "
								"no Material Transfer needed."
							),
							"completed_on": now_datetime(),
						},
					)
		return

	last_mop_data = _find_last_operation(mop_data_list)
	if not last_mop_data:
		_mark_all_mwo_mop_logs_synced([mwo], selective=selective)
		stats["processed_mwos"] += 1
		return

	last_mop_name = last_mop_data["mop_name"]
	last_logs = _get_last_logs_per_item_batch(last_mop_data["logs"])
	# Resolve the target (department) warehouse. The last operation may be an
	# Employee IR receive whose audit MOP Logs carry no warehouse, so fall back to
	# the latest to_warehouse across ALL the MWO's logs, then to the department's
	# Manufacturing warehouse.
	t_warehouse = (
		_get_t_warehouse_from_logs(last_logs)
		or _get_t_warehouse_from_logs(
			[log for md in mop_data_list for log in (md.get("logs") or [])]
		)
		or _resolve_department_warehouse(last_mop_data.get("mop_doc"))
	)

	if not t_warehouse:
		failures.append(
			{
				"step": "no_t_warehouse",
				"mwo": mwo,
				"company": company,
				"last_mop": last_mop_name,
				"affected_mops": all_mop_names,
				"error_message": (
					f"Last operation {last_mop_name} logs have no to_warehouse. "
					"Logs remain unsynced until to_warehouse is available."
				),
				"suggested_fix": (
					"Ensure the last MOP Log row for this MWO has a non-null to_warehouse. "
					"Check Department IR or Employee IR receive vouchers."
				),
			}
		)
		stats["failed_mwos"] += 1
		# Log all items as failed
		if sync_log_name:
			for log in last_logs:
				_insert_sync_log_item(
					sync_log_name,
					{
						"manufacturing_work_order": mwo,
						"manufacturing_operation": last_mop_name,
						"company": company,
						"item_code": log.item_code,
						"batch_no": log.batch_no,
						"qty": flt(log.qty_after_transaction_batch_based, 3),
						"status": "Failed",
						"sync_stage": "Resolve SRE Warehouse",
						"error_type": "Missing Target Warehouse",
						"error_message": f"Last operation {last_mop_name} has no to_warehouse.",
						"suggested_fix": "Check Department IR or Employee IR receive vouchers.",
					},
				)
		return

	# One query for all (item_code, batch_no) → warehouse for this MWO
	sre_map = _preload_sre_warehouse_map(mwo)

	items, skipped_rows = _build_eod_se_rows(
		mwo, last_mop_name, last_logs, t_warehouse, sre_map
	)

	for skip in skipped_rows:
		failures.append(
			{
				"step": "no_sre_warehouse",
				"mwo": mwo,
				"company": company,
				"last_mop": last_mop_name,
				"affected_mops": all_mop_names,
				"item_code": skip.get("item_code"),
				"batch_no": skip.get("batch_no"),
				"t_warehouse": t_warehouse,
				"error_message": (
					f"No active SRE warehouse found for item {skip.get('item_code')} "
					f"batch {skip.get('batch_no')} MWO {mwo}. Row skipped."
				),
				"suggested_fix": (
					"A submitted Stock Reservation Entry must exist for this item/batch/MWO "
					"before EOD can transfer stock. Fix or create the SRE and retry."
				),
			}
		)
		if sync_log_name:
			_insert_sync_log_item(
				sync_log_name,
				{
					"manufacturing_work_order": mwo,
					"manufacturing_operation": last_mop_name,
					"company": company,
					"item_code": skip.get("item_code"),
					"batch_no": skip.get("batch_no"),
					"target_warehouse": t_warehouse,
					"status": "Failed",
					"sync_stage": "Resolve SRE Warehouse",
					"error_type": "Missing SRE",
					"error_message": f"No active SRE warehouse for item {skip.get('item_code')} batch {skip.get('batch_no')} MWO {mwo}.",
					"suggested_fix": "Create or fix submitted Stock Reservation Entry for this item/batch/MWO, then retry EOD sync.",
				},
			)

	if not items:
		frappe.logger().info(
			"MOP EOD Sync MWO %s: no SE rows to create (same WH or all SRE missing); "
			"marking logs synced.",
			mwo,
		)
		_mark_all_mwo_mop_logs_synced([mwo], selective=selective)
		stats["processed_mwos"] += 1
		return

	# Insert child log rows as Pending before we start
	child_row_names = []
	if sync_log_name:
		for item in items:
			row_name = _insert_sync_log_item(
				sync_log_name,
				{
					"manufacturing_work_order": mwo,
					"manufacturing_operation": item.get("manufacturing_operation")
					or last_mop_name,
					"company": company,
					"item_code": item.get("item_code"),
					"batch_no": item.get("batch_no"),
					"serial_no": item.get("serial_no"),
					"source_warehouse": item.get("s_warehouse"),
					"target_warehouse": item.get("t_warehouse") or t_warehouse,
					"qty": flt(item.get("qty"), 3),
					"status": "Pending",
					"sync_stage": "Save Draft Stock Entry",
				},
			)
			child_row_names.append(row_name)

	# Phase 1 — Validate and save draft SE
	draft_se_name = None
	try:
		frappe.db.savepoint("eod_draft_phase")
		_validate_eod_items_for_mwo_reservation(items)
		_validate_eod_source_batch_stock(
			items,
			manufacturing_work_order=mwo,
			mop_data_list=mop_data_list,
			company=company,
		)
		manufacturing_order = last_mop_data["mop_doc"].get("manufacturing_order")
		draft_se_name = _save_draft_eod_se(
			company,
			mwo,
			manufacturing_order,
			items,
			header_mop_name=last_mop_name,
			header_manufacturer=_mop_manufacturer_label(last_mop_data["mop_doc"]),
			sync_log_name=sync_log_name,
		)
		frappe.db.release_savepoint("eod_draft_phase")
	except Exception as exc:
		frappe.db.rollback(save_point="eod_draft_phase")
		failures.append(
			{
				"step": "draft_save",
				"mwo": mwo,
				"company": company,
				"last_mop": last_mop_name,
				"affected_mops": all_mop_names,
				"t_warehouse": t_warehouse,
				"error_message": str(exc),
				"traceback": frappe.get_traceback(),
				"suggested_fix": (
					"Check MOP Log data: item codes, batch numbers, warehouses, and "
					"Stock Reservation Entries. Verify physical batch stock at source warehouse."
				),
			}
		)
		stats["failed_mwos"] += 1
		# Update child rows to Failed
		if sync_log_name and child_row_names:
			for rn in child_row_names:
				frappe.db.set_value(
					"MOP EOD Sync Log Item",
					rn,
					{
						"status": "Failed",
						"sync_stage": "Save Draft Stock Entry",
						"error_type": "Stock Entry Save Failed",
						"error_message": str(exc)[:500],
						"technical_traceback": frappe.get_traceback()[:3000],
						"suggested_fix": "Check MOP Log data: item codes, batch numbers, warehouses, SREs, and physical batch stock.",
						"completed_on": now_datetime(),
					},
					update_modified=False,
				)
		return

	# Phase 2 — Submit SE and mark logs synced (savepoint starts AFTER draft is saved)
	try:
		frappe.db.savepoint("eod_submit_phase")
		# The MWO's stock is reserved at the source warehouse, which would block the
		# transfer from consuming it. Release the reservation by cancelling those SREs
		# BEFORE submit. A Phase-2 failure rolls the savepoint back, restoring them.
		sre_snapshots = _snapshot_mwo_sres_for_relocation(mwo, items, t_warehouse)
		_cancel_sre_snapshots(sre_snapshots)
		frappe.get_doc("Stock Entry", draft_se_name).submit()
		_mark_all_mwo_mop_logs_synced([mwo], selective=selective)
		_stamp_last_eod_sync(mop_data_list)
		frappe.db.release_savepoint("eod_submit_phase")
		stats["submitted_ses"].append(draft_se_name)
		stats["processed_mwos"] += 1

		# Re-reserve at the target where the stock now physically sits. Best-effort:
		# the transfer already succeeded, so a re-reservation hiccup must not undo it —
		# it only leaves the moved stock unreserved (logged for follow-up).
		_safe_recreate_sres_at(sre_snapshots, t_warehouse, mwo)

		# Update child rows to Synced
		if sync_log_name and child_row_names:
			for rn in child_row_names:
				frappe.db.set_value(
					"MOP EOD Sync Log Item",
					rn,
					{
						"status": "Synced",
						"is_synced": 1,
						"stock_entry": draft_se_name,
						"sync_stage": "Completed",
						"completed_on": now_datetime(),
					},
					update_modified=False,
				)

	except Exception as exc:
		# Only the submit + mark-synced steps are rolled back.
		# The draft SE (created in Phase 1, before this savepoint) is preserved.
		frappe.db.rollback(save_point="eod_submit_phase")
		failures.append(
			{
				"step": "submit",
				"mwo": mwo,
				"company": company,
				"last_mop": last_mop_name,
				"affected_mops": all_mop_names,
				"t_warehouse": t_warehouse,
				"draft_se": draft_se_name,
				"error_message": str(exc),
				"traceback": frappe.get_traceback(),
				"suggested_fix": (
					f"Stock Entry {draft_se_name} is saved as Draft but failed to submit. "
					"Review the validation error, fix the underlying issue, and submit manually. "
					"MOP Logs for this MWO remain unsynced until the SE is submitted."
				),
			}
		)
		stats["draft_ses"].append(draft_se_name)
		stats["failed_mwos"] += 1
		# Update child rows to Draft Created
		if sync_log_name and child_row_names:
			for rn in child_row_names:
				frappe.db.set_value(
					"MOP EOD Sync Log Item",
					rn,
					{
						"status": "Draft Created",
						"draft_stock_entry": draft_se_name,
						"sync_stage": "Submit Stock Entry",
						"error_type": "Stock Entry Submit Failed",
						"error_message": str(exc)[:500],
						"technical_traceback": frappe.get_traceback()[:3000],
						"suggested_fix": f"Open draft Stock Entry {draft_se_name}, fix the error, and submit manually. MOP Logs remain unsynced.",
						"completed_on": now_datetime(),
					},
					update_modified=False,
				)


# ---------------------------------------------------------------------------
# Consolidated failure reporting
# ---------------------------------------------------------------------------


def _create_consolidated_error_log(failures, stats, sync_log_name=None):
	"""Create one Error Log with full context for all EOD sync failures. Returns name."""
	now = frappe.utils.now()
	started = stats.get("started_on", "?")

	lines = [
		"=" * 80,
		"MOP EOD SYNC — CONSOLIDATED FAILURE REPORT",
		"=" * 80,
		"",
		"SUMMARY",
		f"  Sync Log          : {sync_log_name or 'N/A'}",
		f"  Started On        : {started}",
		f"  Ended On          : {now}",
		f"  Total MWOs found  : {stats.get('total_mwos', '?')}",
		f"  Processed (OK)    : {stats.get('processed_mwos', '?')}",
		f"  Failed            : {stats.get('failed_mwos', '?')} MWO(s)",
		f"  Submitted SEs     : {', '.join(stats.get('submitted_ses') or []) or 'None'}",
		f"  Draft SEs (stuck) : {', '.join(stats.get('draft_ses') or []) or 'None'}",
		"",
	]

	lines += ["FAILED DETAILS", "-" * 40]
	for i, f in enumerate(failures, 1):
		lines += [
			"",
			f"[Failure #{i}]",
			f"  Step          : {f.get('step', '?')}",
			f"  MWO           : {f.get('mwo', '?')}",
			f"  Company       : {f.get('company', '?')}",
			f"  Last MOP      : {f.get('last_mop', '?')}",
			f"  Affected MOPs : {', '.join(f.get('affected_mops') or [])}",
			f"  Item Code     : {f.get('item_code', '?')}",
			f"  Batch No      : {f.get('batch_no', '?')}",
			f"  S Warehouse   : {f.get('s_warehouse', '?')}",
			f"  T Warehouse   : {f.get('t_warehouse', '?')}",
			f"  Draft SE      : {f.get('draft_se') or 'None'}",
			f"  Error         : {f.get('error_message', '?')}",
			f"  Suggested Fix : {f.get('suggested_fix', '?')}",
		]

	lines += ["", "TRACEBACKS", "-" * 40]
	for i, f in enumerate(failures, 1):
		tb = f.get("traceback")
		if tb:
			lines += [
				"",
				f"[Failure #{i} — step={f.get('step')} mwo={f.get('mwo')}]",
				tb,
			]

	lines += [
		"",
		"GENERAL SUGGESTED FIXES",
		"  1. Check MOP Log to_warehouse for the last Manufacturing Operation.",
		"  2. Check Stock Reservation Entry source warehouse — a submitted SRE must exist.",
		"  3. Check physical batch stock at source warehouse (batch ledger / SLE).",
		"  4. Check for cancelled or stale SREs with remaining reserved qty.",
		"  5. Check missing batch_no / serial_no on MOP Log rows for tracked items.",
		"  6. For Draft SEs listed above: review, correct the issue, and submit manually.",
		"  7. After fixing root cause: retrigger EOD sync via MOP Settings > EOD Sync.",
		"=" * 80,
	]

	msg = "\n".join(lines)
	error_log = frappe.log_error(
		title=f"MOP EOD Sync Failed - {now}",
		message=msg,
	)
	return error_log.name if error_log else None


# ---------------------------------------------------------------------------
# SRE reconciliation (audit-first, dry-run default)
# ---------------------------------------------------------------------------


def _reconcile_reservations_for_mwo(mwo, dry_run=True):
	"""Audit-first Stock Reservation Entry reconciliation."""
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
				frappe.logger().info("%s DRY-RUN -- would cancel.", msg)
			else:
				frappe.db.savepoint("eod_sre_reconcile")
				try:
					frappe.get_doc("Stock Reservation Entry", sre.name).cancel()
					frappe.logger().info("%s CANCELLED.", msg)
					frappe.db.release_savepoint("eod_sre_reconcile")
				except Exception:
					frappe.db.rollback(save_point="eod_sre_reconcile")
					frappe.logger().exception(
						"EOD SRE reconcile cancel failed: %s", sre.name
					)


# ---------------------------------------------------------------------------
# Data gathering: unsynced MOP groups (today-only, with MWO filter)
# ---------------------------------------------------------------------------


def _get_today_range():
	"""Return (today_start_str, tomorrow_start_str) as datetime strings."""
	today = nowdate()
	tomorrow = add_days(today, 1)
	return f"{today} 00:00:00", f"{tomorrow} 00:00:00"


def _get_unsynced_mop_groups(settings=None, sync_log_name=None):
	"""Return a dict of {(company, mwo): [...]} of unsynced MOP Logs to process.

	Two modes, decided by the enabled MWO filter rows in MOP Settings:
	  * Selective (filter rows present): the FULL unsynced history for the listed
	    MWOs is fetched — the today-only window and per-row sync_from_datetime are
	    ignored. Used to backfill / fully re-sync a chosen MWO on demand.
	  * Scheduled (no filter rows): only today-created unsynced logs are fetched;
	    older unsynced logs are counted and reported but never processed.
	"""
	# Enabled MWO filter rows decide the mode. Compute them first.
	filter_rows = []
	if settings:
		for row in settings.eod_sync_work_order_filter or []:
			if cint(row.enabled) and row.manufacturing_work_order:
				filter_rows.append(row)
	selective = bool(filter_rows)

	log_fields = [
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
	]
	log_order_by = "manufacturing_operation, flow_index asc, creation asc"

	if selective:
		# Full unsynced history for the listed MWOs (ignore today-only window).
		filter_mwos = list({r.manufacturing_work_order for r in filter_rows})
		logs = frappe.db.get_all(
			"MOP Log",
			filters={
				"is_synced": 0,
				"is_cancelled": 0,
				"manufacturing_work_order": ["in", filter_mwos],
			},
			fields=log_fields,
			order_by=log_order_by,
		)
	else:
		today_start, tomorrow_start = _get_today_range()

		# Count old unsynced logs for reporting (today-only scheduled mode)
		old_logs = frappe.db.sql(
			"""
            SELECT COUNT(*) as cnt, COALESCE(SUM(qty_after_transaction_batch_based), 0) as qty
            FROM `tabMOP Log`
            WHERE is_synced = 0 AND is_cancelled = 0
              AND creation < %s
            """,
			(today_start,),
			as_dict=True,
		)
		old_count = cint(old_logs[0].get("cnt", 0)) if old_logs else 0
		old_qty = flt(old_logs[0].get("qty", 0), 3) if old_logs else 0.0

		if sync_log_name and old_count > 0:
			frappe.db.set_value(
				"MOP EOD Sync Log",
				sync_log_name,
				{
					"old_unsynced_mop_log_count": old_count,
					"old_unsynced_mop_log_qty": old_qty,
					"old_unsynced_mop_log_message": (
						f"{old_count} older unsynced MOP Log(s) (qty={old_qty}) were found but skipped "
						"because EOD Sync is configured to process only today-created logs."
					),
				},
				update_modified=False,
			)

		logs = frappe.db.get_all(
			"MOP Log",
			filters={
				"is_synced": 0,
				"is_cancelled": 0,
				"creation": ["between", [today_start, tomorrow_start]],
			},
			fields=log_fields,
			order_by=log_order_by,
		)

	if not logs:
		return {}

	# Apply MWO filter (membership + optional operation match; no datetime cutoff)
	excluded_logs = []
	if filter_rows:
		logs, excluded_logs = _apply_mwo_filter_rows(logs, filter_rows)

	# Insert excluded rows into sync log
	if sync_log_name and excluded_logs:
		for exc_log in excluded_logs:
			_insert_sync_log_item(
				sync_log_name,
				{
					"manufacturing_work_order": exc_log.manufacturing_work_order,
					"manufacturing_operation": exc_log.manufacturing_operation,
					"item_code": exc_log.item_code,
					"batch_no": exc_log.batch_no,
					"qty": flt(exc_log.qty_after_transaction_batch_based, 3),
					"status": "Excluded",
					"sync_stage": "Collect MOP Log",
					"error_type": exc_log.get("_exclude_reason")
					or "MWO Not In EOD Filter",
					"error_message": exc_log.get("_exclude_message")
					or "MOP Log excluded by MWO filter.",
					"suggested_fix": "Check the EOD Sync Work Order Filter table in MOP Settings.",
					"mop_log": exc_log.name,
				},
			)

	if not logs:
		return {}

	mop_logs = {}
	for log in logs:
		mop_logs.setdefault(log.manufacturing_operation, []).append(log)

	# Bulk fetch all MOP metadata in one query
	mop_names = list(mop_logs.keys())
	mop_rows = frappe.db.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", mop_names]},
		fields=[
			"name",
			"company",
			"manufacturer",
			"manufacturing_work_order",
			"manufacturing_order",
			"department",
			"loss_wt",
		],
	)
	mop_cache = {r.name: r for r in mop_rows}

	groups = {}
	for mop_name, op_logs in mop_logs.items():
		mop = mop_cache.get(mop_name)
		if not mop:
			continue
		group_key = (mop.company, mop.manufacturing_work_order)
		groups.setdefault(group_key, []).append(
			{"mop_name": mop_name, "mop_doc": mop, "logs": op_logs}
		)

	return groups


def _apply_mwo_filter_rows(logs, filter_rows):
	"""Filter logs to those matching the MWO filter table rows.

	A listed MWO is synced fully: only MWO membership and the optional
	``manufacturing_operation`` narrow the set. ``sync_from_datetime`` is no
	longer honored — selective sync deliberately covers the MWO's full history.

	Returns (included_logs, excluded_logs) where excluded logs have
	_exclude_reason and _exclude_message attributes set.
	"""
	# Build a lookup: mwo → list of filter_row (may carry an optional operation filter)
	filter_map = {}
	for row in filter_rows:
		filter_map.setdefault(row.manufacturing_work_order, []).append(row)

	included = []
	excluded = []

	for log in logs:
		mwo = log.manufacturing_work_order
		mop = log.manufacturing_operation

		if mwo not in filter_map:
			log._exclude_reason = "MWO Not In EOD Filter"
			log._exclude_message = f"MOP Log belongs to MWO {mwo} which is not in the enabled EOD Sync Work Order Filter."
			excluded.append(log)
			continue

		# Check each matching filter row
		matched = False
		for row in filter_map[mwo]:
			# Operation filter (if set)
			if row.manufacturing_operation and mop != row.manufacturing_operation:
				continue

			matched = True
			included.append(log)
			break

		if not matched and "_exclude_reason" not in log:
			log._exclude_reason = "Manufacturing Operation Not In Filter"
			log._exclude_message = (
				f"MOP Log operation {mop} does not match the configured Manufacturing Operation "
				f"filter for MWO {mwo}."
			)
			excluded.append(log)

	return included, excluded


def _find_last_operation(mop_data_list):
	"""Return the mop_data entry whose logs have the highest creation timestamp."""
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
	"""Return one log per (item_code, batch_no) — the latest balance snapshot."""
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
	"""Return the destination warehouse = the latest log's to_warehouse.

	The target is the last to_warehouse of the operation's logs, ordered by
	(flow_index, creation). Falls back to the first non-null to_warehouse if the
	chronologically last log has none.
	"""
	latest_log = None
	latest_sort = None
	for log in logs:
		if not log.to_warehouse:
			continue
		sort_key = (log.flow_index, str(log.get("creation") or ""))
		if latest_sort is None or sort_key > latest_sort:
			latest_sort = sort_key
			latest_log = log
	if latest_log is not None:
		return latest_log.to_warehouse
	return None


def _resolve_department_warehouse(mop_doc):
	"""Resolve the Manufacturing warehouse for a MOP's department.

	Fallback target when no MOP Log carries a to_warehouse (e.g. Employee IR
	receive audit logs). Mirrors the resolution used by Employee IR on submit.
	"""
	if mop_doc is None:
		return None
	department = (
		mop_doc.get("department")
		if isinstance(mop_doc, dict)
		else getattr(mop_doc, "department", None)
	)
	if not department:
		return None
	return frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Manufacturing"},
		"name",
	)


# ---------------------------------------------------------------------------
# SRE warehouse preload (eliminates N+1 per-item queries)
# ---------------------------------------------------------------------------


def _preload_sre_warehouse_map(mwo):
	"""Return {(item_code, batch_no): warehouse} for all submitted SREs of a MWO."""
	sre_map = {}

	batch_rows = frappe.db.sql(
		"""
        SELECT sre.item_code, sbe.batch_no, sre.warehouse
        FROM `tabStock Reservation Entry` sre
        INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
        WHERE sre.manufacturing_work_order = %s
          AND sre.docstatus = 1
        """,
		(mwo,),
		as_dict=True,
	)
	for row in batch_rows:
		key = (row.item_code, row.batch_no)
		if key not in sre_map:
			sre_map[key] = row.warehouse

	qty_rows = frappe.db.get_all(
		"Stock Reservation Entry",
		filters={"manufacturing_work_order": mwo, "docstatus": 1},
		fields=["item_code", "warehouse"],
	)
	for row in qty_rows:
		key = (row.item_code, None)
		if key not in sre_map:
			sre_map[key] = row.warehouse

	return sre_map


# ---------------------------------------------------------------------------
# SRE relocation (release reservation at source so the transfer can consume,
# then re-reserve at the target where the stock physically lands)
# ---------------------------------------------------------------------------


def _snapshot_mwo_sres_for_relocation(mwo, items, t_warehouse):
	"""Snapshot the MWO's submitted SREs that back the moved items at a source
	warehouse other than ``t_warehouse``. Captures the data needed to recreate
	them after the transfer. Returns a list of snapshot dicts."""
	moved_items = {it["item_code"] for it in items}
	source_whs = {it["s_warehouse"] for it in items if it.get("s_warehouse")}
	source_whs.discard(t_warehouse)
	if not moved_items or not source_whs:
		return []

	sres = frappe.db.get_all(
		"Stock Reservation Entry",
		filters={
			"manufacturing_work_order": mwo,
			"docstatus": 1,
			"item_code": ["in", list(moved_items)],
			"warehouse": ["in", list(source_whs)],
		},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"voucher_qty",
			"company",
			"stock_uom",
			"reservation_based_on",
			"manufacturing_work_order",
			"manufacturing_operation",
		],
	)

	snapshots = []
	for sre in sres:
		remaining = flt(sre.reserved_qty) - flt(sre.delivered_qty)
		if remaining <= 1e-9:
			continue
		has_batch_no, has_serial_no = frappe.get_cached_value(
			"Item", sre.item_code, ["has_batch_no", "has_serial_no"]
		)
		sb_entries = []
		if cint(has_batch_no):
			for sb in frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": sre.name},
				fields=["batch_no", "qty", "delivered_qty"],
			):
				rem = flt(sb.qty) - flt(sb.delivered_qty)
				if rem > 1e-9:
					sb_entries.append({"batch_no": sb.batch_no, "qty": rem})
		snapshots.append(
			{
				"sre": sre,
				"remaining": remaining,
				"has_batch_no": cint(has_batch_no),
				"has_serial_no": cint(has_serial_no),
				"sb_entries": sb_entries,
			}
		)
	return snapshots


def _cancel_sre_snapshots(snapshots):
	"""Cancel the snapshotted SREs, releasing their reservation at the source."""
	for snap in snapshots:
		doc = frappe.get_doc("Stock Reservation Entry", snap["sre"].name)
		if doc.docstatus == 1:
			doc.flags.ignore_permissions = True
			doc.cancel()


def _recreate_sres_at(snapshots, new_warehouse):
	"""Recreate the snapshotted SREs at ``new_warehouse`` (same voucher/qty/batches)."""
	if not snapshots:
		return
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	for snap in snapshots:
		sre = snap["sre"]
		# Batch-tracked SRE with no remaining batch qty → nothing to re-reserve.
		if snap["has_batch_no"] and not snap["sb_entries"]:
			continue
		if snap["sb_entries"]:
			available = get_available_qty_to_reserve(
				sre.item_code, new_warehouse, batch_no=snap["sb_entries"][0]["batch_no"]
			)
		else:
			available = get_available_qty_to_reserve(sre.item_code, new_warehouse)

		new_sre = frappe.new_doc("Stock Reservation Entry")
		new_sre.voucher_type = sre.voucher_type
		new_sre.voucher_no = sre.voucher_no
		new_sre.voucher_detail_no = sre.voucher_detail_no
		new_sre.voucher_qty = sre.voucher_qty
		new_sre.item_code = sre.item_code
		new_sre.warehouse = new_warehouse
		new_sre.reserved_qty = snap["remaining"]
		new_sre.company = sre.company
		new_sre.stock_uom = sre.stock_uom
		new_sre.reservation_based_on = sre.reservation_based_on
		new_sre.has_batch_no = snap["has_batch_no"]
		new_sre.has_serial_no = snap["has_serial_no"]
		new_sre.available_qty = max(flt(available), snap["remaining"])
		new_sre.manufacturing_work_order = sre.manufacturing_work_order
		new_sre.manufacturing_operation = sre.manufacturing_operation
		for sb in snap["sb_entries"]:
			new_sre.append("sb_entries", {"batch_no": sb["batch_no"], "qty": sb["qty"]})
		new_sre.flags.ignore_permissions = True
		new_sre.insert(ignore_links=1)
		new_sre.submit()


def _safe_recreate_sres_at(snapshots, new_warehouse, mwo):
	"""Re-reserve at the target, best-effort. Isolated in its own savepoint so a
	failure here cannot roll back the already-submitted transfer; it only leaves
	the moved stock unreserved and is logged for follow-up."""
	if not snapshots:
		return
	try:
		frappe.db.savepoint("eod_sre_rereserve")
		_recreate_sres_at(snapshots, new_warehouse)
		frappe.db.release_savepoint("eod_sre_rereserve")
	except Exception:
		frappe.db.rollback(save_point="eod_sre_rereserve")
		frappe.logger().exception(
			"MOP EOD Sync: re-reservation at %s failed for MWO %s; the transfer "
			"succeeded but the moved stock is left unreserved.",
			new_warehouse,
			mwo,
		)


# ---------------------------------------------------------------------------
# SE row construction
# ---------------------------------------------------------------------------


def _build_eod_se_rows(mwo, last_mop_name, last_logs, t_warehouse, sre_map):
	"""Build Stock Entry item rows for the EOD material transfer."""
	rows = []
	skipped = []
	for log in last_logs:
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue

		s_warehouse = sre_map.get((log.item_code, log.batch_no)) or sre_map.get(
			(log.item_code, None)
		)
		if not s_warehouse:
			skipped.append({"item_code": log.item_code, "batch_no": log.batch_no})
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
	return rows, skipped


# ---------------------------------------------------------------------------
# MOP Log sync-marking (today-only safety net, or full history in selective mode)
# ---------------------------------------------------------------------------


def _mark_all_mwo_mop_logs_synced(manufacturing_work_orders, selective=False):
	"""Mark unsynced non-cancelled MOP Logs for the given MWOs as synced.

	Scheduled runs (``selective=False``) only mark today's logs — a safety net so
	a misconfigured run can never silently bury an old backlog. Selective runs
	(an enabled MWO filter row) mark the MWO's full unsynced history.
	"""
	if not manufacturing_work_orders:
		return
	if selective:
		frappe.db.sql(
			"""
            UPDATE `tabMOP Log`
            SET is_synced = 1
            WHERE manufacturing_work_order IN %(mwos)s
              AND is_synced = 0
              AND is_cancelled = 0
            """,
			{"mwos": manufacturing_work_orders},
		)
		return
	today_start, tomorrow_start = _get_today_range()
	frappe.db.sql(
		"""
        UPDATE `tabMOP Log`
        SET is_synced = 1
        WHERE manufacturing_work_order IN %(mwos)s
          AND is_synced = 0
          AND is_cancelled = 0
          AND creation >= %(today_start)s
          AND creation < %(tomorrow_start)s
        """,
		{
			"mwos": manufacturing_work_orders,
			"today_start": today_start,
			"tomorrow_start": tomorrow_start,
		},
	)

# ---------------------------------------------------------------------------
# MOP Log sync-marking (today-only safety net)
# ---------------------------------------------------------------------------

def _mwo_realized_by_artifact(mwo):
	"""Return the submitted Manufacture Stock Entry name for this MWO, else None.

	The Serial Number Creator creates a submitted ``Manufacture`` Stock Entry (the
	finished-product artifact) for the MWO and physically consumes the reserved
	stock. When one exists, EOD sync must not attempt a Material Transfer for the
	MWO's leftover MOP Logs — they are realized by the artifact instead.
	"""
	if not mwo:
		return None
	return frappe.db.get_value(
		"Stock Entry",
		{
			"manufacturing_work_order": mwo,
			"stock_entry_type": "Manufacture",
			"docstatus": 1,
		},
		"name",
	)


def _stamp_last_eod_sync(mop_data_list):
	"""Stamp ``last_eod_sync_on`` on every Manufacturing Operation in the group."""
	now_ts = frappe.utils.now()
	for d in mop_data_list:
		frappe.db.set_value(
			"Manufacturing Operation",
			d["mop_name"],
			"last_eod_sync_on",
			now_ts,
			update_modified=False,
		)


# ---------------------------------------------------------------------------
# Stock Entry creation
# ---------------------------------------------------------------------------


def _mop_manufacturer_label(mop):
	if mop is None:
		return None
	if isinstance(mop, dict):
		return mop.get("manufacturer")
	return getattr(mop, "manufacturer", None)


def _save_draft_eod_se(
	company,
	mwo,
	manufacturing_order,
	items,
	header_mop_name=None,
	header_manufacturer=None,
	sync_log_name=None,
):
	"""Create and SAVE (draft only) one Material Transfer to Department Stock Entry."""
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
	# Audit markers (custom fields added by migration patch)
	se.custom_is_eod_sync_stock_entry = 1
	if sync_log_name:
		se.custom_eod_sync_log = sync_log_name
	se.custom_eod_sync_source = "MOP EOD Sync"
	for item in items:
		se.append("items", item)
	se.flags.ignore_permissions = True
	se.save()
	return se.name


# ---------------------------------------------------------------------------
# Validations (bulk-optimized)
# ---------------------------------------------------------------------------


def _validate_eod_items_for_mwo_reservation(items_to_transfer):
	"""Ensure lines carry batch/serial data required by stock_reservation_entry_for_mwo."""
	if not items_to_transfer:
		return

	item_codes = list(
		{item.get("item_code") for item in items_to_transfer if item.get("item_code")}
	)
	if not item_codes:
		return

	item_rows = frappe.db.get_all(
		"Item",
		filters={"name": ["in", item_codes]},
		fields=["name", "has_batch_no", "has_serial_no"],
	)
	item_flags = {r.name: r for r in item_rows}

	for item in items_to_transfer:
		item_code = item.get("item_code")
		if not item_code or flt(item.get("qty")) <= 0:
			continue
		flags = item_flags.get(item_code)
		if not flags:
			frappe.throw(
				_("MOP EOD Sync: Item {0} not found.").format(frappe.bold(item_code))
			)
		mop_label = item.get("manufacturing_operation") or "?"
		if cint(flags.has_batch_no) and not item.get("batch_no"):
			frappe.throw(
				_(
					"MOP EOD Sync: item {0} is batch-tracked but the MOP Log line has no Batch No "
					"(Manufacturing Operation {1}). Stock Reservation on submit cannot build "
					"sb_entries — fix the source MOP Log / vouchers, then retry."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)
		if cint(flags.has_serial_no) and not item.get("serial_no"):
			frappe.throw(
				_(
					"MOP EOD Sync: item {0} is serialized but the MOP Log line has no Serial No "
					"(Manufacturing Operation {1})."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)


def _resolve_eod_manufacturer_label(mop_data_list, manufacturing_work_order):
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
		m = (
			mdoc.get("manufacturer")
			if isinstance(mdoc, dict)
			else getattr(mdoc, "manufacturer", None)
		)
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


# ---------------------------------------------------------------------------
# Sync Log helpers
# ---------------------------------------------------------------------------


def _insert_sync_log_item(sync_log_name, row_dict):
	"""Insert a child row in MOP EOD Sync Log Item. Returns the new row name."""
	if not sync_log_name:
		return None
	doc = frappe.get_doc(
		{
			"doctype": "MOP EOD Sync Log Item",
			"parent": sync_log_name,
			"parentfield": "items",
			"parenttype": "MOP EOD Sync Log",
			"manufacturing_work_order": row_dict.get("manufacturing_work_order") or "",
			"manufacturing_operation": row_dict.get("manufacturing_operation") or "",
			"company": row_dict.get("company") or "",
			"item_code": row_dict.get("item_code") or "",
			"batch_no": row_dict.get("batch_no") or "",
			"serial_no": row_dict.get("serial_no") or "",
			"source_warehouse": row_dict.get("source_warehouse") or "",
			"target_warehouse": row_dict.get("target_warehouse") or "",
			"qty": flt(row_dict.get("qty"), 3),
			"pcs": flt(row_dict.get("pcs"), 3),
			"status": row_dict.get("status") or "Pending",
			"sync_stage": row_dict.get("sync_stage") or "",
			"is_synced": cint(row_dict.get("is_synced")),
			"stock_reservation_entry": row_dict.get("stock_reservation_entry") or "",
			"stock_entry": row_dict.get("stock_entry") or "",
			"draft_stock_entry": row_dict.get("draft_stock_entry") or "",
			"mop_log": row_dict.get("mop_log") or "",
			"error_type": row_dict.get("error_type") or "",
			"error_message": (row_dict.get("error_message") or "")[:500],
			"technical_traceback": row_dict.get("technical_traceback") or "",
			"suggested_fix": row_dict.get("suggested_fix") or "",
			"created_on": now_datetime(),
			"completed_on": row_dict.get("completed_on") or None,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
	return doc.name


def recalculate_sync_log_totals(sync_log_name):
	"""SQL aggregation over child rows to update parent totals and progress_percent."""
	if not sync_log_name:
		return

	rows = frappe.db.sql(
		"""
        SELECT
            status,
            COUNT(name) AS item_count,
            COALESCE(SUM(qty), 0) AS total_qty
        FROM `tabMOP EOD Sync Log Item`
        WHERE parent = %s
        GROUP BY status
        """,
		(sync_log_name,),
		as_dict=True,
	)

	totals = {
		"total_items": 0,
		"total_qty": 0.0,
		"synced_items": 0,
		"synced_qty": 0.0,
		"failed_items": 0,
		"failed_qty": 0.0,
		"unsynced_items": 0,
		"unsynced_qty": 0.0,
		"skipped_items": 0,
		"skipped_qty": 0.0,
		"excluded_items": 0,
		"excluded_qty": 0.0,
	}

	for row in rows:
		status = row.status or ""
		cnt = cint(row.item_count)
		qty = flt(row.total_qty, 3)
		totals["total_items"] += cnt
		totals["total_qty"] += qty

		if status == "Synced":
			totals["synced_items"] += cnt
			totals["synced_qty"] += qty
		elif status in ("Failed", "Draft Created"):
			totals["failed_items"] += cnt
			totals["failed_qty"] += qty
		elif status == "Excluded":
			totals["excluded_items"] += cnt
			totals["excluded_qty"] += qty
		elif status == "Skipped":
			totals["skipped_items"] += cnt
			totals["skipped_qty"] += qty
		elif status in ("Pending", "Unsynced"):
			totals["unsynced_items"] += cnt
			totals["unsynced_qty"] += qty

	eligible_qty = flt(totals["total_qty"] - totals["excluded_qty"], 3)
	if eligible_qty > 0:
		progress_pct = flt((totals["synced_qty"] / eligible_qty) * 100, 1)
	else:
		progress_pct = 0.0

	frappe.db.set_value(
		"MOP EOD Sync Log",
		sync_log_name,
		{
			"total_items": totals["total_items"],
			"total_qty": flt(totals["total_qty"], 3),
			"eligible_qty": eligible_qty,
			"synced_items": totals["synced_items"],
			"synced_qty": flt(totals["synced_qty"], 3),
			"failed_items": totals["failed_items"],
			"failed_qty": flt(totals["failed_qty"], 3),
			"unsynced_items": totals["unsynced_items"],
			"unsynced_qty": flt(totals["unsynced_qty"], 3),
			"skipped_items": totals["skipped_items"],
			"skipped_qty": flt(totals["skipped_qty"], 3),
			"excluded_items": totals["excluded_items"],
			"excluded_qty": flt(totals["excluded_qty"], 3),
			"progress_percent": progress_pct,
		},
		update_modified=False,
	)


# ---------------------------------------------------------------------------
# Diagnostic helpers (SRE audit)
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


def _list_open_sre_other_warehouses(
	item_code, batch_no, manufacturing_work_order=None, exclude_warehouse=None, limit=5
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
		sre_here = _list_open_sre_for_batch(item_code, warehouse, batch_no)

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
	items_to_transfer, manufacturing_work_order=None, mop_data_list=None, company=None
):
	"""Ensure aggregated transfer qty per (source warehouse, item, batch) does not exceed
	physical batch balance (SLE / serial-batch ledger).
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
				frappe.logger().warning(
					"MOP EOD Sync reservation audit: transferring %s of %s batch %s from %s "
					"(MWO %s) — physical qty %s allows the move but undelivered SRE qty for "
					"this item/batch/warehouse/MWO is only %s. "
					"Verify SO reservation vs physical issue rules.",
					req_qty,
					item_code,
					batch_no,
					wh,
					manufacturing_work_order,
					physical,
					sre_open,
				)
