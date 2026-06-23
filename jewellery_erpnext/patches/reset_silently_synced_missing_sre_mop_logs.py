"""One-off remediation for the silent-sync bug in MOP EOD Sync.

A partially-completed EOD run (e.g. ``MOP-EOD-SYNC-2026-03606``) marked MOP Logs
``is_synced=1`` for MWOs whose every item had a *Missing SRE* — even though no Stock
Entry was created and no stock moved. Those logs are now buried (``is_synced=1``
excludes them from future runs) so they can never be re-picked.

This script resets ``is_synced=0`` on exactly those logs so a future EOD run re-picks
them — but only AFTER the underlying Stock Reservation Entries are created/fixed,
otherwise they will fail the same way (now visibly, not silently).

It is NOT registered in patches.txt; run it manually. Dry-run first:

    bench --site gk execute \
        jewellery_erpnext.patches.reset_silently_synced_missing_sre_mop_logs.execute

Apply:

    bench --site gk execute \
        jewellery_erpnext.patches.reset_silently_synced_missing_sre_mop_logs.execute \
        --kwargs "{'dry_run': False}"

Idempotent and scoped strictly to the sync log + its posting-date day window (matching
``_mark_all_mwo_mop_logs_synced``'s window), so it is safe to re-run.
"""

import frappe
from frappe.utils import add_days, getdate

DEFAULT_SYNC_LOG = "MOP-EOD-SYNC-2026-03606"


def execute(sync_log_name=DEFAULT_SYNC_LOG, dry_run=True):
	if not frappe.db.exists("MOP EOD Sync Log", sync_log_name):
		print(f"[reset-missing-sre] Sync log {sync_log_name} not found; nothing to do.")
		return

	posting_date = frappe.db.get_value(
		"MOP EOD Sync Log", sync_log_name, "posting_date"
	)
	day_start = getdate(posting_date)
	# Same window semantics as _mark_all_mwo_mop_logs_synced: [start 00:00, next 00:00)
	today_start = f"{day_start} 00:00:00"
	tomorrow_start = f"{add_days(day_start, 1)} 00:00:00"

	# MWOs whose items failed with Missing SRE in this run (every such MWO was wrongly
	# marked synced because all its rows were skipped → no transfer happened).
	mwos = frappe.get_all(
		"MOP EOD Sync Log Item",
		filters={
			"parent": sync_log_name,
			"status": "Failed",
			"error_type": "Missing SRE",
		},
		pluck="manufacturing_work_order",
		distinct=True,
	)
	mwos = sorted({m for m in mwos if m})

	if not mwos:
		print("[reset-missing-sre] No Missing-SRE failures found; nothing to do.")
		return

	# Per-MWO counts of buried logs (is_synced=1) within the run's day window.
	rows = frappe.db.sql(
		"""
		SELECT manufacturing_work_order AS mwo, COUNT(*) AS n
		FROM `tabMOP Log`
		WHERE manufacturing_work_order IN %(mwos)s
		  AND is_synced = 1
		  AND is_cancelled = 0
		  AND creation >= %(today_start)s
		  AND creation < %(tomorrow_start)s
		GROUP BY manufacturing_work_order
		""",
		{"mwos": mwos, "today_start": today_start, "tomorrow_start": tomorrow_start},
		as_dict=True,
	)
	per_mwo = {r.mwo: r.n for r in rows}
	total = sum(per_mwo.values())

	print(
		f"[reset-missing-sre] Sync log {sync_log_name} (posting_date {day_start}): "
		f"{len(mwos)} Missing-SRE MWO(s), {total} buried MOP Log(s) to reset."
	)
	for mwo in mwos:
		print(f"  {mwo}: {per_mwo.get(mwo, 0)} log(s)")

	if dry_run:
		print(
			"[reset-missing-sre] DRY RUN — no changes written. Re-run with dry_run=False to apply."
		)
		return

	if not total:
		print("[reset-missing-sre] Nothing to reset (already clean).")
		return

	frappe.db.sql(
		"""
		UPDATE `tabMOP Log`
		SET is_synced = 0
		WHERE manufacturing_work_order IN %(mwos)s
		  AND is_synced = 1
		  AND is_cancelled = 0
		  AND creation >= %(today_start)s
		  AND creation < %(tomorrow_start)s
		""",
		{"mwos": mwos, "today_start": today_start, "tomorrow_start": tomorrow_start},
	)
	frappe.db.commit()
	print(f"[reset-missing-sre] Reset is_synced=0 on {total} MOP Log(s). Done.")
