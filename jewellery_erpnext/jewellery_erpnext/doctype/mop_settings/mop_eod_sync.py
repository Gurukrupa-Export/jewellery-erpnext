"""
EOD MOP Log Sync — converts unsynced MOP Log entries into consolidated Stock Entries.

MOP Logs with ``is_synced = 0`` represent virtual warehouse movements recorded by
Department IR and Employee IR. Each run **plans** every Manufacturing Work Order, then
**commits** ONE consolidated **Material Transfer to Department** Stock Entry per
(company, manufacturer) covering all fully-resolvable MWOs — moving each item from its
Stock Reservation Entry warehouse (where stock is physically reserved) to the last
Manufacturing Operation's final ``to_warehouse``. Classification is all-or-nothing per
MWO: an MWO with any unresolvable / batch-short / invalid row is held out of the
transfer (its MOP Logs stay unsynced and retryable) and its buildable rows are placed in
a per-bucket DRAFT "issues" Stock Entry for visibility; rows with no SRE source warehouse
are reported in the consolidated Error Log only.

**Windowed filter (scheduled mode)**: With no enabled MWO filter rows, only MOP
Logs created inside the run's window are processed. The window defaults to today
(creation between today 00:00:00 and today 23:59:59); the manual EOD Sync button may
pass a custom From/To. The nightly scheduler always uses today. Unsynced logs outside
the window are counted and reported but never marked synced or used for SEs.

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

**Syncing**: after SE submit, ALL unsynced non-cancelled MOP Logs for the MWO (created
inside the run's window) are marked synced atomically within the same savepoint.

**Transaction phases (per (company, manufacturer) bucket)**:
  Phase 1 (savepoint "eod_draft_phase"): save the consolidated SE as draft.
  Phase 2 (savepoint "eod_submit_phase"): cancel every included MWO's source-warehouse
    SREs (releasing the reservations so the transfer can consume the stock), submit the
    single SE, re-reserve at each MWO's target warehouse where its stock lands, then mark
    each MWO's MOP Logs synced. All of this is ONE atomic savepoint — the SRE relocation
    is all-or-nothing for the whole bucket.
  On Phase 2 failure: rollback the whole submit savepoint; the cancelled SREs are
    restored (never left cancelled without a transfer/re-reservation) and the draft SE
    survives for manual recovery — so a single submit failure holds the whole bucket as a
    draft (the "one SE for the run" trade-off).

**MOP EOD Sync Log**: every run creates/updates a MOP EOD Sync Log document with full
progress tracking. Every item/batch/MWO decision is recorded in the child table.

**Error reporting**: all failures are collected in-memory; ONE consolidated Error Log is
created at the end of the full sync — no per-row or per-MWO log_error calls in loops.
"""

import frappe
from frappe import _
from frappe.utils import (
	cint,
	flt,
	now_datetime,
	nowdate,
)

from .eod_lock import release_eod_sync_lock, set_eod_sync_running

# Savepoint wrapping a single diagnostic child-row insert, so a rejected Select value or
# any other write error rolls back that row alone and never the caller's bucket.
_LOG_ROW_SAVEPOINT = "eod_log_row"

# {fieldname: {valid option, ...}} for MOP EOD Sync Log Item, filled on first use.
_SELECT_OPTION_CACHE = {}

# {doctype: {column, ...}} -- guards writes against partially-provisioned sites.
_TABLE_COLUMN_CACHE = {}

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def sync_mop_logs(sync_log_name=None, from_datetime=None, to_datetime=None):
	"""Main entry point called by scheduler or manual trigger. Returns a summary dict.

	``from_datetime`` / ``to_datetime`` bound the MOP Log ``creation`` window scanned in
	scheduled (non-selective) mode. Only the manual EOD Sync button passes them; the
	nightly scheduler leaves them empty so it always uses today. When both are absent the
	window defaults to today's start/end (see :func:`_resolve_run_range`). Selective
	MWO-filter mode ignores the window and always covers full unsynced history.
	"""
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
	# Resolve the creation window once for the whole run. Scheduled runs pass nothing
	# → today's range; the manual button passes the user-chosen From/To. Stash it on a
	# flag so the gather + mark-synced steps share one source of truth.
	frappe.flags.eod_sync_range = _resolve_run_range(from_datetime, to_datetime)
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

			# PLAN: classify every MWO without writing any Stock Entry. Resolvable MWOs are
			# bucketed by (company, manufacturer) for ONE consolidated transfer SE; failed
			# MWOs contribute their buildable rows to a per-bucket draft "issues" SE.
			main_buckets = {}
			issues_buckets = {}
			for group_key, mop_data_list in unsynced_groups.items():
				# Isolate each MWO: an unexpected error while planning ONE MWO must not
				# abort the whole run — record it as a failure and move on.
				try:
					result = _plan_mwo_group(
						group_key,
						mop_data_list,
						failures,
						stats,
						sync_log_name,
						selective=selective,
					)
				except Exception:
					stats["failed_mwos"] += 1
					failures.append(
						{
							"step": "plan",
							"company": group_key[0],
							"mwo": group_key[1],
							"error_message": "Unexpected error while planning this MWO.",
							"traceback": frappe.get_traceback(),
							"suggested_fix": (
								"Investigate the traceback; this MWO was skipped. Other MWOs "
								"were unaffected."
							),
						}
					)
					continue
				if not result:
					continue
				bucket_key = (result["company"], result.get("manufacturer"))
				if result["kind"] == "resolvable":
					main_buckets.setdefault(bucket_key, []).append(result)
				elif result["kind"] == "failed" and result.get("issues_rows"):
					issues_buckets.setdefault(bucket_key, []).extend(
						result["issues_rows"]
					)

			# COMMIT: one submitted Material Transfer to Department per (company,
			# manufacturer) for all resolvable MWOs.
			for (company, manufacturer), main_mwos in main_buckets.items():
				_commit_company_main_se(
					company,
					manufacturer,
					main_mwos,
					failures,
					stats,
					sync_log_name,
					selective=selective,
				)
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

			# Best-effort draft "issues" SE per bucket for unresolved-but-buildable rows.
			for (company, manufacturer), issues_rows in issues_buckets.items():
				_commit_company_issues_se(
					company, manufacturer, issues_rows, stats, sync_log_name
				)
				if sync_log_name:
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
						# Read-only audit: it never blocks a transfer, so it is reported
						# but must not downgrade the run's final status.
						"advisory": True,
						"error_message": "SRE reconciliation check raised an exception.",
						"traceback": frappe.get_traceback(),
						"suggested_fix": (
							"Investigate the SRE reconcile error for MWO "
							f"{mwo}. This does not affect SE or MOP Log sync status."
						),
					}
				)

		if not sync_log_name:
			# The PLAN + COMMIT block below is guarded by `if sync_log_name`, so without
			# a log NOTHING was processed. Reporting success here would stamp
			# eod_sync_last_completed_on and make the scheduler treat the day as done,
			# stranding it permanently (scheduled runs only scan their own window).
			# Deliberately NOT fixed by dedenting the block: running the transfer with no
			# audit trail would cancel SREs, submit stock and mark MOP Logs synced with
			# no record of what happened.
			failures.append(
				{
					"step": "no_sync_log",
					"error_message": (
						"MOP EOD Sync could not create its Sync Log, so no MWO was "
						"processed. The run is reported as failed so this day is retried."
					),
					"suggested_fix": (
						"Check why a MOP EOD Sync Log could not be inserted (permissions, "
						"schema, disk), then re-run the sync."
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
			# Advisory failures (the read-only SRE reconcile audit) record a diagnostic
			# but did not stop any MWO from syncing, so they must not downgrade a clean
			# run to "Partially Completed".
			blocking = [f for f in failures if not f.get("advisory")]
			final_status = (
				"Completed"
				if not blocking
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
					# synced_mwos / failed_mwos / skipped_mwos / processed_mwos are NOT
					# written here: recalculate_sync_log_totals above derives them from
					# the child rows. Writing them from `stats` afterwards is what
					# produced impossible headers (synced_mwos=19, synced_items=0).
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
		frappe.flags.eod_sync_range = None

	return {
		"processed": stats["processed_mwos"],
		"stock_entries": stats["submitted_ses"],
	}


# ---------------------------------------------------------------------------
# Plan + commit (consolidated single Stock Entry per company/manufacturer)
# ---------------------------------------------------------------------------


def _bulk_set_child_rows(child_row_names, values):
	"""Apply the same field updates to a list of MOP EOD Sync Log Item rows."""
	for rn in child_row_names or []:
		frappe.db.set_value("MOP EOD Sync Log Item", rn, values, update_modified=False)


def _heal_missing_sre_in_plan(mwo, operation, company, skipped_rows, sync_log_name):
	"""Try to create the missing WIP reservations for rows PLAN could not source.

	A row with no candidate warehouse holds its whole MWO out of the transfer, yet on the
	live site 1412 of 1414 such (item, batch, MWO) keys still have the batch physically in
	stock -- the reservation is missing, not the metal.

	OFF by default (``MOP Settings.enable_eod_plan_sre_heal``). It submits real Stock
	Reservation Entries, and the per-bucket commit later in the run makes them permanent
	even if this MWO is subsequently held back, so it is opt-in and each heal gets its own
	savepoint: a failure mid-way (the healer inserts then submits as two statements)
	rolls back that reservation instead of stranding a draft SRE that
	``_active_sre_exists`` -- which only looks at docstatus 1 -- could never see again.

	Returns True when at least one reservation was created.
	"""
	if not _eod_feature_enabled("enable_eod_plan_sre_heal"):
		return False

	healed = False
	for skip in skipped_rows:
		batch_no = skip.get("batch_no")
		if not batch_no:
			continue
		savepoint = (
			f"eod_plan_heal_{abs(hash((mwo, skip.get('item_code'), batch_no))) % 10**8}"
		)
		try:
			frappe.db.savepoint(savepoint)
			created = _reserve_batch_at_physical_warehouse(
				mwo,
				skip.get("item_code"),
				batch_no,
				flt(skip.get("qty")),
				operation,
				company,
				sync_log_name=sync_log_name,
			)
			frappe.db.release_savepoint(savepoint)
			healed = healed or bool(created)
		except Exception:
			# Degrade to today's behaviour (the row stays a Missing SRE failure) rather
			# than letting a heal attempt take down the MWO's whole plan.
			_rollback_to_savepoint(savepoint)
			frappe.logger().exception(
				"MOP EOD Sync: PLAN-phase reservation heal failed for %s / %s / %s",
				mwo,
				skip.get("item_code"),
				batch_no,
			)
	return healed


def _plan_mwo_group(
	group_key, mop_data_list, failures, stats, sync_log_name=None, selective=False
):
	"""Classify one (company, mwo) group for the consolidated run — no Stock Entry is
	created here.

	Returns one of:
	  * ``None`` — fully handled in place (artifact / no last operation / genuine
	    no-op / no target warehouse). Sync marking, stats and sync-log rows are done.
	  * ``{"kind": "resolvable", ...}`` — every row resolves and validates; the payload
	    carries the rows + relocation metadata for the bucket's single transfer SE.
	  * ``{"kind": "failed", "issues_rows": [...], ...}`` — the MWO cannot be fully
	    transferred (missing SRE / batch short / invalid row). It is NOT marked synced;
	    its buildable rows are returned for the per-bucket draft "issues" SE.

	Classification is all-or-nothing per MWO: a partially transferable MWO is held
	entirely so its MOP Logs are never buried as synced and its good rows are not
	double-transferred on a later run.
	"""
	company, mwo = group_key
	all_mop_names = [md["mop_name"] for md in mop_data_list]

	# Artifact (Serial Number Creator already realized the MWO) → mark synced, no transfer.
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
		return None

	last_mop_data = _find_last_operation(mop_data_list)
	if not last_mop_data:
		_mark_all_mwo_mop_logs_synced([mwo], selective=selective)
		stats["processed_mwos"] += 1
		# Record the MWO even though nothing moved: without a child row this MWO is
		# counted in processed_mwos but is invisible to the child-derived counters, so
		# the header would not add up (a run reported 841 total / 602 in children).
		_insert_sync_log_item(
			sync_log_name,
			{
				"manufacturing_work_order": mwo,
				"company": company,
				"status": "Synced",
				"is_synced": 1,
				"sync_stage": "Completed",
				"error_message": (
					"No resolvable last operation for this MWO; nothing to transfer. "
					"MOP Logs marked synced."
				),
				"completed_on": now_datetime(),
			},
		)
		return None

	last_mop_name = last_mop_data["mop_name"]
	last_logs = _get_last_logs_per_item_batch(last_mop_data["logs"])
	manufacturer = _mop_manufacturer_label(last_mop_data["mop_doc"])
	# Resolve the target (department) warehouse. The last operation may be an Employee IR
	# receive whose audit MOP Logs carry no warehouse, so fall back to the latest
	# to_warehouse across ALL the MWO's logs, then to the department's Manufacturing WH.
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
		return None

	# One query for all (item_code, batch_no) → warehouse for this MWO
	sre_map = _preload_sre_warehouse_map(mwo)
	items, skipped_rows = _build_eod_se_rows(
		mwo, last_mop_name, last_logs, t_warehouse, sre_map
	)

	if skipped_rows and _heal_missing_sre_in_plan(
		mwo, last_mop_name, company, skipped_rows, sync_log_name
	):
		# At least one reservation was created; re-resolve once with the new SREs.
		sre_map = _preload_sre_warehouse_map(mwo)
		items, skipped_rows = _build_eod_se_rows(
			mwo, last_mop_name, last_logs, t_warehouse, sre_map
		)

	# Missing-SRE rows have no source warehouse and cannot be transferred or even placed
	# in a draft SE — record them as failures only.
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

	# Non-throwing validation of the buildable rows (batch/serial presence + batch stock).
	invalid = False
	try:
		_validate_eod_items_for_mwo_reservation(items)
	except Exception as exc:
		invalid = True
		failures.append(
			{
				"step": "reservation_validate",
				"mwo": mwo,
				"company": company,
				"last_mop": last_mop_name,
				"affected_mops": all_mop_names,
				"t_warehouse": t_warehouse,
				"error_message": str(exc),
				"suggested_fix": (
					"Fix the MOP Log batch/serial data for this MWO, then retry EOD sync."
				),
			}
		)
	short_keys = _check_eod_source_batch_stock(items) if items else {}
	if short_keys:
		invalid = True
		for (wh, item_code, batch_no), (req_qty, physical) in short_keys.items():
			failures.append(
				{
					"step": "batch_short",
					"mwo": mwo,
					"company": company,
					"last_mop": last_mop_name,
					"affected_mops": all_mop_names,
					"item_code": item_code,
					"batch_no": batch_no,
					"s_warehouse": wh,
					"t_warehouse": t_warehouse,
					"error_message": (
						f"Insufficient physical batch stock for item {item_code} batch {batch_no} "
						f"in {wh}: require {req_qty}, have {physical}."
					),
					"suggested_fix": (
						"Reconcile vouchers / MOP Log / stale reservations so physical batch "
						"stock covers the transfer, then retry EOD sync."
					),
				}
			)

	# FAILED: any unresolvable / short / invalid row holds the whole MWO. Its buildable
	# rows go to the per-bucket draft "issues" SE; its MOP Logs are NOT marked synced.
	if skipped_rows or invalid:
		stats["failed_mwos"] += 1
		if sync_log_name:
			for item in items:
				_insert_sync_log_item(
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
						"status": "Failed",
						"sync_stage": "Build Stock Entry Row",
						"error_type": "Validation Failed",
						"error_message": (
							"MWO has unresolved or short rows; held out of the consolidated "
							"transfer. Buildable rows placed in the draft issues Stock Entry."
						),
						"suggested_fix": "Resolve the failures above, then retry EOD sync.",
					},
				)
		return {
			"kind": "failed",
			"company": company,
			"manufacturer": manufacturer,
			"issues_rows": items,
		}

	if not items:
		# Genuine no-op: every row was same-warehouse or non-positive qty — nothing to
		# move. Safe to mark the MWO's logs synced.
		frappe.logger().info(
			"MOP EOD Sync MWO %s: no SE rows to create (same WH); marking logs synced.",
			mwo,
		)
		_mark_all_mwo_mop_logs_synced([mwo], selective=selective)
		stats["processed_mwos"] += 1
		# See the "no last operation" branch: a processed MWO with no child row makes
		# the header counters disagree with the child table.
		_insert_sync_log_item(
			sync_log_name,
			{
				"manufacturing_work_order": mwo,
				"manufacturing_operation": last_mop_name,
				"company": company,
				"target_warehouse": t_warehouse,
				"status": "Synced",
				"is_synced": 1,
				"sync_stage": "Completed",
				"error_message": (
					"Stock already at the target warehouse; no Material Transfer needed."
				),
				"completed_on": now_datetime(),
			},
		)
		return None

	# RESOLVABLE — insert Pending child rows; commit happens later in the bucket SE.
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

	return {
		"kind": "resolvable",
		"company": company,
		"manufacturer": manufacturer,
		"mwo": mwo,
		"items": items,
		"t_warehouse": t_warehouse,
		"mop_data_list": mop_data_list,
		"last_mop_name": last_mop_name,
		"child_row_names": child_row_names,
	}


def _rollback_to_savepoint(save_point):
	"""Roll back to ``save_point``, tolerating its absence (F-012).

	A deadlock (MariaDB 1213) rolls back the ENTIRE transaction and discards every
	savepoint. A subsequent ``rollback(save_point=...)`` from inside a failure handler
	would then raise 1305 ("SAVEPOINT ... does not exist"), and that secondary error
	would propagate out and abort the rest of the EOD run (all remaining buckets). A 1205
	lock-wait timeout rolls back only the statement (savepoint intact) and takes the
	normal path.

	When the savepoint rollback fails, fall back to a FULL ``frappe.db.rollback()``
	(guarded): in the dominant 1305-after-1213 case the server has already discarded
	every uncommitted write, so the full rollback loses nothing and merely re-aligns
	frappe's client-side transaction state before the caller's failure-bookkeeping
	writes; in the residual savepoint-gone-but-transaction-alive case it prevents the
	next per-bucket commit from persisting a half-applied bucket (e.g. cancelled SREs
	without the submitted SE). All four callers only record in-memory failures/stats
	plus re-issue child-row status writes afterwards, so a full rollback is safe."""
	try:
		frappe.db.rollback(save_point=save_point)
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			# Even the full rollback failed (dead connection etc.) -- nothing more we
			# can do here; the caller's bookkeeping/commit will surface the state.
			pass


def _existing_columns(doctype):
	"""Column names actually present on ``doctype``'s table (empty set if unknowable)."""
	cached = _TABLE_COLUMN_CACHE.get(doctype)
	if cached is None:
		try:
			cached = set(frappe.db.get_table_columns(doctype))
		except Exception:
			cached = set()
		_TABLE_COLUMN_CACHE[doctype] = cached
	return cached


def _writable_values(doctype, values):
	"""Drop keys that have no column on ``doctype``, so a partially-provisioned site
	cannot fail the whole write.

	Fields shipped by a patch or by ``custom_fields/*.json`` are absent on any site that
	has not migrated (and on sites whose schema is incomplete for other reasons). A single
	such key makes the entire ``set_value`` raise
	``OperationalError (1054, "Unknown column 'x' in 'SET'")``, taking down the run --
	``draft_items`` did exactly that to the sync log write, and ``last_eod_sync_on`` did it
	to every bucket submit. Writing the columns that do exist keeps the run correct and
	merely degrades the reporting.
	"""
	existing = _existing_columns(doctype)
	if not existing:
		return values
	kept = {k: v for k, v in values.items() if k in existing}
	missing = set(values) - set(kept)
	if missing:
		frappe.logger().warning(
			"MOP EOD Sync: %s is missing column(s) %s; skipping them. Run "
			"patches.add_eod_sync_reporting_and_allocation_fields / bench migrate.",
			doctype,
			sorted(missing),
		)
	return kept


def _eod_feature_enabled(fieldname):
	"""Read an EOD feature flag, treating an absent field as OFF.

	``frappe.db.get_single_value`` does NOT return ``None`` for a field the site does not
	have -- it THROWS ``InvalidColumnName``. These flags arrive with
	``patches/add_eod_sync_reporting_and_allocation_fields.py``, so on any site that has
	not migrated yet (and on ``gk``, whose truncated restore cannot reload doctypes at
	all) a direct read aborts the entire sync at the top of ``_commit_company_main_se``.

	Fails CLOSED: an unreadable flag reads as OFF, which is also how both flags ship, so a
	site missing the field keeps exactly its previous behaviour instead of crashing.
	"""
	try:
		return bool(cint(frappe.db.get_single_value("MOP Settings", fieldname)))
	except Exception:
		frappe.logger().warning(
			"MOP EOD Sync: MOP Settings.%s is not available on this site; treating it as "
			"disabled. Run patches.add_eod_sync_reporting_and_allocation_fields to enable "
			"it.",
			fieldname,
		)
		return False


def _describe_shortfall(shortfall):
	"""Human-readable '<key>: need X, cap Y, already taken Z' for the Error Log."""
	key, need, cap, *rest = shortfall
	taken = rest[0] if rest else 0.0
	label = " / ".join(str(part) for part in key)
	return f"{label}: needs {flt(need, 3)}, cap {flt(cap, 3)}, already allocated {flt(taken, 3)}"


def _apply_bucket_allocation(main_mwos, failures, stats, sync_log_name):
	"""Run the allocator and record whatever it holds back. Returns the admitted MWOs."""
	admitted, deferred, infeasible = _allocate_bucket_by_physical_stock(main_mwos)
	if not deferred and not infeasible:
		return admitted

	for entry, permanent in [(e, False) for e in deferred] + [
		(e, True) for e in infeasible
	]:
		shortfalls = entry.get("_allocation_shortfalls") or []
		detail = "; ".join(_describe_shortfall(s) for s in shortfalls[:5])
		stats["deferred_mwos"] = stats.get("deferred_mwos", 0) + 1
		failures.append(
			{
				"step": "bucket_allocation",
				"mwo": entry.get("mwo"),
				"company": entry.get("company"),
				# Deferred MWOs are a normal outcome of stock contention and clear
				# themselves on a later run, so they are advisory. A permanently short
				# MWO is a real data defect and must colour the run's status.
				"advisory": not permanent,
				"error_message": (
					(
						"Held out of the consolidated transfer: demand exceeds available "
						"stock even on its own. "
						if permanent
						else "Deferred to a later run: another MWO in this bucket took the "
						"shared stock first. "
					)
					+ detail
				),
				"suggested_fix": (
					"Reconcile physical stock / cancel stale reservations for the listed "
					"keys."
					if permanent
					else "No action needed; this MWO is retried on the next EOD run."
				),
			}
		)
		if sync_log_name:
			for item in entry.get("items") or []:
				_insert_sync_log_item(
					sync_log_name,
					{
						"manufacturing_work_order": entry.get("mwo"),
						"manufacturing_operation": item.get("manufacturing_operation"),
						"company": entry.get("company"),
						"item_code": item.get("item_code"),
						"batch_no": item.get("batch_no"),
						"source_warehouse": item.get("s_warehouse"),
						"target_warehouse": item.get("t_warehouse"),
						"qty": flt(item.get("qty"), 3),
						"status": "Deferred",
						"sync_stage": "Allocate Bucket Stock",
						"error_type": (
							"Permanently Short"
							if permanent
							else "Deferred - Bucket Stock Contention"
						),
						"error_message": detail,
					},
				)

	frappe.logger().info(
		"MOP EOD Sync bucket allocation: %s admitted, %s deferred, %s permanently short",
		len(admitted),
		len(deferred),
		len(infeasible),
	)
	return admitted


def _commit_company_main_se(
	company,
	manufacturer,
	main_mwos,
	failures,
	stats,
	sync_log_name=None,
	selective=False,
):
	"""Build ONE submitted *Material Transfer to Department* for all resolvable MWOs of a
	(company, manufacturer).

	Phase 1 (savepoint ``eod_draft_phase``) saves the consolidated draft. Phase 2
	(savepoint ``eod_submit_phase``) is atomic for the whole bucket: cancel every MWO's
	source SREs, submit the single SE, reserve at each MWO's target FROM the submitted SE's
	rows (Sales-Order-anchored), hard-delete the now-cancelled source SREs, then mark each
	MWO's MOP Logs synced. On Phase-2 failure the whole bucket rolls back (SREs restored,
	never deleted) and the draft survives for manual recovery — the user-chosen "one SE for
	the run" trade-off: a single submit failure holds the whole bucket as a draft.
	"""
	# Admit MWOs against a running stock tally BEFORE anything is written. Placed here
	# rather than in sync_mop_logs so _process_mwo_group (the manual retry path) is
	# covered by the same guard.
	if main_mwos and _eod_feature_enabled("enable_eod_bucket_allocation"):
		main_mwos = _apply_bucket_allocation(main_mwos, failures, stats, sync_log_name)
		if not main_mwos:
			# Everything was held back; _save_draft_eod_se throws on an empty item list
			# and its error would misleadingly blame the MOP Log data.
			return

	items = [it for m in main_mwos for it in m["items"]]
	child_rows = [rn for m in main_mwos for rn in m["child_row_names"]]
	mwo_list = [m["mwo"] for m in main_mwos]
	mop_names = sorted({md["mop_name"] for m in main_mwos for md in m["mop_data_list"]})

	# Phase 1 — save consolidated draft (no header MWO; rows carry their own MWO/op).
	try:
		frappe.db.savepoint("eod_draft_phase")
		draft_se_name = _save_draft_eod_se(
			company,
			None,
			None,
			items,
			header_manufacturer=manufacturer,
			sync_log_name=sync_log_name,
		)
		frappe.db.release_savepoint("eod_draft_phase")
	except Exception as exc:
		_rollback_to_savepoint("eod_draft_phase")
		failures.append(
			{
				"step": "draft_save",
				"mwo": ", ".join(mwo_list),
				"company": company,
				"affected_mops": mop_names,
				"error_message": str(exc),
				"traceback": frappe.get_traceback(),
				"suggested_fix": (
					"Check MOP Log data for the listed MWOs: item codes, batch numbers, "
					"warehouses, Stock Reservation Entries, and physical batch stock."
				),
			}
		)
		stats["failed_mwos"] += len(main_mwos)
		_bulk_set_child_rows(
			child_rows,
			{
				"status": "Failed",
				"sync_stage": "Save Draft Stock Entry",
				"error_type": "Stock Entry Save Failed",
				"error_message": str(exc)[:500],
				"technical_traceback": frappe.get_traceback()[:3000],
				"suggested_fix": "Check MOP Log data, warehouses, SREs, and physical batch stock.",
				"completed_on": now_datetime(),
			},
		)
		return

	# Phase 2 — cancel SREs, submit once, reserve from the SE rows, delete the cancelled
	# sources, mark synced. Atomic for the bucket.
	try:
		frappe.db.savepoint("eod_submit_phase")
		snaps_by_mwo = {}
		for m in main_mwos:
			snaps = _snapshot_mwo_sres_for_relocation(
				m["mwo"], m["items"], m["t_warehouse"]
			)
			snaps_by_mwo[m["mwo"]] = snaps
			_cancel_sre_snapshots(snaps)
		submitted_se = frappe.get_doc("Stock Entry", draft_se_name)
		submitted_se.submit()
		# Reserve at each MWO's target FROM the submitted SE's rows (Sales-Order-anchored,
		# per-row MWO/operation), then hard-delete the now-cancelled source SREs.
		# Read the rows back off the SUBMITTED document rather than the planned dicts:
		# before_validate hooks can drop or rewrite rows (one live draft ended up with
		# 968 rows against 976 planned), and a dropped row's source SRE would otherwise
		# be cancelled and hard-deleted with nothing transferred to re-reserve from.
		_reserve_sres_from_eod_se_rows(
			company,
			_eod_rows_from_submitted_se(submitted_se),
			sync_log_name=sync_log_name,
		)
		# POST-CONDITION GUARD: never commit an orphaned cancellation. Re-reservation above
		# silently skips a batched row whose batch is not physically at the SE row target, so a
		# cancelled source SRE can end up with no active replacement — the exact defect that
		# leaves Employee IR Process Loss with "No active Stock Reservation Entry found". For
		# each cancelled batched snapshot with no live SRE, heal it at the batch's physical
		# warehouse; if that is impossible, raise so the whole Phase-2 savepoint rolls back
		# (SREs restored, submit undone, draft kept for manual recovery) rather than leaving
		# the reservation orphaned. Runs while the snapshots are still in memory and BEFORE
		# _hard_delete_cancelled_snapshots makes the cancellation permanent.
		for m in main_mwos:
			for snap in snaps_by_mwo[m["mwo"]]:
				for sb in snap.get("sb_entries") or []:
					batch_no = sb.get("batch_no")
					if not batch_no:
						continue
					item_code = snap["sre"].item_code
					if _active_sre_exists(m["mwo"], item_code, batch_no):
						continue
					healed = _reserve_batch_at_physical_warehouse(
						m["mwo"],
						item_code,
						batch_no,
						flt(sb.get("qty")),
						snap["sre"].manufacturing_operation,
						company,
						sync_log_name=sync_log_name,
					)
					if healed:
						continue
					frappe.throw(
						_(
							"EOD orphaned WIP reservation: MWO {0}, item {1}, batch {2} — the "
							"source Stock Reservation Entry was cancelled but no warehouse "
							"physically holds free batch qty to re-reserve. The bucket is rolled "
							"back; investigate the batch's physical stock before re-running."
						).format(m["mwo"], item_code, batch_no),
						title=_("EOD Reservation Not Replaceable"),
					)
		for m in main_mwos:
			_hard_delete_cancelled_snapshots(snaps_by_mwo[m["mwo"]])
		for m in main_mwos:
			# Pinned to the rows this run planned from: the transfer only moved those,
			# so anything logged since must stay unsynced for the next run.
			_mark_all_mwo_mop_logs_synced(
				[m["mwo"]],
				selective=selective,
				log_names=_gathered_log_names(m["mop_data_list"]),
			)
			_stamp_last_eod_sync(m["mop_data_list"])
		frappe.db.release_savepoint("eod_submit_phase")
		stats["submitted_ses"].append(draft_se_name)
		stats["processed_mwos"] += len(main_mwos)
		_bulk_set_child_rows(
			child_rows,
			{
				"status": "Synced",
				"is_synced": 1,
				"stock_entry": draft_se_name,
				"sync_stage": "Completed",
				"completed_on": now_datetime(),
			},
		)
	except Exception as exc:
		# Whole Phase-2 savepoint rolled back: submit + SRE cancel/re-reserve undone, so
		# reservations are restored. The Phase-1 draft (saved before this savepoint)
		# survives for manual recovery.
		_rollback_to_savepoint("eod_submit_phase")
		failures.append(
			{
				"step": "submit",
				"mwo": ", ".join(mwo_list),
				"company": company,
				"affected_mops": mop_names,
				"draft_se": draft_se_name,
				"error_message": str(exc),
				"traceback": frappe.get_traceback(),
				"suggested_fix": (
					f"Consolidated Stock Entry {draft_se_name} is saved as Draft but failed "
					"to submit. Stock Reservation Entries were restored (rolled back). Review "
					"the validation error, fix the underlying issue, and submit the draft "
					"manually. MOP Logs for these MWOs remain unsynced until then."
				),
			}
		)
		stats["draft_ses"].append(draft_se_name)
		stats["failed_mwos"] += len(main_mwos)
		_bulk_set_child_rows(
			child_rows,
			{
				"status": "Draft Created",
				"draft_stock_entry": draft_se_name,
				"sync_stage": "Submit Stock Entry",
				"error_type": "Stock Entry Submit Failed",
				"error_message": str(exc)[:500],
				"technical_traceback": frappe.get_traceback()[:3000],
				"suggested_fix": f"Open draft Stock Entry {draft_se_name}, fix the error, and submit manually.",
				"completed_on": now_datetime(),
			},
		)


def _commit_company_issues_se(
	company, manufacturer, issues_rows, stats, sync_log_name=None
):
	"""Best-effort single DRAFT *Material Transfer to Department* holding the buildable
	rows of failed MWOs, for visibility. Wrapped so it can never block the run."""
	if not issues_rows:
		return
	try:
		frappe.db.savepoint("eod_issues_phase")
		draft_se_name = _save_draft_eod_se(
			company,
			None,
			None,
			issues_rows,
			header_manufacturer=manufacturer,
			sync_log_name=sync_log_name,
			eod_sync_source="MOP EOD Sync (Unresolved)",
		)
		frappe.db.release_savepoint("eod_issues_phase")
		stats["draft_ses"].append(draft_se_name)
	except Exception:
		_rollback_to_savepoint("eod_issues_phase")
		frappe.logger().exception(
			"MOP EOD Sync: could not build draft issues SE for %s / %s",
			company,
			manufacturer,
		)


def _process_mwo_group(
	group_key, mop_data_list, failures, stats, sync_log_name=None, selective=False
):
	"""Plan and commit a single MWO group end-to-end.

	Convenience wrapper: ``sync_mop_logs`` drives the production flow with
	:func:`_plan_mwo_group` + per-bucket :func:`_commit_company_main_se`, but processing
	exactly one group (one resolvable MWO ⇒ one consolidated SE for that MWO) is a useful
	unit for the manual retry patch and tests.
	"""
	result = _plan_mwo_group(
		group_key, mop_data_list, failures, stats, sync_log_name, selective=selective
	)
	if not result:
		return
	if result["kind"] == "resolvable":
		_commit_company_main_se(
			result["company"],
			result.get("manufacturer"),
			[result],
			failures,
			stats,
			sync_log_name,
			selective=selective,
		)
	elif result["kind"] == "failed" and result.get("issues_rows"):
		_commit_company_issues_se(
			result["company"],
			result.get("manufacturer"),
			result["issues_rows"],
			stats,
			sync_log_name,
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
					_rollback_to_savepoint("eod_sre_reconcile")
					frappe.logger().exception(
						"EOD SRE reconcile cancel failed: %s", sre.name
					)


def _active_sre_exists(mwo, item_code, batch_no):
	"""True when an active (docstatus=1) SRE already reserves this item/batch on the MWO."""
	if batch_no:
		return bool(
			frappe.db.sql(
				"""
				SELECT 1 FROM `tabStock Reservation Entry` sre
				INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
				WHERE sre.docstatus = 1
				  AND sre.manufacturing_work_order = %s
				  AND sre.item_code = %s
				  AND sbe.batch_no = %s
				LIMIT 1
				""",
				(mwo, item_code, batch_no),
			)
		)
	return bool(
		frappe.db.exists(
			"Stock Reservation Entry",
			{
				"manufacturing_work_order": mwo,
				"item_code": item_code,
				"docstatus": 1,
			},
		)
	)


@frappe.whitelist()
def backfill_missing_wip_reservations(mwo=None, dry_run=True):
	"""Re-create the destination SREs that EOD sync skipped for batched WIP.

	Older EOD runs dropped batched WIP rows while reserving at the new operation:
	ERPNext's batch availability reads 0 under v16 Serial-and-Batch-Bundle stock
	(see ``_free_batch_qty_to_reserve``), so the source SRE was cancelled and no
	destination SRE was ever created. The current operation then holds physical WIP
	with no active Stock Reservation Entry, and Employee IR Process Loss throws
	"No active Stock Reservation Entry found".

	For each affected MWO's current operation, rebuild the missing (item, batch)
	reservations from the live MOP balance via ``_reserve_batch_at_physical_warehouse``,
	which reserves at the warehouse where the batch PHYSICALLY sits (discovered from SBB
	stock) — NOT the current-op department warehouse, which fixes nothing when the WIP is
	parked at an earlier operation's warehouse. The new SREs stay Sales-Order-anchored
	exactly like a fresh EOD run. Idempotent: an (item, batch) that already has an active
	SRE is skipped.

	Pass ``mwo`` to fix one work order; omit to scan every submitted/draft Employee
	IR loss row for MWOs missing their reservation. ``dry_run`` (default) only reports.

	Run:  bench --site <site> execute \
	  jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.backfill_missing_wip_reservations \
	  --kwargs "{'mwo': 'MWO-...', 'dry_run': False}"
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	dry_run = cint(dry_run)
	if mwo:
		mwos = [mwo]
	else:
		mwos = [
			r[0]
			for r in frappe.db.sql(
				"""
				SELECT DISTINCT eld.manufacturing_work_order
				FROM `tabEmployee Loss Details` eld
				INNER JOIN `tabEmployee IR` eir ON eir.name = eld.parent
				WHERE eir.docstatus < 2
				  AND eld.proportionally_loss > 0
				  AND eld.manufacturing_work_order IS NOT NULL
				"""
			)
		]

	report = []
	for mwo_name in mwos:
		op, company = frappe.db.get_value(
			"Manufacturing Work Order",
			mwo_name,
			["manufacturing_operation", "company"],
		) or (None, None)
		if not op:
			continue
		# Rebuild each missing (item, batch) reservation at the warehouse where the batch
		# PHYSICALLY sits. _reserve_batch_at_physical_warehouse discovers that from live SBB
		# stock — the old approach pinned every row to the current-op department warehouse,
		# which fixes nothing when the WIP is parked at an earlier operation's warehouse (the
		# exact case that leaves Employee IR Process Loss with "No active Stock Reservation
		# Entry found"). Idempotent via _active_sre_exists; skips rows already reserved.
		balance_rows = get_current_mop_balance_rows(
			op,
			include_fields=[
				"item_code",
				"batch_no",
				"qty_after_transaction_batch_based as qty",
				"to_warehouse",
			],
		)
		for b in balance_rows:
			item_code = b.get("item_code")
			batch_no = b.get("batch_no")
			qty = flt(b.get("qty"))
			if not item_code or qty <= 0:
				continue
			if _active_sre_exists(mwo_name, item_code, batch_no):
				continue
			if dry_run:
				report.append(
					{
						"mwo": mwo_name,
						"operation": op,
						"would_heal": {
							"item_code": item_code,
							"batch_no": batch_no,
							"qty": qty,
							"physical_warehouses": _physical_batch_warehouses(
								item_code, batch_no
							),
						},
					}
				)
			else:
				created = _reserve_batch_at_physical_warehouse(
					mwo_name, item_code, batch_no, qty, op, company
				)
				report.append(
					{
						"mwo": mwo_name,
						"operation": op,
						"item_code": item_code,
						"batch_no": batch_no,
						"created": created,
						"healed": bool(created),
					}
				)

	return report


# ---------------------------------------------------------------------------
# Data gathering: unsynced MOP groups (configurable window, with MWO filter)
# ---------------------------------------------------------------------------


def _today_range():
	"""Return today's (start_str, end_str) as inclusive datetime strings."""
	today = nowdate()
	return f"{today} 00:00:00", f"{today} 23:59:59"


def _resolve_run_range(from_datetime=None, to_datetime=None):
	"""Resolve the (start, end) creation window for a sync run as datetime strings.

	If both bounds are supplied (manual EOD Sync), they are used verbatim; otherwise
	(scheduler, or a manual run that left them blank) the window defaults to today's
	start/end. Bounds are inclusive on both ends.
	"""
	if from_datetime and to_datetime:
		return str(from_datetime), str(to_datetime)
	return _today_range()


def _get_sync_range():
	"""Return the (start, end) creation window for the current run.

	Reads the window stashed on ``frappe.flags.eod_sync_range`` by
	:func:`sync_mop_logs`; falls back to today's range when unset (e.g. when a helper
	is called outside a full run). Bounds are inclusive on both ends.
	"""
	rng = getattr(frappe.flags, "eod_sync_range", None)
	if rng:
		return rng
	return _today_range()


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
		window_start, window_end = _get_sync_range()

		# Count unsynced logs before the window for reporting (they are skipped).
		old_logs = frappe.db.sql(
			"""
            SELECT COUNT(*) as cnt, COALESCE(SUM(qty_after_transaction_batch_based), 0) as qty
            FROM `tabMOP Log`
            WHERE is_synced = 0 AND is_cancelled = 0
              AND creation < %s
            """,
			(window_start,),
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
						f"{old_count} unsynced MOP Log(s) (qty={old_qty}) before the sync window "
						f"({window_start}) were found but skipped — EOD Sync only processes logs "
						"created inside the configured window."
					),
				},
				update_modified=False,
			)

		logs = frappe.db.get_all(
			"MOP Log",
			filters={
				"is_synced": 0,
				"is_cancelled": 0,
				"creation": ["between", [window_start, window_end]],
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
	"""Return {(item_code, batch_no): [warehouse, ...]} for all submitted SREs of a MWO.

	Each key maps to an ORDERED, de-duplicated list of candidate source warehouses.
	Active SREs are listed before stale ones (``Delivered``/``Cancelled``): a delivered
	SRE's reserved stock has already been consumed/moved out, so its warehouse is only a
	low-priority candidate — kept (not dropped) so residual physical stock there can still
	be used iff it physically covers the qty. The caller (``_build_eod_se_rows`` ->
	``_pick_eod_source_warehouse``) chooses by PHYSICAL batch stock, so this SRE order is a
	tie-breaker only, never the source of truth.
	"""
	sre_map = {}

	def _add(key, warehouse):
		if not warehouse:
			return
		bucket = sre_map.setdefault(key, [])
		if warehouse not in bucket:
			bucket.append(warehouse)

	batch_rows = frappe.db.sql(
		"""
        SELECT sre.item_code, sbe.batch_no, sre.warehouse
        FROM `tabStock Reservation Entry` sre
        INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
        WHERE sre.manufacturing_work_order = %s
          AND sre.docstatus = 1
        ORDER BY (sre.status IN ('Delivered', 'Cancelled')) ASC, sre.modified DESC
        """,
		(mwo,),
		as_dict=True,
	)
	for row in batch_rows:
		_add((row.item_code, row.batch_no), row.warehouse)

	qty_rows = frappe.db.sql(
		"""
        SELECT item_code, warehouse
        FROM `tabStock Reservation Entry`
        WHERE manufacturing_work_order = %s
          AND docstatus = 1
        ORDER BY (status IN ('Delivered', 'Cancelled')) ASC, modified DESC
        """,
		(mwo,),
		as_dict=True,
	)
	for row in qty_rows:
		_add((row.item_code, None), row.warehouse)

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


def _eod_base_mr_voucher_qty(manufacturing_order, manufacturer):
	"""Mirror ``stock_reservation_entry_for_mwo``'s voucher_qty base.

	Returns the Material Request total (``SUM(custom_total_quantity)`` of non-cancelled
	MRs for the MO) inflated by the manufacturer's Manufacturing Setting
	``addition_maximum_item__tolerance_percentage``. Returns ``None`` when there is no
	MR data (caller then falls back to its previous voucher_qty model).
	"""
	if not manufacturing_order:
		return None
	row = frappe.db.sql(
		"""
        SELECT sum(custom_total_quantity) FROM `tabMaterial Request`
        WHERE manufacturing_order=%s AND docstatus!=2
        """,
		(manufacturing_order,),
	)
	if not (row and row[0] and row[0][0] is not None):
		return None
	base = flt(row[0][0])
	tolerance = None
	if manufacturer:
		tolerance = frappe.db.get_value(
			"Manufacturing Setting",
			manufacturer,
			"addition_maximum_item__tolerance_percentage",
		)
	if tolerance:
		base = base + (base * (flt(tolerance) / 100))
	return base


def _free_batch_qty_to_reserve(item_code, warehouse, batch_no):
	"""Free batch qty that can still be reserved at ``warehouse``.

	Computed SBB-aware: physical batch qty minus the qty still open on active SREs for
	that batch at that warehouse. ERPNext's ``get_available_qty_to_reserve`` is not used
	because it delegates to the same ``get_batch_qty`` and adds nothing here.

	``ignore_reserved_stock=True`` is essential: WITHOUT it ``get_batch_qty`` already
	nets out reservations, and the subtraction below then removes them a SECOND time,
	collapsing free qty toward zero and making the reservation healer a silent no-op.

	The open-qty query mirrors :func:`_get_sre_undelivered_batch_qty` exactly --
	``qty - delivered_qty`` (not raw ``qty``), only live statuses, and only
	batch-based reservations. Anything looser over-subtracts.
	"""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	physical = flt(
		get_batch_qty(
			batch_no=batch_no,
			warehouse=warehouse,
			item_code=item_code,
			ignore_reserved_stock=True,
		)
	)
	if physical <= 0:
		return 0.0
	reserved = flt(
		frappe.db.sql(
			"""
			SELECT COALESCE(SUM(sbe.qty - sbe.delivered_qty), 0)
			FROM `tabStock Reservation Entry` sre
			INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
			WHERE sre.docstatus = 1
			  AND sre.item_code = %s
			  AND sre.warehouse = %s
			  AND sbe.batch_no = %s
			  AND sre.status NOT IN ('Delivered', 'Cancelled')
			  AND sre.reservation_based_on = 'Serial and Batch'
			""",
			(item_code, warehouse, batch_no),
		)[0][0]
	)
	return max(physical - reserved, 0.0)


def _physical_batch_warehouses(item_code, batch_no):
	"""Every warehouse that PHYSICALLY holds (item, batch), SBB-aware, reservations ignored.

	``get_batch_qty(warehouse=None)`` returns a per-warehouse breakdown of the batch's
	physical stock (ERPNext batch.py). In v16 batch stock lives in the Serial and Batch
	Bundle (``Stock Ledger Entry.batch_no`` is NULL), so this is the authoritative way to
	discover where WIP that EOD parked at a non-current-operation warehouse actually sits.
	``ignore_reserved_stock=True`` so the batch we are re-reserving is not netted out of its
	own warehouse. Returns {warehouse: physical_qty}.
	"""
	if not (item_code and batch_no):
		return {}
	from erpnext.stock.doctype.batch.batch import get_batch_qty
	from frappe.utils import nowtime, today

	try:
		rows = get_batch_qty(
			batch_no=batch_no,
			item_code=item_code,
			warehouse=None,
			posting_date=today(),
			posting_time=nowtime(),
			ignore_reserved_stock=True,
		)
	except Exception:
		return {}
	out = {}
	for r in rows or []:
		wh = r.get("warehouse")
		if wh and flt(r.get("qty")) > 0:
			out[wh] = out.get(wh, 0.0) + flt(r.get("qty"))
	return out


def _resolve_mwo_so_anchor(mwo, mwo_cache=None):
	"""Resolve ``{"sales_order","sales_order_item","base_mr_voucher_qty"}`` for a MWO.

	Returns ``None`` when the MWO has no Sales Order anchor (e.g. a stock-based MWO); the
	caller then skips reservation rather than minting a malformed SRE. Lifted from the inner
	closure of ``_reserve_sres_from_eod_se_rows`` so the physical-warehouse heal
	(``_reserve_batch_at_physical_warehouse``) shares the exact same SO-anchor resolution.
	Pass ``mwo_cache`` (a dict) to memoise across rows.
	"""
	if mwo_cache is not None and mwo in mwo_cache:
		return mwo_cache[mwo]
	mo, mfr = frappe.db.get_value(
		"Manufacturing Work Order", mwo, ["manufacturing_order", "manufacturer"]
	) or (None, None)
	sales_order = sales_order_item = mo_manufacturer = None
	if mo:
		sales_order, sales_order_item, mo_manufacturer = frappe.get_cached_value(
			"Parent Manufacturing Order",
			mo,
			["sales_order", "sales_order_item", "manufacturer"],
		) or (None, None, None)
	resolved = None
	if sales_order:
		resolved = {
			"sales_order": sales_order,
			"sales_order_item": sales_order_item,
			"base_mr_voucher_qty": _eod_base_mr_voucher_qty(mo, mfr or mo_manufacturer),
		}
	if mwo_cache is not None:
		mwo_cache[mwo] = resolved
	return resolved


def _build_and_submit_mwo_sre(
	company,
	mwo,
	item_code,
	warehouse,
	batch_no,
	reserved_qty,
	available,
	manufacturing_operation,
	resolved,
	has_batch_no,
	has_serial_no,
	stock_uom,
):
	"""Construct + submit ONE Sales-Order-anchored MWO Stock Reservation Entry at
	``warehouse`` and return its name.

	Extracted verbatim from ``_reserve_sres_from_eod_se_rows`` (the SO demand-cap lift plus
	the ``new_doc`` build/insert/submit). Its two callers differ ONLY in how ``warehouse``
	and ``reserved_qty``/``available`` are chosen — the normal EOD path pins them to the SE
	row's target where the stock landed; the physical-warehouse heal picks them from live
	SBB batch stock — so everything from the SO cap down is shared here. ``resolved`` is the
	dict returned by ``_resolve_mwo_so_anchor``.
	"""
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_sre_reserved_qty_for_voucher_detail_no,
	)

	# Lift the Sales Order demand cap exactly like the original creator: the SO line is
	# intentionally over-reserved across components, so size voucher_qty to cover the
	# current reservation. The source SRE was already cancelled, so it is excluded from
	# this global sum.
	total_so_reserved = get_sre_reserved_qty_for_voucher_detail_no(
		item_code,
		"Sales Order",
		resolved["sales_order"],
		resolved["sales_order_item"],
	)
	floor = flt(total_so_reserved) + reserved_qty
	base_mr_voucher_qty = resolved["base_mr_voucher_qty"]
	if base_mr_voucher_qty is not None:
		effective_voucher_qty = max(flt(base_mr_voucher_qty), floor)
	else:
		effective_voucher_qty = floor

	new_sre = frappe.new_doc("Stock Reservation Entry")
	new_sre.voucher_type = "Sales Order"
	new_sre.voucher_no = resolved["sales_order"]
	new_sre.voucher_detail_no = resolved["sales_order_item"]
	new_sre.voucher_qty = effective_voucher_qty
	new_sre.item_code = item_code
	new_sre.warehouse = warehouse
	new_sre.reserved_qty = reserved_qty
	new_sre.company = company
	new_sre.stock_uom = stock_uom
	new_sre.has_batch_no = cint(has_batch_no)
	new_sre.has_serial_no = cint(has_serial_no)
	new_sre.available_qty = max(flt(available), reserved_qty)
	new_sre.manufacturing_work_order = mwo
	new_sre.manufacturing_operation = manufacturing_operation
	if has_batch_no and batch_no:
		new_sre.reservation_based_on = "Serial and Batch"
		new_sre.append(
			"sb_entries",
			{
				"batch_no": batch_no,
				"warehouse": warehouse,
				"qty": reserved_qty,
			},
		)
	else:
		new_sre.reservation_based_on = "Qty"
	new_sre.flags.ignore_permissions = True
	new_sre.insert(ignore_links=1)
	new_sre.submit()
	return new_sre.name


def _cancelled_and_sibling_sre_warehouses(mwo, item_code, batch_no):
	"""Candidate warehouses from SRE history for (item, batch):

	  (a) this MWO's recently-cancelled (docstatus=2) SREs, and
	  (b) active (docstatus=1) SREs for the SAME item/batch on OTHER MWOs (siblings sharing
	      the casting batch).

	Best-effort hints only — every candidate is re-validated against physical free qty by
	the caller, so a stale/emptied warehouse is harmlessly dropped. Usually (a) is empty in
	the fully-committed bug case because ``_hard_delete_cancelled_snapshots`` already deleted
	the docstatus=2 rows; (b) still surfaces the shared batch's live warehouse.
	"""
	if not (item_code and batch_no):
		return []
	return [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT DISTINCT sre.warehouse
			FROM `tabStock Reservation Entry` sre
			INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
			WHERE sre.item_code = %s
			  AND sbe.batch_no = %s
			  AND (
			        (sre.manufacturing_work_order = %s AND sre.docstatus = 2)
			     OR (sre.manufacturing_work_order != %s AND sre.docstatus = 1)
			  )
			""",
			(item_code, batch_no, mwo, mwo),
		)
		if r[0]
	]


def _mop_log_to_warehouses(mwo, item_code, batch_no):
	"""``to_warehouse`` values from this MWO's non-cancelled MOP Logs for the item/batch."""
	if not (item_code and batch_no):
		return []
	return [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT DISTINCT to_warehouse
			FROM `tabMOP Log`
			WHERE manufacturing_work_order = %s
			  AND item_code = %s
			  AND batch_no = %s
			  AND is_cancelled = 0
			  AND to_warehouse IS NOT NULL AND to_warehouse != ''
			""",
			(mwo, item_code, batch_no),
		)
		if r[0]
	]


def _mwo_batch_balance(operation, item_code, batch_no):
	"""This MWO's current WIP balance for (item, batch) at ``operation`` (0.0 if none).

	Reserving the MWO's own balance (rather than the whole free pool at the warehouse) keeps
	a batch shared by several orphaned MWOs from being over-reserved by the first one healed.
	Mirrors the qty source used by ``backfill_missing_wip_reservations``.
	"""
	if not operation:
		return 0.0
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	rows = get_current_mop_balance_rows(
		operation,
		include_fields=[
			"item_code",
			"batch_no",
			"qty_after_transaction_batch_based as qty",
		],
		keys=[(item_code, batch_no)],
	)
	for b in rows:
		if b.get("item_code") == item_code and b.get("batch_no") == batch_no:
			return flt(b.get("qty"))
	return 0.0


def _heal_ownership_allowed(item_code, batch_no, resolved):
	"""Never reserve one customer's goods against another customer's Sales Order.

	The healer mints a real, submitted reservation. A Customer Goods batch belongs to a
	specific customer, so anchoring it to a Sales Order for a different customer would
	commit their metal to the wrong order -- exactly the ownership-loss class that the
	EOD transfer rows already suffered from.
	"""
	inventory_type, batch_customer = frappe.db.get_value(
		"Batch", batch_no, ["custom_inventory_type", "custom_customer"]
	) or (None, None)
	if inventory_type != "Customer Goods":
		return True

	so = resolved.get("sales_order") if hasattr(resolved, "get") else None
	so_customer = frappe.db.get_value("Sales Order", so, "customer") if so else None
	if batch_customer and so_customer and batch_customer == so_customer:
		return True

	frappe.logger().warning(
		"MOP EOD Sync: refusing to heal reservation for Customer Goods batch %s "
		"(item %s, batch customer %s) against Sales Order %s (customer %s).",
		batch_no,
		item_code,
		batch_customer,
		so,
		so_customer,
	)
	return False


def _warehouses_of_company(warehouses, company):
	"""Drop candidates that belong to another company.

	Reserving across companies would build a Stock Entry the ledger cannot post.

	Fails OPEN on a query error. Not every site in this bench has a queryable
	``tabWarehouse`` (the ``gk`` site has the DocType but no base table), and a filter
	that failed closed there would silently return "no candidate warehouse" for every
	row -- disabling reservation healing entirely and looking exactly like the missing-SRE
	bug it exists to fix. The healer's other guards (free batch qty, ownership, Sales
	Order anchor) still apply.
	"""
	warehouses = {w for w in warehouses if w}
	if not warehouses or not company:
		return warehouses
	try:
		return set(
			frappe.db.get_all(
				"Warehouse",
				filters={"name": ("in", sorted(warehouses)), "company": company},
				pluck="name",
			)
		)
	except Exception:
		frappe.logger().warning(
			"MOP EOD Sync: could not verify warehouse company for %s; keeping all "
			"candidates.",
			company,
		)
		return warehouses


def _reserve_batch_at_physical_warehouse(
	mwo, item_code, batch_no, needed_qty, operation, company, sync_log_name=None
):
	"""Re-create ONE Sales-Order-anchored SRE for (mwo, item, batch) at the warehouse where
	the batch PHYSICALLY sits with the most free qty. Returns ``[sre_name]`` or ``None``.

	Repairs the EOD orphaned-cancellation bug: EOD cancels the source SREs, then silently
	skips re-reserving the batched row when the batch is not physically at the SE row's
	target (v16 SBB batch stock), leaving zero active SREs. Reserving at the current-operation
	department warehouse (the old backfill) fixes nothing when the WIP is parked elsewhere,
	and — because the later Process Loss SE consumes from ``sre.warehouse`` — reserving at the
	WRONG warehouse would only trade "no SRE" for negative batch stock. So we discover the real
	warehouse from SBB physical stock and reserve there.

	Idempotent (returns ``None`` when an active SRE already exists). Fails safe: returns
	``None`` — never a negative-driving SRE — when no candidate warehouse physically holds
	enough free batch qty to cover ``needed_qty``, or when the MWO has no Sales Order anchor.
	"""
	if not batch_no:
		return None
	if _active_sre_exists(mwo, item_code, batch_no):
		return None

	resolved = _resolve_mwo_so_anchor(mwo)
	if not resolved:
		# Stock-based MWO with no Sales Order anchor — do not mint a malformed reservation.
		return None

	if not _heal_ownership_allowed(item_code, batch_no, resolved):
		return None

	# Candidate warehouses. Physical SBB stock is ground truth; the SRE-history and MOP-Log
	# hints only broaden the set and are each re-validated by _free_batch_qty_to_reserve.
	candidates = set(_physical_batch_warehouses(item_code, batch_no))
	candidates.update(_cancelled_and_sibling_sre_warehouses(mwo, item_code, batch_no))
	if operation:
		dept = _resolve_department_warehouse(
			frappe.get_cached_doc("Manufacturing Operation", operation)
		)
		if dept:
			candidates.add(dept)
	candidates.update(_mop_log_to_warehouses(mwo, item_code, batch_no))
	candidates = _warehouses_of_company(candidates, company)

	# Pick the warehouse with the most FREE batch qty (physical − active reservations).
	best_wh, best_free = None, 0.0
	for wh in candidates:
		free = _free_batch_qty_to_reserve(item_code, wh, batch_no)
		if free > best_free:
			best_wh, best_free = wh, free

	needed_qty = flt(needed_qty)
	# Fail loudly rather than relocate the crash: if the best warehouse cannot even cover the
	# loss floor, healing here would only push the negative-stock failure downstream.
	if not best_wh or best_free <= 1e-9 or best_free + 1e-6 < needed_qty:
		return None

	# Reserve this MWO's OWN balance (never the whole free pool, which may include a sibling
	# orphaned MWO's contribution), clamped to what is free and floored at needed_qty so the
	# loss is always covered. best_free >= needed_qty holds from the guard above.
	target = _mwo_batch_balance(operation, item_code, batch_no)
	reserved_qty = min(max(flt(target), needed_qty), best_free)

	has_batch_no, has_serial_no, stock_uom = frappe.get_cached_value(
		"Item", item_code, ["has_batch_no", "has_serial_no", "stock_uom"]
	)
	name = _build_and_submit_mwo_sre(
		company,
		mwo,
		item_code,
		best_wh,
		batch_no,
		reserved_qty,
		best_free,
		operation,
		resolved,
		has_batch_no,
		has_serial_no,
		stock_uom,
	)
	if sync_log_name:
		# Audit-only row: it records which SRE was minted for which item/batch/warehouse
		# (nothing else links a healer-minted SRE back to its context). qty is left at 0
		# deliberately -- reserved_qty is the MWO's whole batch balance, not a transfer,
		# so counting it would double-count against the transfer row for the same batch
		# and could push progress_percent past 100.
		_insert_sync_log_item(
			sync_log_name,
			{
				"manufacturing_work_order": mwo,
				"manufacturing_operation": operation or "",
				"company": company,
				"item_code": item_code,
				"batch_no": batch_no,
				"source_warehouse": best_wh,
				"qty": 0,
				"status": "Synced",
				"sync_stage": "WIP Reservation Healed",
				"stock_reservation_entry": name,
				"suggested_fix": (
					f"Reservation healed at {best_wh} for {flt(reserved_qty, 3)} "
					f"{stock_uom or ''}".strip()
				),
			},
		)
	return [name]


def _eod_rows_from_submitted_se(se_doc):
	"""Re-reservation inputs taken from what the Stock Entry ACTUALLY moved.

	``_reserve_sres_from_eod_se_rows``' docstring always claimed to read the submitted
	Stock Entry's rows, but it was handed the plan-phase dicts. Those diverge: SE
	``before_validate`` hooks can drop or rewrite rows, and a row that never made it into
	the submitted document must not have its source reservation cancelled and deleted.
	"""
	rows = []
	# Attribute access, not .get(): this must work for a real Document and for the
	# lightweight Stock Entry stand-ins the suite builds.
	for item in getattr(se_doc, "items", None) or []:
		rows.append(
			{
				"item_code": item.get("item_code"),
				"qty": flt(item.get("qty"), 3),
				"s_warehouse": item.get("s_warehouse"),
				"t_warehouse": item.get("t_warehouse"),
				"batch_no": item.get("batch_no"),
				"serial_no": item.get("serial_no"),
				"manufacturing_operation": item.get("manufacturing_operation"),
				"custom_manufacturing_work_order": item.get(
					"custom_manufacturing_work_order"
				),
				"inventory_type": item.get("inventory_type"),
				"customer": item.get("customer"),
			}
		)
	return rows


def _reserve_sres_from_eod_se_rows(company, items, sync_log_name=None):
	"""Build Sales-Order-anchored Stock Reservation Entries from the just-submitted
	consolidated EOD Stock Entry's rows.

	Replaces the old snapshot-copy re-reservation: instead of cloning the cancelled source
	SRE, the reservation is built from what the Stock Entry actually moved. Each row carries
	its own MWO (``custom_manufacturing_work_order``) and the last operation
	(``manufacturing_operation``); the reservation stays anchored to that MWO's Sales Order
	(so ERPNext SO delivered/reserved-qty accounting and downstream Make Receive consumption
	keep working) and lands at the row's ``t_warehouse`` where the stock now sits.

	Mirrors ``stock_reservation_entry_for_mwo`` (doc_events/stock_entry.py) per row, with the
	EOD divergences: it reads the MWO/operation PER ROW (not the SE header, which the
	consolidated SE leaves blank) and SKIPS a row when nothing is free at the target rather
	than throwing. Returns the list of created SRE names.
	"""
	if not items:
		return []
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	# Resolve the Sales Order anchor + MR-based voucher_qty cap once per MWO.
	mwo_cache = {}

	created = []
	for row in items:
		mwo = row.get("custom_manufacturing_work_order")
		if not mwo:
			continue
		resolved = _resolve_mwo_so_anchor(mwo, mwo_cache)
		# No Sales Order anchor (e.g. a stock-based MWO) — the stock still moved, but we do
		# not create a malformed reservation. Skip.
		if not resolved:
			continue

		item_code = row.get("item_code")
		t_warehouse = row.get("t_warehouse")
		if not (item_code and t_warehouse):
			continue
		batch_no = row.get("batch_no")
		has_batch_no, has_serial_no, stock_uom = frappe.get_cached_value(
			"Item", item_code, ["has_batch_no", "has_serial_no", "stock_uom"]
		)

		# Clamp to what is actually free at the target. The stock just landed here, but part
		# of a batch may already be reserved — never reserve more than is free, and skip
		# entirely when nothing is free (rather than letting ERPNext throw).
		if has_batch_no and batch_no:
			# v16 keeps batch stock in the Serial and Batch Bundle (SLE.batch_no is NULL),
			# so ERPNext's get_available_qty_to_reserve(..., batch_no=...) returns 0 and
			# EVERY batched WIP row would be silently skipped — the new operation would end
			# up holding physical stock with no Stock Reservation Entry, and Employee IR
			# Process Loss then fails with "No active Stock Reservation Entry found".
			# Compute the free batch qty SBB-aware instead.
			available = _free_batch_qty_to_reserve(item_code, t_warehouse, batch_no)
		else:
			available = get_available_qty_to_reserve(item_code, t_warehouse)
		reserved_qty = min(flt(row.get("qty")), flt(available))
		if reserved_qty <= 1e-9:
			continue

		created.append(
			_build_and_submit_mwo_sre(
				company,
				mwo,
				item_code,
				t_warehouse,
				batch_no,
				reserved_qty,
				available,
				row.get("manufacturing_operation"),
				resolved,
				has_batch_no,
				has_serial_no,
				stock_uom,
			)
		)

	return created


def _hard_delete_cancelled_snapshots(snapshots):
	"""Hard-delete the source SREs this run just cancelled (the snapshots).

	Called inside the Phase-2 savepoint AFTER the new reservation succeeds so the cancelled
	reservation records do not accumulate. Any delete error propagates so the bucket's
	savepoint rolls back (restoring the cancelled SREs and the submit, never leaving them
	deleted without a transfer). Mirrors the force-delete pattern in
	retry_stuck_eod_submit_mwos.py.
	"""
	for snap in snapshots or []:
		name = snap["sre"].name
		if frappe.db.exists("Stock Reservation Entry", name):
			frappe.delete_doc(
				"Stock Reservation Entry",
				name,
				force=1,
				ignore_permissions=True,
			)


# ---------------------------------------------------------------------------
# SE row construction
# ---------------------------------------------------------------------------


def _eod_physical_batch_qty(item_code, batch_no, warehouse):
	"""Physical SBB qty of (item, batch) in ``warehouse``, reservations ignored.

	Mirrors ``pc_tagging_stock_sync._physical_batch_qty`` and EOD's own
	``_check_eod_source_batch_stock``. Returns ``None`` for non-batch lines (the SBB
	negative-batch validator only fires on batched items). ``ignore_reserved_stock=True``
	is required: the batch we are about to consume is itself reserved by the SRE being
	processed, so the default (reservation-subtracted) qty would understate the warehouse.
	"""
	if not batch_no or not warehouse:
		return None
	from erpnext.stock.doctype.batch.batch import get_batch_qty
	from frappe.utils import nowtime, today

	try:
		return flt(
			get_batch_qty(
				batch_no=batch_no,
				warehouse=warehouse,
				item_code=item_code,
				posting_date=today(),
				posting_time=nowtime(),
				ignore_reserved_stock=True,
			),
			3,
		)
	except Exception:
		return 0.0


def _eod_authoritative_batch_cap(item_code, batch_no, warehouse):
	"""Batch qty a submit will actually allow at ``warehouse``.

	``_eod_physical_batch_qty`` resolves to ERPNext's ``get_batch_qty`` ->
	``get_available_batches``, which INNER JOINs Stock Ledger Entry and therefore ignores
	an ORPHANED bundle that has no SLE. ERPNext's own submit-time negative-batch guard
	sums ``Serial and Batch Entry`` with no such join. On the live site 606 (item,
	warehouse, batch) keys disagree by 24,413 units, so trusting only the SLE-joined
	number reproduces the very failure this allocator exists to prevent.

	The cap is therefore the MINIMUM of both readings -- whichever guard fires first is
	the one that matters.
	"""
	from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
		get_authoritative_batch_qty,
	)

	sle_joined = flt(_eod_physical_batch_qty(item_code, batch_no, warehouse) or 0.0, 3)
	try:
		authoritative = flt(
			get_authoritative_batch_qty(item_code, warehouse, [batch_no]).get(
				batch_no, 0.0
			),
			3,
		)
	except Exception:
		# Never let a cap query failure silently read as "no stock" -- that would evict
		# every MWO. Fall back to the SLE-joined reading and say so.
		frappe.logger().exception(
			"MOP EOD Sync: authoritative batch qty failed for %s / %s / %s",
			item_code,
			batch_no,
			warehouse,
		)
		return sle_joined
	return min(sle_joined, authoritative)


def _eod_warehouse_headroom(item_code, warehouse, released_sre_qty=0.0):
	"""Item-level qty a submit will allow at ``warehouse``, ignoring batches.

	ERPNext's warehouse guard is ``qty_after_transaction + actual_qty - reserved_stock``:
	item+warehouse scoped and batch-blind, and it accounts for 526 of the held rows. This
	is HEADROOM, not stock -- physical qty alone is roughly an order of magnitude too
	generous on the hot WIP warehouses, because most of it is reserved.

	``released_sre_qty`` is the reservation this bucket is about to cancel for the same
	(item, warehouse); it becomes available to the transfer, so it is added back.
	"""
	bin_row = (
		frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			["actual_qty", "reserved_stock"],
			as_dict=True,
		)
		or {}
	)
	actual = flt(bin_row.get("actual_qty"), 3)
	reserved = flt(bin_row.get("reserved_stock"), 3)
	return flt(actual - max(0.0, reserved - flt(released_sre_qty, 3)), 3)


def _eod_released_sre_qty(main_mwos):
	"""``{(item_code, warehouse): qty}`` this bucket will free by cancelling its own SREs.

	``_snapshot_mwo_sres_for_relocation`` cancels the source reservations of the MWOs in
	THIS bucket before the transfer submits, so that qty is not really a constraint on
	it. Reservations held by MWOs outside the bucket stay put and must still be honoured.
	"""
	mwos = sorted({m["mwo"] for m in main_mwos if m.get("mwo")})
	if not mwos:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT item_code, warehouse, COALESCE(SUM(reserved_qty - delivered_qty), 0) AS qty
		FROM `tabStock Reservation Entry`
		WHERE docstatus = 1
		  AND status NOT IN ('Delivered', 'Cancelled')
		  AND manufacturing_work_order IN %(mwos)s
		GROUP BY item_code, warehouse
		""",
		{"mwos": mwos},
		as_dict=True,
	)
	return {(r.item_code, r.warehouse): flt(r.qty, 3) for r in rows}


def _mwo_sort_key(main_mwo):
	"""Oldest unsynced MOP Log first.

	NOT MWO name: this bench mixes ``KGJPL-MWO-24-...`` and ``MWO-KGJPL-<design>-...``,
	so name order sorts by product family and would starve one family indefinitely. Age
	is self-correcting -- a deferred MWO's creation timestamp is immutable, so its
	priority rises on every subsequent run until it is admitted.
	"""
	creations = [
		log.get("creation")
		for md in main_mwo.get("mop_data_list") or []
		for log in (md.get("logs") or [])
		if log.get("creation")
	]
	# Missing timestamps sort last, never first, so they cannot jump the queue.
	return (str(min(creations)) if creations else "9999", main_mwo.get("mwo") or "")


def _allocate_bucket_by_physical_stock(main_mwos):
	"""Admit MWOs into the bucket's single Stock Entry against a running stock tally.

	The bucket's rows are flattened into ONE Stock Entry that consumes their SUM, but
	each MWO was validated alone. Two MWOs that each fit can therefore jointly overdraw a
	batch: 0.510 + 0.520 against 0.803 physical is exactly how one live run put 402 MWOs
	into a draft with a -0.227 negative-batch error.

	Pure function: no writes, no doc mutation, safe to call outside every savepoint.
	Returns ``(admitted, deferred, infeasible)`` of whole MWO dicts.

	  * ``deferred``   -- fits on its own but lost tonight's race for shared stock.
	  * ``infeasible`` -- exceeds the cap even against an empty tally; a data defect that
	                      will never clear on its own and needs reconciliation.

	Allocation is whole-MWO: on a miss the tally is left untouched. Rows are never
	filtered out of a surviving MWO, because the commit path iterates ``main_mwos`` to
	cancel and re-reserve SREs -- a row-filtered MWO would have its reservation cancelled
	with no replacement row to re-reserve from.
	"""
	if not main_mwos:
		return [], [], []

	released = _eod_released_sre_qty(main_mwos)
	batch_cap, wh_cap = {}, {}
	used_batch, used_wh = {}, {}
	admitted, deferred, infeasible = [], [], []

	def _demand(mwo_entry):
		"""Aggregate this MWO's own demand per batch key and per warehouse key."""
		batch_need, wh_need = {}, {}
		for item in mwo_entry.get("items") or []:
			warehouse = item.get("s_warehouse")
			item_code = item.get("item_code")
			qty = flt(item.get("qty"), 3)
			# No source warehouse => not a transfer this allocator can reason about
			# (the commit path skips it too). Skip, never evict the MWO for it.
			if not warehouse or not item_code or qty <= 0:
				continue
			wh_need[(warehouse, item_code)] = flt(
				wh_need.get((warehouse, item_code), 0.0) + qty, 3
			)
			if item.get("batch_no"):
				key = (warehouse, item_code, item["batch_no"])
				batch_need[key] = flt(batch_need.get(key, 0.0) + qty, 3)
		return batch_need, wh_need

	# Match the tolerance the rest of the module compares stock with, so a row that is
	# exactly equal to the cap is admitted rather than deferred on float noise.
	tol = 1e-6
	for entry in sorted(main_mwos, key=_mwo_sort_key):
		batch_need, wh_need = _demand(entry)
		if not batch_need and not wh_need:
			admitted.append(entry)
			continue

		shortfalls, own_shortfalls = [], []
		for key, need in batch_need.items():
			warehouse, item_code, batch_no = key
			if key not in batch_cap:
				batch_cap[key] = _eod_authoritative_batch_cap(
					item_code, batch_no, warehouse
				)
			cap = batch_cap[key]
			if used_batch.get(key, 0.0) + need > cap + tol:
				shortfalls.append((key, need, cap, used_batch.get(key, 0.0)))
				if need > cap + tol:
					own_shortfalls.append((key, need, cap))
		for key, need in wh_need.items():
			warehouse, item_code = key
			if key not in wh_cap:
				wh_cap[key] = _eod_warehouse_headroom(
					item_code, warehouse, released.get((item_code, warehouse), 0.0)
				)
			cap = wh_cap[key]
			if used_wh.get(key, 0.0) + need > cap + tol:
				shortfalls.append((key, need, cap, used_wh.get(key, 0.0)))
				if need > cap + tol:
					own_shortfalls.append((key, need, cap))

		if not shortfalls:
			for key, need in batch_need.items():
				used_batch[key] = flt(used_batch.get(key, 0.0) + need, 3)
			for key, need in wh_need.items():
				used_wh[key] = flt(used_wh.get(key, 0.0) + need, 3)
			admitted.append(entry)
			continue

		entry["_allocation_shortfalls"] = shortfalls
		if own_shortfalls:
			entry["_allocation_own_shortfalls"] = own_shortfalls
			infeasible.append(entry)
		else:
			deferred.append(entry)

	return admitted, deferred, infeasible


def _pick_eod_source_warehouse(
	item_code, batch_no, required_qty, candidates, t_warehouse
):
	"""Choose the EOD source warehouse by PHYSICAL batch stock (the SRE is only logical).

	``candidates`` is the ordered list of SRE-derived source warehouses for the (item,
	batch) — active-SRE warehouses first (see ``_preload_sre_warehouse_map``).

	  * Non-batch line: first candidate (legacy behaviour), else ``None``.
	  * Batch line, in order:
	      1. the target physically covers ``required_qty`` -> return the target. The stock
	         has already moved to the department warehouse, so the caller turns this into a
	         completed no-op (source == target). This is exactly what an out-of-band
	         transfer (or a prior run) leaves behind, and the case that previously failed as
	         a phantom ``batch_short`` against a stale ``Delivered`` SRE warehouse.
	      2. else the first candidate that physically covers ``required_qty`` -> the real
	         transfer source (skips stale/delivered warehouses that no longer hold stock).
	      3. else the first candidate (legacy choice) -> a transfer row is still built so the
	         downstream ``_check_eod_source_batch_stock`` reports an accurate ``batch_short``
	         (require X, have Y); when that candidate is the target (the SRE reserves at the
	         department itself) it stays a clean source == target no-op.
	      4. else ``None`` (no candidate at all) -> reported as no_sre_warehouse.

	The target is returned as a no-op ONLY when it physically covers the qty (step 1); a
	batch missing at the target falls through to the candidate path, so a genuinely missing
	batch is never mistaken for a completed transfer.
	"""
	if not batch_no:
		return candidates[0] if candidates else None

	tol = 1e-6
	# 1. Physically already at the target department warehouse -> completed no-op.
	if (
		flt(_eod_physical_batch_qty(item_code, batch_no, t_warehouse) or 0) + tol
		>= required_qty
	):
		return t_warehouse
	# 2. First candidate warehouse that physically covers the qty.
	for wh in candidates:
		if (
			flt(_eod_physical_batch_qty(item_code, batch_no, wh) or 0) + tol
			>= required_qty
		):
			return wh
	# 3/4. Nothing covers it: keep the first (active-ordered) candidate so batch_short fires
	# with real numbers; only the genuine "no candidate at all" case is left unresolved.
	return candidates[0] if candidates else None


def _eod_batch_ownership(batch_nos):
	"""``{batch_no: (custom_inventory_type, custom_customer)}`` in ONE query.

	``resolve_batch_ownership`` reads one Batch per row; a consolidated EOD Stock Entry
	carries hundreds of rows, so the lookup is batched here and the shared normalisation
	rules are applied per row afterwards.
	"""
	batch_nos = sorted({b for b in batch_nos if b})
	if not batch_nos:
		return {}
	rows = frappe.db.get_all(
		"Batch",
		filters={"name": ("in", batch_nos)},
		fields=["name", "custom_inventory_type", "custom_customer"],
		limit_page_length=0,
	)
	return {r.name: (r.custom_inventory_type, r.custom_customer) for r in rows}


def _stamp_eod_row_ownership(row, ownership):
	"""Carry the SOURCE batch's ownership onto an EOD transfer row.

	Without this the row reaches ``doc_events/stock_entry.py``'s blanket
	"default blank inventory_type to Regular Stock", which silently books a customer's
	metal as company stock -- and ``_save_draft_eod_se`` sets ``auto_created = 1``, which
	short-circuits ``CustomStockEntry.update_batches``' ownership backfill, so nothing
	else fills it in. Uses the same normalisation rules as every other builder
	(``customization/utils/row_ownership``), so a customer type with no customer is
	downgraded rather than minting a row that trips the Customer Goods guard.
	"""
	from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
		normalize_ownership,
	)

	batch_inv, batch_customer = ownership.get(row.get("batch_no")) or (None, None)
	inventory_type, customer = normalize_ownership(
		batch_inv,
		batch_customer,
		batch_no=row.get("batch_no"),
		item_code=row.get("item_code"),
	)
	row["inventory_type"] = inventory_type
	if customer:
		row["customer"] = customer
	return row


def _build_eod_se_rows(mwo, last_mop_name, last_logs, t_warehouse, sre_map):
	"""Build Stock Entry item rows for the EOD material transfer."""
	rows = []
	skipped = []
	ownership = _eod_batch_ownership(log.batch_no for log in last_logs)
	for log in last_logs:
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue

		candidates = list(sre_map.get((log.item_code, log.batch_no)) or [])
		for c in sre_map.get((log.item_code, None)) or []:
			if c not in candidates:
				candidates.append(c)

		s_warehouse = _pick_eod_source_warehouse(
			log.item_code, log.batch_no, qty, candidates, t_warehouse
		)
		if not s_warehouse:
			# qty travels with the skip so the reservation healer knows how much to
			# cover without re-deriving it from the MOP Log.
			skipped.append(
				{"item_code": log.item_code, "batch_no": log.batch_no, "qty": qty}
			)
			continue

		if s_warehouse == t_warehouse:
			# Stock already sits at the target — nothing to transfer (completed no-op).
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
		# pcs is a reqd Data field defaulting to "1". Only Diamond/Gemstone items are
		# counted in pieces — set their real count from the MOP Log balance so the EOD
		# SE reflects actual stone counts. Metal/Finding rows keep the "1" default.
		if (log.item_code or "")[:1] in ("D", "G"):
			row["pcs"] = cint(log.pcs_after_transaction_batch_based)
		if log.batch_no:
			row["batch_no"] = log.batch_no
		if getattr(log, "serial_no", None):
			row["serial_no"] = log.serial_no
		rows.append(_stamp_eod_row_ownership(row, ownership))
	return rows, skipped


# ---------------------------------------------------------------------------
# MOP Log sync-marking (window-bounded safety net, or full history in selective mode)
# ---------------------------------------------------------------------------


def _mark_all_mwo_mop_logs_synced(
	manufacturing_work_orders, selective=False, log_names=None
):
	"""Mark unsynced non-cancelled MOP Logs for the given MWOs as synced.

	Scheduled runs (``selective=False``) only mark logs inside the run's window — a
	safety net so a misconfigured run can never silently bury logs outside it. The
	window matches the one used to gather logs (today by default, or the manual
	From/To). Selective runs (an enabled MWO filter row) mark the MWO's full unsynced
	history.

	``log_names`` pins the update to the exact rows this run gathered. Selective mode has
	no window to bound it, so without this it marks EVERY unsynced log of the MWO —
	including any created after planning — burying work the transfer never moved.
	"""
	if not manufacturing_work_orders:
		return
	if selective:
		conditions = ""
		params = {"mwos": manufacturing_work_orders}
		if log_names:
			conditions = " AND name IN %(log_names)s"
			params["log_names"] = list(log_names)
		frappe.db.sql(
			"""
            UPDATE `tabMOP Log`
            SET is_synced = 1
            WHERE manufacturing_work_order IN %(mwos)s
              AND is_synced = 0
              AND is_cancelled = 0
            """
			+ conditions,
			params,
		)
		return
	window_start, window_end = _get_sync_range()
	frappe.db.sql(
		"""
        UPDATE `tabMOP Log`
        SET is_synced = 1
        WHERE manufacturing_work_order IN %(mwos)s
          AND is_synced = 0
          AND is_cancelled = 0
          AND creation >= %(window_start)s
          AND creation <= %(window_end)s
        """,
		{
			"mwos": manufacturing_work_orders,
			"window_start": window_start,
			"window_end": window_end,
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
	"""Stamp ``last_eod_sync_on`` on every Manufacturing Operation in the group.

	``last_eod_sync_on`` is a custom field (``custom_fields/manufacturing_operation.json``)
	and is absent on sites that have not had it provisioned. This runs INSIDE the Phase-2
	savepoint, so a 1054 here used to roll back the entire bucket -- the transfer was
	sound and only its audit timestamp was unwritable. Skip the stamp instead.
	"""
	if "last_eod_sync_on" not in _existing_columns("Manufacturing Operation"):
		frappe.logger().warning(
			"MOP EOD Sync: Manufacturing Operation.last_eod_sync_on is not provisioned "
			"on this site; skipping the sync timestamp."
		)
		return
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
	eod_sync_source="MOP EOD Sync",
):
	"""Create and SAVE (draft only) one Material Transfer to Department Stock Entry.

	For the consolidated run, ``mwo`` / ``manufacturing_order`` / ``header_mop_name`` /
	``header_manufacturer`` are left ``None`` — each item row already carries
	``custom_manufacturing_work_order`` and ``manufacturing_operation``, and the
	reservation / MOP-log hooks do not run for this stock entry type, so no header MWO
	is required. ``eod_sync_source`` tags the draft (e.g. the unresolved "issues" SE).
	"""
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
	se.custom_eod_sync_source = eod_sync_source
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


def _gathered_log_names(mop_data_list):
	"""MOP Log row names this run actually planned from.

	Used to pin ``_mark_all_mwo_mop_logs_synced`` in selective mode so it can only bury
	logs the transfer really covered.
	"""
	return sorted(
		{
			log.get("name")
			for md in mop_data_list or []
			for log in (md.get("logs") or [])
			if log.get("name")
		}
	)


def _collect_mop_names(mop_data_list):
	if not mop_data_list:
		return ""
	names = sorted({md.get("mop_name") for md in mop_data_list if md.get("mop_name")})
	return ", ".join(names)


# ---------------------------------------------------------------------------
# Sync Log helpers
# ---------------------------------------------------------------------------


def _select_options(fieldname):
	"""Cached valid Select options for a MOP EOD Sync Log Item field."""
	cache = _SELECT_OPTION_CACHE.get(fieldname)
	if cache is None:
		field = frappe.get_meta("MOP EOD Sync Log Item").get_field(fieldname)
		cache = {
			o.strip() for o in ((field.options or "") if field else "").split("\n")
		}
		_SELECT_OPTION_CACHE[fieldname] = cache
	return cache


def _coerce_select(row_dict, fieldname, fallback):
	"""Return a value guaranteed valid for ``fieldname``'s Select options.

	A Select value the doctype does not know makes ``doc.insert()`` raise, and because
	these diagnostic rows are written from inside the Phase-2 savepoint (see the healer
	at :func:`_reserve_batch_at_physical_warehouse`), that throw used to roll back the
	whole bucket -- the audit trail destroying the work it was meant to record. Coerce
	instead, and keep the rejected value in the caller's dict so it is not lost.
	"""
	value = (row_dict.get(fieldname) or "").strip()
	if value in _select_options(fieldname):
		return value
	if value:
		row_dict["error_message"] = (
			f"[{fieldname}={value!r} not a valid option] {row_dict.get('error_message') or ''}"
		).strip()
		frappe.logger().warning(
			"MOP EOD Sync: invalid %s value %r coerced to %r",
			fieldname,
			value,
			fallback,
		)
	return fallback


def _insert_sync_log_item(sync_log_name, row_dict):
	"""Insert a child row in MOP EOD Sync Log Item. Returns the new row name.

	Diagnostics must never be able to fail a sync: every Select value is coerced to a
	valid option first, and the insert itself runs in its own savepoint so a failure
	rolls back only this row, never the caller's bucket.
	"""
	if not sync_log_name:
		return None
	try:
		return _do_insert_sync_log_item(sync_log_name, dict(row_dict))
	except Exception:
		# A diagnostics row is never worth failing a sync for. The caller is typically
		# mid-savepoint (see the Phase-2 healer), so anything escaping here would roll
		# back real work. Roll back just this row and keep going; the swallowed count is
		# reported in the consolidated Error Log.
		_rollback_to_savepoint(_LOG_ROW_SAVEPOINT)
		frappe.local.eod_sync_log_row_failures = (
			getattr(frappe.local, "eod_sync_log_row_failures", 0) + 1
		)
		frappe.logger().exception(
			"MOP EOD Sync: could not write sync log row for %s", sync_log_name
		)
		return None


def _do_insert_sync_log_item(sync_log_name, row_dict):
	"""Build + insert the child row. Always call via :func:`_insert_sync_log_item`."""
	status = _coerce_select(row_dict, "status", "Pending")
	sync_stage = _coerce_select(row_dict, "sync_stage", "")
	error_type = _coerce_select(row_dict, "error_type", "Unknown Error")
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
			"status": status,
			"sync_stage": sync_stage,
			"is_synced": cint(row_dict.get("is_synced")),
			"stock_reservation_entry": row_dict.get("stock_reservation_entry") or "",
			"stock_entry": row_dict.get("stock_entry") or "",
			"draft_stock_entry": row_dict.get("draft_stock_entry") or "",
			"mop_log": row_dict.get("mop_log") or "",
			"error_type": error_type,
			"error_message": (row_dict.get("error_message") or "")[:500],
			"technical_traceback": row_dict.get("technical_traceback") or "",
			"suggested_fix": row_dict.get("suggested_fix") or "",
			"created_on": now_datetime(),
			"completed_on": row_dict.get("completed_on") or None,
		}
	)
	doc.flags.ignore_permissions = True
	frappe.db.savepoint(_LOG_ROW_SAVEPOINT)
	doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
	frappe.db.release_savepoint(_LOG_ROW_SAVEPOINT)
	return doc.name


# Child-row status -> parent counter family. "Draft Created" is deliberately NOT in the
# "failed" family: the Stock Entry exists and is recoverable by re-running or submitting
# it, whereas a "Failed" row produced nothing. Lumping them together is what made a run
# report failed_items=1349 when only 277 rows had actually failed.
_STATUS_BUCKET = {
	"Synced": "synced",
	"Draft Created": "draft",
	"Submitted": "synced",
	"Failed": "failed",
	"Excluded": "excluded",
	"Skipped": "skipped",
	"Deferred": "skipped",
	"Pending": "unsynced",
	"Unsynced": "unsynced",
}

_COUNTER_FAMILIES = ("synced", "draft", "failed", "unsynced", "skipped", "excluded")


def recalculate_sync_log_totals(sync_log_name):
	"""SQL aggregation over child rows to update parent totals and progress_percent.

	MWO counters are derived from the SAME query as the item counters, so the header can
	never contradict its own child rows (runs used to report synced_mwos=19 alongside
	synced_items=0 because the two were written by different code paths).
	"""
	if not sync_log_name:
		return

	rows = frappe.db.sql(
		"""
        SELECT
            status,
            COUNT(name) AS item_count,
            COUNT(DISTINCT manufacturing_work_order) AS mwo_count,
            COALESCE(SUM(qty), 0) AS total_qty
        FROM `tabMOP EOD Sync Log Item`
        WHERE parent = %s
        GROUP BY status
        """,
		(sync_log_name,),
		as_dict=True,
	)

	totals = {"total_items": 0, "total_qty": 0.0}
	for family in _COUNTER_FAMILIES:
		totals[f"{family}_items"] = 0
		totals[f"{family}_qty"] = 0.0
		totals[f"{family}_mwos"] = 0

	unhandled = []
	for row in rows:
		status = row.status or ""
		cnt = cint(row.item_count)
		qty = flt(row.total_qty, 3)
		totals["total_items"] += cnt
		totals["total_qty"] += qty

		family = _STATUS_BUCKET.get(status)
		if not family:
			# A status with no family would inflate total_items and no sub-counter, so
			# the header would silently stop adding up. Count it as unsynced (the
			# conservative reading: not known to have moved stock) and say so.
			unhandled.append(status)
			family = "unsynced"
		totals[f"{family}_items"] += cnt
		totals[f"{family}_qty"] += qty
		totals[f"{family}_mwos"] += cint(row.mwo_count)

	if unhandled:
		frappe.logger().warning(
			"MOP EOD Sync %s: child rows carry unmapped status(es) %s -- counted as "
			"unsynced. Add them to _STATUS_BUCKET.",
			sync_log_name,
			sorted(set(unhandled)),
		)

	eligible_qty = flt(totals["total_qty"] - totals["excluded_qty"], 3)
	progress_pct = (
		flt((totals["synced_qty"] / eligible_qty) * 100, 1) if eligible_qty > 0 else 0.0
	)

	values = {
		"total_items": totals["total_items"],
		"total_qty": flt(totals["total_qty"], 3),
		"eligible_qty": eligible_qty,
		"progress_percent": progress_pct,
	}
	for family in _COUNTER_FAMILIES:
		values[f"{family}_items"] = totals[f"{family}_items"]
		values[f"{family}_qty"] = flt(totals[f"{family}_qty"], 3)
	values.update(_sync_log_mwo_counters(sync_log_name))
	totals.update(values)

	frappe.db.set_value(
		"MOP EOD Sync Log",
		sync_log_name,
		_writable_values("MOP EOD Sync Log", values),
		update_modified=False,
	)
	return totals


# Worst outcome wins when one MWO has rows in several statuses: an MWO with even one
# Failed row is a failed MWO, however many of its rows synced. Lower index = worse.
_MWO_OUTCOME_ORDER = ("failed", "draft", "skipped", "unsynced", "synced", "excluded")


def _sync_log_mwo_counters(sync_log_name):
	"""MWO-level counters derived from the same child rows as the item counters.

	One MWO can own rows in several statuses, so counting per status would double-count
	it. Each MWO is assigned its single worst outcome instead.
	"""
	rows = frappe.db.sql(
		"""
        SELECT manufacturing_work_order AS mwo, status
        FROM `tabMOP EOD Sync Log Item`
        WHERE parent = %s AND IFNULL(manufacturing_work_order, '') != ''
        GROUP BY manufacturing_work_order, status
        """,
		(sync_log_name,),
		as_dict=True,
	)

	rank = {name: i for i, name in enumerate(_MWO_OUTCOME_ORDER)}
	worst = {}
	for row in rows:
		family = _STATUS_BUCKET.get(row.status or "", "unsynced")
		if rank.get(family, len(rank)) < rank.get(worst.get(row.mwo), len(rank)):
			worst[row.mwo] = family

	counts = {f: 0 for f in _MWO_OUTCOME_ORDER}
	for family in worst.values():
		counts[family] += 1

	# Only three MWO counters exist on the parent; "draft" MWOs are not failures but
	# they did not sync either, so they are reported alongside skipped as not-synced.
	return {
		"synced_mwos": counts["synced"],
		"failed_mwos": counts["failed"],
		"skipped_mwos": counts["draft"] + counts["skipped"] + counts["unsynced"],
		"processed_mwos": len(worst),
	}


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


def _check_eod_source_batch_stock(items_to_transfer):
	"""Non-throwing batch-stock check used by the plan phase.

	Returns ``{(s_warehouse, item_code, batch_no): (required_qty, physical_qty)}`` for
	every aggregated row whose required transfer qty exceeds physical batch balance
	(SLE / serial-batch ledger; reservations do NOT create stock). An empty dict means
	all batch rows have enough physical stock.
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

	short = {}
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
			short[(wh, item_code, batch_no)] = (req_qty, physical)
	return short


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
