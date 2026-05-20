"""Phase J — Reusable concurrency reproduction harness.

Runs N workers (multiprocessing.spawn) that execute the same Frappe action
against the SAME copied/staging site at the SAME time, synchronized by a
multiprocessing.Barrier so all workers cross the action boundary together.
Captures each worker's result/exception and writes a JSON summary to
``/tmp/jewellery_concurrency_results_<timestamp>.json``.

Purpose:
  - Reproduce deadlocks (1213) and lock-wait-timeouts (1205) deterministically.
  - Prove same-document submit races converge to ONE successful submit.
  - Prove same-Reserve workflow races converge to ONE reserved Stock Entry.
  - Validate the harness is faithful by triggering an intentional
    reverse-lock deadlock scenario.

Safety:
  This harness is destructive. It commits real documents and may produce
  real Error Log rows. It MUST run only on a copied/staging site whose
  name contains one of: test, copy, staging, dummy, qa — OR with the
  environment variable ALLOW_JEWELLERY_CONCURRENCY_TESTS=1 explicitly
  set by an operator. Production-like site names are refused with
  RuntimeError before any worker is spawned.

Bench invocation:
  bench --site <COPY-SITE> execute \\
      jewellery_erpnext.jewellery_erpnext.tests.concurrency_harness.run_concurrent \\
      --kwargs '{"site": "<COPY-SITE>", "action": "submit_doc",
                 "payloads": [{"doctype": "Product Certification", "name": "PC-XYZ"},
                              {"doctype": "Product Certification", "name": "PC-XYZ"}]}'
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import time
import traceback
from datetime import datetime
from typing import Any

_SAFE_SITE_RE = re.compile(r"(test|copy|staging|dummy|qa)", re.IGNORECASE)


def _assert_safe_site(site: str) -> None:
	"""Refuse to run on a production-like site.

	The opt-in env var ``ALLOW_JEWELLERY_CONCURRENCY_TESTS=1`` is provided
	for emergency situations where an operator has explicit approval and
	the site name pattern check is too restrictive.
	"""
	if os.environ.get("ALLOW_JEWELLERY_CONCURRENCY_TESTS") == "1":
		return
	if not site or not _SAFE_SITE_RE.search(site):
		raise RuntimeError(
			f"Refusing to run jewellery_erpnext concurrency tests on site {site!r}. "
			"Concurrency tests MUST run on a copied/staging site only. Either "
			"rename the target site to include 'test', 'copy', 'staging', "
			"'dummy', or 'qa', or set the environment variable "
			"ALLOW_JEWELLERY_CONCURRENCY_TESTS=1 if you have explicit operator "
			"approval. This guard prevents accidental production data damage."
		)


def _worker(site: str, action: str, payload: dict, barrier, outq) -> None:
	"""Per-process worker. Initializes Frappe in this process, waits on the
	barrier, executes ``action``, commits or rolls back, and pushes a result
	dict onto the shared queue. Any exception is captured into the result
	rather than crashing the worker process — the parent reads every result.
	"""
	import frappe

	result: dict[str, Any] = {"action": action, "payload": payload}
	try:
		frappe.init(site=site)
		frappe.connect()
		# Sync all workers — every worker reaches this point before any of
		# them proceeds, so contention is real.
		barrier.wait(timeout=30)

		if action == "submit_doc":
			doc = frappe.get_doc(payload["doctype"], payload["name"])
			doc.submit()
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "save_doc":
			doc = frappe.get_doc(payload["doctype"], payload["name"])
			for field, value in (payload.get("set") or {}).items():
				setattr(doc, field, value)
			doc.save()
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "workflow_action":
			from frappe.model.workflow import apply_workflow

			doc = frappe.get_doc(payload["doctype"], payload["name"])
			apply_workflow(doc, payload["workflow_action"])
			frappe.db.commit()
			result.update({"ok": True, "workflow_action": payload["workflow_action"]})

		elif action == "custom_method":
			dotted = payload["method"]
			module_path, fn_name = dotted.rsplit(".", 1)
			mod = __import__(module_path, fromlist=[fn_name])
			fn = getattr(mod, fn_name)
			fn_result = fn(**(payload.get("kwargs") or {}))
			frappe.db.commit()
			result.update({"ok": True, "result": str(fn_result)[:2000]})

		elif action == "deadlock_reverse_lock":
			# Copied-site only: deliberately lock two rows in reverse order
			# to validate the harness can faithfully reproduce 1213.
			rows = payload["rows"]
			order = payload["order"]  # e.g. [0, 1] vs [1, 0]
			doctype = payload["doctype"]
			first = rows[order[0]]
			second = rows[order[1]]
			frappe.db.sql(
				f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE",
				first,
			)
			time.sleep(payload.get("sleep", 2))
			frappe.db.sql(
				f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE",
				second,
			)
			frappe.db.commit()
			result.update({"ok": True, "order": order})

		elif action == "reserve_material":
			# Phase M-4: convenience wrapper — apply the Reserve Material
			# workflow action to a Material Request.
			from frappe.model.workflow import apply_workflow

			doc = frappe.get_doc("Material Request", payload["mr_name"])
			apply_workflow(doc, payload.get("workflow_action", "Reserve Material"))
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "submit_product_certification":
			# Phase M-4 convenience wrapper.
			doc = frappe.get_doc("Product Certification", payload["name"])
			doc.submit()
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "submit_serial_number_creator":
			doc = frappe.get_doc("Serial Number Creator", payload["name"])
			doc.submit()
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "submit_employee_ir":
			doc = frappe.get_doc("Employee IR", payload["name"])
			doc.submit()
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "bulk_reserve_material":
			# Phase M-4: invoke the bulk-update entry point used in production.
			from jewellery_erpnext.jewellery_erpnext.doc_events.bulk_update import (
				custom_submit_cancel_or_update_docs,
			)

			custom_submit_cancel_or_update_docs(
				"Material Request",
				payload["docnames"],
				action="Reserve Material",
			)
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "mop_eod_sync":
			# Phase M-4: invoke the MOP EOD scheduler entry point.
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
				sync_mop_logs,
			)

			out = sync_mop_logs()
			frappe.db.commit()
			result.update({"ok": True, "result": str(out)[:1000]})

		elif action == "lock_with_short_timeout":
			# Phase M-6: deterministic 1205 reproduction. Shrink the session
			# lock-wait-timeout so the worker fails fast (~1 s) instead of
			# the MariaDB default (~50 s). Use only with a paired worker that
			# is HOLDING the row this worker tries to lock.
			frappe.db.sql(
				"SET SESSION innodb_lock_wait_timeout = %s",
				(int(payload.get("timeout_seconds", 1)),),
			)
			frappe.db.sql(payload["sql"], payload.get("params") or ())
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "hold_row_for_update":
			# Phase M-6 / M-7 partner action: lock a row FOR UPDATE and
			# sleep, so the paired ``lock_with_short_timeout`` worker hits
			# 1205. The hold worker commits normally after the sleep.
			frappe.db.sql(payload["sql"], payload.get("params") or ())
			time.sleep(payload.get("hold_seconds", 3))
			frappe.db.commit()
			result.update({"ok": True})

		elif action == "bin_reverse_lock":
			# Phase M-7: Bin-specific reverse-lock that exercises the
			# stock-engine lock path. order=[0,1] vs [1,0] across two Bin
			# rows identified by (item_code, warehouse) name strings.
			bins = payload["bin_names"]
			order = payload["order"]
			first, second = bins[order[0]], bins[order[1]]
			frappe.db.sql("SELECT name FROM `tabBin` WHERE name=%s FOR UPDATE", first)
			time.sleep(payload.get("sleep", 2))
			frappe.db.sql("SELECT name FROM `tabBin` WHERE name=%s FOR UPDATE", second)
			frappe.db.commit()
			result.update({"ok": True, "order": order})

		else:
			result.update({"ok": False, "error": f"Unknown action {action!r}"})

	except Exception as exc:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		result.update(
			{
				"ok": False,
				"exc_type": type(exc).__name__,
				"error": str(exc)[:2000],
				"traceback": traceback.format_exc()[:4000],
			}
		)
	finally:
		try:
			frappe.destroy()
		except Exception:
			pass
		try:
			outq.put(result)
		except Exception:
			pass


def run_concurrent(
	site: str,
	action: str,
	payloads: list[dict],
	timeout: int = 120,
	write_json: bool = True,
) -> dict[str, Any]:
	"""Spawn ``len(payloads)`` workers, each running ``action`` with its
	payload, synchronized at a barrier. Returns a summary dict and writes
	the same to ``/tmp/jewellery_concurrency_results_<timestamp>.json``.

	Arguments:
	  site:     bench site name. Must pass _assert_safe_site.
	  action:   one of submit_doc / save_doc / workflow_action /
	            custom_method / deadlock_reverse_lock.
	  payloads: list of per-worker payload dicts; len = worker count.
	  timeout:  per-result get() timeout in seconds.
	  write_json: write summary to /tmp; set False for unit-test runs.

	Returns dict with keys: site, action, n_workers, n_ok, n_fail,
	exception_types, results, optionally json_path.
	"""
	_assert_safe_site(site)
	if not payloads:
		raise ValueError("payloads must be non-empty")

	ctx = mp.get_context("spawn")
	barrier = ctx.Barrier(len(payloads))
	outq = ctx.Queue()
	procs = []
	for payload in payloads:
		p = ctx.Process(target=_worker, args=(site, action, payload, barrier, outq))
		p.start()
		procs.append(p)

	results: list[dict] = []
	deadline = time.time() + timeout
	for _ in procs:
		remaining = max(1, int(deadline - time.time()))
		try:
			results.append(outq.get(timeout=remaining))
		except Exception as exc:
			results.append(
				{
					"ok": False,
					"action": action,
					"exc_type": type(exc).__name__,
					"error": f"queue.get timeout or failure: {exc!s}",
				}
			)

	for p in procs:
		p.join(timeout=10)
		if p.is_alive():
			p.terminate()
			p.join(timeout=5)

	summary = {
		"site": site,
		"action": action,
		"n_workers": len(payloads),
		"n_ok": sum(1 for r in results if r.get("ok")),
		"n_fail": sum(1 for r in results if not r.get("ok")),
		"exception_types": sorted(
			{
				r.get("exc_type")
				for r in results
				if not r.get("ok") and r.get("exc_type")
			}
		),
		"results": results,
	}
	if write_json:
		path = f"/tmp/jewellery_concurrency_results_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
		try:
			with open(path, "w") as f:
				json.dump(summary, f, indent=2, default=str)
			summary["json_path"] = path
		except Exception as exc:
			summary["json_write_error"] = str(exc)
	return summary
