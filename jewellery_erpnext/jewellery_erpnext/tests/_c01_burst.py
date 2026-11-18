"""C-01 convoy-removal experiments (D1 4.5) for the THROWAWAY site. These submits COMMIT.

Removing the unconditional naming pre-lock lets receives that mint nothing run side by side.
The convoy used to hide two lock-order hazards; these runs measure them on both builds.

S1 burst -- ``run_burst``: ``workers`` spawned processes submit committed Receive drafts (a mix
of no-mint and Process-Loss-only, from ``build_burst_drafts``), optionally while a holder process
keeps ``MAT-STE-`` for ``hold_seconds`` every ``hold_every`` seconds. Reports 1205 / 1213 /
NamingCounterBusy counts with the innermost app frame, p50/p95 latency per class and throughput.
Expected on C-01: no-mint 1205 = 0; Process-Loss-only unchanged; 1213 not above the baseline.

S2 PMO cross-order -- ``run_pmo_cross_order``: two no-mint receives R1 [PMO A row, PMO B row] and
R2 [PMO B row, PMO A row], each paused after its first row on a file barrier (10 s). Each
Manufacturing Operation insert updates its Parent Manufacturing Order row in row order
(``set_pmo_weight_details_in_bulk``). Baseline: R2 queues at its naming pre-lock, no deadlock.
C-01 alone: one 1213 expected by mechanism -- if it reproduces, ship C-01b (sorted PMO pre-lock).

Take a snapshot before, restore it after: nothing here cleans up. ::

    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._c01_burst.build_burst_drafts --kwargs "{'n': 40}"
    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._c01_burst.run_burst --kwargs "{'workers': 6}"
    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._c01_burst.run_pmo_cross_order
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import frappe

from jewellery_erpnext.jewellery_erpnext.tests._c01_harness import (
	_open_issues,
	build_receive_draft,
	require_throwaway_site,
	tag_draft,
	tagged_drafts,
)

BURST_TAG = "C01-BURST"


def build_burst_drafts(n=40, company="KG GK Jewellers Private Limited", seed=17):
	"""Commit ``n`` Receive drafts: half flat (mint nothing), half 0.01 g short (Process Loss)."""
	require_throwaway_site()
	rng = random.Random(seed)
	built, failed = [], []
	for issue in _open_issues(company, limit=4 * n):
		if len(built) >= n:
			break
		kind = rng.choice(("no-mint", "process-loss"))
		try:
			name = build_receive_draft(issue, 0.0 if kind == "no-mint" else -0.01)
			tag_draft(name, f"{BURST_TAG}:{kind}")
			frappe.db.commit()
			built.append((name, kind))
		except Exception as exc:
			frappe.db.rollback()
			failed.append((issue.name, repr(exc)[:200]))
	out = {"built": built, "failed": failed}
	print(json.dumps(out, indent=1))
	return out


def _classify(exc):
	from jewellery_erpnext.jewellery_erpnext.lock_order import NamingCounterBusy

	if isinstance(exc, frappe.QueryDeadlockError):
		return "1213"
	if isinstance(exc, frappe.QueryTimeoutError):
		return "1205"
	if isinstance(exc, NamingCounterBusy):
		return "naming-busy"
	return "other"


def _app_frame(exc):
	tb, frame = exc.__traceback__, None
	while tb is not None:
		if "jewellery_erpnext" in tb.tb_frame.f_code.co_filename:
			frame = f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno} {tb.tb_frame.f_code.co_name}"
		tb = tb.tb_next
	return frame


def _burst_worker(payload):
	import frappe as _frappe

	_frappe.init(site=payload["site"], sites_path=payload["sites_path"])
	_frappe.connect()
	results = []
	try:
		for name, kind in payload["drafts"]:
			started = time.monotonic()
			try:
				_frappe.get_doc("Employee IR", name).submit()
				_frappe.db.commit()
				outcome, frame = "ok", None
			except Exception as exc:
				_frappe.db.rollback()
				outcome, frame = _classify(exc), _app_frame(exc)
			results.append(
				{
					"name": name,
					"kind": kind,
					"outcome": outcome,
					"frame": frame,
					"seconds": round(time.monotonic() - started, 3),
				}
			)
	finally:
		_frappe.destroy()
	return results


def _holder(payload):
	"""Keep MAT-STE- locked for ``hold_seconds`` every ``hold_every`` seconds until ``stop`` exists."""
	import frappe as _frappe

	_frappe.init(site=payload["site"], sites_path=payload["sites_path"])
	_frappe.connect()
	holds = 0
	try:
		while not os.path.exists(payload["stop"]):
			_frappe.db.sql(
				"SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE",
				(payload["series"],),
			)
			time.sleep(payload["hold_seconds"])
			_frappe.db.rollback()
			holds += 1
			time.sleep(max(0, payload["hold_every"] - payload["hold_seconds"]))
	finally:
		_frappe.destroy()
	return holds


def _pct(values, q):
	if not values:
		return None
	values = sorted(values)
	return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def run_burst(workers=6, hold_seconds=0, hold_every=60, series="MAT-STE-"):
	require_throwaway_site()
	drafts = [
		(name, tag.split(":", 1)[1])
		for name, tag in tagged_drafts(f"{BURST_TAG}:")
		if not tag.endswith(":cross-order")
	]
	base = {"site": frappe.local.site, "sites_path": frappe.local.sites_path}
	chunks = [drafts[i::workers] for i in range(workers)]
	stop = os.path.join(tempfile.mkdtemp(prefix="c01-s1-"), "stop")
	holds = None
	started = time.monotonic()
	ctx = get_context("spawn")
	with ProcessPoolExecutor(max_workers=workers + 1, mp_context=ctx) as pool:
		holder = None
		if hold_seconds:
			holder = pool.submit(
				_holder,
				{
					**base,
					"series": series,
					"hold_seconds": hold_seconds,
					"hold_every": hold_every,
					"stop": stop,
				},
			)
		futures = [
			pool.submit(_burst_worker, {**base, "drafts": c}) for c in chunks if c
		]
		try:
			rows = [r for f in futures for r in f.result()]
		finally:
			open(stop, "w").close()
		if holder:
			holds = holder.result()
	elapsed = time.monotonic() - started
	summary = {}
	for kind in ("no-mint", "process-loss"):
		mine = [r for r in rows if r["kind"] == kind]
		summary[kind] = {
			"n": len(mine),
			"outcomes": {
				o: sum(1 for r in mine if r["outcome"] == o)
				for o in {r["outcome"] for r in mine}
			},
			"p50": _pct([r["seconds"] for r in mine if r["outcome"] == "ok"], 0.5),
			"p95": _pct([r["seconds"] for r in mine if r["outcome"] == "ok"], 0.95),
			"wait_frames": sorted({r["frame"] for r in mine if r["frame"]}),
		}
	out = {
		"workers": workers,
		"hold_seconds": hold_seconds,
		"holds": holds,
		"elapsed": round(elapsed, 1),
		"per_minute": round(
			60 * sum(1 for r in rows if r["outcome"] == "ok") / max(elapsed, 1e-6), 1
		),
		"summary": summary,
		"rows": rows,
	}
	print(json.dumps(out, indent=1))
	return out


# ---------------------------------------------------------------------------
# S2: deterministic PMO cross-order
# ---------------------------------------------------------------------------
def _cross_order_issue(company):
	"""An open Issue with at least two rows on each of two PMOs: ``(issue, {pmo: [rows]})``."""
	for issue in _open_issues(company, limit=400):
		rows = frappe.db.sql(
			"""SELECT o.manufacturing_operation, o.manufacturing_work_order, w.manufacturing_order
			FROM `tabEmployee IR Operation` o
			JOIN `tabManufacturing Work Order` w ON w.name = o.manufacturing_work_order
			WHERE o.parent = %s AND o.parenttype = 'Employee IR' ORDER BY o.idx""",
			issue.name,
			as_dict=True,
		)
		by_pmo = {}
		for r in rows:
			by_pmo.setdefault(r.manufacturing_order, []).append(r)
		pmos = [p for p, rs in by_pmo.items() if p and len(rs) >= 2]
		if len(pmos) >= 2:
			return issue, {p: by_pmo[p] for p in pmos[:2]}
	return None, None


def _receive_from_rows(issue, rows):
	doc = frappe.new_doc("Employee IR")
	doc.update(
		{
			"type": "Receive",
			"company": issue.company,
			"department": issue.department,
			"operation": issue.operation,
			"subcontracting": issue.subcontracting,
			"employee": issue.employee,
			"subcontractor": issue.subcontractor,
		}
	)
	for r in rows:
		gross = frappe.db.get_value(
			"Manufacturing Operation", r.manufacturing_operation, "gross_wt"
		)
		doc.append(
			"employee_ir_operations",
			{
				"manufacturing_operation": r.manufacturing_operation,
				"manufacturing_work_order": r.manufacturing_work_order,
				"gross_wt": gross,
				"received_gross_wt": gross,
			},
		)
	doc.insert(ignore_permissions=True)
	tag_draft(doc.name, f"{BURST_TAG}:cross-order")
	return doc.name


def _cross_worker(payload):
	import frappe as _frappe

	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import (
		employee_ir as eir_mod,
	)

	_frappe.init(site=payload["site"], sites_path=payload["sites_path"])
	_frappe.connect()
	real = eir_mod.create_operation_for_next_op
	calls = {"n": 0}

	def _paused(*args, **kwargs):
		out = real(*args, **kwargs)
		calls["n"] += 1
		if calls["n"] == 1:  # after row 1: both sides hold their first PMO row
			open(payload["mine"], "w").close()
			deadline = time.monotonic() + payload["barrier"]
			while not os.path.exists(payload["theirs"]) and time.monotonic() < deadline:
				time.sleep(0.05)
		return out

	started = time.monotonic()
	try:
		eir_mod.create_operation_for_next_op = _paused
		_frappe.get_doc("Employee IR", payload["name"]).submit()
		_frappe.db.commit()
		outcome, frame = "ok", None
	except Exception as exc:
		_frappe.db.rollback()
		outcome, frame = _classify(exc), _app_frame(exc)
	finally:
		eir_mod.create_operation_for_next_op = real
		_frappe.destroy()
	return {
		"name": payload["name"],
		"outcome": outcome,
		"frame": frame,
		"seconds": round(time.monotonic() - started, 3),
	}


def run_pmo_cross_order(company="KG GK Jewellers Private Limited", barrier=10):
	require_throwaway_site()
	issue, by_pmo = _cross_order_issue(company)
	if not issue:
		out = {"skipped": "no open Issue with two rows on each of two PMOs"}
		print(json.dumps(out))
		return out
	(pmo_a, rows_a), (pmo_b, rows_b) = list(by_pmo.items())
	r1 = _receive_from_rows(issue, [rows_a[0], rows_b[0]])
	r2 = _receive_from_rows(issue, [rows_b[1], rows_a[1]])
	frappe.db.commit()
	base = {
		"site": frappe.local.site,
		"sites_path": frappe.local.sites_path,
		"barrier": barrier,
	}
	tmp = tempfile.mkdtemp(prefix="c01-s2-")
	f1, f2 = os.path.join(tmp, "r1"), os.path.join(tmp, "r2")
	with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
		a = pool.submit(_cross_worker, {**base, "name": r1, "mine": f1, "theirs": f2})
		b = pool.submit(_cross_worker, {**base, "name": r2, "mine": f2, "theirs": f1})
		results = [a.result(), b.result()]
	out = {
		"pmos": [pmo_a, pmo_b],
		"receives": [r1, r2],
		"results": results,
		"deadlocks": sum(1 for r in results if r["outcome"] == "1213"),
	}
	print(json.dumps(out, indent=1))
	return out
