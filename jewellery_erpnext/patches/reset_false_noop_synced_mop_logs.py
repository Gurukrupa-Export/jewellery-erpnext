"""One-off remediation for the false "already at target" no-op in MOP EOD Sync.

Before the ``_pick_eod_source_warehouse`` step-0 guard, EOD decided a transfer was
already done whenever the TARGET department warehouse physically held enough of the
batch. Metal batches here are shared pools worked by hundreds of MWOs at once, so that
check kept matching on other work orders' stock: the run created no Stock Entry, moved
nothing, relocated no reservation -- and still marked the MWO's MOP Logs ``is_synced=1``
via the ``if not items:`` branch of ``_plan_mwo_group``.

The damage is permanent without this script, because a synced log is never re-gathered.
The virtual ledger (MOP Log) says the metal reached the department; the reservation and
the physical stock are still at the previous operation's warehouse. It surfaces later as
Employee IR Process Loss failing with "loss qty ... cannot be covered by any single Stock
Reservation Entry", or as ``NegativeStockError`` when Serial Number Creator consumes the
balance.

This script resets ``is_synced = 0`` on the buried logs of exactly those MWOs so a future
EOD run re-picks them. With the picker fixed, the re-run now builds the real transfer
instead of repeating the no-op.

**Scope.** Only MWOs whose sync log carries the no-op marker AND that still hold a live
reservation at a warehouse OTHER than the target recorded for that no-op -- i.e. the ones
where the reservation demonstrably never followed the metal. A no-op that was genuine
(stock really had moved, no live reservation left behind) is untouched.

It is NOT registered in patches.txt; run it manually. Dry-run first:

    bench --site gk.localhost execute \
        jewellery_erpnext.patches.reset_false_noop_synced_mop_logs.execute

Apply:

    bench --site gk.localhost execute \
        jewellery_erpnext.patches.reset_false_noop_synced_mop_logs.execute \
        --kwargs "{'dry_run': False}"

Narrow to one work order (or one sync log) while investigating:

    --kwargs "{'mwo': 'MWO-KGJPL-EA00941-001-2-91.75-Y-01', 'dry_run': False}"
    --kwargs "{'sync_log': 'MOP-EOD-SYNC-2026-00068', 'dry_run': False}"

Idempotent: a repaired MWO whose logs are already unsynced reports zero and is skipped, so
it is safe to re-run.
"""

import frappe
from frappe.utils import flt

# The marker `_plan_mwo_group` writes on the no-op branch. Matched as a prefix because the
# line now carries the item/batch/qty detail after it.
NOOP_MARKER = "Stock already at the target warehouse%"


def _noop_targets(mwo=None, sync_log=None):
	"""``[(mwo, target_warehouse)]`` for every recorded no-op, newest first."""
	conditions = ["li.error_message LIKE %(marker)s", "li.docstatus < 2"]
	params = {"marker": NOOP_MARKER}
	if mwo:
		conditions.append("li.manufacturing_work_order = %(mwo)s")
		params["mwo"] = mwo
	if sync_log:
		conditions.append("li.parent = %(sync_log)s")
		params["sync_log"] = sync_log

	return frappe.db.sql(
		"""
		SELECT DISTINCT
			li.manufacturing_work_order AS mwo,
			li.target_warehouse AS target_warehouse
		FROM `tabMOP EOD Sync Log Item` li
		WHERE {conditions}
		  AND IFNULL(li.manufacturing_work_order, '') != ''
		  AND IFNULL(li.target_warehouse, '') != ''
		""".format(conditions=" AND ".join(conditions)),
		params,
		as_dict=True,
	)


def _current_warehouse(mwo):
	"""``to_warehouse`` of the MWO's newest non-cancelled MOP Log.

	Where the virtual ledger says the metal is NOW. A reservation sitting here has
	followed the metal even if it is not at the department warehouse some older run
	recorded as its no-op target -- an Employee IR issue legitimately moves stock on to
	the operator's WIP warehouse. Without this the repair would flag every such MWO.
	"""
	row = frappe.db.sql(
		"""
		SELECT to_warehouse
		FROM `tabMOP Log`
		WHERE manufacturing_work_order = %s
		  AND is_cancelled = 0
		  AND IFNULL(to_warehouse, '') != ''
		ORDER BY creation DESC
		LIMIT 1
		""",
		(mwo,),
	)
	return row[0][0] if row else None


def _stranded_reservations(mwo, exclude_warehouses):
	"""Live reservations this MWO holds away from where its metal should be.

	"Live" matches ``_preload_active_sre_warehouse_map``: submitted, not Delivered or
	Cancelled, with undelivered batch qty outstanding. A row here is the proof that the
	no-op was false -- the reservation is still sitting where the metal was before the
	operation the MOP Log has already advanced to.

	``exclude_warehouses`` holds the target the no-op named plus the MWO's current
	``to_warehouse``; a reservation at either has followed the metal and is not stranded.
	"""
	warehouses = sorted({wh for wh in exclude_warehouses if wh}) or [""]
	return frappe.db.sql(
		"""
		SELECT sre.name, sre.item_code, sbe.batch_no, sre.warehouse,
		       (sbe.qty - IFNULL(sbe.delivered_qty, 0)) AS remaining
		FROM `tabStock Reservation Entry` sre
		INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
		WHERE sre.manufacturing_work_order = %(mwo)s
		  AND sre.docstatus = 1
		  AND sre.status NOT IN ('Delivered', 'Cancelled')
		  AND sre.reservation_based_on = 'Serial and Batch'
		  AND sre.warehouse NOT IN %(warehouses)s
		  AND (sbe.qty - IFNULL(sbe.delivered_qty, 0)) > 0
		ORDER BY sre.warehouse, sre.item_code
		""",
		{"mwo": mwo, "warehouses": warehouses},
		as_dict=True,
	)


def _buried_log_count(mwo):
	"""Non-cancelled MOP Logs of ``mwo`` currently marked synced."""
	return frappe.db.sql(
		"""
		SELECT COUNT(*)
		FROM `tabMOP Log`
		WHERE manufacturing_work_order = %s
		  AND is_synced = 1
		  AND is_cancelled = 0
		""",
		(mwo,),
	)[0][0]


def execute(mwo=None, sync_log=None, dry_run=True, limit=None):
	targets = _noop_targets(mwo=mwo, sync_log=sync_log)
	if not targets:
		print("[reset-false-noop] No no-op sync log lines matched; nothing to do.")
		return

	# One MWO can carry several no-ops across runs; repair it once, against every target
	# warehouse it was ever declared complete at.
	by_mwo = {}
	for row in targets:
		by_mwo.setdefault(row.mwo, set()).add(row.target_warehouse)

	repairable = []
	for mwo_name in sorted(by_mwo):
		buried = _buried_log_count(mwo_name)
		if not buried:
			continue
		# Every warehouse the metal is legitimately allowed to be reserved at: the
		# no-op targets this MWO was ever declared complete at, plus where its MOP Log
		# says the metal is now.
		settled = set(by_mwo[mwo_name])
		settled.add(_current_warehouse(mwo_name))
		stranded = _stranded_reservations(mwo_name, settled)
		if not stranded:
			# Genuine no-op, or already repaired: nothing is reserved away from where
			# the metal should be.
			continue
		repairable.append((mwo_name, buried, stranded))
		if limit and len(repairable) >= int(limit):
			break

	if not repairable:
		print(
			"[reset-false-noop] {0} MWO(s) carry a no-op line, but none still hold a "
			"stranded reservation. Nothing to repair.".format(len(by_mwo))
		)
		return

	total_logs = sum(buried for _m, buried, _s in repairable)
	print(
		"[reset-false-noop] {0} MWO(s) with a false no-op, {1} buried MOP Log(s) to "
		"reset.".format(len(repairable), total_logs)
	)
	for mwo_name, buried, stranded in repairable:
		print(f"  {mwo_name}: {buried} log(s) buried")
		for row in stranded:
			print(
				"      stranded {0} {1} / {2} = {3} @ {4}".format(
					row.name,
					row.item_code,
					row.batch_no,
					flt(row.remaining, 3),
					row.warehouse,
				)
			)

	if dry_run:
		print(
			"[reset-false-noop] DRY RUN — no changes written. Re-run with "
			"dry_run=False to apply."
		)
		return

	names = [mwo_name for mwo_name, _buried, _stranded in repairable]
	frappe.db.sql(
		"""
		UPDATE `tabMOP Log`
		SET is_synced = 0
		WHERE manufacturing_work_order IN %(mwos)s
		  AND is_synced = 1
		  AND is_cancelled = 0
		""",
		{"mwos": names},
	)
	frappe.db.commit()
	print(
		"[reset-false-noop] Reset is_synced=0 on {0} MOP Log(s) across {1} MWO(s). "
		"The next EOD run will re-pick them.".format(total_logs, len(names))
	)
