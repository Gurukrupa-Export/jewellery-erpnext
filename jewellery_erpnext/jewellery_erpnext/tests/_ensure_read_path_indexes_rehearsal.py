"""Rehearsal (T-09 / T-C07-1, -3, -4) of ``patches/ensure_read_path_indexes`` on the THROWAWAY
site only. It runs DDL, so it is an explicit entrypoint, not an auto-discovered unittest (hence
the leading underscore, like ``_lock_contention_stress.py``). Every phase refuses to run on any
site other than ``kggk-pi-test.localhost``.

Proves, on a restore of production data:

* EXPLAIN: before the build, R1-R7 walk ``creation`` / ``voucher_type`` or the whole table;
  after it, each one is a ``ref`` / ``range`` lookup on the new index.
* Result sets: R1-R7 (through the app and ERPNext functions themselves where they are
  read-only) return the same rows before and after, for the same sampled IDs. Ordered results
  compare exactly; lists that are only used as IN-lists or sets compare as multisets (an order
  change is still reported).
* Schema sync keeps the indexes: identical ``information_schema.STATISTICS`` after
  ``bench migrate`` and after ``frappe.db.updatedb`` of the three doctypes; ERPNext's
  ``frappe.db.add_index("Serial and Batch Bundle", ["voucher_no", "voucher_type"])`` (v16.55+)
  adds nothing; a re-run of ``ensure`` issues no DDL.
* LEGACY: the kggk_uat name is renamed when alone and dropped when it duplicates the upstream
  name, and MariaDB accepts both statements online.

Run, in order::

    S=kggk-pi-test.localhost
    M=jewellery_erpnext.jewellery_erpnext.tests._ensure_read_path_indexes_rehearsal
    bench --site $S execute $M.capture --kwargs "{'sample': 300}"   # add 'reset': 1 if built
    bench --site $S execute $M.build --kwargs "{'lock_wait': 5}"
    bench --site $S execute $M.verify
    bench --site $S migrate
    bench --site $S execute $M.verify_after_migrate
    bench --site $S execute $M.verify_legacy                        # optional, DDL on SBE

``capture`` before the indexes exist is slow by design: each sampled MOP costs about four
1.5 s scans and each sampled voucher about 5 s (copy timings), so ``sample=300`` takes roughly
an hour. A smaller sample is fine for a first pass. The snapshot is written to
``sites/<site>/private/ensure_read_path_indexes_rehearsal.json``; each phase raises
``AssertionError`` with the failing check and otherwise returns a summary.
"""

import json
from collections import Counter

import frappe

from jewellery_erpnext.patches import ensure_read_path_indexes as perf

SITE = "kggk-pi-test.localhost"
SNAPSHOT = "ensure_read_path_indexes_rehearsal.json"
TABLES = ("tabMOP Log", "tabSerial and Batch Bundle", "tabSerial and Batch Entry")
OURS = {(t, n) for t, n, _c in perf.SPECS}

R1_SQL = """
	SELECT item_code, batch_no, qty_after_transaction_batch_based AS qaf_batch,
		pcs_after_transaction_batch_based AS pcs_batch, manufacturing_work_order, name, creation
	FROM `tabMOP Log`
	WHERE manufacturing_operation = %(mop)s AND is_cancelled = 0
	ORDER BY creation ASC
"""
R3_SQL = """
	SELECT MAX(flow_index) FROM `tabMOP Log`
	WHERE manufacturing_operation = %(mop)s AND is_cancelled = 0
"""
R5_SQL = """
	SELECT DISTINCT manufacturing_operation FROM `tabMOP Log`
	WHERE voucher_type = %(vt)s AND voucher_no = %(vn)s AND is_cancelled = 0
	  AND manufacturing_operation IS NOT NULL
"""


def _r2(s):
	return frappe.get_all(
		"MOP Log",
		filters={"manufacturing_operation": s["mop"], "is_cancelled": 0},
		fields=["item_code", "batch_no", "name", "creation"],
		order_by="creation desc",
		run=0,
	)


def _r4(s):
	"""The SQL ``frappe.db.get_value(..., order_by=...)`` sends: same filters, LIMIT 1."""
	return frappe.get_all(
		"MOP Log",
		filters={
			"manufacturing_operation": s["mop"],
			"item_code": s["item"],
			"is_cancelled": 0,
		},
		fields=["batch_no"],
		order_by="flow_index desc, creation desc",
		limit=1,
		run=0,
	)


def _r5_prefetch(s):
	return frappe.get_all(
		"MOP Log",
		filters={"voucher_type": s["vt"], "voucher_no": s["vn"], "is_cancelled": 0},
		fields=["row_name", "manufacturing_operation"],
		run=0,
	)


def _r6(s):
	return frappe.get_all(
		"Serial and Batch Bundle",
		{
			"type_of_transaction": "Outward",
			"voucher_type": s["vt"],
			"voucher_no": s["vn"],
		},
		pluck="name",
		run=0,
	)


def _r7(s):
	return frappe.get_all(
		"Serial and Batch Entry",
		filters={
			"voucher_type": s["vt"],
			"voucher_no": s["vn"],
			"docstatus": 1,
			"batch_no": ("is", "set"),
			"qty": ("<", 0),
		},
		pluck="batch_no",
		run=0,
	)


# label -> (sample -> (sql, values), sample kind, indexes the plan may use after the build).
# The SQL is the code's own (file:line at 0863ac34); get_all reads are rendered by get_all
# itself (run=0), so the plan is that of the statement production sends.
EXPLAINS = {
	"R1 mop_log.py:176 recalculate_manufacturing_operation_weights": (
		lambda s: (R1_SQL, s),
		"mop",
		{"mop_balance_idx"},
	),
	"R2 mop_log.py:491 get_current_mop_balance_rows": (
		lambda s: (str(_r2(s)), None),
		"mop",
		{"mop_balance_idx"},
	),
	"R3 mop_log.py:472 get_last_mop_index": (
		lambda s: (R3_SQL, s),
		"mop",
		{"mop_balance_idx"},
	),
	"R4 stock_entry.py:408 get_receive_work_order_batch": (
		lambda s: (str(_r4(s)), None),
		"mop",
		{"mop_balance_idx"},
	),
	"R5 employee_ir.py:338 DISTINCT MOPs of a voucher": (
		lambda s: (R5_SQL, s),
		"mop_voucher",
		{"mop_voucher_cancel_idx"},
	),
	"R5 stock_entry.py:1144 bridge prefetch": (
		lambda s: (str(_r5_prefetch(s)), None),
		"mop_voucher",
		{"mop_voucher_cancel_idx"},
	),
	"R6 sbb utils.py:53 update_parent_batch_id": (
		lambda s: (str(_r6(s)), None),
		"sbb_voucher",
		{"voucher_no_voucher_type_index"},
	),
	"R7 stock_controller.py validate_reserved_batches": (
		lambda s: (str(_r7(s)), None),
		"sbb_voucher",
		{"voucher_no_voucher_type_index"},
	),
}
GOOD_ACCESS = {"ref", "eq_ref", "const", "range", "index_merge"}


# -- plumbing ---------------------------------------------------------------------------------


def _guard():
	if frappe.local.site != SITE:
		raise AssertionError(
			f"refusing to run on {frappe.local.site!r}; this rehearsal is for {SITE}"
		)


def _path():
	return frappe.get_site_path("private", SNAPSHOT)


def _canon(value):
	"""JSON round trip, so before (loaded from disk) and after compare like for like."""
	return json.loads(json.dumps(value, default=str, sort_keys=True))


def _load():
	with open(_path()) as f:
		return json.load(f)


def _save(snapshot):
	with open(_path(), "w") as f:
		json.dump(snapshot, f, default=str, sort_keys=True, indent=1)


def _statistics():
	rows = frappe.db.sql(
		f"""
		SELECT table_name, index_name, seq_in_index, column_name, sub_part, non_unique, index_type
		FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name IN ({", ".join(["%s"] * len(TABLES))})
		ORDER BY table_name, index_name, seq_in_index
		""",
		TABLES,
	)
	return _canon([list(r) for r in rows if (r[0], r[1]) in OURS])


def _sbb_indexes_leading_with_voucher_no():
	return sorted(
		name
		for name, parts in perf._indexes("tabSerial and Batch Bundle").items()
		if [c for c, _n in parts[:2]] == ["voucher_no", "voucher_type"]
	)


# -- samples and the reads under test ---------------------------------------------------------


def _samples(n):
	"""Newest first, through the ``creation`` index (cheap before the build)."""
	mops, seen = [], set()
	for mop, item in frappe.db.sql(
		"""SELECT manufacturing_operation, item_code FROM `tabMOP Log`
		WHERE is_cancelled = 0 AND manufacturing_operation IS NOT NULL AND item_code IS NOT NULL
		ORDER BY creation DESC LIMIT %s""",
		(n * 50,),
	):
		if mop not in seen and len(mops) < n:
			seen.add(mop)
			mops.append({"mop": mop, "item": item})
	mop_vouchers = [
		{"vt": vt, "vn": vn}
		for vt, vn in frappe.db.sql(
			"""SELECT DISTINCT voucher_type, voucher_no FROM (
				SELECT voucher_type, voucher_no FROM `tabMOP Log`
				WHERE voucher_no IS NOT NULL ORDER BY creation DESC LIMIT %s) t LIMIT %s""",
			(n * 50, n),
		)
	]
	sbb_vouchers = [
		{"vt": vt, "vn": vn}
		for vt, vn in frappe.db.sql(
			"""SELECT DISTINCT voucher_type, voucher_no FROM (
				SELECT voucher_type, voucher_no FROM `tabSerial and Batch Bundle`
				WHERE voucher_type = 'Stock Entry' AND docstatus = 1
				ORDER BY creation DESC LIMIT %s) t LIMIT %s""",
			(n * 20, n),
		)
	]
	return {"mop": mops, "mop_voucher": mop_vouchers, "sbb_voucher": sbb_vouchers}


def _results(samples):
	"""``{label: {"ordered": bool, "rows": [result per sample]}}``, from the real readers or the
	exact SQL they send (get_all rendered with run=0).

	``ordered`` False: the caller uses the list as an IN-list or a set, so it compares as a
	multiset.
	"""
	from erpnext.stock.serial_batch_bundle import get_batchwise_qty

	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
		get_last_mop_index,
	)

	readers = (
		("R1", True, "mop", lambda s: frappe.db.sql(R1_SQL, s, as_dict=True)),
		("R2", True, "mop", lambda s: get_current_mop_balance_rows(s["mop"])),
		("R3", True, "mop", lambda s: get_last_mop_index(s["mop"])),
		(
			"R4 get_value LIMIT 1",
			True,
			"mop",
			lambda s: frappe.db.get_value(
				"MOP Log",
				{
					"manufacturing_operation": s["mop"],
					"item_code": s["item"],
					"is_cancelled": 0,
				},
				"batch_no",
				order_by="flow_index desc, creation desc",
			),
		),
		(
			"R5 DISTINCT MOPs",
			False,
			"mop_voucher",
			lambda s: [r[0] for r in frappe.db.sql(R5_SQL, s)],
		),
		(
			"R5 bridge prefetch",
			False,
			"mop_voucher",
			lambda s: [list(r) for r in frappe.db.sql(str(_r5_prefetch(s)))],
		),
		(
			"R6 outward bundles",
			False,
			"sbb_voucher",
			lambda s: frappe.db.sql(str(_r6(s)), pluck=True),
		),
		(
			"R6 get_batchwise_qty",
			True,
			"sbb_voucher",
			lambda s: get_batchwise_qty(s["vt"], s["vn"]),
		),
		(
			"R7 reserved batches",
			False,
			"sbb_voucher",
			lambda s: frappe.db.sql(str(_r7(s)), pluck=True),
		),
	)
	return {
		label: {"ordered": ordered, "rows": _canon([read(s) for s in samples[kind]])}
		for label, ordered, kind, read in readers
	}


def _explains(samples):
	plans = {}
	for label, (render, kind, _allowed) in EXPLAINS.items():
		if samples[kind]:
			sql, values = render(samples[kind][0])
			# get_all-rendered SQL already carries its literals: send it without values.
			args = (values,) if values is not None else ()
			plans[label] = _canon(frappe.db.sql("EXPLAIN " + sql, *args, as_dict=True))
	return plans


def _multiset(rows):
	return Counter(json.dumps(r, sort_keys=True) for r in rows or [])


def _compare(before, after):
	"""Returns ``(mismatches, order_changes)``, each a list of ``(label, sample index)``."""
	mismatches, order_changes = [], []
	for label, b in before.items():
		a = after[label]
		if len(b["rows"]) != len(a["rows"]):
			mismatches.append((label, "sample count"))
		for i, (rb, ra) in enumerate(zip(b["rows"], a["rows"])):
			if rb == ra:
				continue
			if not b["ordered"] and _multiset(rb) == _multiset(ra):
				order_changes.append((label, i))
			else:
				mismatches.append((label, i))
	return mismatches, order_changes


# -- phases -----------------------------------------------------------------------------------


def capture(sample=300, reset=False):
	"""Before the build: sample IDs, EXPLAIN R1-R7, and their result sets, saved to disk."""
	_guard()
	present = [n for t, n, _c in perf.SPECS if n in perf._indexes(t)]
	if present and not reset:
		raise AssertionError(
			f"already present: {present}; pass reset=1 to drop them first ({SITE} only)"
		)
	for table, name, _cols in perf.SPECS:
		if name in perf._indexes(table):
			frappe.db.sql_ddl(
				f"ALTER TABLE `{table}` DROP INDEX `{name}`, {perf.ONLINE}"
			)

	samples = _samples(int(sample))
	snapshot = {
		"samples": samples,
		"explain_before": _explains(samples),
		"results_before": _results(samples),
	}
	for label, plan in snapshot["explain_before"].items():
		used = {row.get("key") for row in plan}
		assert not used & {n for _t, n, _c in perf.SPECS}, (label, used)
	_save(snapshot)
	return {
		"samples": {k: len(v) for k, v in samples.items()},
		"explain_before": {
			k: [(r["type"], r["key"], r["rows"]) for r in v]
			for k, v in snapshot["explain_before"].items()
		},
	}


def build(lock_wait=5):
	"""Run the real ``ensure`` and record its report (status and seconds per index)."""
	_guard()
	report = perf.ensure(lock_wait=lock_wait)
	bad = [
		(r["index"], r["status"])
		for r in report
		if r["status"] not in ("built", "present", "renamed")
	]
	assert not bad, bad
	snapshot = _load()
	snapshot["build_report"] = _canon(report)
	_save(snapshot)
	return report


def verify():
	"""After the build: EXPLAIN uses the new indexes; results equal; STATISTICS snapshot saved."""
	_guard()
	snapshot = _load()
	samples = snapshot["samples"]

	plans = _explains(samples)
	wrong = {}
	for label, plan in plans.items():
		allowed = EXPLAINS[label][2]
		row = plan[0]
		if row["key"] not in allowed or row["type"] not in GOOD_ACCESS:
			wrong[label] = (row["type"], row["key"], row["rows"])
	assert not wrong, f"plans not on the new indexes: {wrong}"

	mismatches, order_changes = _compare(snapshot["results_before"], _results(samples))
	assert not mismatches, f"result sets differ: {mismatches[:20]}"

	assert [r["status"] for r in perf.ensure(dry_run=True, verbose=False)] == [
		"present"
	] * len(perf.SPECS)
	snapshot["explain_after"] = plans
	snapshot["statistics_after_build"] = _statistics()
	_save(snapshot)
	return {
		"explain_after": {
			k: [(r["type"], r["key"], r["rows"]) for r in v] for k, v in plans.items()
		},
		"order_changes_in_multiset_lists": order_changes,
	}


def verify_after_migrate():
	"""After ``bench migrate``: indexes unchanged, also through updatedb and ERPNext's add_index."""
	_guard()
	snapshot = _load()
	expected = snapshot["statistics_after_build"]
	assert _statistics() == expected, "STATISTICS changed by migrate"

	report = perf.ensure(verbose=False)
	assert [r["status"] for r in report] == ["present"] * len(perf.SPECS), report

	for doctype in ("MOP Log", "Serial and Batch Bundle", "Serial and Batch Entry"):
		frappe.db.updatedb(doctype)
	assert _statistics() == expected, "STATISTICS changed by updatedb"

	frappe.db.add_index("Serial and Batch Bundle", ["voucher_no", "voucher_type"])
	assert _sbb_indexes_leading_with_voucher_no() == [
		"voucher_no_voucher_type_index"
	], _sbb_indexes_leading_with_voucher_no()
	assert _statistics() == expected, "STATISTICS changed by add_index"

	mismatches, _order = _compare(
		snapshot["results_before"], _results(snapshot["samples"])
	)
	assert not mismatches, f"result sets differ after migrate: {mismatches[:20]}"
	return {"statistics": "unchanged", "ensure": "no DDL", "results": "equal"}


def verify_legacy():
	"""LEGACY on SBE: rename when alone, drop when it duplicates the upstream name. Leaves the
	upstream-named index in place."""
	_guard()
	table, name, legacy = (
		"tabSerial and Batch Entry",
		"voucher_no_voucher_type_index",
		"sbe_voucher_no_idx",
	)
	assert name in perf._indexes(table), "run build first"

	frappe.db.sql_ddl(
		f"ALTER TABLE `{table}` RENAME INDEX `{name}` TO `{legacy}`, {perf.ONLINE}"
	)
	status = {
		r["index"]: r["status"]
		for r in perf.ensure(verbose=False)
		if r["table"] == table
	}
	assert status == {name: "renamed"}, status
	assert legacy not in perf._indexes(table)

	frappe.db.sql_ddl(
		f"ALTER TABLE `{table}` ADD INDEX `{legacy}` (`voucher_no`, `voucher_type`), {perf.ONLINE}"
	)
	status = {
		r["index"]: r["status"]
		for r in perf.ensure(verbose=False)
		if r["table"] == table
	}
	assert status == {name: "dropped-duplicate"}, status
	assert legacy not in perf._indexes(table) and name in perf._indexes(table)
	return {"renamed": "ok", "dropped-duplicate": "ok"}
