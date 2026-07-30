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


def _classify(sync_log_name=None):
	"""Return (retryable, skipped): ``{mwo: [draft_se, ...]}``.

	``sync_log_name=None`` sweeps EVERY sync log instead of one, which is what the
	backlog needs -- 52 stuck drafts accumulated across many nights.

	Three preconditions decide "retryable", each protecting against a double-transfer:

	1. **Source.** Only drafts tagged ``MOP EOD Sync`` are retried. Drafts tagged
	   ``MOP EOD Sync (Unresolved)`` are the best-effort "issues" SEs holding buildable
	   rows of MWOs that were held back; no child row points at them and a future run
	   rebuilds that work, so re-driving them would post the same stock twice. On the
	   live site the 52 drafts split 25 / 27 between the two, so a scope of
	   ``custom_is_eod_sync_stock_entry = 1 AND docstatus = 0`` silently doubles.
	2. **Not already submitted.** A draft submitted by hand moved stock without the SRE
	   relocation; re-running would build a second transfer. Manual reconciliation only.
	3. **Still unsynced.** An MWO whose MOP Logs are already ``is_synced = 1`` has been
	   accounted for; re-planning it would transfer the same balance again.
	"""
	filters = {"status": "Draft Created"}
	if sync_log_name:
		filters["parent"] = sync_log_name
	rows = frappe.get_all(
		"MOP EOD Sync Log Item",
		filters=filters,
		fields=["manufacturing_work_order", "draft_stock_entry"],
		limit_page_length=0,
	)
	mwo_drafts = {}
	for r in rows:
		if r.manufacturing_work_order and r.draft_stock_entry:
			mwo_drafts.setdefault(r.manufacturing_work_order, set()).add(
				r.draft_stock_entry
			)

	retryable, skipped = {}, {}
	for mwo, drafts in mwo_drafts.items():
		states = []
		for se in drafts:
			row = (
				frappe.db.get_value(
					"Stock Entry",
					se,
					["docstatus", "custom_eod_sync_source"],
					as_dict=True,
				)
				or {}
			)
			if not row:
				continue  # draft already deleted by an earlier recovery
			states.append((se, row.get("docstatus"), row.get("custom_eod_sync_source")))

		if not states:
			continue
		# (1) only the real consolidated transfer drafts.
		main_drafts = [
			se for se, ds, src in states if ds == 0 and src == "MOP EOD Sync"
		]
		# (2) submitted by hand -> that stock already moved, so retrying would double it.
		#
		# EOD now writes one Stock Entry per CHUNK, so a single MWO can own several drafts.
		# The old blanket `any(ds == 1)` quarantined the whole MWO the moment an operator
		# submitted any one of them, stranding its still-retryable siblings. Quarantine only
		# when a submitted SE exists AND no clean draft is left to retry: a mixed MWO is
		# genuinely ambiguous, but "one submitted, none pending" is simply done.
		submitted = [se for se, ds, _ in states if ds == 1]
		if submitted and not main_drafts:
			skipped[mwo] = submitted
			continue
		if submitted and main_drafts:
			# Partially submitted across chunks -- a human has to decide. Report loudly
			# rather than guessing which half already moved stock.
			skipped[mwo] = submitted + main_drafts
			print(
				f"[retry-stuck-eod] {mwo}: {len(submitted)} submitted and "
				f"{len(main_drafts)} draft EOD Stock Entry(s) — partially committed across "
				"chunks. Skipped: reconcile by hand before retrying."
			)
			continue
		if not main_drafts:
			continue
		# (3) nothing left to sync means it was already accounted for.
		if not frappe.db.exists(
			"MOP Log",
			{"manufacturing_work_order": mwo, "is_synced": 0, "is_cancelled": 0},
		):
			skipped[mwo] = main_drafts
			continue
		retryable[mwo] = main_drafts
	return retryable, skipped


def execute(sync_log_name=None, dry_run=True):
	"""``sync_log_name=None`` (the default) sweeps every sync log."""
	if sync_log_name and not frappe.db.exists("MOP EOD Sync Log", sync_log_name):
		print(f"[retry-stuck-eod] Sync log {sync_log_name} not found; nothing to do.")
		return

	retryable, skipped = _classify(sync_log_name)

	print(f"[retry-stuck-eod] Sync log {sync_log_name or '(ALL LOGS)'}")
	print(f"  Retryable MWOs (will delete orphan draft + re-run): {len(retryable)}")
	for mwo, drafts in retryable.items():
		print(f"    {mwo}: drafts={drafts or '(none)'}")
	print(
		f"  Skipped MWOs (manually submitted, or already fully synced): {len(skipped)}"
	)
	for mwo, drafts in skipped.items():
		print(f"    {mwo}: {drafts}")

	# Ownership pre-flight. EOD transfer rows used to carry no inventory_type, so the
	# blanket "default blank to Regular Stock" booked customer-owned metal as company
	# stock. Re-driving these MWOs must not repeat that, so refuse to run until the
	# builder fix is in place.
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
		_build_eod_se_rows,
	)

	if "_stamp_eod_row_ownership" not in _build_eod_se_rows.__code__.co_names:
		print(
			"[retry-stuck-eod] ABORT: _build_eod_se_rows does not stamp row ownership. "
			"Re-driving these MWOs would book Customer Goods batches as Regular Stock. "
			"Apply the ownership fix first."
		)
		return

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
			company,
			manufacturer,
			main_mwos,
			failures,
			stats,
			sync_log_name=None,
			selective=True,
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
		unsynced = sum(1 for lo in logs if not lo.is_synced)
		print(f"    {mwo}: logs={total} still_unsynced={unsynced}")


def verify(sync_log_name=DEFAULT_SYNC_LOG):
	"""Read-only post-retry report: for each formerly-retryable MWO show its new
	submitted transfer SE(s) and active SRE count at the target.

	``custom_manufacturing_work_order`` lives on **Stock Entry Detail**, not on the Stock
	Entry parent -- and the consolidated EOD Stock Entry deliberately leaves its header MWO
	blank. Filtering the parent by it therefore returned nothing (or an Unknown column),
	which made this report silently useless. Join through the child table instead, and
	expect several SEs per MWO now that EOD writes one per chunk.
	"""
	retryable, _ = _classify(sync_log_name)
	for mwo in retryable:
		ses = frappe.db.sql(
			"""
			SELECT DISTINCT se.name
			FROM `tabStock Entry` se
			INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
			WHERE sed.custom_manufacturing_work_order = %s
			  AND se.stock_entry_type = 'Material Transfer to Department'
			  AND se.docstatus = 1
			ORDER BY se.name
			""",
			(mwo,),
			as_dict=True,
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
