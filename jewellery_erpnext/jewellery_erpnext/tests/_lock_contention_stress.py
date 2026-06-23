"""Concurrency stress harness — prove the lock-contention remediation drives MariaDB
1205 (lock-wait timeout) and 1213 (deadlock) to ZERO under real parallelism, WITHOUT
leaning on retries.

Run from ``bench execute`` (needs a real DB + separate worker connections, so it is an
explicit entrypoint, not an auto-discovered unittest — hence the leading underscore, like
``_flow_and_perf_audit.py``)::

    bench --site gk execute \
        jewellery_erpnext.jewellery_erpnext.tests._lock_contention_stress.run_stress \
        --kwargs "{'workers': 8, 'per_worker': 5}"

What it does
------------
Spawns ``workers`` OS processes (spawn context, each with its OWN frappe connection) that
each submit ``per_worker`` Material Receipt Stock Entries of the SAME item into the SAME
warehouse. Every submit:

* takes the shared ``tabSeries[MAT-STE-...]`` counter lock (Root Cause 1, the dominant
  source of the production 1205/1213), and
* pre-locks the SAME ``tabBin`` row (item+warehouse) via the ``prelock_bins`` hook,

so the workers maximally contend on exactly the rows that produced the errors.

Why this proves the *fix*, not a retry mask
-------------------------------------------
A plain Material Receipt submit is NOT wrapped by ``bounded_retry`` — so any 1205/1213
surfaces immediately and is counted. If the canonical series/Bin pre-locking
(``preallocate_series_for_docs`` + ``lock_bins`` in ``prelock_bins``) and the SRE-hash
patch are working, concurrent workers serialise cleanly on the series row and the Bin and
NONE of them error. A non-zero deadlock/timeout count means the root cause is not yet
fixed (do NOT "solve" it by raising retries).

Safety
------
Material Receipt only ADDS stock (no negative-stock hazard). The harness records the Bin
quantity before, then cancels + deletes everything it created at the end (best-effort), so
net stock is unchanged. Use a dedicated/non-critical item+warehouse if you prefer; pass
``item_code`` / ``warehouse`` explicitly to override auto-discovery.

Pass criteria: zero deadlock AND zero lock-wait-timeout across all workers; every SE
submitted; Bin actual_qty restored after cleanup.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from time import perf_counter

import frappe


# ---------------------------------------------------------------------------
# Worker (runs in a separate spawned process with its own frappe connection)
# ---------------------------------------------------------------------------
def _worker(payload: dict) -> dict:
	"""Submit N Material Receipt SEs; classify every failure. Returns a counts dict.

	Must be a top-level function (picklable for the spawn context). Each call owns its
	frappe connection for the whole batch and commits per submit so locks release like a
	real request would.
	"""
	import frappe as _frappe
	from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

	_frappe.init(site=payload["site"], sites_path=payload["sites_path"])
	_frappe.connect()

	created, deadlocks, timeouts, others, other_samples = [], 0, 0, 0, []
	try:
		for _ in range(payload["per_worker"]):
			try:
				se = _frappe.new_doc("Stock Entry")
				se.stock_entry_type = "Material Receipt"
				se.company = payload["company"]
				se.append(
					"items",
					{
						"item_code": payload["item_code"],
						"qty": payload["qty"],
						"t_warehouse": payload["warehouse"],
						"basic_rate": 1,
						"allow_zero_valuation_rate": 1,
					},
				)
				se.flags.ignore_permissions = True
				se.insert()
				se.submit()
				_frappe.db.commit()
				created.append(se.name)
			except QueryDeadlockError:
				deadlocks += 1
				_frappe.db.rollback()
			except QueryTimeoutError:
				timeouts += 1
				_frappe.db.rollback()
			except (
				Exception
			) as exc:  # non-lock failure (setup / validation) — keep separate
				others += 1
				if len(other_samples) < 3:
					other_samples.append(f"{type(exc).__name__}: {str(exc)[:160]}")
				_frappe.db.rollback()
	finally:
		_frappe.destroy()

	return {
		"created": created,
		"deadlocks": deadlocks,
		"timeouts": timeouts,
		"others": others,
		"other_samples": other_samples,
	}


# ---------------------------------------------------------------------------
# Discovery + measurement helpers (parent process)
# ---------------------------------------------------------------------------
def _discover(company: str | None):
	if not company:
		company = (
			frappe.db.get_value("Global Defaults", "Global Defaults", "default_company")
			or (frappe.get_all("Company", limit=1, pluck="name") or [None])[0]
		)
	item = frappe.db.get_value(
		"Item",
		{"is_stock_item": 1, "has_batch_no": 0, "has_serial_no": 0, "disabled": 0},
		"name",
	)
	warehouse = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 0, "disabled": 0}, "name"
	)
	return company, item, warehouse


def _bin_qty(item_code, warehouse):
	return (
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
		)
		or 0
	)


def _row_lock_waits():
	row = frappe.db.sql("SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_waits'")
	return int(row[0][1]) if row else None


def _cleanup(names):
	"""Best-effort cancel + delete of the SEs created, so net stock is unchanged."""
	cancelled = 0
	for name in names:
		try:
			doc = frappe.get_doc("Stock Entry", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Stock Entry", name, force=1, ignore_permissions=True)
			frappe.db.commit()
			cancelled += 1
		except Exception:
			frappe.db.rollback()
	return cancelled


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def run_stress(
	workers: int = 8,
	per_worker: int = 5,
	item_code: str | None = None,
	warehouse: str | None = None,
	company: str | None = None,
	qty: float = 0.001,
	cleanup: bool = True,
):
	"""Spawn `workers` processes each submitting `per_worker` contending Material Receipts.

	Prints a JSON summary and raises AssertionError if ANY 1205/1213 occurred (the
	root-cause pass/fail gate).
	"""
	workers, per_worker = int(workers), int(per_worker)
	company, disc_item, disc_wh = _discover(company)
	item_code = item_code or disc_item
	warehouse = warehouse or disc_wh

	if not (company and item_code and warehouse):
		frappe.throw(
			f"Could not resolve test fixtures (company={company}, item={item_code}, "
			f"warehouse={warehouse}). Pass them explicitly via --kwargs."
		)

	qty_before = _bin_qty(item_code, warehouse)
	waits_before = _row_lock_waits()

	payload = {
		"site": frappe.local.site,
		"sites_path": frappe.local.sites_path,
		"company": company,
		"item_code": item_code,
		"warehouse": warehouse,
		"per_worker": per_worker,
		"qty": float(qty),
	}

	# Commit any open parent transaction so children see a clean baseline and don't inherit
	# parent row locks.
	frappe.db.commit()

	t0 = perf_counter()
	ctx = get_context("spawn")
	with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
		results = list(pool.map(_worker, [payload] * workers))
	wall_s = perf_counter() - t0

	deadlocks = sum(r["deadlocks"] for r in results)
	timeouts = sum(r["timeouts"] for r in results)
	others = sum(r["others"] for r in results)
	created = [n for r in results for n in r["created"]]
	other_samples = [s for r in results for s in r["other_samples"]][:5]

	waits_after = _row_lock_waits()
	cleaned = _cleanup(created) if cleanup else 0
	qty_after = _bin_qty(item_code, warehouse)

	summary = {
		"config": {
			"workers": workers,
			"per_worker": per_worker,
			"attempted": workers * per_worker,
			"item_code": item_code,
			"warehouse": warehouse,
			"company": company,
		},
		"results": {
			"submitted_ok": len(created),
			"deadlocks_1213": deadlocks,
			"timeouts_1205": timeouts,
			"other_failures": others,
			"other_samples": other_samples,
		},
		"measure": {
			"wall_seconds": round(wall_s, 3),
			"innodb_row_lock_waits_delta": (
				(waits_after - waits_before)
				if (waits_before is not None and waits_after is not None)
				else None
			),
		},
		"cleanup": {
			"cancelled_deleted": cleaned,
			"bin_qty_before": qty_before,
			"bin_qty_after": qty_after,
			"bin_qty_restored": abs((qty_after or 0) - (qty_before or 0)) < 1e-9
			if cleanup
			else None,
		},
	}
	print(json.dumps(summary, indent=2, default=str))

	# Root-cause pass/fail gate: contention errors must be zero.
	assert (
		deadlocks == 0
	), f"FAIL: {deadlocks} deadlock(s) (1213) under concurrency — root cause not fixed"
	assert (
		timeouts == 0
	), f"FAIL: {timeouts} lock-wait timeout(s) (1205) under concurrency — root cause not fixed"
	return summary
