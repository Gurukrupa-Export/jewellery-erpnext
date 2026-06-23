"""One-off retry for MOP EOD Sync MWOs left stuck after a Phase-2 submit failure.

A partially-completed EOD run (e.g. ``MOP-EOD-SYNC-2026-03606``) left some MWOs with a
draft Stock Entry because the EOD re-reservation failed (over-reserved SO line / batch not
free). That re-reservation bug is now fixed, so these MWOs can be re-processed.

Two groups (decided per MWO from the sync log's "Draft Created" child rows):
  * Retryable — the draft SE is still un-submitted (docstatus 0/2). Phase 2 rolled back, so
    the source SREs are intact and the MOP Logs are still ``is_synced=0``. We delete the
    orphan draft SE and re-run the MWO through the real (fixed) per-MWO sync path in
    selective mode (scheduled mode only handles *today's* logs; these are older).
  * Skipped — the draft SE was submitted manually after the run. Stock moved but the SRE
    relocation never ran and the logs are still unsynced: an inconsistent state that needs
    separate manual reconciliation. We do NOT auto-retry (EOD would build a 2nd transfer).

NOT registered in patches.txt; run it manually. Dry-run first:

    bench --site gk execute \
        jewellery_erpnext.patches.retry_stuck_eod_submit_mwos.execute

Apply:

    bench --site gk execute \
        jewellery_erpnext.patches.retry_stuck_eod_submit_mwos.execute \
        --kwargs "{'dry_run': False}"
"""

import frappe

DEFAULT_SYNC_LOG = "MOP-EOD-SYNC-2026-03606"


def _classify(sync_log_name):
	"""Return (retryable, skipped): {mwo: [draft_se,...]} from the sync log."""
	rows = frappe.get_all(
		"MOP EOD Sync Log Item",
		filters={"parent": sync_log_name, "status": "Draft Created"},
		fields=["manufacturing_work_order", "draft_stock_entry"],
		limit_page_length=0,
	)
	mwo_drafts = {}
	for r in rows:
		if r.manufacturing_work_order:
			mwo_drafts.setdefault(r.manufacturing_work_order, set()).add(
				r.draft_stock_entry or ""
			)

	retryable, skipped = {}, {}
	for mwo, drafts in mwo_drafts.items():
		states = []
		for se in drafts:
			if not se:
				continue
			ds = frappe.db.get_value("Stock Entry", se, "docstatus")
			states.append((se, ds))
		# Submitted (docstatus 1) draft anywhere → inconsistent, manual reconciliation.
		if any(ds == 1 for _, ds in states):
			skipped[mwo] = [se for se, ds in states if ds == 1]
		else:
			retryable[mwo] = [se for se, ds in states if ds == 0]
	return retryable, skipped


def execute(sync_log_name=DEFAULT_SYNC_LOG, dry_run=True):
	if not frappe.db.exists("MOP EOD Sync Log", sync_log_name):
		print(f"[retry-stuck-eod] Sync log {sync_log_name} not found; nothing to do.")
		return

	retryable, skipped = _classify(sync_log_name)

	print(f"[retry-stuck-eod] Sync log {sync_log_name}")
	print(f"  Retryable MWOs (will delete orphan draft + re-run): {len(retryable)}")
	for mwo, drafts in retryable.items():
		print(f"    {mwo}: drafts={drafts or '(none)'}")
	print(
		f"  Skipped MWOs (manually submitted — manual reconciliation needed): {len(skipped)}"
	)
	for mwo, drafts in skipped.items():
		print(f"    {mwo}: submitted={drafts}")

	if dry_run:
		print(
			"[retry-stuck-eod] DRY RUN — no changes written. Re-run with dry_run=False to apply."
		)
		return

	if not retryable:
		print("[retry-stuck-eod] No retryable MWOs; nothing to do.")
		return

	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
		_commit_company_issues_se,
		_commit_company_main_se,
		_get_unsynced_mop_groups,
		_plan_mwo_group,
	)

	# EOD-internal writes (SE submit, SRE relocation) must bypass the transaction lock.
	frappe.flags.in_eod_mop_sync = True

	# 1. Delete the orphan draft SEs so the re-run recreates cleanly.
	for mwo, drafts in retryable.items():
		for se in drafts:
			frappe.delete_doc("Stock Entry", se, force=1, ignore_permissions=True)
	frappe.db.commit()
	print(
		f"[retry-stuck-eod] Deleted {sum(len(d) for d in retryable.values())} orphan draft SE(s)."
	)

	# 2. Re-process the retryable MWOs through the real per-MWO path in selective mode
	#    (full unsynced history, ignores the today-only window).
	mwos = list(retryable.keys())
	settings = frappe._dict(
		eod_sync_work_order_filter=[
			frappe._dict(enabled=1, manufacturing_work_order=m) for m in mwos
		]
	)
	groups = _get_unsynced_mop_groups(settings=settings)
	failures = []
	stats = {
		"total_mwos": len(groups),
		"processed_mwos": 0,
		"failed_mwos": 0,
		"submitted_ses": [],
		"draft_ses": [],
		"artifact_skipped": [],
		"started_on": frappe.utils.now_datetime(),
	}
	main_buckets = {}
	issues_buckets = {}
	for group_key, mop_data_list in groups.items():
		result = _plan_mwo_group(
			group_key,
			mop_data_list,
			failures,
			stats,
			sync_log_name=None,
			selective=True,
		)
		if not result:
			continue
		bucket_key = (result["company"], result.get("manufacturer"))
		if result["kind"] == "resolvable":
			main_buckets.setdefault(bucket_key, []).append(result)
		elif result["kind"] == "failed" and result.get("issues_rows"):
			issues_buckets.setdefault(bucket_key, []).extend(result["issues_rows"])
	for (company, manufacturer), main_mwos in main_buckets.items():
		_commit_company_main_se(
			company, manufacturer, main_mwos, failures, stats, sync_log_name=None, selective=True
		)
	for (company, manufacturer), issues_rows in issues_buckets.items():
		_commit_company_issues_se(
			company, manufacturer, issues_rows, stats, sync_log_name=None
		)
	frappe.db.commit()

	print(
		f"[retry-stuck-eod] Re-run: processed(ok)={stats['processed_mwos']} "
		f"failed={stats['failed_mwos']} submitted_SEs={stats['submitted_ses']} "
		f"new_drafts={stats['draft_ses']}"
	)
	if failures:
		for f in failures:
			print(
				f"  FAILURE mwo={f.get('mwo')} step={f.get('step')} :: {f.get('error_message')}"
			)

	# 3. Verify each retried MWO is now fully synced.
	print("[retry-stuck-eod] Verification:")
	for mwo in mwos:
		logs = frappe.get_all(
			"MOP Log",
			filters={"manufacturing_work_order": mwo, "is_cancelled": 0},
			fields=["is_synced"],
			limit_page_length=0,
		)
		total = len(logs)
		unsynced = sum(1 for l in logs if not l.is_synced)
		print(f"    {mwo}: logs={total} still_unsynced={unsynced}")


def verify(sync_log_name=DEFAULT_SYNC_LOG):
	"""Read-only post-retry report: for each formerly-retryable MWO show its new
	submitted transfer SE (if any) and active SRE count at the target."""
	retryable, _ = _classify(sync_log_name)
	for mwo in retryable:
		ses = frappe.get_all(
			"Stock Entry",
			filters={
				"custom_manufacturing_work_order": mwo,
				"stock_entry_type": "Material Transfer to Department",
				"docstatus": 1,
			},
			fields=["name"],
			limit_page_length=0,
		)
		sres = frappe.get_all(
			"Stock Reservation Entry",
			filters={"manufacturing_work_order": mwo, "docstatus": 1},
			fields=["name"],
			limit_page_length=0,
		)
		unsynced = frappe.db.count(
			"MOP Log",
			{"manufacturing_work_order": mwo, "is_cancelled": 0, "is_synced": 0},
		)
		print(
			f"  {mwo}: submitted_transfer_SEs={[s.name for s in ses]} "
			f"active_SREs={len(sres)} unsynced_logs={unsynced}"
		)
