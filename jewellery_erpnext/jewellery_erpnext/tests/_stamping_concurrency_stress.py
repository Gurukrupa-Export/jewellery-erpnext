# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Concurrency proof for the Serial No stamping counter. NOT a unit test.

Run from ``bench execute`` (needs real, SEPARATE worker connections, so it is an ad-hoc
script like ``_lock_contention_stress``; a single-connection unittest cannot express
contention at all):

    bench --site <site> execute \
        jewellery_erpnext.jewellery_erpnext.tests._stamping_concurrency_stress.run_stress

Spawns ``workers`` OS processes, each with its OWN frappe connection, each claiming
``per_worker`` stamping numbers and committing per claim, exactly as concurrent Serial
Number Creator submits would.

PASS: every number distinct, no gaps, zero 1213 (deadlock), zero 1205 (lock wait timeout).

RUN THIS AGAINST THE PRE-FIX CODE FIRST. A concurrency test that never fails on the broken
version proves nothing -- with the old ``MAX(...) + 1`` read this must report duplicates.
"""

import json
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import frappe

# A year code no real data uses, so a stress run never perturbs live counters.
STRESS_PREFIX = "2Z"


def _worker(payload: dict) -> dict:
	"""Claim `per_worker` stamping numbers on this process's own connection.

	Must be a top-level function (picklable for the spawn context).
	"""
	import frappe as _frappe
	from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

	from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
		reserve_stamping_sequence,
	)

	_frappe.init(site=payload["site"], sites_path=payload["sites_path"])
	_frappe.connect()

	numbers, deadlocks, timeouts, others = [], 0, 0, []
	try:
		for _ in range(payload["per_worker"]):
			try:
				numbers.append(reserve_stamping_sequence(payload["prefix"]))
				_frappe.db.commit()
			except QueryDeadlockError:
				deadlocks += 1
				_frappe.db.rollback()
			except QueryTimeoutError:
				timeouts += 1
				_frappe.db.rollback()
			except Exception as e:
				others.append(f"{type(e).__name__}: {e}")
				_frappe.db.rollback()
	finally:
		_frappe.destroy()

	return {
		"numbers": numbers,
		"deadlocks": deadlocks,
		"timeouts": timeouts,
		"others": others,
	}


def _counter_key():
	from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
		stamping_series_key,
	)

	return stamping_series_key(STRESS_PREFIX)


def run_stress(workers: int = 8, per_worker: int = 25, cleanup: bool = True):
	"""Spawn `workers` processes each claiming `per_worker` stamping numbers.

	Prints a JSON summary and raises AssertionError on any duplicate, gap, 1213 or 1205.
	"""
	workers, per_worker = int(workers), int(per_worker)
	key = _counter_key()

	frappe.db.sql("DELETE FROM `tabSeries` WHERE `name` = %s", (key,))
	frappe.db.commit()

	payload = {
		"site": frappe.local.site,
		"sites_path": frappe.utils.get_sites_path(),
		"per_worker": per_worker,
		"prefix": STRESS_PREFIX,
	}

	results = []
	with ProcessPoolExecutor(
		max_workers=workers, mp_context=get_context("spawn")
	) as pool:
		for res in pool.map(_worker, [payload] * workers):
			results.append(res)

	issued = [n for r in results for n in r["numbers"]]
	deadlocks = sum(r["deadlocks"] for r in results)
	timeouts = sum(r["timeouts"] for r in results)
	others = [o for r in results for o in r["others"]]

	duplicates = sorted({n for n in issued if issued.count(n) > 1})
	expected = list(range(1, len(issued) + 1))
	gaps = sorted(set(expected) - set(issued))

	summary = {
		"workers": workers,
		"per_worker": per_worker,
		"claimed": len(issued),
		"distinct": len(set(issued)),
		"duplicates": duplicates,
		"gaps": gaps,
		"deadlocks": deadlocks,
		"timeouts": timeouts,
		"other_errors": others[:10],
		"counter_after": frappe.db.sql(
			"SELECT `current` FROM `tabSeries` WHERE `name` = %s", (key,)
		),
	}
	print(json.dumps(summary, indent=2, default=str))

	if cleanup:
		frappe.db.sql("DELETE FROM `tabSeries` WHERE `name` = %s", (key,))
		frappe.db.commit()

	assert not duplicates, f"DUPLICATE stamping numbers issued: {duplicates}"
	assert not gaps, f"GAPS in the stamping sequence: {gaps}"
	assert not deadlocks, f"{deadlocks} deadlock(s) (1213)"
	assert not timeouts, f"{timeouts} lock wait timeout(s) (1205)"
	assert not others, f"unexpected errors: {others[:5]}"
	print("\nPASS: every stamping number unique, no gaps, no 1213/1205.")
	return summary
