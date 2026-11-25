"""One-off recovery for work orders that EOD marked synced with a false "already at target".

Until the fix in ``mop_eod_sync._current_balance_logs``, EOD sized each transfer from the
UNSYNCED MOP Logs only. Every Stock Entry writes its MOP Logs already synced, so metal that a
Material Transfer (WORK ORDER), a Material Receive (WORK ORDER) return or a Customer Gold
Settle swap added or took away after a Department / Employee IR was invisible to EOD. When the
work order's own stock at the target then covered the stale quantity, EOD logged "Stock
already at the target warehouse; no Material Transfer needed" and marked the IR logs synced,
although part of the job's metal was still in the previous warehouse
(MWO-KGJPL-RI05630-004-3-91.75-Y-01: 2.0 g left in Waxing WO by MOP-EOD-SYNC-2026-00133).

Such a work order has no unsynced logs left, so no EOD run plans it again until the job's next
Department / Employee IR. This script puts back exactly the IR rows that the no-op buried, so
that a selective EOD re-plans the work order with the fixed planner. It never runs EOD and
never moves stock itself.

NOT registered in patches.txt; run it manually. Dry-run first (prints, per work order, the
rows it would reset and the transfer EOD would then plan):

    bench --site <site> execute \\
        jewellery_erpnext.patches.recover_eod_balance_noops.execute

Apply (only work orders whose dry-run plan is a clean transfer are touched):

    bench --site <site> execute \\
        jewellery_erpnext.patches.recover_eod_balance_noops.execute \\
        --kwargs "{'dry_run': False}"

Then add each applied work order to MOP Settings > EOD Sync Work Order Filter (enabled) and run
EOD Sync; remove the filter rows afterwards. Other work orders in the same state can be listed,
read-only, with:

    bench --site <site> execute \\
        jewellery_erpnext.patches.recover_eod_balance_noops.find_candidates
"""

import frappe
from frappe.utils import cstr, flt

DEFAULT_MWOS = (
	"MWO-KGJPL-RI05630-004-3-91.75-Y-01",
	"MWO-KGJPL-RI00634-004-1-91.75-Y-01",
	"MWO-KGJPL-MU00135-001-5-91.75-Y-01",
)


def _eod():
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import mop_eod_sync

	return mop_eod_sync


def _last_eod_transfer(mwo, operation):
	"""Creation of the newest submitted EOD transfer that moved rows for this MWO's operation."""
	row = frappe.db.sql(
		"""
		SELECT MAX(se.creation)
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1
		  AND se.custom_is_eod_sync_stock_entry = 1
		  AND sed.custom_manufacturing_work_order = %s
		  AND sed.manufacturing_operation = %s
		""",
		(mwo, operation),
	)
	return row[0][0] if row and row[0] else None


def _worker_warehouses(warehouses):
	"""The warehouses in ``warehouses`` that belong to a worker (karigar) or subcontractor."""
	if not warehouses:
		return set()
	return set(
		frappe.get_all(
			"Warehouse",
			filters={"name": ["in", list(warehouses)], "employee": ["is", "set"]},
			pluck="name",
		)
	)


def _inspect(mwo):
	"""What a recovery would reset for ``mwo``, and the transfer EOD would then plan."""
	eod = _eod()
	info = {"mwo": mwo, "ready": False, "reset": [], "rows": [], "notes": []}
	wo = frappe.db.get_value(
		"Manufacturing Work Order",
		mwo,
		["docstatus", "manufacturing_operation"],
		as_dict=True,
	)
	if not wo or wo.docstatus != 1 or not wo.manufacturing_operation:
		info["notes"].append("not a submitted work order with a current operation")
		return info
	operation = info["operation"] = wo.manufacturing_operation

	logs = frappe.get_all(
		"MOP Log",
		filters={"manufacturing_operation": operation, "is_cancelled": 0},
		fields=list(eod._MOP_LOG_GATHER_FIELDS) + ["is_synced", "qty_change"],
		order_by="creation asc, name asc",
	)
	if any(not log.is_synced for log in logs):
		info["notes"].append(
			"the operation still has unsynced MOP Logs: the next EOD run plans it already"
		)
		return info

	last_transfer = _last_eod_transfer(mwo, operation)
	reset = [
		log
		for log in logs
		if log.voucher_type in eod._CLONE_VOUCHER_TYPES
		and not flt(log.qty_change)
		and (not last_transfer or log.creation > last_transfer)
	]
	if not reset:
		info["notes"].append(
			"no balance-copy rows after the last EOD transfer: nothing was buried"
		)
		return info
	info["reset"] = [log.name for log in reset]

	# Exactly what _plan_mwo_group does once those rows are unsynced again.
	last_logs = eod._get_last_logs_per_item_batch(reset)
	target = info["target"] = eod._get_t_warehouse_from_logs(last_logs)
	balance = eod._current_balance_logs(mwo, operation, reset, last_logs)
	noop_rows, short_rows = [], []
	rows, skipped = eod._build_eod_se_rows(
		mwo,
		operation,
		balance,
		target,
		eod._preload_sre_warehouse_map(mwo),
		eod._preload_active_sre_warehouse_map(mwo),
		noop_rows,
		short_rows,
	)
	info["rows"] = [
		(
			r["item_code"],
			r.get("batch_no"),
			r["s_warehouse"],
			r["t_warehouse"],
			r["qty"],
		)
		for r in rows
	]
	for row in short_rows:
		info["notes"].append(
			f"short: {row['item_code']} / {row['batch_no']} needs {row['need']}, "
			f"own reservations {row['reserved']}"
		)
	for row in skipped:
		info["notes"].append(
			f"no reservation warehouse: {row['item_code']} / {row['batch_no']} {row['qty']}"
		)
	for warehouse in _worker_warehouses({r[2] for r in info["rows"]}):
		info["notes"].append(
			f"source {warehouse} is a worker's warehouse while the job is not issued to "
			"that worker; check the Stock Entry that put the metal there"
		)
	info["ready"] = bool(rows) and not short_rows and not skipped
	return info


def _print(info):
	print(
		f"\n{info['mwo']}  operation={info.get('operation')}  target={info.get('target')}"
	)
	print(f"  rows to reset ({len(info['reset'])}): {', '.join(info['reset']) or '-'}")
	for item_code, batch_no, source, target, qty in info["rows"]:
		print(
			f"  EOD would move {qty} of {item_code} / {batch_no}: {source} -> {target}"
		)
	for note in info["notes"]:
		print(f"  note: {note}")
	print(f"  ready: {'yes' if info['ready'] else 'no'}")


def execute(mwos=None, dry_run=True):
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
		is_eod_sync_locked,
	)

	eod = _eod()
	if not hasattr(eod, "_current_balance_logs"):
		frappe.throw(
			"This site's EOD code does not size transfers from the operation balance yet. "
			"Deploy the fix first: without it a re-run repeats the same no-op."
		)
	report = [_inspect(cstr(mwo).strip()) for mwo in (mwos or DEFAULT_MWOS)]
	for info in report:
		_print(info)

	if dry_run:
		print(
			"\nDRY RUN: nothing changed. Re-run with --kwargs \"{'dry_run': False}\"."
		)
		return report

	if is_eod_sync_locked():
		frappe.throw("EOD sync is running. Wait for it to finish, then run this again.")
	applied = [info for info in report if info["ready"]]
	for info in applied:
		frappe.db.sql(
			"""
			UPDATE `tabMOP Log` SET is_synced = 0
			WHERE name IN %(names)s AND is_synced = 1 AND is_cancelled = 0
			""",
			{"names": info["reset"]},
		)
	frappe.db.commit()
	skipped = [info["mwo"] for info in report if not info["ready"]]
	print(f"\nReset is_synced on the IR rows of {len(applied)} work order(s).")
	if skipped:
		print(f"Not touched (not ready): {', '.join(skipped)}")
	if applied:
		print(
			"Next: add "
			+ ", ".join(info["mwo"] for info in applied)
			+ " to MOP Settings > EOD Sync Work Order Filter (enabled) and run EOD Sync."
		)
	return report


def find_candidates(limit=500):
	"""Read-only: work orders that look like a buried false no-op.

	Current operation not Finished and fully synced; a synced Stock Entry on it after its
	newest IR row; and a live reservation of the work order away from that IR's target.
	"""
	eod = _eod()
	operations = frappe.db.sql(
		"""
		SELECT wo.name AS mwo, wo.manufacturing_operation AS operation
		FROM `tabManufacturing Work Order` wo
		INNER JOIN `tabManufacturing Operation` mo ON mo.name = wo.manufacturing_operation
		WHERE wo.docstatus = 1 AND IFNULL(mo.status, '') != 'Finished'
		""",
		as_dict=True,
	)
	by_operation = {row.operation: row.mwo for row in operations}
	names = list(by_operation)
	candidates = []
	for start in range(0, len(names), 500):
		chunk = names[start : start + 500]
		logs = frappe.get_all(
			"MOP Log",
			filters={"manufacturing_operation": ["in", chunk], "is_cancelled": 0},
			fields=[
				"manufacturing_operation",
				"voucher_type",
				"qty_change",
				"is_synced",
				"to_warehouse",
				"creation",
			],
			order_by="creation asc, name asc",
		)
		grouped = {}
		for log in logs:
			grouped.setdefault(log.manufacturing_operation, []).append(log)
		for operation, rows in grouped.items():
			if any(not row.is_synced for row in rows):
				continue
			irs = [
				row
				for row in rows
				if row.voucher_type in eod._CLONE_VOUCHER_TYPES
				and not flt(row.qty_change)
			]
			if not irs:
				continue
			newest_ir = irs[-1]
			if not any(
				row.voucher_type == "Stock Entry" and row.creation > newest_ir.creation
				for row in rows
			):
				continue
			mwo = by_operation[operation]
			away = {
				key: {
					wh: qty for wh, qty in held.items() if wh != newest_ir.to_warehouse
				}
				for key, held in eod._preload_active_sre_warehouse_map(mwo).items()
			}
			away = {key: held for key, held in away.items() if held}
			if away:
				candidates.append(
					{
						"mwo": mwo,
						"operation": operation,
						"target": newest_ir.to_warehouse,
						"held_elsewhere": away,
					}
				)
			if len(candidates) >= limit:
				break
		if len(candidates) >= limit:
			break
	for row in candidates:
		print(row)
	print(f"{len(candidates)} candidate(s). Dry-run them with execute(mwos=[...]).")
	return candidates
