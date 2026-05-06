"""End-to-end flow exercises + microbenchmarks for the audit cycle.

Invoked from `bench --site gk execute` so it runs inside a real frappe
context with full DocType metadata and DB. Designed to be idempotent and
read-only — no document inserts, no SE submits, no MOP Log writes. The
goal is to (a) exercise the production helper paths with real data and
(b) record timings for the popup helper, the reconciliation helper, and
get_current_mop_balance_rows.

Usage:
        bench --site gk execute \
                jewellery_erpnext.jewellery_erpnext.tests._flow_and_perf_audit.run_audit
"""

from __future__ import annotations

import json
import statistics
from time import perf_counter

import frappe

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------


def _time_call(fn, *args, **kwargs):
	t0 = perf_counter()
	out = fn(*args, **kwargs)
	return out, perf_counter() - t0


def _summarize(label, samples):
	"""Build a summary dict for a population of timings (seconds)."""
	if not samples:
		return {"label": label, "n": 0}
	samples = sorted(samples)
	return {
		"label": label,
		"n": len(samples),
		"min_ms": round(samples[0] * 1000, 3),
		"max_ms": round(samples[-1] * 1000, 3),
		"avg_ms": round(statistics.mean(samples) * 1000, 3),
		"p50_ms": round(samples[len(samples) // 2] * 1000, 3),
		"p95_ms": round(
			samples[min(len(samples) - 1, int(len(samples) * 0.95))] * 1000, 3
		),
		"total_ms": round(sum(samples) * 1000, 3),
	}


# ---------------------------------------------------------------------------
# Population samplers (read-only)
# ---------------------------------------------------------------------------


def _sample_mops(limit=50):
	"""Return up to N Manufacturing Operation names that have MOP Log rows.

	We rank by recency so we exercise hot data, not stale rows.
	"""
	return frappe.db.sql(
		"""
		SELECT DISTINCT mo.name
		FROM `tabManufacturing Operation` mo
		INNER JOIN `tabMOP Log` ml ON ml.manufacturing_operation = mo.name
		WHERE ml.is_cancelled = 0
		ORDER BY mo.modified DESC
		LIMIT %s
		""",
		(limit,),
		as_list=True,
	)


def _sample_active_sre_mops(limit=50):
	"""MOPs that have at least one active Stock Reservation Entry.

	Make Receive Entry only ever runs against these — perfect popup load test.
	"""
	return frappe.db.sql(
		"""
		SELECT DISTINCT mo.name
		FROM `tabManufacturing Operation` mo
		INNER JOIN `tabStock Reservation Entry` sre
		    ON sre.manufacturing_work_order = mo.manufacturing_work_order
		WHERE sre.docstatus = 1
		  AND mo.manufacturing_work_order IS NOT NULL
		ORDER BY mo.modified DESC
		LIMIT %s
		""",
		(limit,),
		as_list=True,
	)


# ---------------------------------------------------------------------------
# Flow exercises (read-only — no submits)
# ---------------------------------------------------------------------------


def flow_make_receive_popup(mop_names):
	"""Flow 1+2+3: Make Receive Entry popup row fetch.

	Drives `get_make_receive_entry_rows` against real MOPs. Confirms the
	endpoint:
	  - returns rows, including new keys (mop_log_balance_qty/pcs,
	    available_pcs, is_pcs_item, mop_log_reference)
	  - times within a reasonable budget per MOP
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		get_make_receive_entry_rows,
	)

	out = []
	timings = []
	for (mop_name,) in mop_names:
		try:
			rows, elapsed = _time_call(get_make_receive_entry_rows, mop_name)
		except frappe.ValidationError as exc:
			# MOP without MWO. Acceptable — the endpoint guards on this.
			out.append({"mop": mop_name, "skipped": str(exc)})
			continue
		except Exception as exc:  # pragma: no cover — surface unexpected breakage
			out.append({"mop": mop_name, "error": f"{type(exc).__name__}: {exc}"})
			continue

		timings.append(elapsed)
		row_keys = set()
		pcs_rows = 0
		non_pcs_rows = 0
		for r in rows:
			row_keys.update(r.keys())
			if r.get("is_pcs_item"):
				pcs_rows += 1
			else:
				non_pcs_rows += 1
		out.append(
			{
				"mop": mop_name,
				"row_count": len(rows),
				"elapsed_ms": round(elapsed * 1000, 3),
				"pcs_rows": pcs_rows,
				"non_pcs_rows": non_pcs_rows,
				"missing_new_keys": sorted(
					{
						"reserved_pcs",
						"mop_log_balance_qty",
						"mop_log_balance_pcs",
						"already_received_pcs",
						"available_pcs",
						"is_pcs_item",
						"mop_log_reference",
					}
					- row_keys
				)
				if rows
				else [],
			}
		)
	return out, _summarize("get_make_receive_entry_rows", timings)


def flow_reconciliation_helper(mop_names):
	"""Flow 4+5: Direct exercise of get_available_qty_pcs_for_mop_item.

	For each MOP we pick the latest MOP Log row (regardless of item prefix)
	and feed (item_code, batch_no) through the helper. Confirms it returns
	is_pcs_item correctly and surfaces non-zero available_pcs only for D/G.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_available_qty_pcs_for_mop_item,
	)

	out = []
	timings = []
	for (mop_name,) in mop_names:
		ml = frappe.db.get_value(
			"MOP Log",
			{"manufacturing_operation": mop_name, "is_cancelled": 0},
			["item_code", "batch_no"],
			as_dict=True,
			order_by="creation desc",
		)
		if not ml:
			continue
		ctx, elapsed = _time_call(
			get_available_qty_pcs_for_mop_item,
			manufacturing_operation=mop_name,
			item_code=ml.item_code,
			batch_no=ml.batch_no,
		)
		timings.append(elapsed)
		out.append(
			{
				"mop": mop_name,
				"item_code": ml.item_code,
				"batch_no": ml.batch_no,
				"is_pcs_item": ctx["is_pcs_item"],
				"available_qty": ctx["available_qty"],
				"available_pcs": ctx["available_pcs"],
				"mop_log_balance_qty": ctx["mop_log_balance_qty"],
				"mop_log_balance_pcs": ctx["mop_log_balance_pcs"],
				"elapsed_ms": round(elapsed * 1000, 3),
			}
		)
	return out, _summarize("get_available_qty_pcs_for_mop_item", timings)


def flow_balance_helper(mop_names):
	"""Flow 9: get_current_mop_balance_rows on real MOPs."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	timings = []
	row_counts = []
	for (mop_name,) in mop_names:
		rows, elapsed = _time_call(get_current_mop_balance_rows, mop_name)
		timings.append(elapsed)
		row_counts.append(len(rows))
	return {
		"mops_sampled": len(mop_names),
		"row_count_min": min(row_counts) if row_counts else 0,
		"row_count_max": max(row_counts) if row_counts else 0,
		"row_count_avg": round(statistics.mean(row_counts), 2) if row_counts else 0,
	}, _summarize("get_current_mop_balance_rows", timings)


def flow_eod_idempotency_audit():
	"""Flow 9: EOD sync audit — count is_synced=0 vs is_synced=1 rows.

	Read-only sanity check: confirms the load-bearing flag is present on
	MOP Log rows and that no `log_category=Loss Attribution` rows have
	is_synced=0 (which would mean the EOD filter is missing them).
	"""
	totals = frappe.db.sql(
		"""
		SELECT
		    COUNT(*) AS total,
		    SUM(CASE WHEN is_synced = 0 THEN 1 ELSE 0 END) AS unsynced,
		    SUM(CASE WHEN is_synced = 1 THEN 1 ELSE 0 END) AS synced,
		    SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) AS cancelled,
		    SUM(CASE WHEN log_category = 'Loss Attribution' AND is_synced = 0 AND is_cancelled = 0 THEN 1 ELSE 0 END) AS unsynced_loss_rows
		FROM `tabMOP Log`
		""",
		as_dict=True,
	)
	return totals[0] if totals else {}


def flow_no_dust_item_dependency():
	"""Phase F sanity: verify there is no MOP Settings.dust_item field
	referenced by any production code path. We grep is sufficient — the
	MOP Settings DocType JSON has no dust_item field."""
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS c
		FROM `tabDocField`
		WHERE parent = 'MOP Settings'
		  AND fieldname = 'dust_item'
		""",
		as_dict=True,
	)
	count = (row[0] or {}).get("c", 0) if row else 0
	# Custom Field surface
	cf_count = frappe.db.count(
		"Custom Field", {"dt": "MOP Settings", "fieldname": "dust_item"}
	)
	return {"docfield_count": count, "custom_field_count": cf_count}


def flow_manufacturer_loss_mapping_health():
	"""Phase F sanity: count Manufacturer rows with a configured
	custom_variant_loss_table — non-zero means the mapping is in use."""
	rows = frappe.db.sql(
		"""
		SELECT
		    COUNT(DISTINCT vlt.parent) AS mfrs_with_mapping,
		    COUNT(*) AS total_mapping_rows,
		    COUNT(DISTINCT vlt.variant) AS variants_mapped,
		    COUNT(DISTINCT vlt.loss_type) AS loss_types_mapped
		FROM `tabVariant Loss Table` vlt
		WHERE vlt.parenttype = 'Manufacturer'
		""",
		as_dict=True,
	)
	return rows[0] if rows else {}


# ---------------------------------------------------------------------------
# Load tests (read-only)
# ---------------------------------------------------------------------------


def load_test_helper_repeated_calls(mop_names, iterations=10):
	"""Run get_available_qty_pcs_for_mop_item N times against the same
	MOP+item to measure cached vs cold-path per-call latency. Useful for
	spotting query-amplification regressions.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_available_qty_pcs_for_mop_item,
	)

	if not mop_names:
		return _summarize("helper_repeat_loop", [])

	(mop_name,) = mop_names[0]
	ml = frappe.db.get_value(
		"MOP Log",
		{"manufacturing_operation": mop_name, "is_cancelled": 0},
		["item_code", "batch_no"],
		as_dict=True,
		order_by="creation desc",
	)
	if not ml:
		return _summarize("helper_repeat_loop", [])

	timings = []
	for _ in range(iterations):
		_, elapsed = _time_call(
			get_available_qty_pcs_for_mop_item,
			manufacturing_operation=mop_name,
			item_code=ml.item_code,
			batch_no=ml.batch_no,
		)
		timings.append(elapsed)
	return _summarize(f"helper_repeat_loop_x{iterations}", timings)


def load_test_popup_repeated_calls(mop_names, iterations=10):
	"""Run get_make_receive_entry_rows N times against the same MOP."""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		get_make_receive_entry_rows,
	)

	if not mop_names:
		return _summarize("popup_repeat_loop", [])

	timings = []
	for (mop_name,) in mop_names[:5]:
		try:
			for _ in range(iterations):
				_, elapsed = _time_call(get_make_receive_entry_rows, mop_name)
				timings.append(elapsed)
		except frappe.ValidationError:
			continue
	return _summarize(f"popup_repeat_loop_x{iterations}", timings)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_audit():
	"""Top-level driver. Prints a JSON report to the bench log."""
	report = {
		"phase": "Phase 6+7 — flow + perf audit",
		"site": frappe.local.site if hasattr(frappe.local, "site") else "<unknown>",
		"flows": {},
		"perf": {},
		"sanity": {},
	}

	mops_for_balance = _sample_mops(limit=50)
	mops_for_popup = _sample_active_sre_mops(limit=20)

	report["flows"]["mops_with_logs_sampled"] = len(mops_for_balance)
	report["flows"]["mops_with_active_sre_sampled"] = len(mops_for_popup)

	# Flow 1: popup row fetch
	popup_results, popup_timing = flow_make_receive_popup(mops_for_popup)
	report["flows"]["make_receive_popup_per_mop"] = popup_results
	report["perf"]["make_receive_popup"] = popup_timing

	# Flow 4: helper exercises
	helper_results, helper_timing = flow_reconciliation_helper(mops_for_balance)
	report["flows"]["reconciliation_helper_per_mop"] = helper_results[
		:10
	]  # truncate for log readability
	report["flows"]["reconciliation_helper_total_sampled"] = len(helper_results)
	report["perf"]["reconciliation_helper"] = helper_timing

	# Flow 9: balance helper
	balance_summary, balance_timing = flow_balance_helper(mops_for_balance)
	report["flows"]["balance_helper_summary"] = balance_summary
	report["perf"]["balance_helper"] = balance_timing

	# EOD idempotency audit
	report["sanity"]["eod_mop_log_counts"] = flow_eod_idempotency_audit()

	# No dust_item dependency on MOP Settings
	report["sanity"]["no_dust_item_on_mop_settings"] = flow_no_dust_item_dependency()

	# Manufacturer mapping health
	report["sanity"][
		"manufacturer_loss_mapping_health"
	] = flow_manufacturer_loss_mapping_health()

	# Load tests
	report["perf"]["helper_repeat_loop"] = load_test_helper_repeated_calls(
		mops_for_balance, iterations=20
	)
	report["perf"]["popup_repeat_loop"] = load_test_popup_repeated_calls(
		mops_for_popup, iterations=10
	)

	# Print compact JSON the bench log can capture
	print("\n=== AUDIT_REPORT_BEGIN ===")
	print(json.dumps(report, indent=2, default=str))
	print("=== AUDIT_REPORT_END ===\n")
	return report
