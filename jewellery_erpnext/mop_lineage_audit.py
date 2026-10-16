# Copyright (c) 2026, Nirali and contributors
# SPDX-License-Identifier: MIT
"""Operational audit helpers for Department IR ↔ MOP Log verification.

Run on a live site (operator / support):

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.run_all_audits

Optional kwargs for `bench execute` (Frappe 15):

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.run_all_audits --kwargs "{'receive_doc': 'DIR-RECV-00001'}"

Strict Receive (no tail fallback): set in ``site_config.json``:

    "department_ir_receive_strict_lineage": 1

When enabled, Department IR Receive submit raises if Issue voucher MOP Log rows are missing
instead of cloning the tail snapshot (see ``create_mop_log_for_department_ir``).

**Proof pack (issue families):**

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.run_proof_pack_audits

**Server Script bodies (for manual review / archive):**

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.run_server_script_review_bundle --kwargs "{'preview_chars': 6000}"

**Negative batch balances (ledger corruption sweep):**

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.audit_negative_batch_balances

**Work-order current-operation conflicts (read-only; stale Employee IR incident 2026-10-05):**

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.audit_current_operation_conflicts
    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.build_stale_issue_manifest \\
        --kwargs "{'employee_ir': 'EMP-IR-Labh-2026-43405'}"
    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.save_stale_issue_manifests \\
        --kwargs "{'employee_irs': ['EMP-IR-Labh-2026-43405'], 'out_dir': '/path/to/manifests'}"

The repair that consumes the manifests is ``patches/repair_current_operation_conflicts.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, now_datetime, strip_html

from jewellery_erpnext.utils import carat_to_gram, clamp_negative_balance

# row_name stamped on correcting rows appended by
# patches/repair_mwo_wide_mop_log_balances.py, so they stay identifiable and
# re-runs are no-ops.
REPAIR_ROW_TAG = "repair-mwo-wide-balance"


def _app_root() -> Path:
	return Path(__file__).resolve().parent


def get_deployment_parity_record() -> dict:
	"""Record git identity of this app checkout for parity with production."""
	root = _app_root()
	try:
		rev = subprocess.check_output(
			["git", "rev-parse", "HEAD"],
			cwd=root,
			text=True,
			stderr=subprocess.DEVNULL,
		).strip()
	except (OSError, subprocess.CalledProcessError):
		rev = None
	try:
		short = subprocess.check_output(
			["git", "log", "-1", "--oneline"],
			cwd=root,
			text=True,
			stderr=subprocess.DEVNULL,
		).strip()
	except (OSError, subprocess.CalledProcessError):
		short = None
	files = [
		root / "jewellery_erpnext" / "doctype" / "mop_log" / "mop_log.py",
		root / "jewellery_erpnext" / "doctype" / "department_ir" / "department_ir.py",
	]
	markers = {}
	for rel in files:
		try:
			text = rel.read_text(encoding="utf-8", errors="replace")
		except OSError:
			markers[str(rel.name)] = {"readable": False}
			continue
		markers[rel.name] = {
			"readable": True,
			"has_receive_against_clone": '"voucher_no": self.receive_against' in text,
			"has_max_issue_flow_slice": "max_issue_flow" in text,
			"has_dir_receive_idempotency": (
				'"voucher_type": "Department IR"' in text
				and '"voucher_no": self.name' in text
			),
			"has_validate_receive_lineage": "validate_receive_lineage" in text,
		}
	return {
		"app_root": str(root),
		"git_head": rev,
		"git_log_1": short,
		"expected_source_markers": markers,
		"compare_on_production": "Run the same execute on production and diff git_head + markers.",
	}


def sql_mop_log_lineage_proof(
	issue_name: str | None,
	receive_name: str | None,
	manufacturing_operation: str | None,
) -> str:
	"""Safe printable SQL for the Issue / Receive / MOP lineage slice (uses frappe.db.escape)."""
	clauses: list[str] = []
	if issue_name:
		clauses.append(
			f"(ml.voucher_type = 'Department IR' AND ml.voucher_no = {frappe.db.escape(issue_name)})"
		)
	if receive_name:
		clauses.append(
			f"(ml.voucher_type = 'Department IR' AND ml.voucher_no = {frappe.db.escape(receive_name)})"
		)
	if manufacturing_operation:
		clauses.append(
			f"(ml.manufacturing_operation = {frappe.db.escape(manufacturing_operation)})"
		)
	if not clauses:
		return "-- provide at least one of issue_name, receive_name, manufacturing_operation"
	or_expr = " OR ".join(clauses)
	return f"""
SELECT
  ml.name, ml.creation, ml.modified, ml.owner,
  ml.voucher_type, ml.voucher_no, ml.row_name,
  ml.manufacturing_operation, ml.manufacturing_work_order,
  ml.from_warehouse, ml.to_warehouse,
  ml.item_code, ml.batch_no, ml.flow_index,
  ml.qty_change, ml.pcs_change,
  ml.qty_after_transaction, ml.qty_after_transaction_item_based, ml.qty_after_transaction_batch_based,
  ml.is_cancelled, ml.is_synced
FROM `tabMOP Log` ml
WHERE ml.is_cancelled = 0
  AND ({or_expr})
ORDER BY ml.manufacturing_operation, ml.flow_index, ml.creation;
""".strip()


def audit_active_server_scripts() -> list[dict]:
	"""List enabled Server Scripts tied to hot-path doctypes or mentioning them in code."""
	rows = frappe.db.sql(
		"""
		SELECT name, script_type, reference_doctype, doctype_event, disabled
		FROM `tabServer Script`
		WHERE disabled = 0
		  AND script_type IN ('DocType Event', 'API', 'Scheduler Event', 'Permission Query')
		  AND (
			reference_doctype IN (
				%(d1)s, %(d2)s, %(d3)s, %(d4)s, %(d5)s
			)
			OR script LIKE %(like_dir)s
			OR script LIKE %(like_mop)s
			OR script LIKE %(like_mo)s
		  )
		ORDER BY reference_doctype, name
		""",
		{
			"d1": "Department IR",
			"d2": "MOP Log",
			"d3": "Manufacturing Operation",
			"d4": "Stock Entry",
			"d5": "Employee IR",
			"like_dir": "%Department IR%",
			"like_mop": "%MOP Log%",
			"like_mo": "%Manufacturing Operation%",
		},
		as_dict=True,
	)
	return rows or []


def audit_submission_queue_department_ir_duplicates() -> list[dict]:
	"""Rows where more than one non-cancelled queue row exists per Department IR ref."""
	return (
		frappe.db.sql(
			"""
			SELECT ref_doctype, ref_docname, status, COUNT(*) AS cnt
			FROM `tabSubmission Queue`
			WHERE ref_doctype = 'Department IR'
			  AND status IN ('Queued', 'Finished', 'Started')
			GROUP BY ref_doctype, ref_docname, status
			HAVING cnt > 1
			ORDER BY cnt DESC
			LIMIT 50
			""",
			as_dict=True,
		)
		or []
	)


def audit_error_log_dir_fallback(limit: int = 30) -> list[dict]:
	return (
		frappe.get_all(
			"Error Log",
			filters=[["error", "like", "%DIR Receive missing Issue logs%"]],
			fields=["name", "creation", "method", "error"],
			order_by="creation desc",
			limit_page_length=limit,
		)
		or []
	)


def _latest_submitted_receive() -> dict | None:
	row = frappe.db.sql(
		"""
		SELECT name AS receive_name, receive_against AS issue_name, modified
		FROM `tabDepartment IR`
		WHERE docstatus = 1 AND type = 'Receive' AND IFNULL(receive_against,'') != ''
		ORDER BY modified DESC
		LIMIT 1
		""",
		as_dict=True,
	)
	return row[0] if row else None


def _mops_for_receive(receive_name: str) -> list[str]:
	return (
		frappe.db.sql(
			"""
		SELECT DISTINCT manufacturing_operation
		FROM `tabDepartment IR Operation`
		WHERE parent = %(p)s AND IFNULL(manufacturing_operation,'') != ''
		""",
			{"p": receive_name},
			pluck="manufacturing_operation",
		)
		or []
	)


def get_sql_proof_templates() -> dict[str, str]:
	"""Static SQL templates for DBA / staging archives (no site-specific escaping)."""
	return {
		"dir_duplicate_department_ir_mop_logs": """
-- Rows: duplicate virtual-key Department IR MOP Log lines (same voucher + mop + tier + item + batch)
SELECT
  ml.voucher_type,
  ml.voucher_no,
  ml.manufacturing_operation,
  ml.flow_index,
  ml.item_code,
  IFNULL(ml.batch_no, '') AS batch_key,
  COUNT(*) AS row_cnt,
  GROUP_CONCAT(ml.name ORDER BY ml.creation) AS mop_log_names
FROM `tabMOP Log` ml
WHERE ml.is_cancelled = 0
  AND ml.voucher_type = 'Department IR'
GROUP BY
  ml.voucher_type, ml.voucher_no, ml.manufacturing_operation, ml.flow_index,
  ml.item_code, IFNULL(ml.batch_no, '')
HAVING row_cnt > 1
ORDER BY row_cnt DESC
LIMIT 100;
""".strip(),
		"stock_entry_multiple_mops_same_voucher": """
-- Rows: one Stock Entry voucher_no on MOP Log pointing at more than one Manufacturing Operation
SELECT
  ml.voucher_no AS stock_entry,
  COUNT(DISTINCT ml.manufacturing_operation) AS distinct_mop_cnt,
  GROUP_CONCAT(DISTINCT ml.manufacturing_operation ORDER BY ml.manufacturing_operation) AS mops
FROM `tabMOP Log` ml
WHERE ml.is_cancelled = 0
  AND ml.voucher_type = 'Stock Entry'
  AND IFNULL(ml.voucher_no, '') != ''
GROUP BY ml.voucher_no
HAVING distinct_mop_cnt > 1
ORDER BY distinct_mop_cnt DESC
LIMIT 100;
""".strip(),
		"snc_submitted_empty_source_table": """
-- Rows: submitted Serial Number Creator with zero SNC Source Table children (raw-material visibility family)
SELECT
  snc.name,
  snc.docstatus,
  snc.manufacturing_work_order,
  snc.manufacturing_operation,
  snc.modified,
  COUNT(st.name) AS source_row_cnt
FROM `tabSerial Number Creator` snc
LEFT JOIN `tabSNC Source Table` st ON st.parent = snc.name
WHERE snc.docstatus = 1
GROUP BY snc.name, snc.docstatus, snc.manufacturing_work_order, snc.manufacturing_operation, snc.modified
HAVING source_row_cnt = 0
ORDER BY snc.modified DESC
LIMIT 100;
""".strip(),
		"pmo_submitted_recent_slice": """
-- Rows: recent submitted Parent Manufacturing Order header slice (extend with BOM/item joins per your PMO schema)
SELECT
  pmo.name,
  pmo.docstatus,
  pmo.item_code,
  pmo.manufacturing_order,
  pmo.modified
FROM `tabParent Manufacturing Order` pmo
WHERE pmo.docstatus = 1
ORDER BY pmo.modified DESC
LIMIT 50;
""".strip(),
		"submission_queue_department_ir_timeline": """
-- Rows: Department IR refs with multiple queue rows (any status) — timeline for replay investigation
SELECT
  sq.ref_docname AS department_ir,
  COUNT(*) AS queue_row_cnt,
  GROUP_CONCAT(
    CONCAT(IFNULL(sq.status,''), ':', IFNULL(sq.creation,'')) ORDER BY sq.creation SEPARATOR ' | '
  ) AS status_creation_chain
FROM `tabSubmission Queue` sq
WHERE sq.ref_doctype = 'Department IR'
GROUP BY sq.ref_docname
HAVING queue_row_cnt > 1
ORDER BY queue_row_cnt DESC
LIMIT 100;
""".strip(),
	}


def _sql_with_limit(sql: str, limit: int) -> str:
	"""Normalize trailing LIMIT … on a single-statement SQL fragment."""
	s = sql.strip().rstrip(";")
	lim = max(1, min(int(limit), 5000))
	if re.search(r"(?i)\blimit\s+\d+\s*$", s):
		return re.sub(r"(?i)\blimit\s+\d+\s*$", f"LIMIT {lim}", s)
	return f"{s} LIMIT {lim}"


def run_proof_query_pack(limit: int = 50) -> dict:
	"""Execute proof SQL against the current site; safe read-only checks."""
	out: dict = {"templates": get_sql_proof_templates(), "results": {}}
	queries = {
		"dir_duplicate_department_ir_mop_logs": out["templates"][
			"dir_duplicate_department_ir_mop_logs"
		],
		"stock_entry_multiple_mops_same_voucher": out["templates"][
			"stock_entry_multiple_mops_same_voucher"
		],
		"snc_submitted_empty_source_table": out["templates"][
			"snc_submitted_empty_source_table"
		],
		"pmo_submitted_recent_slice": out["templates"]["pmo_submitted_recent_slice"],
		"submission_queue_department_ir_timeline": out["templates"][
			"submission_queue_department_ir_timeline"
		],
	}
	for key, sql in queries.items():
		try:
			rows = frappe.db.sql(_sql_with_limit(sql, limit), as_dict=True)
			out["results"][key] = {"count": len(rows or []), "rows": rows or []}
		except Exception as e:
			out["results"][key] = {"error": str(e), "count": 0, "rows": []}
	return out


def run_proof_pack_audits(limit: int = 50) -> dict:
	"""Bench entry: templates + executed proof queries + parity + DIR fallback errors."""
	base = run_all_audits()
	base["proof_pack"] = run_proof_query_pack(limit=limit)
	base[
		"stock_entry_legacy_balance_trace"
	] = get_stock_entry_legacy_balance_table_trace()
	base["proof_pack"][
		"archive_hint"
	] = "Save this JSON from bench output to your ticket / evidence store; re-run after each deploy."
	return base


def audit_server_scripts_with_preview(preview_chars: int = 4000) -> list[dict]:
	"""Return enabled hot-path Server Scripts including a script body preview for manual review."""
	preview_chars = max(500, min(int(preview_chars), 50000))
	rows = frappe.db.sql(
		"""
		SELECT name, script_type, reference_doctype, doctype_event, disabled,
		       CHAR_LENGTH(script) AS script_length,
		       SUBSTRING(script, 1, %(pc)s) AS script_preview
		FROM `tabServer Script`
		WHERE disabled = 0
		  AND script_type IN ('DocType Event', 'API', 'Scheduler Event', 'Permission Query')
		  AND (
			reference_doctype IN (
				%(d1)s, %(d2)s, %(d3)s, %(d4)s, %(d5)s
			)
			OR script LIKE %(like_dir)s
			OR script LIKE %(like_mop)s
			OR script LIKE %(like_mo)s
		  )
		ORDER BY reference_doctype, name
		""",
		{
			"pc": preview_chars,
			"d1": "Department IR",
			"d2": "MOP Log",
			"d3": "Manufacturing Operation",
			"d4": "Stock Entry",
			"d5": "Employee IR",
			"like_dir": "%Department IR%",
			"like_mop": "%MOP Log%",
			"like_mo": "%Manufacturing Operation%",
		},
		as_dict=True,
	)
	return rows or []


def run_server_script_review_bundle(preview_chars: int = 4000) -> dict:
	"""Bench entry: script list + previews for operator review (not an automated security scan)."""
	return {
		"parity": get_deployment_parity_record(),
		"server_scripts_with_preview": audit_server_scripts_with_preview(preview_chars),
		"review_checklist": [
			"Confirm each script does not write MOP Log / Stock Entry in a way that duplicates app hooks.",
			"Search previews for mop_balance_table, doc.save without guard, frappe.db.commit.",
			"Match event (Before Submit vs After Submit) to intended side effects.",
		],
	}


def get_stock_entry_legacy_balance_table_trace() -> dict:
	"""Documentation-only trace for ``stock_entry.update_mop_details`` / ``update_balance_table`` (no DB)."""
	return {
		"entrypoints": [
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.update_manufacturing_operation",
			"-> update_mop_details(se_doc, is_cancelled=...)",
			"-> update_balance_table(mop_data) when any department_/employee_ tables non-empty",
		],
		"legacy_keys_in_mop_data": [
			"department_source_table",
			"department_target_table",
			"employee_source_table",
			"employee_target_table",
		],
		"behavior": (
			"update_balance_table loads Manufacturing Operation and calls mop_doc.append(table, row) "
			"for each non-empty list. Child row dicts are shaped from Stock Entry Detail __dict__ plus sed_item."
		),
		"schema_warning": (
			"Repository manufacturing_operation.json may not define these child table fieldnames; if the "
			"fields are absent on a site without Custom Fields, append/save can fail. Confirm schema on each site."
		),
		"cancel_path": (
			"When is_cancelled=True, update_mop_details deletes rows from standalone doctypes "
			"Department Source Table / Department Target Table / Employee Source Table / Employee Target Table "
			"linked by sed_item = Stock Entry Detail name."
		),
	}


def audit_employee_ir_diamond_lineage(
	employee_ir_receive: str | None = None,
	manufacturing_operation: str | None = None,
) -> dict:
	"""Diamond / gemstone lineage trace for an Employee IR Receive case.

	Targets the failing pattern: Material Request transfers diamond onto a Manufacturing
	Operation, Employee IR Issue is submitted, but the Receive entry does not show the
	diamond weight. Compares the four sources of truth that should agree:

	1. ``Manufacturing Operation`` header (``diamond_wt`` / ``diamond_pcs``).
	2. ``MOP Log`` current balance snapshot (``D%`` / ``G%`` lines).
	3. ``MOP Log`` lines cloned by the matching Employee IR Issue voucher.
	4. ``Stock Entry Detail`` rows posted against the MOP for diamond / gemstone items.
	"""
	if employee_ir_receive and not manufacturing_operation:
		manufacturing_operation = frappe.db.get_value(
			"Employee IR Operation",
			{"parent": employee_ir_receive},
			"manufacturing_operation",
		)
	if not manufacturing_operation:
		return {"error": "Provide employee_ir_receive or manufacturing_operation"}

	receive_meta = (
		frappe.db.get_value(
			"Employee IR",
			employee_ir_receive,
			["name", "type", "docstatus", "emp_ir_id", "operation", "department"],
			as_dict=True,
		)
		if employee_ir_receive
		else None
	)

	issue_voucher = None
	if receive_meta and receive_meta.get("emp_ir_id"):
		issue_voucher = receive_meta["emp_ir_id"]
	if not issue_voucher:
		row = frappe.db.sql(
			"""
			SELECT eir.name
			FROM `tabEmployee IR` eir
			JOIN `tabEmployee IR Operation` op ON op.parent = eir.name
			WHERE eir.docstatus = 1 AND eir.type = 'Issue'
			  AND op.manufacturing_operation = %s
			ORDER BY eir.modified DESC LIMIT 1
			""",
			(manufacturing_operation,),
		)
		issue_voucher = row[0][0] if row else None

	mop_header = frappe.db.get_value(
		"Manufacturing Operation",
		manufacturing_operation,
		[
			"name",
			"manufacturing_work_order",
			"department",
			"status",
			"gross_wt",
			"net_wt",
			"diamond_wt",
			"diamond_wt_in_gram",
			"diamond_pcs",
			"gemstone_wt",
			"gemstone_pcs",
		],
		as_dict=True,
	)

	def _mop_log_rows(extra_filter: str = "", params: tuple = ()) -> list[dict]:
		return (
			frappe.db.sql(
				f"""
				SELECT name, creation, voucher_type, voucher_no, row_name,
				       item_code, batch_no, flow_index,
				       qty_change, qty_after_transaction_batch_based AS qty_after,
				       pcs_change, pcs_after_transaction_batch_based AS pcs_after,
				       from_warehouse, to_warehouse, is_synced
				FROM `tabMOP Log`
				WHERE manufacturing_operation = %s AND is_cancelled = 0 {extra_filter}
				ORDER BY flow_index, creation
				""",
				(manufacturing_operation, *params),
				as_dict=True,
			)
			or []
		)

	all_logs = _mop_log_rows()
	diamond_logs = [
		row
		for row in all_logs
		if row.get("item_code") and row["item_code"][0] in ("D", "G")
	]
	issue_logs = (
		_mop_log_rows(
			"AND voucher_type = 'Employee IR' AND voucher_no = %s", (issue_voucher,)
		)
		if issue_voucher
		else []
	)
	issue_diamond_logs = [
		row
		for row in issue_logs
		if row.get("item_code") and row["item_code"][0] in ("D", "G")
	]

	se_diamond_lines = (
		frappe.db.sql(
			"""
			SELECT se.name AS stock_entry, se.stock_entry_type, se.docstatus,
			       sed.item_code, sed.batch_no, sed.qty, sed.pcs, sed.uom,
			       sed.s_warehouse, sed.t_warehouse,
			       sed.material_request, sed.material_request_item
			FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name = sed.parent
			WHERE sed.manufacturing_operation = %s
			  AND se.docstatus = 1
			  AND LEFT(sed.item_code, 1) IN ('D', 'G')
			ORDER BY se.posting_date, se.posting_time, sed.idx
			""",
			(manufacturing_operation,),
			as_dict=True,
		)
		or []
	)

	se_keys = {(r["item_code"], r.get("batch_no")) for r in se_diamond_lines}
	current_keys = {(r["item_code"], r.get("batch_no")) for r in diamond_logs}
	issue_keys = {(r["item_code"], r.get("batch_no")) for r in issue_diamond_logs}

	diagnosis: list[str] = []
	if se_diamond_lines and not diamond_logs:
		diagnosis.append(
			"Stock Entry posted diamond onto MOP but no MOP Log D/G rows exist — "
			"Material Request submit did not bridge into MOP Log "
			"(create_mop_log_for_stock_transfer_to_mo not called for this stock_entry_type)."
		)
	if diamond_logs and not issue_diamond_logs and issue_voucher:
		diagnosis.append(
			"Diamond exists in current MOP balance but Employee IR Issue voucher cloned no D/G rows — "
			"Issue snapshot is metal-only; Receive cannot replay diamond from this Issue."
		)
	if mop_header and (mop_header.get("diamond_wt") or 0) and not diamond_logs:
		diagnosis.append(
			"Manufacturing Operation header still carries diamond_wt but MOP Log has no D rows — "
			"header is stale relative to the ledger."
		)
	if not se_diamond_lines and not diamond_logs:
		diagnosis.append(
			"No diamond Stock Entry posted against this MOP and no MOP Log D rows — "
			"Material Request may not have targeted this Manufacturing Operation."
		)

	return {
		"inputs": {
			"employee_ir_receive": employee_ir_receive,
			"manufacturing_operation": manufacturing_operation,
			"resolved_issue_voucher": issue_voucher,
		},
		"receive_meta": receive_meta,
		"manufacturing_operation_header": mop_header,
		"counts": {
			"mop_log_total": len(all_logs),
			"mop_log_diamond_or_gemstone": len(diamond_logs),
			"issue_voucher_logs": len(issue_logs),
			"issue_voucher_diamond_or_gemstone": len(issue_diamond_logs),
			"stock_entry_diamond_lines": len(se_diamond_lines),
		},
		"key_set_diff": {
			"in_stock_entry_only": sorted(se_keys - current_keys),
			"in_current_balance_only": sorted(current_keys - se_keys),
			"in_current_but_missing_from_issue_snapshot": sorted(
				current_keys - issue_keys
			),
		},
		"mop_log_diamond_rows": diamond_logs,
		"issue_voucher_diamond_rows": issue_diamond_logs,
		"stock_entry_diamond_lines": se_diamond_lines,
		"diagnosis": diagnosis
		or [
			"Diamond present uniformly in Stock Entry, MOP Log, Issue snapshot and header — "
			"investigate UI / Receive form rendering instead of data lineage."
		],
	}


def _latest_mop_log_balance_rows(
	mwos: list[str] | None = None, mops: list[str] | None = None
) -> list[dict]:
	"""Latest non-cancelled MOP Log row per ``(operation, item, batch)``.

	The SQL shape :func:`audit_mop_balance_drift` has always used, factored out so
	every balance auditor reads one definition of "current" -- two auditors
	disagreeing about that is how a repair script corrupts data. Mirrors
	``get_current_mop_balance_rows``'s dedup across every operation in one query;
	``<=>`` on the item/batch join so a NULL batch matches itself rather than
	dropping the row.

	Caveat inherited from the original: ``MAX(creation)`` + join returns ALL rows
	tied at the max creation, where ``get_current_mop_balance_rows`` picks exactly
	one. ``creation`` is datetime(6), so ties are effectively impossible.

	``mops`` is pushed into BOTH query levels; ``mwos`` deliberately is not. Only the
	outer WHERE was ever filtered, so a "scoped" call still materialised the whole
	ledger's GROUP BY -- the scoping was cosmetic. ``manufacturing_operation`` IS a
	GROUP BY key of the derived table, so restricting it there is exactly equivalent
	and lands on ``mop_balance_idx``. ``manufacturing_work_order`` is NOT a group key
	and is a ``Data`` pseudo-FK (DATA-002), so pushing it down could change which row
	is "latest" for a key whose MWO is blank or wrong.
	"""
	conditions = ""
	inner_conditions = ""
	params: dict = {}
	if mwos:
		conditions = "AND ml.manufacturing_work_order IN %(mwos)s"
		params["mwos"] = tuple(mwos)
	if mops:
		conditions += " AND ml.manufacturing_operation IN %(mops)s"
		inner_conditions = "AND manufacturing_operation IN %(mops)s"
		params["mops"] = tuple(mops)

	return frappe.db.sql(
		f"""
		SELECT
			ml.manufacturing_operation AS mop,
			ml.manufacturing_work_order AS mwo,
			ml.item_code,
			ml.batch_no,
			ml.qty_after_transaction_batch_based AS qty,
			ml.pcs_after_transaction_batch_based AS pcs,
			ml.voucher_type,
			ml.voucher_no,
			ml.name AS mop_log,
			ml.creation
		FROM `tabMOP Log` ml
		INNER JOIN (
			SELECT manufacturing_operation, item_code, batch_no, MAX(creation) AS mx
			FROM `tabMOP Log`
			WHERE is_cancelled = 0 AND IFNULL(manufacturing_operation, '') != ''
			  {inner_conditions}
			GROUP BY manufacturing_operation, item_code, batch_no
		) latest
			ON latest.manufacturing_operation = ml.manufacturing_operation
			AND latest.item_code <=> ml.item_code
			AND latest.batch_no <=> ml.batch_no
			AND latest.mx = ml.creation
		WHERE ml.is_cancelled = 0
		  {conditions}
		""",
		params,
		as_dict=True,
	)


def audit_mop_balance_drift(
	mwos: list[str] | None = None, limit: int = 200, rows: list[dict] | None = None
) -> list[dict]:
	"""Operations whose stored ``gross_wt`` disagrees with a per-operation ledger replay.

	Read-only. This is the detector for the MWO-wide-balance bug: the MOP Log writer used
	to derive ``qty_after_transaction*`` from ``SUM(qty_change)`` over the whole
	Manufacturing Work Order, so residue stranded on a finished operation was folded into
	the next operation's opening balance. A drifted operation shows a ``gross_wt`` above
	the weight actually issued/received into it.

	Compares three views of the same operation:

	* ``ledger_gross`` -- sum of the latest MOP Log row per (item, batch), the value
	  ``recalculate_manufacturing_operation_weights`` would write.
	* ``stored_gross`` -- ``Manufacturing Operation.gross_wt``.
	* ``received_gross_wt`` -- what the operator actually weighed in.

	``mwos`` narrows the scan; omit it to sweep every MWO with active MOP Log rows.

	``rows`` accepts an already-fetched :func:`_latest_mop_log_balance_rows` result
	so a caller running several balance auditors together pays for the sweep once.
	It is a whole-ledger scan -- ~1M rows on a dev site -- and running it twice
	back to back is enough to lose the MySQL connection.
	"""
	if rows is None:
		rows = _latest_mop_log_balance_rows(mwos)

	ledger: dict[str, dict] = {}
	for r in rows:
		bucket = ledger.setdefault(
			r["mop"],
			{
				"mwo": r["mwo"],
				"ledger_gross": 0.0,
				"diamond_ct": 0.0,
				"gemstone_ct": 0.0,
			},
		)
		# Only metal-ish prefixes contribute to gross_wt in grams; D/G are carats and
		# are converted by the recompute, so mirror FIELD_MAP's treatment. The recompute
		# sums the family in CARATS and converts the gram twin ONCE, so carats accumulate
		# here and convert per family below -- rounding per row would make this detector
		# report a phantom 0.001 g drift against a correctly written header.
		#
		# Clamped, because the WRITER clamps: recalculate_manufacturing_operation_weights
		# drops negative batch balances from the header buckets. Replaying them RAW here
		# would report ledger_minus_stored == -|negative| on every operation carrying a
		# negative key -- a false drift hit on data the writer handled correctly. The two
		# auditors ask different questions from one shared definition: THIS one answers
		# "does the stored header match what the writer would write"; "which keys are
		# corrupt" is audit_negative_batch_balances', which reads the RAW qty via
		# _is_negative and must keep doing so.
		first_char = (r.get("item_code") or "")[:1]
		qty, _pcs = clamp_negative_balance(r.get("qty"))
		if first_char == "D":
			bucket["diamond_ct"] += qty
		elif first_char == "G":
			bucket["gemstone_ct"] += qty
		elif first_char in ("M", "F", "O"):
			bucket["ledger_gross"] += qty

	for bucket in ledger.values():
		bucket["ledger_gross"] += carat_to_gram(
			bucket.pop("diamond_ct")
		) + carat_to_gram(bucket.pop("gemstone_ct"))

	if not ledger:
		return []

	stored = frappe.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", list(ledger)]},
		fields=[
			"name",
			"gross_wt",
			"received_gross_wt",
			"previous_mop",
			"prev_gross_wt",
			"status",
			"department",
			"manufacturing_work_order",
		],
		limit_page_length=0,
	)

	out: list[dict] = []
	for mop in stored:
		led = flt(ledger[mop["name"]]["ledger_gross"], 3)
		stored_gross = flt(mop.get("gross_wt"), 3)
		received = flt(mop.get("received_gross_wt"), 3)
		ledger_vs_stored = flt(led - stored_gross, 3)
		# A receive that weighed less than the operation carried is normal; what is not
		# normal is the ledger holding MORE than was received into the operation.
		received_vs_ledger = flt(led - received, 3) if received else 0.0
		if not ledger_vs_stored and not received_vs_ledger:
			continue
		out.append(
			{
				"manufacturing_operation": mop["name"],
				"manufacturing_work_order": mop.get("manufacturing_work_order"),
				"department": mop.get("department"),
				"status": mop.get("status"),
				"ledger_gross": led,
				"stored_gross": stored_gross,
				"received_gross_wt": received,
				"ledger_minus_stored": ledger_vs_stored,
				"ledger_minus_received": received_vs_ledger,
				"previous_mop": mop.get("previous_mop"),
				"prev_gross_wt": flt(mop.get("prev_gross_wt"), 3),
			}
		)

	out.sort(key=lambda r: abs(r["ledger_minus_received"]), reverse=True)
	return out[:limit]


def audit_negative_batch_balances(
	mwos: list[str] | None = None,
	limit: int = 200,
	tolerance: float = 0.001,
	rows: list[dict] | None = None,
	mops: list[str] | None = None,
) -> list[dict]:
	"""``(operation, item, batch)`` keys whose CURRENT balance is negative.

	Read-only. A negative batch balance says the ledger consumed more of a batch
	than it ever held -- impossible in reality, so every hit is data corruption.
	Known writers that can produce one:

	* ``create_mop_log_for_stock_transfer_to_mo`` posting a Material Receive
	  per-operation against a cap validated MWO-wide, so a receive whose balance
	  sits on a SIBLING operation writes ``0 - qty`` on the stamped one.
	* a receive row carrying a different (or blank) ``batch_no`` than the transfer
	  it answers, so the whole qty lands on a fresh key at ``0 - qty``.
	* the FG-MWO seed's ``HAVING SUM(qty_change) > 0 OR SUM(pcs_change) > 0``,
	  which admits a row whose qty sum is negative.
	* ``Refining Entry``'s ``if qty <= 0 and pcs <= 0: continue`` -- refining does
	  not clear a negative row, so it survives the zeroing and is cloned onward.

	And once written it PROPAGATES: every Department IR / Employee IR handoff
	clones the row verbatim onto the next operation. ``origin_mop`` walks the
	``previous_mop`` chain back to the operation where the key first went
	negative -- the only one worth investigating, since everything after it merely
	inherited the number. ``inherited`` is False on that origin, and the sort puts
	origins first. Note a gap in the chain (the key not cloned onto some
	intermediate operation) stops the walk, so one defect can report more than one
	origin; treat ``inherited=False`` as "start here", not as a unique marker.

	Nothing here is auto-repairable. Writing a negative balance up to zero adds
	metal no Stock Ledger Entry ever created, which then flows into the next
	operation's opening balance and can be issued, reserved and refined -- leaving
	the ledger self-consistently wrong. ``patches/repair_mwo_wide_mop_log_balances``
	already enforces this: its ``_repairable`` refuses any positive delta without
	``allow_increase=True``, so the rows this sweep finds are NOT fixable by the
	default run of that patch and need an explicit human decision.

	``tolerance`` is a plain kwarg (not ``_float_tolerance()``) so this module
	keeps its narrow import surface; 0.001 matches the precision-3 grid the MOP
	Log tiers are written at. ``rows`` shares an already-fetched sweep with
	:func:`audit_mop_balance_drift` -- see the note on its ``rows`` argument.
	"""
	if rows is None:
		rows = _latest_mop_log_balance_rows(mwos, mops)
	if not rows:
		return []

	def _is_negative(row):
		return flt(row.get("qty")) < -flt(tolerance) or cint(row.get("pcs")) < 0

	negatives = [r for r in rows if _is_negative(r)]
	if not negatives:
		return []

	# Only rows on a negative key are reachable by the origin walk, and the walk
	# only steps onto an operation that is itself negative for that key -- so
	# neither lookup needs the full ledger.
	neg_keys = {(r["item_code"], r["batch_no"]) for r in negatives}
	latest = {
		(r["mop"], r["item_code"], r["batch_no"]): r
		for r in rows
		if (r["item_code"], r["batch_no"]) in neg_keys
	}

	meta: dict = {}

	def _meta(mop):
		"""Operation header, fetched once per operation actually walked."""
		if mop not in meta:
			meta[mop] = (
				frappe.db.get_value(
					"Manufacturing Operation",
					mop,
					["name", "previous_mop", "status", "department"],
					as_dict=True,
				)
				or {}
			)
		return meta[mop]

	def _origin(mop, item_code, batch_no):
		"""Walk back while the SAME key is negative on the predecessor."""
		seen: set = set()
		cur = mop
		while cur and cur not in seen:
			seen.add(cur)
			prev = _meta(cur).get("previous_mop")
			prev_row = latest.get((prev, item_code, batch_no)) if prev else None
			if not prev_row or not _is_negative(prev_row):
				return cur
			cur = prev
		return cur

	out: list[dict] = []
	for r in negatives:
		origin = _origin(r["mop"], r["item_code"], r["batch_no"])
		m = _meta(r["mop"])
		out.append(
			{
				"manufacturing_operation": r["mop"],
				"manufacturing_work_order": r["mwo"],
				"item_code": r["item_code"],
				"batch_no": r["batch_no"],
				"qty": flt(r.get("qty"), 3),
				"pcs": cint(r.get("pcs")),
				"origin_mop": origin,
				"inherited": origin != r["mop"],
				"department": m.get("department"),
				"status": m.get("status"),
				"latest_row": r.get("mop_log"),
				"latest_voucher": f"{r.get('voucher_type')} {r.get('voucher_no')}",
				"creation": r.get("creation"),
			}
		)

	# Origins first -- that is where the investigation starts -- then by size.
	out.sort(key=lambda r: (r["inherited"], -abs(flt(r["qty"]))))
	# ``limit=0`` returns everything. The sort puts origins FIRST, so truncating drops
	# CLONES preferentially -- which silently corrupts any downstream clone count or
	# per-origin roll-up. Callers that aggregate must pass limit=0.
	return out[:limit] if limit else out


def operation_ancestors(mop: str, max_depth: int = 200) -> list[str]:
	"""``mop`` plus its ``previous_mop`` chain, oldest last. Bounded and cycle-safe.

	Any SCOPED negative-balance query must include these. ``_origin`` only steps onto a
	predecessor that is itself negative for the same key, so a scope that omits the
	ancestors makes every inherited row look like its own origin -- scoping to
	``MOP-A463A`` alone reports ``inherited=False, origin=MOP-A463A`` where the full
	sweep correctly reports ``origin=MOP-49T4D``.
	"""
	chain: list[str] = []
	seen: set = set()
	cur = mop
	while cur and cur not in seen and len(chain) < max_depth:
		seen.add(cur)
		chain.append(cur)
		cur = frappe.db.get_value("Manufacturing Operation", cur, "previous_mop")
	return chain


def negative_balance_findings(
	mwos: list[str] | None = None,
	mops: list[str] | None = None,
	include_ancestors: bool = True,
	tolerance: float = 0.001,
	rows: list[dict] | None = None,
) -> dict:
	"""Enriched view of :func:`audit_negative_batch_balances` -- one detector, many surfaces.

	Read-only. Adds what a report or an operator needs and the raw detector does not
	carry: the PCS/UOM context, how much ``gross_wt`` each key suppresses in GRAMS, and
	how far the defect has been cloned.

	``understatement_g`` is the number to aggregate. The raw ``qty`` column mixes Grams
	(M/F/O) and Carats (D/G), so summing it is a unit error; this converts D/G through
	``carat_to_gram`` and reports 0 for a prefix outside ``FIELD_MAP``, which never
	reaches a weight bucket at all.

	Clone counts are derived by grouping on ``(origin_mop, item, batch)`` BEFORE any
	display filter, and the detector is called with ``limit=0`` -- it sorts origins
	first, so a truncated call would drop clones preferentially and undercount.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import FIELD_MAP

	if rows is None:
		scope = list(mops) if mops else None
		if scope and include_ancestors:
			expanded: list[str] = []
			for mop in scope:
				expanded.extend(operation_ancestors(mop))
			scope = sorted(set(expanded))
		rows = _latest_mop_log_balance_rows(mwos, scope)

	findings = audit_negative_batch_balances(rows=rows, tolerance=tolerance, limit=0)
	if not findings:
		return {"findings": [], "by_operation": {}, "totals": _empty_negative_totals()}

	clone_counts: dict = {}
	for f in findings:
		key = (f["origin_mop"], f["item_code"], f["batch_no"])
		clone_counts[key] = clone_counts.get(key, 0) + 1

	mop_meta = {
		d.name: d
		for d in frappe.get_all(
			"Manufacturing Operation",
			filters={
				"name": ["in", sorted({f["manufacturing_operation"] for f in findings})]
			},
			fields=["name", "manufacturing_order", "for_fg", "gross_wt"],
			limit_page_length=0,
		)
	}
	uoms = {
		d.name: d.stock_uom
		for d in frappe.get_all(
			"Item",
			filters={
				"name": [
					"in",
					sorted({f["item_code"] for f in findings if f["item_code"]}),
				]
			},
			fields=["name", "stock_uom"],
			limit_page_length=0,
		)
	}

	by_operation: dict = {}
	for f in findings:
		prefix = FIELD_MAP.get((f["item_code"] or "")[:1])
		qty = flt(f["qty"])
		if not prefix or qty >= 0:
			understatement = 0.0
		elif prefix in ("diamond", "gemstone"):
			understatement = carat_to_gram(abs(qty))
		else:
			understatement = flt(abs(qty), 3)

		meta = mop_meta.get(f["manufacturing_operation"]) or {}
		f["understatement_g"] = understatement
		f["understatement_pcs"] = max(0, -cint(f.get("pcs")))
		f["downstream_clone_count"] = (
			clone_counts[(f["origin_mop"], f["item_code"], f["batch_no"])] - 1
		)
		f["parent_manufacturing_order"] = meta.get("manufacturing_order")
		f["for_fg"] = cint(meta.get("for_fg"))
		f["stored_gross_wt"] = flt(meta.get("gross_wt"))
		f["uom"] = uoms.get(f["item_code"])

		agg = by_operation.setdefault(
			f["manufacturing_operation"],
			{"understatement_g": 0.0, "understatement_pcs": 0, "keys": 0},
		)
		agg["understatement_g"] = flt(agg["understatement_g"] + understatement, 3)
		agg["understatement_pcs"] += f["understatement_pcs"]
		agg["keys"] += 1

	totals = {
		"keys": len(findings),
		"origin_keys": sum(1 for f in findings if not f["inherited"]),
		"inherited_keys": sum(1 for f in findings if f["inherited"]),
		"operations": len(by_operation),
		"mwos": len({f["manufacturing_work_order"] for f in findings}),
		"understatement_g": flt(sum(f["understatement_g"] for f in findings), 3),
		"understatement_pcs": sum(f["understatement_pcs"] for f in findings),
	}
	return {"findings": findings, "by_operation": by_operation, "totals": totals}


def _empty_negative_totals() -> dict:
	return {
		"keys": 0,
		"origin_keys": 0,
		"inherited_keys": 0,
		"operations": 0,
		"mwos": 0,
		"understatement_g": 0.0,
		"understatement_pcs": 0,
	}


def refining_cutoffs(mwos: list[str] | None = None) -> dict:
	"""``{mwo: refined_on}`` for MWOs consumed by a submitted Work Order Refining Entry.

	``refined_on`` is the entry's ``modified`` -- the moment the MWO's balances were
	zeroed. Every MOP Log row at or before it describes metal that no longer exists.
	"""
	conditions = ""
	params: dict = {}
	if mwos:
		conditions = "AND d.manufacturing_work_order IN %(mwos)s"
		params["mwos"] = tuple(mwos)

	rows = frappe.db.sql(
		f"""
		SELECT d.manufacturing_work_order AS mwo, MAX(re.modified) AS refined_on
		FROM `tabManufacturing Work Order Refining Details` d
		INNER JOIN `tabRefining Entry` re ON re.name = d.parent
		WHERE d.parenttype = 'Refining Entry'
		  AND re.docstatus = 1
		  AND re.refining_type = 'Work Order Refining'
		  {conditions}
		GROUP BY d.manufacturing_work_order
		""",
		params,
		as_dict=True,
	)
	return {r["mwo"]: r["refined_on"] for r in rows}


def audit_post_refining_contamination(
	mwos: list[str] | None = None, limit: int = 200
) -> list[dict]:
	"""Balances a refined MWO should not still be carrying.

	Read-only, and the single source of truth the repair script consumes -- audit and
	repair cannot disagree about what is wrong.

	A Work Order Refining Entry zeroes the MWO, so the post-refining ledger can be
	replayed from a known zero. For each ``(operation, item, batch)``::

	    expected(op) = expected(op.previous_mop) + SUM(op's own qty_change)

	with ``expected = 0`` where ``previous_mop`` is absent or itself pre-dates refining --
	that operation starts a fresh chain and may only hold what was issued into it. A
	handoff clone contributes ``qty_change = 0``, so carry-forward along a chain is
	preserved; what the replay refuses to carry is residue from an operation the chain
	*left behind*. That is the defect: 0.010g stranded on ``MOP-I5D24`` turned a 3.210g
	re-cast issue on ``MOP-0K3Q4`` into a 3.220g balance.

	An operation with any row at or before the cutoff **straddles** refining; its opening
	balance is not derivable this way, and neither is that of anything downstream of it.
	Those are reported with ``straddles = True`` and excluded from automatic repair.

	Returns one entry per ``(operation, item, batch)`` that disagrees, each carrying
	``expected`` (the replayed balance), ``actual`` (what the ledger holds) and ``delta``.
	"""
	cutoffs = refining_cutoffs(mwos)
	if not cutoffs:
		return []

	rows = frappe.db.sql(
		"""
		SELECT
			manufacturing_work_order AS mwo,
			manufacturing_operation AS mop,
			item_code,
			batch_no,
			qty_change,
			qty_after_transaction_batch_based AS balance,
			voucher_type,
			voucher_no,
			row_name,
			name,
			creation
		FROM `tabMOP Log`
		WHERE is_cancelled = 0
		  AND IFNULL(manufacturing_operation, '') != ''
		  AND manufacturing_work_order IN %(mwos)s
		ORDER BY creation ASC
		""",
		{"mwos": tuple(cutoffs)},
		as_dict=True,
	)
	if not rows:
		return []

	previous_mop = dict(
		frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", sorted({r["mop"] for r in rows})]},
			fields=["name", "previous_mop"],
			as_list=True,
			limit_page_length=0,
		)
	)

	by_mwo: dict = {}
	for r in rows:
		by_mwo.setdefault(r["mwo"], []).append(r)

	out: list[dict] = []
	for mwo, mwo_rows in by_mwo.items():
		refined_on = cutoffs[mwo]
		# Three populations, and the distinction matters:
		#   pre_only    -- every row pre-dates refining. The operation is dead; a
		#                  successor of it starts a FRESH chain at 0. Derivable.
		#   straddling  -- rows on both sides of the cutoff. Its own opening balance is
		#                  not derivable, and neither is anything downstream of it.
		#   post_only   -- entirely after refining. Replayable.
		mops_pre = {r["mop"] for r in mwo_rows if r["creation"] <= refined_on}
		mops_post = {r["mop"] for r in mwo_rows if r["creation"] > refined_on}
		straddling = mops_pre & mops_post
		pre_only = mops_pre - mops_post
		underivable = set(straddling)

		own_change: dict = {}
		actual: dict = {}
		latest: dict = {}
		repaired: set = set()
		order: list = []
		for r in mwo_rows:
			if r["creation"] <= refined_on:
				continue
			key = (r["mop"], r["item_code"], r["batch_no"])
			if key not in own_change:
				order.append(key)
				own_change[key] = 0.0
			if r["row_name"] == REPAIR_ROW_TAG:
				# A correcting row is not a movement -- it restates the balance. Its
				# effect is visible through `actual` only, so a repaired key reports
				# delta 0 and drops out of the findings instead of re-reporting its
				# own correction as fresh drift.
				repaired.add(key)
			else:
				own_change[key] = flt(own_change[key] + flt(r["qty_change"]), 3)
			actual[key] = flt(r["balance"], 3)
			latest[key] = r

		# `order` follows first-appearance in creation order, so a predecessor's
		# closing balance is always resolved before its successor needs it.
		expected: dict = {}
		for key in order:
			mop, item_code, batch_no = key
			prev = previous_mop.get(mop)
			if prev in underivable:
				# Cannot trust the predecessor's closing balance, so cannot derive
				# this one either.
				underivable.add(mop)
				opening = 0.0
			elif not prev or prev in pre_only:
				# Fresh chain: the predecessor's metal was consumed by refining, so
				# this operation may hold only what was issued into it.
				opening = 0.0
			else:
				opening = flt(expected.get((prev, item_code, batch_no), 0.0))
			expected[key] = flt(opening + own_change[key], 3)

		for key in order:
			delta = flt(expected[key] - actual[key], 3)
			if not delta:
				continue
			mop, item_code, batch_no = key
			ref = latest[key]
			out.append(
				{
					"manufacturing_work_order": mwo,
					"manufacturing_operation": mop,
					"item_code": item_code,
					"batch_no": batch_no,
					"expected": expected[key],
					"actual": actual[key],
					"delta": delta,
					"straddles": mop in underivable,
					"already_repaired": key in repaired,
					"latest_row": ref["name"],
					"latest_voucher": f"{ref['voucher_type']} {ref['voucher_no']}",
					"refined_on": refined_on,
				}
			)

	out.sort(key=lambda r: (r["straddles"], -abs(r["delta"])))
	return out[:limit]


def run_all_audits(receive_doc: str | None = None) -> dict:
	"""Entry point for `bench execute jewellery_erpnext.mop_lineage_audit.run_all_audits`."""
	out: dict = {"parity": get_deployment_parity_record()}
	recv = receive_doc
	if not recv:
		latest = _latest_submitted_receive()
		if latest:
			recv = latest["receive_name"]
			out["sample_chain"] = latest
	else:
		out["sample_chain"] = frappe.db.get_value(
			"Department IR",
			recv,
			["name", "receive_against", "modified", "docstatus", "type"],
			as_dict=True,
		)

	mops: list[str] = []
	if recv:
		mops = _mops_for_receive(recv)
		issue = frappe.db.get_value("Department IR", recv, "receive_against")
		mop0 = mops[0] if mops else None
		out["mop_log_sql"] = sql_mop_log_lineage_proof(issue, recv, mop0)
		if mops:
			out["mop_log_rows_sample"] = frappe.get_all(
				"MOP Log",
				filters={
					"manufacturing_operation": ["in", mops[:5]],
					"is_cancelled": 0,
				},
				fields=[
					"name",
					"creation",
					"voucher_type",
					"voucher_no",
					"row_name",
					"manufacturing_operation",
					"from_warehouse",
					"to_warehouse",
					"item_code",
					"batch_no",
					"flow_index",
					"is_synced",
				],
				order_by="manufacturing_operation asc, flow_index asc, creation asc",
				limit_page_length=200,
			)

	out["server_scripts"] = audit_active_server_scripts()
	out[
		"submission_queue_duplicates"
	] = audit_submission_queue_department_ir_duplicates()
	out["error_log_dir_fallback_recent"] = audit_error_log_dir_fallback(20)
	# One whole-ledger sweep, shared: each of these is a ~1M-row scan on its own.
	balance_rows = _latest_mop_log_balance_rows()
	out["mop_balance_drift"] = audit_mop_balance_drift(rows=balance_rows)
	out["negative_batch_balances"] = audit_negative_batch_balances(rows=balance_rows)
	out["post_refining_contamination"] = audit_post_refining_contamination()
	return out


# =============================================================================================
# Work-order current-operation audit and stale Employee Issue manifests (incident 2026-10-05)
# =============================================================================================
#
# THE RULE (doc_events/current_operation_guard.py): a work order acts only through its CURRENT
# operation, ``Manufacturing Work Order.manufacturing_operation`` (the pointer). The pointer
# belongs to the work order, has no successor (no operation names it as ``previous_mop``) and is
# the work order's only open operation. On the kggk-prod copy of 2026-10-05 34 open operations
# broke it, in three unrelated families:
#
#   C  a stale Employee IR Issue -- a draft submitted after its work order had moved on --
#      reopened a finished operation (EMP-IR-Labh-2026-43405 and five older cases);
#   A  Department IR Receives cancelled on 2026-10-01 put superseded operations back In-Transit;
#   B  one Department IR Issue carried a work order on two rows and minted twin operations.
#
# Families are assigned from EVIDENCE -- which document did what to which operation -- never from
# timestamps alone. Everything here is read-only by construction (``_ReadOnlyAudit``,
# ``_co_select``), and patches/repair_current_operation_conflicts.py reads current state through
# the same helpers, so the audit, the manifest and the repair cannot disagree.
#
# Index note: kggk-prod has no index on `tabEmployee IR Operation`.manufacturing_operation /
# manufacturing_work_order (the fix adds them), none on `tabManufacturing Operation`.previous_mop
# and none on `tabMOP Log`.voucher_no / manufacturing_operation. Every statement below is
# therefore either scoped through an existing index (MOP / DIR Operation / MOP Log by work order,
# Version by (ref_doctype, docname), time logs and child rows by parent) or a single
# pre-aggregated scan -- never a correlated subquery.

_CO_CHUNK = 500
_CO_STATEMENT_SECONDS = 300
# An Employee Issue opens its time log inside on_submit, moments after before_submit stamped
# ``issue_submitted_on`` (0.3 s for 43405; a queued submit stays inside one worker transaction).
# Ten minutes is far wider than any such gap and far narrower than the gap between two Issues of
# the same operation.
_CO_TIME_LOG_WINDOW = 600
_CO_TIME_LOG_EARLY = 5

CO_MANIFEST_VERSION = 1
CO_MANIFEST_KIND = "stale_employee_issue"
CO_UNRESOLVED_EOD_SOURCE = "MOP EOD Sync (Unresolved)"

CO_EIR_FIELDS = (
	"name",
	"type",
	"docstatus",
	"company",
	"department",
	"operation",
	"employee",
	"subcontracting",
	"subcontractor",
	"issue_submitted_on",
	"owner",
	"creation",
	"modified",
	"modified_by",
)
CO_EIR_ROW_FIELDS = (
	"name",
	"idx",
	"manufacturing_operation",
	"manufacturing_work_order",
	"rpt_wt_issue",
	"gross_wt",
	"docstatus",
	"modified",
)
CO_MOP_FIELDS = (
	"name",
	"manufacturing_work_order",
	"company",
	"department",
	"department_ir_status",
	"status",
	"operation",
	"employee",
	"subcontractor",
	"for_subcontracting",
	"previous_mop",
	"employee_ir",
	"start_time",
	"started_time",
	"finish_time",
	"rpt_wt_issue",
	"gross_wt",
	"net_wt",
	"finding_wt",
	"diamond_wt",
	"gemstone_wt",
	"other_wt",
	"modified",
	"modified_by",
)
CO_MWO_FIELDS = (
	"name",
	"docstatus",
	"status",
	"company",
	"department",
	"manufacturing_operation",
	"modified",
	"modified_by",
)
CO_TIME_LOG_FIELDS = (
	"name",
	"parent",
	"idx",
	"from_time",
	"to_time",
	"employee",
	"owner",
	"creation",
	"modified",
)
CO_MOP_LOG_FIELDS = (
	"name",
	"manufacturing_operation",
	"manufacturing_work_order",
	"voucher_type",
	"voucher_no",
	"row_name",
	"item_code",
	"batch_no",
	"flow_index",
	"qty_change",
	"qty_after_transaction_batch_based",
	"pcs_after_transaction_batch_based",
	"from_warehouse",
	"to_warehouse",
	"is_synced",
	"is_cancelled",
	"creation",
	"modified",
)
# What the Employee Issue cancel branch (on_submit_issue_new(cancel=True)) writes on each row
# operation -- and therefore what the reviewed restore must put back.
CO_RESTORE_FIELDS_EMPLOYEE = (
	"status",
	"operation",
	"employee",
	"start_time",
	"started_time",
	"finish_time",
	"rpt_wt_issue",
)
CO_RESTORE_FIELDS_SUBCONTRACTOR = (
	"status",
	"operation",
	"subcontractor",
	"for_subcontracting",
	"start_time",
	"started_time",
	"finish_time",
	"rpt_wt_issue",
)


def _co_guard():
	"""The guard module, imported lazily so this module stays importable on its own."""
	from jewellery_erpnext.jewellery_erpnext.doc_events import current_operation_guard

	return current_operation_guard


class _ReadOnlyAudit:
	"""Run a block of audit code with no way to write.

	* Nothing pending in the caller's transaction (no write statements, no queued commit
	  callbacks): ``START TRANSACTION READ ONLY`` -- MariaDB rejects every write with error
	  1792 -- and a rollback at the end. Starting a transaction implicitly commits the previous
	  one, which is why this mode is taken only when that one holds nothing.
	* Writes pending (the repair's post-verify runs inside its own write transaction): the
	  transaction is left alone and the block may only SELECT (``_co_select`` refuses anything
	  else, including locking reads).

	Either way ``frappe.db.transaction_writes`` must be unchanged when the block ends.
	"""

	def __init__(self):
		self.writes_before = 0
		self.own_transaction = False

	def __enter__(self):
		self.writes_before = cint(frappe.db.transaction_writes)
		pending_callbacks = any(
			getattr(callbacks, "_functions", None)
			for callbacks in (frappe.db.before_commit, frappe.db.after_commit)
		)
		# Inside a repair the caller's transaction holds row locks without having written yet;
		# START TRANSACTION would implicitly commit it and release them.
		self.own_transaction = (
			not self.writes_before
			and not pending_callbacks
			and not frappe.flags.get("current_operation_repair_in_progress")
		)
		if self.own_transaction:
			frappe.db.begin(read_only=True)
		return self

	def __exit__(self, exc_type, exc, tb):
		writes_after = cint(frappe.db.transaction_writes)
		if self.own_transaction:
			frappe.db.rollback()
		if exc_type is None and writes_after != self.writes_before:
			raise AssertionError(
				"current-operation audit issued "
				f"{writes_after - self.writes_before} write statement(s); it must be read-only"
			)
		return False

	def summary(self):
		return {
			"mode": "read_only_transaction"
			if self.own_transaction
			else "select_only_inside_caller_transaction",
			"write_statements": 0,
		}


_CO_LOCKING_READ = re.compile(
	r"(?i)\bfor\s+update\b|\block\s+in\s+share\s+mode\b|\bfor\s+share\b"
)


def _co_select(query, values=None, *, pluck=False):
	"""Run ONE plain read: a single non-locking SELECT, bounded by max_statement_time."""
	text = query.strip()
	if (
		not re.match(r"(?is)^select\b", text)
		or ";" in text
		or _CO_LOCKING_READ.search(text)
	):
		raise frappe.ValidationError(
			"current-operation audit: only single, non-locking SELECT statements are allowed"
		)
	if frappe.db.db_type == "mariadb":
		text = f"SET STATEMENT max_statement_time={_CO_STATEMENT_SECONDS} FOR {text}"
	kwargs = {"pluck": True} if pluck else {"as_dict": True}
	if values is None:
		# frappe.db.sql wraps a bare None into (None,), which MySQLdb then fails to format.
		return list(frappe.db.sql(text, **kwargs))
	return list(frappe.db.sql(text, values, **kwargs))


def _co_chunks(names, size=_CO_CHUNK):
	names = list(names)
	for start in range(0, len(names), size):
		yield names[start : start + size]


def _co_select_in(query, key, names, values=None, *, pluck=False):
	"""Run ``query`` once per chunk of ``names`` bound to ``%(key)s``; concatenate the rows."""
	names = sorted({cstr(n) for n in names or () if cstr(n)})
	out = []
	for chunk in _co_chunks(names):
		params = dict(values or {})
		params[key] = tuple(chunk)
		out.extend(_co_select(query, params, pluck=pluck))
	return out


def _co_names(value) -> list[str]:
	"""Normalise a name-list argument (list / tuple / set / JSON list / comma-separated)."""
	if not value:
		return []
	if isinstance(value, str):
		text = value.strip()
		value = json.loads(text) if text.startswith("[") else text.split(",")
	return sorted({cstr(v).strip() for v in value if cstr(v).strip()})


def _co_norm(value):
	"""The JSON-stable form used for every manifest value AND every comparison with the DB."""
	if value is None:
		return None
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, datetime):
		return value.isoformat(sep=" ", timespec="microseconds")
	if isinstance(value, date):
		return value.isoformat()
	if isinstance(value, timedelta):
		return str(value)
	if isinstance(value, Decimal):
		return round(float(value), 6)
	if isinstance(value, float):
		return round(value, 6)
	if isinstance(value, int):
		return value
	return cstr(value)


def _co_norm_row(row, fields):
	return {field: _co_norm(row.get(field)) for field in fields}


def _co_seconds(later, earlier):
	if not later or not earlier:
		return None
	return round((get_datetime(later) - get_datetime(earlier)).total_seconds(), 3)


def _co_time_log_opener(from_time, issues):
	"""The submitted Issue whose own submit opened a time log starting at ``from_time``.

	An Issue opens its time log moments after its submit, so the opener is the Issue with the
	closest submit inside [-5 s, +10 min] of ``from_time``. "Closest" matters: two Issues of
	one operation can be submitted minutes apart (31998 / 31995: 9 min 21 s), so a plain
	window would hand both time logs to the first one.
	"""
	best = None
	for issue in issues or []:
		gap = _co_seconds(from_time, issue.issue_submitted_on)
		if gap is None or gap < -_CO_TIME_LOG_EARLY or gap > _CO_TIME_LOG_WINDOW:
			continue
		if best is None or abs(gap) < best[0]:
			best = (abs(gap), issue)
	return best[1] if best else None


def _co_limited(rows, limit):
	rows = list(rows)
	return {"count": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit]}


# ---------------------------------------------------------------------------------------------
# bulk readers (each one statement per chunk; all plain reads)
# ---------------------------------------------------------------------------------------------


# kggk_uat keeps the operation a cancelled Department Issue / Employee Receive had created and marks
# it ``department_ir_status = 'Revert'`` (status Not Started). Such a row is history: it is never a
# live operation and never a successor (the guard's ``is_open_operation`` / ``_has_successor``).
_CO_NOT_REVERTED_SQL = "IFNULL({p}department_ir_status, '') != 'Revert' AND IFNULL({p}status, '') != 'Revert'"


def _co_reverted(row):
	return (
		cstr(row.get("department_ir_status")) == "Revert"
		or cstr(row.get("status")) == "Revert"
	)


def _co_open_operations(scope, open_statuses):
	"""Every open operation (with its work order's pointer) -- one pass over the MOP table.

	"Open" is the guard's ``is_open_operation``: an open status and not reverted. kggk_uat marks
	the operation a cancelled Department Issue / Employee Receive had created
	``department_ir_status = 'Revert'`` and leaves it Not Started; it is history, not live work.
	"""
	query = """
		SELECT m.name, m.manufacturing_work_order AS mwo, m.status, m.department,
			m.department_ir_status, m.operation, m.employee, m.subcontractor, m.previous_mop,
			m.employee_ir, m.department_issue_id, m.department_receive_id, m.creation,
			m.modified, m.modified_by,
			w.name AS w_name, w.docstatus AS w_docstatus, w.status AS mwo_status,
			IFNULL(w.manufacturing_operation, '') AS pointer
		FROM `tabManufacturing Operation` m
		LEFT JOIN `tabManufacturing Work Order` w ON w.name = m.manufacturing_work_order
		WHERE m.status IN %(open)s AND IFNULL(m.department_ir_status, '') != 'Revert' {scope}
	"""
	params = {"open": tuple(open_statuses)}
	if scope:
		return _co_select_in(
			query.format(scope="AND m.manufacturing_work_order IN %(mwos)s"),
			"mwos",
			scope,
			params,
		)
	return _co_select(query.format(scope=""), params)


def _co_submitted_work_orders(scope):
	"""Submitted work orders with their pointer row (a primary-key join)."""
	query = """
		SELECT w.name AS mwo, w.status AS mwo_status, w.company, w.department AS mwo_department,
			IFNULL(w.manufacturing_operation, '') AS pointer, w.modified,
			p.name AS p_name, p.manufacturing_work_order AS p_mwo, p.status AS p_status,
			p.department AS p_department, p.department_ir_status AS p_dirs
		FROM `tabManufacturing Work Order` w
		LEFT JOIN `tabManufacturing Operation` p ON p.name = w.manufacturing_operation
		WHERE w.docstatus = 1 {scope}
	"""
	if scope:
		return _co_select_in(
			query.format(scope="AND w.name IN %(mwos)s"), "mwos", scope
		)
	return _co_select(query.format(scope=""))


def _co_pointer_successors(scope, families, work_orders):
	"""``{mwo: [successor names]}`` for submitted work orders whose pointer has a successor.

	Unscoped: one pre-aggregated scan (``previous_mop`` is not indexed), joined to the pointers.
	Scoped: from the already-loaded families (the work-order index).
	"""
	if scope:
		out = {}
		pointers = {r.mwo: r.pointer for r in work_orders if r.pointer}
		for mwo, pointer in pointers.items():
			succ = [
				r.name
				for r in families.get(mwo, [])
				if r.previous_mop == pointer and not _co_reverted(r)
			]
			if succ:
				out[mwo] = succ
		return out
	rows = _co_select(
		"""
		SELECT w.name AS mwo, w.manufacturing_operation AS pointer, s.n
		FROM `tabManufacturing Work Order` w
		INNER JOIN (
			SELECT previous_mop, COUNT(*) AS n
			FROM `tabManufacturing Operation`
			WHERE IFNULL(previous_mop, '') != '' AND {not_reverted}
			GROUP BY previous_mop
		) s ON s.previous_mop = w.manufacturing_operation
		WHERE w.docstatus = 1
		""".format(not_reverted=_CO_NOT_REVERTED_SQL.format(p=""))
	)
	out = {}
	if rows:
		fams = _co_families({r.mwo for r in rows})
		for r in rows:
			out[r.mwo] = [
				s.name
				for s in fams.get(r.mwo, [])
				if s.previous_mop == r.pointer and not _co_reverted(s)
			] or [f"({r.n} successor(s))"]
	return out


def _co_families(mwos):
	"""Every operation of the given work orders, keyed by work order (work-order index)."""
	out = {}
	rows = _co_select_in(
		"""
		SELECT name, manufacturing_work_order AS mwo, previous_mop, status, department,
			department_ir_status, operation, employee, subcontractor, employee_ir,
			department_issue_id, department_receive_id, start_time, started_time, finish_time,
			creation, modified, modified_by
		FROM `tabManufacturing Operation`
		WHERE manufacturing_work_order IN %(mwos)s
		""",
		"mwos",
		mwos,
	)
	for row in rows:
		out.setdefault(row.mwo, []).append(row)
	for fam in out.values():
		fam.sort(key=lambda r: (r.creation or datetime.min, r.name))
	return out


def _co_operation_work_orders(mops):
	"""``{mop: mwo}`` straight from the operation rows (primary key)."""
	return {
		r.name: r.mwo
		for r in _co_select_in(
			"""
			SELECT name, manufacturing_work_order AS mwo
			FROM `tabManufacturing Operation` WHERE name IN %(mops)s
			""",
			"mops",
			mops,
		)
	}


def _co_employee_ir_rows(mops):
	"""Every Employee IR row (any type / docstatus) naming the given operations, by operation.

	Without the new index this is one scan of `tabEmployee IR Operation` per chunk.
	"""
	out = {}
	rows = _co_select_in(
		"""
		SELECT o.parent AS employee_ir, o.name AS row_name, o.idx,
			o.manufacturing_operation AS mop, o.manufacturing_work_order AS mwo,
			o.rpt_wt_issue, o.docstatus AS row_docstatus,
			e.type, e.docstatus, e.company, e.department, e.operation, e.employee,
			e.subcontracting, e.subcontractor, e.creation, e.issue_submitted_on, e.modified,
			e.modified_by, e.owner
		FROM `tabEmployee IR Operation` o
		INNER JOIN `tabEmployee IR` e ON e.name = o.parent
		WHERE o.parenttype = 'Employee IR' AND o.manufacturing_operation IN %(mops)s
		""",
		"mops",
		mops,
	)
	for row in rows:
		out.setdefault(row.mop, []).append(row)
	for found in out.values():
		found.sort(key=lambda r: (r.creation or datetime.min, r.employee_ir, r.idx))
	return out


def _co_employee_ir_rows_of(documents):
	"""Every row of the given Employee IRs (parent index), by document."""
	out = {}
	rows = _co_select_in(
		"""
		SELECT o.parent AS employee_ir, o.name AS row_name, o.idx,
			o.manufacturing_operation AS mop, o.manufacturing_work_order AS mwo, o.rpt_wt_issue,
			o.docstatus AS row_docstatus
		FROM `tabEmployee IR Operation` o
		WHERE o.parenttype = 'Employee IR' AND o.parent IN %(docs)s
		""",
		"docs",
		documents,
	)
	for row in rows:
		out.setdefault(row.employee_ir, []).append(row)
	for found in out.values():
		found.sort(key=lambda r: cint(r.idx))
	return out


def _co_department_ir_rows(mwos):
	"""Every Department IR row naming the given work orders (DIR Operation work-order index)."""
	out = {}
	rows = _co_select_in(
		"""
		SELECT o.parent AS department_ir, o.idx, o.manufacturing_operation AS mop,
			o.manufacturing_work_order AS mwo, o.docstatus AS row_docstatus,
			d.type, d.docstatus, d.current_department, d.next_department, d.receive_against,
			d.creation, d.modified, d.modified_by
		FROM `tabDepartment IR Operation` o
		INNER JOIN `tabDepartment IR` d ON d.name = o.parent
		WHERE o.parenttype = 'Department IR' AND o.manufacturing_work_order IN %(mwos)s
		""",
		"mwos",
		mwos,
	)
	for row in rows:
		out.setdefault(row.mwo, []).append(row)
	return out


def _co_reopen_rows(scope):
	"""Submitted Employee Issue rows submitted AFTER their operation already had a successor.

	``issue_submitted_on`` is stamped in before_submit and is never NULL on a submitted Issue.
	"""
	inner = outer = ""
	not_reverted = _CO_NOT_REVERTED_SQL.format(p="")
	params = {}
	if scope:
		inner = "AND manufacturing_work_order IN %(mwos)s"
		outer = "AND o.manufacturing_work_order IN %(mwos)s"
		params["mwos"] = tuple(scope)
	return _co_select(
		f"""
		SELECT e.name AS employee_ir, o.idx, o.manufacturing_operation AS mop,
			o.manufacturing_work_order AS mwo, e.issue_submitted_on, e.creation, e.owner,
			e.modified_by, c.first_child
		FROM `tabEmployee IR Operation` o
		INNER JOIN `tabEmployee IR` e ON e.name = o.parent
		INNER JOIN (
			SELECT previous_mop, MIN(creation) AS first_child
			FROM `tabManufacturing Operation`
			WHERE IFNULL(previous_mop, '') != '' AND {not_reverted} {inner}
			GROUP BY previous_mop
		) c ON c.previous_mop = o.manufacturing_operation
		WHERE o.parenttype = 'Employee IR' AND e.docstatus = 1 AND e.type = 'Issue'
			AND c.first_child < e.issue_submitted_on {outer}
		""",
		params,
	)


def _co_duplicate_issue_mops(scope):
	"""Operations named by more than one submitted Employee Issue (one grouped scan)."""
	scope_sql = "AND o.manufacturing_work_order IN %(mwos)s" if scope else ""
	return _co_select(
		f"""
		SELECT o.manufacturing_operation AS mop, COUNT(DISTINCT o.parent) AS n
		FROM `tabEmployee IR Operation` o
		INNER JOIN `tabEmployee IR` e ON e.name = o.parent
		WHERE o.parenttype = 'Employee IR' AND e.docstatus = 1 AND e.type = 'Issue'
			AND IFNULL(o.manufacturing_operation, '') != '' {scope_sql}
		GROUP BY o.manufacturing_operation
		HAVING n > 1
		""",
		{"mwos": tuple(scope)} if scope else None,
	)


def _co_open_time_logs(scope, closed_statuses):
	"""Open time logs (to_time NULL) on finished or non-current operations of submitted MWOs.

	Reverted operations (kggk_uat keeps a cancelled transfer's operation as Revert) are history,
	not stale-Issue evidence, and are left out like everywhere else in this audit.
	"""
	query = """
		SELECT t.name, t.parent AS mop, t.idx, t.from_time, t.employee, t.owner, t.creation,
			m.status AS mop_status, m.manufacturing_work_order AS mwo,
			IFNULL(w.manufacturing_operation, '') AS pointer
		FROM `tabManufacturing Operation Time Log` t
		INNER JOIN `tabManufacturing Operation` m ON m.name = t.parent
		INNER JOIN `tabManufacturing Work Order` w ON w.name = m.manufacturing_work_order
		WHERE t.parenttype = 'Manufacturing Operation' AND t.parentfield = 'time_logs'
			AND t.to_time IS NULL AND w.docstatus = 1
			AND (m.status IN %(closed)s OR m.name != IFNULL(w.manufacturing_operation, ''))
			AND {not_reverted}
			{scope}
	"""
	params = {"closed": tuple(closed_statuses)}
	not_reverted = _CO_NOT_REVERTED_SQL.format(p="m.")
	if scope:
		return _co_select_in(
			query.format(
				scope="AND m.manufacturing_work_order IN %(mwos)s",
				not_reverted=not_reverted,
			),
			"mwos",
			scope,
			params,
		)
	return _co_select(query.format(scope="", not_reverted=not_reverted), params)


# ---------------------------------------------------------------------------------------------
# evidence rules
# ---------------------------------------------------------------------------------------------


def _co_successors(family_rows, mop):
	return sorted(
		(r for r in family_rows or [] if r.previous_mop == mop and not _co_reverted(r)),
		key=lambda r: (r.creation or datetime.min, r.name),
	)


def _co_submitted_issues(eir_rows):
	return sorted(
		(
			r
			for r in eir_rows or []
			if r.type == "Issue" and cint(r.docstatus) == 1 and r.issue_submitted_on
		),
		key=lambda r: (r.issue_submitted_on, r.employee_ir),
	)


def _co_issue_row_kind(employee_ir, submitted_on, mop, eir_rows, family_rows):
	"""Classify one row of a submitted Employee Issue against what the operation had been through.

	Returns ``(kind, legit_issue_row, successors)``:

	* ``reopened`` -- the operation already had a successor when this Issue was submitted (the
	  2026-10-05 pattern: the work order had moved on, the submit reopened a finished operation);
	* ``double_issued`` -- another submitted Issue had already issued the operation, and this
	  one issued it again while it was still with the employee;
	* ``current`` -- this Issue was the first to issue the operation while it was current: the
	  row is legitimate.

	``legit_issue_row`` is the Issue that issued the operation while it was current (the
	earliest submitted one before the successor existed), or None.
	"""
	successors = _co_successors(family_rows, mop)
	first_child = successors[0].creation if successors else None
	issues = _co_submitted_issues(eir_rows)
	others = [r for r in issues if r.employee_ir != employee_ir]
	before_successor = [
		r for r in others if not first_child or r.issue_submitted_on < first_child
	]
	legit = before_successor[0] if before_successor else None
	if first_child and submitted_on and get_datetime(submitted_on) > first_child:
		return "reopened", legit, successors
	if any(r.issue_submitted_on < get_datetime(submitted_on) for r in others):
		return "double_issued", legit, successors
	return "current", None, successors


def _co_classify_open_operation(row, family_rows, eir_rows, dir_rows):
	"""Family (C / A / B / unknown) of one open, non-current operation, with its evidence."""
	successors = _co_successors(family_rows, row.name)
	first_child = successors[0].creation if successors else None
	matches = []
	evidence = {}

	stale = [
		r
		for r in _co_submitted_issues(eir_rows)
		if first_child and r.issue_submitted_on > first_child
	]
	if stale:
		matches.append("C")
		evidence["C"] = {
			"stale_issues": [
				{
					"employee_ir": r.employee_ir,
					"row": cint(r.idx),
					"submitted_on": _co_norm(r.issue_submitted_on),
					"submitted_by": r.modified_by,
					"created_by": r.owner,
					"holder_matches": bool(
						cstr(row.operation) == cstr(r.operation)
						and (
							cstr(row.employee) == cstr(r.employee)
							if r.subcontracting != "Yes"
							else cstr(row.subcontractor) == cstr(r.subcontractor)
						)
					),
				}
				for r in stale
			],
			"successor": successors[0].name,
			"successor_created": _co_norm(first_child),
			"successor_minted_by": successors[0].employee_ir
			or successors[0].department_issue_id,
		}

	cancelled_receives = sorted(
		{
			d.department_ir
			for d in dir_rows or []
			if d.mop == row.name and d.type == "Receive" and cint(d.docstatus) == 2
		}
	)
	issue_names = {
		d.department_ir
		for d in dir_rows or []
		if d.mop == row.name and d.type == "Issue" and cint(d.docstatus) == 1
	}
	minted = {
		s.name: s.department_issue_id
		for s in successors
		if s.department_issue_id and s.department_issue_id in issue_names
	}
	if cancelled_receives and minted:
		matches.append("A")
		evidence["A"] = {
			"cancelled_department_receives": cancelled_receives,
			"successor_minted_by_department_issue": minted,
			"department_ir_status": row.department_ir_status,
		}

	twins = sorted(
		t.name
		for t in family_rows or []
		if t.name != row.name
		and row.previous_mop
		and t.previous_mop == row.previous_mop
		and row.department_issue_id
		and t.department_issue_id == row.department_issue_id
	)
	issue_rows = sorted(
		cint(d.idx)
		for d in dir_rows or []
		if d.department_ir == row.department_issue_id
		and d.type == "Issue"
		and d.mwo == row.mwo
	)
	if twins and len(issue_rows) > 1:
		matches.append("B")
		evidence["B"] = {
			"twin_operations": twins,
			"department_issue": row.department_issue_id,
			"department_issue_rows": issue_rows,
		}

	return {
		"family": matches[0] if matches else "unknown",
		"also_matches": matches[1:],
		"kind": "superseded" if successors else "leaf_not_pointer",
		"successors": [s.name for s in successors],
		"evidence": evidence,
	}


# ---------------------------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------------------------


@frappe.whitelist()
def audit_current_operation_conflicts(mwos=None, limit=500):
	"""Read-only audit of work orders whose current operation is not unique.

	``mwos`` narrows every section to those work orders (list, JSON list or comma string);
	``limit`` caps each listed section (counts are always complete). Sections:

	* ``pointer_anomalies`` -- submitted work orders whose pointer is missing, unknown, belongs
	  to another work order, has a successor, or is closed while another operation is open. A
	  closed pointer with nothing open is "terminal" (finished goods, parked, split parents) and
	  only counted.
	* ``open_non_pointer`` -- open operations of submitted work orders that are not the pointer,
	  each with its family (C stale Employee Issue reopen / A Department Receive cancel / B
	  duplicate Department Issue row twin / unknown) and the evidence for it. Open operations of
	  cancelled or draft work orders are only counted.
	* ``duplicate_submitted_issues`` -- operations issued by more than one submitted Employee
	  Issue, paired (late document / first-submitted document), with inversion and the
	  REOPENED / DOUBLE_ISSUED label per operation.
	* ``stale_issue_cases`` -- every submitted Employee Issue with a reopened or double-issued
	  row: the family-C repair work list (one manifest each, see build_stale_issue_manifest).
	* ``outstanding_issue_drafts`` -- Employee Issue drafts with a fresh / stale verdict from the
	  guard's own rule evaluation (these start blocking their work orders once the fix is live).
	* ``open_time_log_fingerprint`` -- open time logs on finished or non-current operations, each
	  attributed to the Issue whose submit opened it.
	* ``eod_ambiguity`` -- unsynced, non-cancelled MOP Logs on open non-current operations (what
	  the new EOD hold stops), the EOD runs that touched those work orders and any draft
	  "MOP EOD Sync (Unresolved)" Stock Entry naming them.
	* ``dir_duplicate_rows`` -- Department IRs carrying the same work order on more than one row.
	* ``manual_status_edits`` -- desk/API status edits of operations (Version rows), counted.

	Read-only by construction: see ``_ReadOnlyAudit``.

	bench --site <site> execute jewellery_erpnext.mop_lineage_audit.audit_current_operation_conflicts
	"""
	frappe.only_for("System Manager")
	scope = _co_names(mwos)
	limit = max(1, min(cint(limit) or 500, 5000))
	guard = _ReadOnlyAudit()
	with guard:
		report = _co_audit(scope, limit)
	report["read_only"] = guard.summary()
	return report


def _co_audit(scope, limit):
	gm = _co_guard()
	open_statuses = tuple(gm.OPEN_STATUSES)
	closed_statuses = tuple(gm.CLOSED_STATUSES)
	started = now_datetime()

	# -- phase 1: candidate sets, one statement each ------------------------------------------
	open_rows = _co_open_operations(scope, open_statuses)
	work_orders = _co_submitted_work_orders(scope)
	reopen_rows = _co_reopen_rows(scope)
	duplicate_rows = _co_duplicate_issue_mops(scope)
	time_logs = _co_open_time_logs(scope, closed_statuses)

	submitted_open = [r for r in open_rows if r.w_name and cint(r.w_docstatus) == 1]
	non_pointer = [r for r in submitted_open if r.name != r.pointer]
	duplicate_mops = {r.mop for r in duplicate_rows}

	# -- phase 2: bulk evidence for the union, scoped through indexes -------------------------
	mops = (
		{r.name for r in non_pointer}
		| {r.mop for r in reopen_rows}
		| duplicate_mops
		| {t.mop for t in time_logs}
	)
	eir_by_mop = _co_employee_ir_rows(mops)

	case_documents = {r.employee_ir for r in reopen_rows}
	for mop in duplicate_mops:
		issues = _co_submitted_issues(eir_by_mop.get(mop))
		case_documents.update(r.employee_ir for r in issues[1:])
	case_rows = _co_employee_ir_rows_of(case_documents)
	extra_mops = {
		r.mop
		for rows in case_rows.values()
		for r in rows
		if r.mop and r.mop not in mops
	}
	if extra_mops:
		eir_by_mop.update(_co_employee_ir_rows(extra_mops))
		mops |= extra_mops

	mop_mwo = _co_operation_work_orders(mops)
	family_mwos = set(mop_mwo.values()) | {r.mwo for r in non_pointer if r.mwo}
	if scope:
		family_mwos |= set(scope)
	families = _co_families(family_mwos)
	dir_by_mwo = _co_department_ir_rows({r.mwo for r in non_pointer if r.mwo})
	pointer_successors = _co_pointer_successors(scope, families, work_orders)

	# -- sections ------------------------------------------------------------------------------
	report = {
		"site": frappe.local.site,
		"generated_on": _co_norm(started),
		"scope": {"mwos": scope or "all submitted work orders"},
		"limit": limit,
	}
	report["pointer_anomalies"] = _co_section_pointer_anomalies(
		work_orders,
		submitted_open,
		pointer_successors,
		open_statuses,
		closed_statuses,
		limit,
	)
	report["open_non_pointer"] = _co_section_open_non_pointer(
		open_rows, non_pointer, families, eir_by_mop, dir_by_mwo, limit
	)
	duplicates = _co_section_duplicate_issues(
		duplicate_mops, eir_by_mop, families, mop_mwo, limit
	)
	report["duplicate_submitted_issues"] = duplicates
	report["stale_issue_cases"] = _co_section_stale_cases(
		case_rows, eir_by_mop, families, mop_mwo, closed_statuses, limit
	)
	report["outstanding_issue_drafts"] = _co_section_drafts(scope, limit)
	report["open_time_log_fingerprint"] = _co_section_time_logs(
		time_logs, eir_by_mop, case_documents, limit
	)
	report["eod_ambiguity"] = _co_section_eod_ambiguity(non_pointer, limit)
	report["dir_duplicate_rows"] = _co_section_dir_duplicates(scope, limit)
	report["manual_status_edits"] = _co_section_status_edits(
		scope, families, open_statuses, closed_statuses, limit
	)
	report["summary"] = _co_summary(report)
	report["elapsed_seconds"] = _co_seconds(now_datetime(), started)
	return report


def _co_section_pointer_anomalies(
	work_orders,
	submitted_open,
	pointer_successors,
	open_statuses,
	closed_statuses,
	limit,
):
	open_count = {}
	for r in submitted_open:
		open_count[r.mwo] = open_count.get(r.mwo, 0) + 1
	counts = {
		"submitted_work_orders": len(work_orders),
		"normal": 0,
		"terminal": 0,
		"pointer_missing": 0,
		"pointer_not_found": 0,
		"pointer_other_work_order": 0,
		"pointer_has_successor": 0,
		"closed_pointer_with_open_operation": 0,
		"closed_pointer_in_transit": 0,
		"closed_pointer_not_started_work_order": 0,
		"pointer_status_unknown": 0,
	}
	terminal_by_status = {}
	rows = []
	for w in sorted(work_orders, key=lambda r: r.mwo):
		if not w.pointer:
			kind = "pointer_missing"
		elif not w.p_name:
			kind = "pointer_not_found"
		elif cstr(w.p_mwo) != w.mwo:
			kind = "pointer_other_work_order"
		elif w.mwo in pointer_successors:
			kind = "pointer_has_successor"
		elif cstr(w.p_status) in closed_statuses and cstr(w.p_dirs) == "In-Transit":
			# A transfer's operation closed by hand before it was received (2026-10-01: DIR Issue
			# 05197). Nothing can act on it: a Receive needs it Not Started, an Issue needs it out
			# of transit. Not "terminal" -- the work order is stuck until someone decides.
			kind = "closed_pointer_in_transit"
		elif (
			cstr(w.p_status) in closed_statuses
			and cstr(w.mwo_status) == "Not Started"
			and not open_count.get(w.mwo)
		):
			# Same 2026-10-01 hand edits, received variant (receives 00036 / 05303): the work order
			# never started, yet its only current operation is closed -- nothing can act on it.
			kind = "closed_pointer_not_started_work_order"
		elif cstr(w.p_status) in closed_statuses:
			if open_count.get(w.mwo):
				kind = "closed_pointer_with_open_operation"
			else:
				counts["terminal"] += 1
				key = cstr(w.mwo_status) or "(blank)"
				terminal_by_status[key] = terminal_by_status.get(key, 0) + 1
				continue
		elif cstr(w.p_status) not in open_statuses:
			kind = "pointer_status_unknown"
		else:
			counts["normal"] += 1
			continue
		counts[kind] += 1
		rows.append(
			{
				"anomaly": kind,
				"manufacturing_work_order": w.mwo,
				"mwo_status": w.mwo_status,
				"pointer": w.pointer or None,
				"pointer_status": w.p_status,
				"pointer_department": w.p_department,
				"pointer_department_ir_status": w.p_dirs,
				"pointer_work_order": w.p_mwo,
				"open_operations": open_count.get(w.mwo, 0),
				"pointer_successors": pointer_successors.get(w.mwo, []),
			}
		)
	out = _co_limited(rows, limit)
	out["counts"] = counts
	out["terminal_by_mwo_status"] = terminal_by_status
	return out


def _co_section_open_non_pointer(
	open_rows, non_pointer, families, eir_by_mop, dir_by_mwo, limit
):
	buckets = {
		"submitted_current": 0,
		"submitted_not_current": len(non_pointer),
		"cancelled_work_order": 0,
		"draft_work_order": 0,
		"missing_work_order": 0,
	}
	for r in open_rows:
		if not r.w_name:
			buckets["missing_work_order"] += 1
		elif cint(r.w_docstatus) == 2:
			buckets["cancelled_work_order"] += 1
		elif cint(r.w_docstatus) == 0:
			buckets["draft_work_order"] += 1
		elif r.name == r.pointer:
			buckets["submitted_current"] += 1

	rows = []
	by_family = {}
	by_kind = {}
	for r in sorted(non_pointer, key=lambda x: (x.mwo or "", x.name)):
		fam = families.get(r.mwo, [])
		verdict = _co_classify_open_operation(
			r, fam, eir_by_mop.get(r.name), dir_by_mwo.get(r.mwo)
		)
		pointer_row = next((f for f in fam if f.name == r.pointer), None)
		by_family[verdict["family"]] = by_family.get(verdict["family"], 0) + 1
		kind_key = f"{verdict['kind']}:{r.status}"
		by_kind[kind_key] = by_kind.get(kind_key, 0) + 1
		rows.append(
			{
				"manufacturing_operation": r.name,
				"manufacturing_work_order": r.mwo,
				"mwo_status": r.mwo_status,
				"status": r.status,
				"department": r.department,
				"department_ir_status": r.department_ir_status,
				"operation": r.operation,
				"employee": r.employee,
				"subcontractor": r.subcontractor,
				"pointer": r.pointer or None,
				"pointer_status": pointer_row.status if pointer_row else None,
				"pointer_department": pointer_row.department if pointer_row else None,
				"modified": _co_norm(r.modified),
				"modified_by": r.modified_by,
				**verdict,
			}
		)
	out = _co_limited(rows, limit)
	out["open_operation_buckets"] = buckets
	out["by_family"] = by_family
	out["by_kind_and_status"] = by_kind
	return out


def _co_section_duplicate_issues(duplicate_mops, eir_by_mop, families, mop_mwo, limit):
	pairs = {}
	operations = []
	for mop in sorted(duplicate_mops):
		issues = _co_submitted_issues(eir_by_mop.get(mop))
		if len(issues) < 2:
			continue
		mwo = mop_mwo.get(mop) or issues[0].mwo
		successors = _co_successors(families.get(mwo), mop)
		first_child = successors[0].creation if successors else None
		first = issues[0]
		labels = []
		for later in issues[1:]:
			label = (
				"REOPENED"
				if first_child and later.issue_submitted_on > first_child
				else "DOUBLE_ISSUED"
			)
			labels.append({"employee_ir": later.employee_ir, "label": label})
			key = (later.employee_ir, first.employee_ir)
			pair = pairs.setdefault(
				key,
				{
					"late_document": later.employee_ir,
					"first_submitted": first.employee_ir,
					"inverted": bool(
						later.creation
						and first.creation
						and later.creation < first.creation
					),
					"same_creator": cstr(later.owner) == cstr(first.owner),
					"creation_gap_seconds": _co_seconds(first.creation, later.creation),
					"late_submitted_on": _co_norm(later.issue_submitted_on),
					"first_submitted_on": _co_norm(first.issue_submitted_on),
					"operations": [],
				},
			)
			pair["operations"].append(
				{
					"manufacturing_operation": mop,
					"manufacturing_work_order": mwo,
					"label": label,
				}
			)
		operations.append(
			{
				"manufacturing_operation": mop,
				"manufacturing_work_order": mwo,
				"issues": [
					{
						"employee_ir": r.employee_ir,
						"created": _co_norm(r.creation),
						"submitted_on": _co_norm(r.issue_submitted_on),
						"owner": r.owner,
					}
					for r in issues
				],
				"successor": successors[0].name if successors else None,
				"successor_created": _co_norm(first_child),
				"later_issues": labels,
			}
		)
	pair_rows = sorted(pairs.values(), key=lambda p: p["late_document"])
	out = _co_limited(pair_rows, limit)
	out["operations"] = operations[:limit]
	out["counts"] = {
		"operations": len(operations),
		"pairs": len(pair_rows),
		"inverted_pairs": sum(1 for p in pair_rows if p["inverted"]),
		"reopened_operations": sum(
			1 for o in operations for x in o["later_issues"] if x["label"] == "REOPENED"
		),
		"double_issued_operations": sum(
			1
			for o in operations
			for x in o["later_issues"]
			if x["label"] == "DOUBLE_ISSUED"
		),
	}
	return out


def _co_section_stale_cases(
	case_rows, eir_by_mop, families, mop_mwo, closed_statuses, limit
):
	headers = {}
	if case_rows:
		headers = {
			r.name: r
			for r in _co_select_in(
				"""
				SELECT name, type, docstatus, department, operation, employee, subcontracting,
					subcontractor, owner, creation, issue_submitted_on, modified_by
				FROM `tabEmployee IR` WHERE name IN %(docs)s
				""",
				"docs",
				list(case_rows),
			)
		}
	cases = []
	for name in sorted(case_rows):
		head = headers.get(name)
		if not head:
			continue
		rows = []
		for row in case_rows[name]:
			mwo = mop_mwo.get(row.mop) or row.mwo
			fam = families.get(mwo, [])
			kind, legit, successors = _co_issue_row_kind(
				name, head.issue_submitted_on, row.mop, eir_by_mop.get(row.mop), fam
			)
			current = next((f for f in fam if f.name == row.mop), None)
			rows.append(
				{
					"row": cint(row.idx),
					"manufacturing_operation": row.mop,
					"manufacturing_work_order": mwo,
					"kind": kind,
					"legitimate_issue": legit.employee_ir if legit else None,
					"successor": successors[0].name if successors else None,
					"operation_status": current.status if current else None,
					"still_open": bool(
						current and cstr(current.status) not in closed_statuses
					),
				}
			)
		stale = [r for r in rows if r["kind"] != "current"]
		cases.append(
			{
				"employee_ir": name,
				"department": head.department,
				"operation": head.operation,
				"employee": head.employee,
				"created": _co_norm(head.creation),
				"created_by": head.owner,
				"submitted_on": _co_norm(head.issue_submitted_on),
				"submitted_by": head.modified_by,
				"rows": rows,
				"reopened_rows": sum(1 for r in rows if r["kind"] == "reopened"),
				"double_issued_rows": sum(
					1 for r in rows if r["kind"] == "double_issued"
				),
				"whole_document_stale": bool(rows) and len(stale) == len(rows),
				"open_operations_left": sum(1 for r in stale if r["still_open"]),
			}
		)
	out = _co_limited(cases, limit)
	out["counts"] = {
		"documents": len(cases),
		"reopened_rows": sum(c["reopened_rows"] for c in cases),
		"double_issued_rows": sum(c["double_issued_rows"] for c in cases),
		"documents_with_legitimate_rows": sum(
			1 for c in cases if not c["whole_document_stale"]
		),
	}
	return out


def _co_section_drafts(scope, limit):
	"""Employee Issue drafts with the guard's verdict, plus counts of the other draft kinds."""
	gm = _co_guard()
	if scope:
		names = _co_select_in(
			"""
			SELECT DISTINCT o.parent
			FROM `tabEmployee IR Operation` o
			INNER JOIN `tabEmployee IR` e ON e.name = o.parent
			WHERE o.parenttype = 'Employee IR' AND e.docstatus = 0
				AND o.manufacturing_work_order IN %(mwos)s
			""",
			"mwos",
			scope,
			pluck=True,
		)
		heads = (
			_co_select_in(
				"""
				SELECT name, type, owner, creation, modified, department, operation, employee
				FROM `tabEmployee IR` WHERE name IN %(docs)s
				""",
				"docs",
				names,
			)
			if names
			else []
		)
	else:
		heads = _co_select(
			"""
			SELECT name, type, owner, creation, modified, department, operation, employee
			FROM `tabEmployee IR` WHERE docstatus = 0
			"""
		)
	dir_drafts = _co_select(
		"SELECT type, COUNT(*) AS n FROM `tabDepartment IR` WHERE docstatus = 0 GROUP BY type"
	)
	now = now_datetime()
	rows = []
	for head in sorted(heads, key=lambda h: h.creation or datetime.min):
		if head.type != "Issue":
			continue
		doc = frappe.get_doc("Employee IR", head.name)
		refs = gm.refs(doc)
		mops = [mop for _idx, mop, _mwo in refs if mop]
		mwos = {mwo for _idx, _mop, mwo in refs if mwo}
		mop_rows, mwo_rows = gm._plain_rows(mops, mwos)
		family = gm._family(mwo_rows.keys())
		problems = gm._row_problems(
			doc, gm.EIR_ISSUE, "submit", mop_rows, mwo_rows, family
		)
		row_mwos = {
			mwo or (mop_rows.get(mop) or {}).get("manufacturing_work_order")
			for _idx, mop, mwo in refs
		} - {None, ""}
		others = gm.outstanding_issue_drafts(row_mwos, exclude=head.name)
		rows.append(
			{
				"draft": head.name,
				"owner": head.owner,
				"created": _co_norm(head.creation),
				"age_hours": round((now - head.creation).total_seconds() / 3600, 1)
				if head.creation
				else None,
				"department": head.department,
				"operation": head.operation,
				"employee": head.employee,
				"rows": len(refs),
				"work_orders": sorted(row_mwos),
				"verdict": "fresh" if not problems else "stale",
				"problems": [strip_html(cstr(message)) for _exc, message in problems],
				"other_drafts_on_same_work_orders": sorted({d.draft for d in others}),
			}
		)
	out = _co_limited(rows, limit)
	out["counts"] = {
		"employee_issue_drafts": len(rows),
		"stale": sum(1 for r in rows if r["verdict"] == "stale"),
		"fresh": sum(1 for r in rows if r["verdict"] == "fresh"),
		"employee_receive_drafts": sum(1 for h in heads if h.type != "Issue"),
		"department_ir_drafts": {r.type: r.n for r in dir_drafts},
	}
	out["note"] = (
		"Draft age is shown for information only; the verdict is the guard's own rule evaluation "
		"(submit phase). After the fix is deployed every Employee Issue draft blocks new Employee "
		"and Department IRs on its work orders until it is submitted, Discarded or Deleted."
	)
	return out


def _co_section_time_logs(time_logs, eir_by_mop, case_documents, limit):
	rows = []
	for t in sorted(time_logs, key=lambda x: (x.mwo or "", x.mop, cint(x.idx))):
		found = _co_time_log_opener(
			t.from_time, _co_submitted_issues(eir_by_mop.get(t.mop))
		)
		opener = found.employee_ir if found else None
		rows.append(
			{
				"time_log": t.name,
				"manufacturing_operation": t.mop,
				"manufacturing_work_order": t.mwo,
				"row": cint(t.idx),
				"from_time": _co_norm(t.from_time),
				"employee": t.employee,
				"owner": t.owner,
				"operation_status": t.mop_status,
				"is_current_operation": t.mop == t.pointer,
				"opened_by_issue": opener,
				"opener_is_stale_issue_case": bool(opener and opener in case_documents),
			}
		)
	out = _co_limited(rows, limit)
	out["counts"] = {
		"open_time_logs": len(rows),
		"on_finished_operations": sum(
			1 for r in rows if r["operation_status"] == "Finished"
		),
		"on_open_non_current_operations": sum(
			1
			for r in rows
			if r["operation_status"] != "Finished" and not r["is_current_operation"]
		),
		"attributed_to_stale_issue_cases": sum(
			1 for r in rows if r["opener_is_stale_issue_case"]
		),
		"unattributed": sum(1 for r in rows if not r["opened_by_issue"]),
	}
	return out


def _co_section_eod_ambiguity(non_pointer, limit):
	stale_mops = {r.name for r in non_pointer}
	mwos = sorted({r.mwo for r in non_pointer if r.mwo})
	logs = _co_select_in(
		"""
		SELECT name, manufacturing_work_order AS mwo, manufacturing_operation AS mop, voucher_type,
			voucher_no, item_code, batch_no, qty_after_transaction_batch_based AS qty,
			to_warehouse, creation
		FROM `tabMOP Log`
		WHERE is_synced = 0 AND is_cancelled = 0 AND manufacturing_work_order IN %(mwos)s
		""",
		"mwos",
		mwos,
	)
	on_stale = [log for log in logs if log.mop in stale_mops]
	newest = {}
	for log in logs:
		cur = newest.get(log.mwo)
		if cur is None or (log.creation, log.name) > (cur.creation, cur.name):
			newest[log.mwo] = log
	held = sorted({log.mwo for log in on_stale})
	work_orders = []
	for mwo in held:
		top = newest[mwo]
		work_orders.append(
			{
				"manufacturing_work_order": mwo,
				"stale_operations": sorted(
					{log.mop for log in on_stale if log.mwo == mwo}
				),
				"unsynced_logs_on_stale_operations": sum(
					1 for log in on_stale if log.mwo == mwo
				),
				"newest_unsynced_log": top.name,
				"newest_on_stale_operation": top.mop in stale_mops,
				"newest_target_warehouse": top.to_warehouse,
			}
		)

	runs = []
	if held:
		since = min(log.creation for log in on_stale).date()
		runs = _co_select_in(
			"""
			SELECT i.parent AS sync_log, sl.posting_date, sl.status AS run_status,
				i.manufacturing_work_order AS mwo, i.manufacturing_operation AS mop,
				i.status, i.is_synced, i.stock_entry, i.draft_stock_entry
			FROM `tabMOP EOD Sync Log Item` i
			INNER JOIN `tabMOP EOD Sync Log` sl ON sl.name = i.parent
			WHERE i.manufacturing_work_order IN %(mwos)s AND sl.posting_date >= %(since)s
			""",
			"mwos",
			held,
			{"since": since},
		)
	drafts = _co_unresolved_eod_drafts(held, stale_mops)
	out = _co_limited(
		[
			{
				"mop_log": log.name,
				"manufacturing_work_order": log.mwo,
				"manufacturing_operation": log.mop,
				"voucher": f"{log.voucher_type} {log.voucher_no}",
				"item_code": log.item_code,
				"batch_no": log.batch_no,
				"qty_after_transaction_batch_based": _co_norm(log.qty),
				"to_warehouse": log.to_warehouse,
				"created": _co_norm(log.creation),
			}
			for log in sorted(on_stale, key=lambda x: (x.mwo, x.creation, x.name))
		],
		limit,
	)
	out["work_orders"] = work_orders
	out["eod_runs_since_first_stale_log"] = [
		{k: _co_norm(v) for k, v in r.items()} for r in runs[:limit]
	]
	out["draft_unresolved_stock_entries"] = drafts
	out["counts"] = {
		"work_orders_held_by_new_rule": len(held),
		"unsynced_logs_on_open_non_current_operations": len(on_stale),
		"work_orders_where_old_planner_would_pick_stale_operation": sum(
			1 for w in work_orders if w["newest_on_stale_operation"]
		),
	}
	return out


def _co_unresolved_eod_drafts(mwos, mops):
	"""Draft "MOP EOD Sync (Unresolved)" Stock Entries naming any of ``mwos`` / ``mops``."""
	if not (mwos or mops) or not frappe.db.has_column(
		"Stock Entry", "custom_eod_sync_source"
	):
		return []
	mwo_column = (
		"sed.custom_manufacturing_work_order"
		if frappe.db.has_column("Stock Entry Detail", "custom_manufacturing_work_order")
		else "NULL"
	)
	rows = _co_select(
		f"""
		SELECT se.name, se.creation, sed.idx, sed.item_code, sed.qty,
			{mwo_column} AS mwo, sed.manufacturing_operation AS mop
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 0 AND se.custom_eod_sync_source = %(source)s
		""",
		{"source": CO_UNRESOLVED_EOD_SOURCE},
	)
	mwos, mops = set(mwos or ()), set(mops or ())
	out = {}
	for r in rows:
		entry = out.setdefault(
			r.name,
			{
				"stock_entry": r.name,
				"created": _co_norm(r.creation),
				"rows": 0,
				"matching_rows": [],
			},
		)
		entry["rows"] += 1
		if (r.mwo and r.mwo in mwos) or (r.mop and r.mop in mops):
			entry["matching_rows"].append(
				{
					"row": cint(r.idx),
					"manufacturing_work_order": r.mwo,
					"manufacturing_operation": r.mop,
					"item_code": r.item_code,
					"qty": _co_norm(r.qty),
				}
			)
	return [e for e in out.values() if e["matching_rows"]]


def _co_section_dir_duplicates(scope, limit):
	scope_sql = "AND o.manufacturing_work_order IN %(mwos)s" if scope else ""
	rows = _co_select(
		f"""
		SELECT o.parent AS department_ir, d.type, d.docstatus, o.manufacturing_work_order AS mwo,
			COUNT(*) AS n, GROUP_CONCAT(o.idx ORDER BY o.idx) AS row_idx,
			GROUP_CONCAT(IFNULL(o.manufacturing_operation, '') ORDER BY o.idx) AS operations
		FROM `tabDepartment IR Operation` o
		INNER JOIN `tabDepartment IR` d ON d.name = o.parent
		WHERE o.parenttype = 'Department IR' AND d.docstatus < 2
			AND IFNULL(o.manufacturing_work_order, '') != '' {scope_sql}
		GROUP BY o.parent, d.type, d.docstatus, o.manufacturing_work_order
		HAVING n > 1
		""",
		{"mwos": tuple(scope)} if scope else None,
	)
	out = _co_limited(
		[
			{
				"department_ir": r.department_ir,
				"type": r.type,
				"docstatus": cint(r.docstatus),
				"manufacturing_work_order": r.mwo,
				"rows": [cint(x) for x in cstr(r.row_idx).split(",") if x],
				"operations": cstr(r.operations).split(","),
			}
			for r in sorted(rows, key=lambda x: (x.department_ir, x.mwo))
		],
		limit,
	)
	out["counts"] = {
		"documents": len({r.department_ir for r in rows}),
		"issues": len({r.department_ir for r in rows if r.type == "Issue"}),
		"receives": len({r.department_ir for r in rows if r.type == "Receive"}),
	}
	out["note"] = (
		"A Receive mirrors its Issue row for row, so each duplicated Issue appears with its "
		"Receive; the Issue is where the twin operations were minted."
	)
	return out


def _co_section_status_edits(scope, families, open_statuses, closed_statuses, limit):
	"""Desk/API status edits of operations (Version rows).

	Internal writers change status with set_value / bulk_update (no Version) or save with an
	unchanged status, so a Version whose ``changed`` list carries ``status`` is a person editing
	the operation. "Status only" = that is the Version's only change.
	"""
	base = """
		SELECT name, docname, owner, creation,
			JSON_LENGTH(data, '$.changed') AS n_changed,
			JSON_UNQUOTE(JSON_EXTRACT(data, '$.changed[0][0]')) AS first_field,
			JSON_UNQUOTE(JSON_EXTRACT(data, '$.changed[0][1]')) AS old_value,
			JSON_UNQUOTE(JSON_EXTRACT(data, '$.changed[0][2]')) AS new_value,
			IFNULL(JSON_LENGTH(data, '$.added'), 0) + IFNULL(JSON_LENGTH(data, '$.removed'), 0)
				+ IFNULL(JSON_LENGTH(data, '$.row_changed'), 0) AS n_rows
		FROM `tabVersion`
		WHERE ref_doctype = 'Manufacturing Operation' AND data LIKE '%%["status",%%' {scope}
	"""
	if scope:
		names = [r.name for fam in families.values() for r in fam]
		rows = (
			_co_select_in(base.format(scope="AND docname IN %(mops)s"), "mops", names)
			if names
			else []
		)
	else:
		rows = _co_select(base.format(scope=""))
	status_only = [
		r
		for r in rows
		if cint(r.n_changed) == 1 and r.first_field == "status" and not cint(r.n_rows)
	]
	reopen = [
		r
		for r in rows
		if r.first_field == "status"
		and cstr(r.old_value) in closed_statuses
		and cstr(r.new_value) in open_statuses
	]
	by_owner = {}
	for r in rows:
		by_owner[r.owner] = by_owner.get(r.owner, 0) + 1
	out = _co_limited(
		[
			{
				"version": r.name,
				"manufacturing_operation": r.docname,
				"owner": r.owner,
				"created": _co_norm(r.creation),
				"status_change": [r.old_value, r.new_value]
				if r.first_field == "status"
				else None,
			}
			for r in sorted(reopen, key=lambda x: x.creation)
		],
		limit,
	)
	out["counts"] = {
		"versions_changing_status": len(rows),
		"status_only_versions": len(status_only),
		"reopen_edits_closed_to_open": len(reopen),
		"operations_edited": len({r.docname for r in rows}),
	}
	out["by_owner"] = dict(sorted(by_owner.items(), key=lambda kv: -kv[1]))
	out[
		"note"
	] = "Rows listed: edits that reopened a closed operation (now blocked by the fix)."
	return out


def _co_summary(report):
	onp = report["open_non_pointer"]
	return {
		"open_non_pointer_operations": onp["count"],
		"open_non_pointer_by_family": onp["by_family"],
		"open_non_pointer_by_kind_and_status": onp["by_kind_and_status"],
		"pointer_anomalies": {
			k: v
			for k, v in report["pointer_anomalies"]["counts"].items()
			if k not in ("normal", "submitted_work_orders") and v
		},
		"stale_issue_documents": report["stale_issue_cases"]["counts"]["documents"],
		"reopened_rows": report["stale_issue_cases"]["counts"]["reopened_rows"],
		"duplicate_issue_pairs": report["duplicate_submitted_issues"]["counts"][
			"pairs"
		],
		"outstanding_issue_drafts": report["outstanding_issue_drafts"]["counts"][
			"employee_issue_drafts"
		],
		"open_time_logs": report["open_time_log_fingerprint"]["count"],
		"eod_unsynced_logs_on_stale_operations": report["eod_ambiguity"]["count"],
		"dir_duplicate_row_documents": report["dir_duplicate_rows"]["counts"][
			"documents"
		],
		"status_only_versions": report["manual_status_edits"]["counts"][
			"status_only_versions"
		],
	}


# ---------------------------------------------------------------------------------------------
# family C: stale Employee Issue manifests
# ---------------------------------------------------------------------------------------------
#
# One manifest case per stale Employee IR Issue. It records (a) the EXPECTED current value of
# every row the repair will touch, including ``modified`` -- the repair aborts on any drift --
# (b) the ACTIONS: cancel the Issue through the supported path, restore each row operation to
# the state the legitimate Receive left it in, delete the stale open time logs, and (c) the
# EVIDENCE for every restored value, read from the documents that wrote it. Nothing is inferred
# from timestamps alone: a time log belongs to an Issue only when it opened inside that Issue's
# own submit, and a successor belongs to a Receive only when that Receive lists the operation.


def _co_one(query, values=None):
	rows = _co_select(query, values)
	return rows[0] if rows else None


def _co_cols(fields):
	return ", ".join(f"`{f}`" for f in fields)


def _co_versions(mops):
	"""Version rows of the given operations (index ref_doctype + docname), parsed, by operation."""
	out = {}
	rows = _co_select_in(
		"""
		SELECT name, docname, owner, creation, data
		FROM `tabVersion`
		WHERE ref_doctype = 'Manufacturing Operation' AND docname IN %(mops)s
		""",
		"mops",
		mops,
	)
	for row in sorted(rows, key=lambda r: (r.creation, r.name)):
		try:
			data = json.loads(row.data or "{}")
		except ValueError:
			data = {}
		out.setdefault(row.docname, []).append(
			frappe._dict(
				name=row.name, owner=row.owner, creation=row.creation, data=data
			)
		)
	return out


def _co_version_adding_row(versions, table, row_name):
	for version in versions or []:
		for added in version.data.get("added") or []:
			if (
				isinstance(added, list)
				and len(added) == 2
				and added[0] == table
				and isinstance(added[1], dict)
				and added[1].get("name") == row_name
			):
				return version
	return None


def _co_versions_changing(versions, field, *, to_none=False, before=None):
	"""Version changes of ``field`` (only those to an empty value with ``to_none``), made
	before ``before``."""
	found = []
	for version in versions or []:
		if before and version.creation >= before:
			continue
		for change in version.data.get("changed") or []:
			if not isinstance(change, list) or len(change) != 3 or change[0] != field:
				continue
			if to_none is True and change[2] not in (None, ""):
				continue
			found.append(
				{
					"version": version.name,
					"owner": version.owner,
					"created": _co_norm(version.creation),
					"old": change[1],
					"new": change[2],
				}
			)
	return found


# Sync Log Item stages written before the EOD planner picks a last operation -- the hold on an
# open non-current operation and the MWO-filter exclusion. Nothing is moved or reserved there.
_CO_EOD_PRE_PLAN_STAGES = ("Collect MOP Log",)
# Item statuses of a transfer that was made (or was in flight when the run stopped).
_CO_EOD_MOVING_STATUSES = ("Synced", "Pending")
# A Stock Entry's on_submit creates its reservations right after the entry's own save.
_CO_SRE_AFTER_ENTRY_SECONDS = 120


def _co_eod_items(mwos, since):
	"""EOD Sync Log Items of ``mwos`` from runs active at or after ``since``.

	The item table has no work-order index, so this is one scan of it per chunk.
	"""
	if not mwos:
		return []
	return _co_select_in(
		"""
		SELECT i.name, i.parent AS sync_log, i.manufacturing_work_order AS mwo,
			i.manufacturing_operation AS mop, i.item_code, i.target_warehouse, i.status,
			i.sync_stage, i.is_synced, i.stock_entry, i.draft_stock_entry, sl.posting_date,
			sl.trigger_type, sl.status AS run_status, sl.started_on,
			IFNULL(sl.completed_on, sl.modified) AS ended_on
		FROM `tabMOP EOD Sync Log Item` i
		INNER JOIN `tabMOP EOD Sync Log` sl ON sl.name = i.parent
		WHERE i.manufacturing_work_order IN %(mwos)s
			AND (sl.started_on >= %(since)s OR IFNULL(sl.completed_on, sl.modified) >= %(since)s)
		""",
		"mwos",
		mwos,
		{"since": since},
	)


def _co_snc_artifacts(mwos, since):
	"""Submitted Serial Number Creator ``Manufacture`` entries of the work orders' parent orders,
	created at or after ``since``, as ``{entry: mwo}``.

	The finished-goods entry carries the FG work order, not the metal one, so it is found through
	the parent order. Its submit marks every MOP Log of every operation of the order synced: it
	consumes the reserved stock, never the logs' balances.
	"""
	if not mwos:
		return {}
	snc_filter = (
		"AND IFNULL(se.custom_serial_number_creator, '') != ''"
		if frappe.db.has_column("Stock Entry", "custom_serial_number_creator")
		else ""
	)
	rows = _co_select_in(
		f"""
		SELECT se.name, w.name AS mwo
		FROM `tabManufacturing Work Order` w
		INNER JOIN `tabStock Entry` se ON se.manufacturing_order = w.manufacturing_order
		WHERE w.name IN %(mwos)s AND IFNULL(w.manufacturing_order, '') != ''
			AND se.stock_entry_type = 'Manufacture' AND se.docstatus = 1
			AND se.creation >= %(since)s {snc_filter}
		""",
		"mwos",
		mwos,
		{"since": since},
	)
	return {r.name: r.mwo for r in rows}


def _co_eod_entry_rows(mops, since, entries):
	"""Rows naming ``mops`` in EOD Stock Entries created at or after ``since``.

	EOD entries are the ones its Sync Log Items name (``entries``) and, where the column exists,
	every entry stamped ``custom_eod_sync_source``: every row EOD builds names the operation it
	moves, so these rows are the ground truth even when an item row was never recorded.
	"""
	stamped = frappe.db.has_column("Stock Entry", "custom_eod_sync_source")
	entries = sorted({e for e in entries or () if e})
	if not mops or not (entries or stamped):
		return []
	conditions = []
	params = {"since": since}
	if entries:
		conditions.append("se.name IN %(entries)s")
		params["entries"] = tuple(entries)
	if stamped:
		conditions.append("IFNULL(se.custom_eod_sync_source, '') != ''")
	return _co_select_in(
		f"""
		SELECT se.name AS stock_entry, se.docstatus, se.creation, sed.idx,
			sed.manufacturing_operation AS mop, sed.item_code, sed.qty, sed.s_warehouse,
			sed.t_warehouse
		FROM `tabStock Entry Detail` sed
		INNER JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE sed.manufacturing_operation IN %(mops)s AND se.creation >= %(since)s
			AND ({" OR ".join(conditions)})
		""",
		"mops",
		mops,
		params,
	)


def _co_eod_acting(items, artifacts):
	"""Items of runs that planned a transfer (past the pre-plan stage), minus SNC-artifact rows."""
	return [
		i
		for i in items
		if cstr(i.sync_stage) not in _CO_EOD_PRE_PLAN_STAGES
		and cstr(i.stock_entry) not in artifacts
	]


def _co_eod_sync_proof(mwo, stale_mop, logs):
	"""Did EOD move the stale operation's balance after its stale logs appeared? Read-only.

	EOD transfers only the operation it picks as the work order's last one, and every row it
	plans -- in its Sync Log Items and in its Stock Entry rows -- names that operation. So the
	synced stale logs are harmless history exactly when NO EOD activity after the first of them
	names the stale operation, and something identifiable closed them:

	* ``contradicted`` -- a run active after the first stale log transferred the stale operation
	  (a synced or in-flight item naming it), or a submitted EOD Stock Entry created after it has
	  a row naming it: stop for a stock review. A HELD item moved nothing; the draft entry and
	  any reservation a held run healed are judged by ``co_required_absent``;
	* ``proven`` -- nothing names it, and the logs were closed by an EOD run that processed the
	  work order after them (its items name other operations) or by the Serial Number Creator
	  realization of the work order's parent order;
	* ``missing`` -- nothing names it, but nothing explains the sync either (a patch, a manual
	  update): a stock review is needed.

	A run is matched by when it ran, not by its posting date: a run on the logs' date that
	finished before them never gathered them, and a later catch-up or selective run may have.
	"""
	since = min(get_datetime(log.creation) for log in logs)
	dates = sorted({get_datetime(log.creation).date() for log in logs})
	artifacts = _co_snc_artifacts([mwo], since)
	items = _co_eod_items([mwo], since)
	naming = [
		i
		for i in _co_eod_acting(items, artifacts)
		if i.mop == stale_mop
		and (cint(i.is_synced) or cstr(i.status) in _CO_EOD_MOVING_STATUSES)
	]
	entry_rows = [
		r
		for r in _co_eod_entry_rows(
			[stale_mop],
			since,
			({i.stock_entry for i in items} | {i.draft_stock_entry for i in items})
			- set(artifacts),
		)
		if cint(r.docstatus) == 1
	]
	by_run = {}
	for r in items:
		entry = by_run.setdefault(
			r.sync_log,
			{
				"sync_log": r.sync_log,
				"posting_date": _co_norm(r.posting_date),
				"trigger_type": r.trigger_type,
				"run_status": r.run_status,
				"started_on": _co_norm(r.started_on),
				"operations": set(),
				"items": 0,
				"synced_items": 0,
				"stock_entries": set(),
			},
		)
		entry["items"] += 1
		entry["synced_items"] += cint(r.is_synced)
		if r.mop:
			entry["operations"].add(r.mop)
		if r.stock_entry:
			entry["stock_entries"].add(r.stock_entry)
	run_rows = []
	for entry in sorted(
		by_run.values(), key=lambda e: (cstr(e["started_on"]), e["sync_log"])
	):
		entry["operations"] = sorted(entry["operations"])
		entry["stock_entries"] = sorted(entry["stock_entries"])
		run_rows.append(entry)
	closing_runs = sorted(
		{i.sync_log for i in items if cint(i.is_synced) and i.mop != stale_mop}
	)
	if naming or entry_rows:
		verdict = "contradicted"
	elif closing_runs or artifacts:
		verdict = "proven"
	else:
		verdict = "missing"
	return {
		"manufacturing_work_order": mwo,
		"stale_operation": stale_mop,
		"synced_logs": sorted(log.name for log in logs),
		"log_dates": [d.isoformat() for d in dates],
		"first_stale_log": _co_norm(since),
		"runs": run_rows,
		"eod_items_naming_stale_operation": [
			{
				"sync_log": i.sync_log,
				"status": i.status,
				"sync_stage": i.sync_stage,
				"item_code": i.item_code,
				"stock_entry": i.stock_entry or i.draft_stock_entry or None,
			}
			for i in naming
		],
		"eod_stock_entry_rows_naming_stale_operation": [
			{
				"stock_entry": r.stock_entry,
				"docstatus": cint(r.docstatus),
				"row": cint(r.idx),
				"item_code": r.item_code,
				"qty": _co_norm(r.qty),
				"t_warehouse": r.t_warehouse,
			}
			for r in entry_rows
		],
		"closed_by": {
			"eod_runs": closing_runs,
			"serial_number_creator_entries": sorted(artifacts),
		},
		"verdict": verdict,
	}


def _co_weights_preview(mwos, mops, voucher_logs):
	"""Header weights from the ledger now vs once the voucher's logs are cancelled (read-only).

	Same reading as ``recalculate_manufacturing_operation_weights``: latest active row per
	(item, batch), negatives clamped, carats converted once per family. The refining cutoff is
	not applied here; it only matters for refined work orders, which a stale Issue never touches.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import FIELD_MAP

	rows = _co_select_in(
		"""
		SELECT name, manufacturing_operation AS mop, item_code, batch_no,
			qty_after_transaction_batch_based AS qty, creation
		FROM `tabMOP Log`
		WHERE manufacturing_work_order IN %(mwos)s AND is_cancelled = 0
			AND manufacturing_operation IN %(mops)s
		""",
		"mwos",
		mwos,
		{"mops": tuple(sorted(mops)) or ("",)},
	)
	excluded = set(voucher_logs)

	def ledger(subset):
		latest = {}
		for r in sorted(subset, key=lambda x: (x.creation, x.name)):
			latest[(r.item_code, r.batch_no)] = r
		grams, carats = 0.0, {"diamond": 0.0, "gemstone": 0.0}
		for r in latest.values():
			prefix = FIELD_MAP.get((r.item_code or "")[:1])
			qty, _pcs = clamp_negative_balance(r.qty)
			if prefix in carats:
				carats[prefix] += qty
			elif prefix:
				grams += qty
		gross = (
			grams + carat_to_gram(carats["diamond"]) + carat_to_gram(carats["gemstone"])
		)
		return latest, flt(gross, 3)

	out = {}
	for mop in sorted(mops):
		own = [r for r in rows if r.mop == mop]
		before, gross_before = ledger(own)
		after, gross_after = ledger([r for r in own if r.name not in excluded])
		changed = []
		for key in sorted(
			set(before) | set(after), key=lambda k: (cstr(k[0]), cstr(k[1]))
		):
			b, a = before.get(key), after.get(key)
			bq = _co_norm(b.qty) if b else None
			aq = _co_norm(a.qty) if a else None
			if bq != aq:
				changed.append(
					{
						"item_code": key[0],
						"batch_no": key[1],
						"latest_now": bq,
						"latest_after": aq,
						"row_now": b.name if b else None,
						"row_after": a.name if a else None,
					}
				)
		out[mop] = {
			"ledger_gross_now": gross_before,
			"ledger_gross_after": gross_after,
			"header_change_expected": bool(changed),
			"keys_changing": changed,
		}
	return out


# Keys of co_required_absent that are listed for the reviewer and never block.
CO_REQUIRED_ABSENT_INFO = ("legitimate_later_artefacts",)


def _co_same_holder(issue_row, spec):
	"""The Issue row has the stale Issue's operation and holder (employee / subcontractor)."""
	if cstr(issue_row.operation) != cstr(spec.get("operation")):
		return False
	subcontracted = spec.get("subcontracting") == "Yes"
	if (issue_row.subcontracting == "Yes") != subcontracted:
		return False
	if subcontracted:
		return cstr(issue_row.subcontractor) == cstr(spec.get("subcontractor"))
	return cstr(issue_row.employee) == cstr(spec.get("employee"))


def _co_current_windows(spec, mops, families, eir_by_mop):
	"""Per row operation: how the stale Issue met it and until when it stayed current.

	Only a DOUBLE-ISSUED row stayed the work order's current operation after the stale submit
	(a REOPENED row already had its successor), until its first successor was minted. Work booked
	on it in that window is the legitimate holder's only when the stale Issue merely repeated the
	legitimate Issue's operation and holder; when it reassigned the operation, that work cannot be
	told apart from the stale holder's, so no window is granted.
	"""
	submitted_on = get_datetime(spec["submitted_on"])
	mop_mwo = {r.name: r.mwo for fam in families.values() for r in fam}
	out = {}
	for mop in mops:
		kind, legit, successors = _co_issue_row_kind(
			spec["employee_ir"],
			submitted_on,
			mop,
			eir_by_mop.get(mop),
			families.get(mop_mwo.get(mop), []),
		)
		out[mop] = frappe._dict(
			kind=kind,
			successor=successors[0].name if successors else None,
			until=successors[0].creation if successors else None,
			holder_kept=bool(legit and _co_same_holder(legit, spec)),
		)
	return out


def _co_while_current(window, at):
	"""``at`` lies in the row operation's legitimate current window (see _co_current_windows)."""
	return bool(
		window
		and window.kind == "double_issued"
		and window.holder_kept
		and window.until
		and at
		and get_datetime(at) < get_datetime(window.until)
	)


def _co_window_text(ctx, ops):
	return ", ".join(
		f"{op} was still current (its successor {ctx.windows[op].successor} came later)"
		for op in ops
	)


def _co_entry_verdict(head, ops, ctx):
	"""``(blocks, why)`` for a Stock Entry booked on row operations after the stale submit."""
	if cstr(head.employee_ir) == ctx.eir:
		return True, "booked by the stale Issue itself"
	if cstr(head.employee_ir) in ctx.allowed:
		return False, f"booked by {head.employee_ir}, part of the reviewed history"
	if head.name in ctx.eod_entries or cstr(head.eod_source):
		return (
			True,
			"an EOD entry on a stale operation after the stale Issue: stock review",
		)
	late = [
		op for op in ops if not _co_while_current(ctx.windows.get(op), head.creation)
	]
	if ops and not late:
		return False, (
			"booked while "
			+ _co_window_text(ctx, ops)
			+ " and the stale Issue only repeated its legitimate holder"
		)
	return True, (
		"booked on "
		+ ", ".join(late or ops)
		+ " after the work order had moved past it, or after the stale Issue reassigned it"
	)


def _co_employee_ir_verdict(row, ctx):
	"""``(blocks, why)`` for another Employee IR naming a row operation after the stale submit."""
	if (
		row.type == "Receive"
		and cint(row.docstatus) == 1
		and _co_while_current(ctx.windows.get(row.mop), row.modified)
	):
		return False, (
			f"Receive {row.employee_ir} while "
			+ _co_window_text(ctx, [row.mop])
			+ " and the stale Issue only repeated its legitimate holder"
		)
	return True, (
		f"{row.type} {row.employee_ir} (docstatus {cint(row.docstatus)}) acted on {row.mop} after "
		"the stale Issue and is not part of the reviewed history"
	)


def _co_reservation_verdict(sre, ctx):
	"""``(blocks, why)`` for a reservation naming a row operation, created after the stale submit.

	Attribution, strongest evidence first: the replaced-reservation snapshot names the Employee
	IR whose loss booking re-created it; otherwise the Stock Entry whose submit created it (same
	operation, item and target warehouse, within that submit); otherwise an EOD run that was
	planning the operation at the time. Anything else is unexplained and blocks.
	"""
	if sre.replaced_by:
		if sre.replaced_by == ctx.eir:
			return True, f"re-created by the stale Issue {sre.replaced_by}"
		if sre.replaced_by in ctx.allowed:
			return False, (
				f"re-created by {sre.replaced_by}'s loss booking (replaced-reservation snapshot), "
				"part of the reviewed history"
			)
		return (
			True,
			f"re-created by {sre.replaced_by}, which is not part of the reviewed history",
		)
	created = get_datetime(sre.creation)
	window = timedelta(seconds=_CO_SRE_AFTER_ENTRY_SECONDS)
	candidates = sorted(
		name
		for name, head in ctx.entry_heads.items()
		if get_datetime(head.creation)
		<= created
		<= get_datetime(head.modified) + window
		and any(
			r.mop == sre.mop
			and cstr(r.item_code) == cstr(sre.item_code)
			and cstr(r.t_warehouse) == cstr(sre.warehouse)
			for r in ctx.entry_rows[name]
		)
	)
	if len(candidates) == 1:
		blocks, why = ctx.entry_verdicts[candidates[0]]
		return blocks, f"created by the submit of {candidates[0]}: {why}"
	runs = sorted(
		{
			i.sync_log
			for i in ctx.eod_acting
			if i.mop == sre.mop
			and i.started_on
			and get_datetime(i.started_on) <= created <= get_datetime(i.ended_on)
		}
	)
	if runs:
		return (
			True,
			f"reserved by EOD run {', '.join(runs)} for a stale operation: stock review",
		)
	return True, "no document of the reviewed history explains it" + (
		f" (ambiguous between {', '.join(candidates)})" if candidates else ""
	)


def _co_mop_log_verdict(log, ctx):
	"""``(blocks, why)`` for a MOP Log on a row operation after the stale submit."""
	if log.voucher_type == "Employee IR":
		if log.voucher_no in ctx.allowed:
			return False, f"written by {log.voucher_no}, part of the reviewed history"
		row = ctx.employee_ir_rows.get((log.voucher_no, log.mop))
		if row:
			return _co_employee_ir_verdict(row, ctx)
		return (
			True,
			f"written by Employee IR {log.voucher_no}, which does not name {log.mop}",
		)
	if log.voucher_type == "Stock Entry":
		if log.voucher_no in ctx.entry_verdicts:
			return ctx.entry_verdicts[log.voucher_no]
		if log.voucher_no in ctx.allowed_entries:
			return False, f"written by {log.voucher_no} of the reviewed history"
		return (
			True,
			f"written by Stock Entry {log.voucher_no}, which does not name {log.mop}",
		)
	return True, (
		f"written by {log.voucher_type} {log.voucher_no} on a stale operation after the stale Issue"
	)


def co_required_absent(spec):
	"""What must NOT exist for the reviewed repair to be safe, and what is legitimately there.

	Read-only. ``spec`` comes from the manifest (``required_absent_spec``), so the repair
	re-runs exactly the checks the reviewer saw. Every list must be empty, except
	``draft_unresolved_eod_stock_entries`` entries the manifest lists for deletion and the
	informational ``legitimate_later_artefacts`` (CO_REQUIRED_ABSENT_INFO), which never blocks.

	Older stale Issues have later history on their row operations, so everything found there
	after the stale submit is attributed by evidence:

	* CAUSED by the stale Issue -- blocks: its own Stock Entries, QC, trees, purchase orders,
	  timesheet rows and amendments; any EOD Stock Entry on a stale operation after the submit,
	  and any reservation an EOD run made for one (an EOD that picked a stale operation moved or
	  reserved the stale balances: stock review); anything that acted on a row operation after
	  the work order had moved past it.
	* LEGITIMATE -- listed for the reviewer: what the legitimate Issue and the Receive that
	  finished the operation wrote (``allowed_documents``) -- their Stock Entries, MOP Logs and the
	  reservations the Receive's loss booking re-created -- and, on a DOUBLE-ISSUED row whose
	  stale Issue only repeated the legitimate holder, work booked while that operation was still
	  the work order's current one (a Material Transfer before the Receive, say).
	* Anything that cannot be attributed blocks.

	Already-synced stale logs are judged separately by the EOD Sync Log proof
	(``_co_eod_sync_proof``), which nothing here relaxes.
	"""
	eir = spec["employee_ir"]
	after = get_datetime(spec["submitted_on"])
	mops = sorted(spec.get("operations") or [])
	mwos = sorted(spec.get("work_orders") or [])
	allowed = sorted(set(spec.get("allowed_documents") or []))
	checks = {}
	legitimate = []

	families = _co_families(mwos)
	eir_by_mop = _co_employee_ir_rows(mops)
	dir_by_mwo = _co_department_ir_rows(mwos)
	eod_items = _co_eod_items(mwos, after)
	artifacts = _co_snc_artifacts(mwos, after)
	ctx = frappe._dict(
		eir=eir,
		allowed=set(allowed),
		windows=_co_current_windows(spec, mops, families, eir_by_mop),
		eod_acting=_co_eod_acting(eod_items, artifacts),
		eod_entries=(
			{i.stock_entry for i in eod_items}
			| {i.draft_stock_entry for i in eod_items}
		)
		- set(artifacts)
		- {None, ""},
		allowed_entries=set(
			_co_select_in(
				"SELECT name FROM `tabStock Entry` WHERE employee_ir IN %(docs)s",
				"docs",
				allowed,
				pluck=True,
			)
		),
	)

	def note(check, doctype, name, mop, created, why, **extra):
		legitimate.append(
			{
				"check": check,
				"doctype": doctype,
				"name": name,
				"manufacturing_operation": mop,
				"created": _co_norm(created),
				"why": why,
				**extra,
			}
		)

	# -- caused by the stale Issue itself -------------------------------------------------------
	checks["stock_entries_linked"] = _co_select(
		"""
		SELECT name, docstatus FROM `tabStock Entry`
		WHERE employee_ir = %(eir)s AND docstatus < 2
		""",
		{"eir": eir},
	)
	checks["quality_checks_linked"] = _co_select(
		"""
		SELECT name, docstatus FROM `tabQC`
		WHERE (employee_ir = %(eir)s OR emp_ir_id = %(eir)s) AND docstatus < 2
		""",
		{"eir": eir},
	)
	checks["tree_numbers_linked"] = _co_select(
		"SELECT name, status FROM `tabTree Number` WHERE employee_ir = %(eir)s",
		{"eir": eir},
	)
	checks["purchase_orders_linked"] = (
		_co_select(
			"""
			SELECT name, docstatus FROM `tabPurchase Order`
			WHERE employee_ir = %(eir)s AND docstatus < 2
			""",
			{"eir": eir},
		)
		if frappe.db.has_column("Purchase Order", "employee_ir")
		else []
	)
	checks["timesheet_details_linked"] = (
		_co_select(
			"""
			SELECT name, parent FROM `tabTimesheet Detail`
			WHERE custom_employee_ir = %(eir)s AND docstatus < 2
			""",
			{"eir": eir},
		)
		if frappe.db.has_column("Timesheet Detail", "custom_employee_ir")
		else []
	)
	checks["amendments"] = _co_select(
		"""
		SELECT name, docstatus FROM `tabEmployee IR`
		WHERE amended_from = %(eir)s AND docstatus < 2
		""",
		{"eir": eir},
	)

	# -- Stock Entries booked on the row operations after the submit ----------------------------
	# An EOD entry always blocks (an EOD that picked a stale operation moved its balance, or
	# will when its draft is retried) -- except a held run's draft "Unresolved" entry, which
	# draft_unresolved_eod_stock_entries reports and the manifest may list for deletion. A held
	# run's item rows alone moved nothing; a reservation it healed is judged further down.
	eod_column = (
		"se.custom_eod_sync_source"
		if frappe.db.has_column("Stock Entry", "custom_eod_sync_source")
		else "NULL"
	)
	entry_rows = _co_select_in(
		f"""
		SELECT se.name, se.docstatus, se.stock_entry_type, se.employee_ir, se.creation,
			se.modified, {eod_column} AS eod_source, se.manufacturing_operation AS head_mop,
			sed.manufacturing_operation AS row_mop, sed.item_code, sed.t_warehouse
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus < 2 AND se.creation > %(after)s
			AND (sed.manufacturing_operation IN %(mops)s OR se.manufacturing_operation IN %(mops)s)
		""",
		"mops",
		mops,
		{"after": after},
	)
	ctx.entry_heads, ctx.entry_rows, ctx.entry_verdicts = {}, {}, {}
	for r in entry_rows:
		ctx.entry_heads.setdefault(r.name, r)
		ctx.entry_rows.setdefault(r.name, []).append(
			frappe._dict(
				mop=r.row_mop or r.head_mop,
				item_code=r.item_code,
				t_warehouse=r.t_warehouse,
			)
		)
	found = []
	for name, head in sorted(ctx.entry_heads.items()):
		ops = sorted(
			{r.row_mop for r in entry_rows if r.name == name and r.row_mop in mops}
			| ({head.head_mop} if head.head_mop in mops else set())
		)
		blocks, why = ctx.entry_verdicts[name] = _co_entry_verdict(head, ops, ctx)
		if cstr(head.employee_ir) == eir:
			continue  # reported by stock_entries_linked
		if (
			cint(head.docstatus) == 0
			and cstr(head.eod_source) == CO_UNRESOLVED_EOD_SOURCE
		):
			continue  # reported by draft_unresolved_eod_stock_entries
		finding = {
			"name": name,
			"docstatus": cint(head.docstatus),
			"stock_entry_type": head.stock_entry_type,
			"employee_ir": head.employee_ir,
			"manufacturing_operations": ops,
			"creation": head.creation,
			"why": why,
		}
		if blocks:
			found.append(finding)
		else:
			note(
				"stock_entries_on_operations_after_submit",
				"Stock Entry",
				name,
				", ".join(ops),
				head.creation,
				why,
			)
	checks["stock_entries_on_operations_after_submit"] = found

	# -- other Employee IRs and Department IRs naming the row operations ------------------------
	ctx.employee_ir_rows = {}
	found = []
	for rows in eir_by_mop.values():
		for r in rows:
			if (
				r.employee_ir == eir
				or r.employee_ir in ctx.allowed
				or cint(r.docstatus) == 2
			):
				continue
			touched = max(
				get_datetime(r.creation), get_datetime(r.modified or r.creation)
			)
			if touched <= after:
				continue
			ctx.employee_ir_rows[(r.employee_ir, r.mop)] = r
			blocks, why = _co_employee_ir_verdict(r, ctx)
			finding = {
				"employee_ir": r.employee_ir,
				"type": r.type,
				"docstatus": cint(r.docstatus),
				"row": cint(r.idx),
				"manufacturing_operation": r.mop,
				"created": _co_norm(r.creation),
				"why": why,
			}
			if blocks:
				found.append(finding)
			else:
				note(
					"employee_ir_rows_after_submit",
					"Employee IR",
					r.employee_ir,
					r.mop,
					r.creation,
					why,
				)
	checks["employee_ir_rows_after_submit"] = found
	checks["department_ir_rows_after_submit"] = [
		{
			"department_ir": d.department_ir,
			"type": d.type,
			"docstatus": cint(d.docstatus),
			"row": cint(d.idx),
			"manufacturing_operation": d.mop,
			"created": _co_norm(d.creation),
			"why": "a Department IR acted on a stale operation after the stale Issue",
		}
		for rows in dir_by_mwo.values()
		for d in rows
		if d.mop in mops and cint(d.docstatus) < 2 and d.creation and d.creation > after
	]

	# -- reservations naming the row operations -------------------------------------------------
	snapshot = (
		"JSON_UNQUOTE(JSON_EXTRACT(custom_replaced_sre_snapshot, '$.employee_ir'))"
		if frappe.db.has_column(
			"Stock Reservation Entry", "custom_replaced_sre_snapshot"
		)
		else "NULL"
	)
	found = []
	for sre in _co_select_in(
		f"""
		SELECT name, status, manufacturing_work_order AS mwo, manufacturing_operation AS mop,
			item_code, warehouse, reserved_qty, owner, creation, {snapshot} AS replaced_by
		FROM `tabStock Reservation Entry`
		WHERE manufacturing_operation IN %(mops)s AND docstatus = 1 AND creation > %(after)s
		""",
		"mops",
		mops,
		{"after": after},
	):
		blocks, why = _co_reservation_verdict(sre, ctx)
		finding = {
			"name": sre.name,
			"manufacturing_operation": sre.mop,
			"item_code": sre.item_code,
			"warehouse": sre.warehouse,
			"status": sre.status,
			"creation": sre.creation,
			"why": why,
		}
		if blocks:
			found.append(finding)
		else:
			note(
				"reservations_on_operations_after_submit",
				"Stock Reservation Entry",
				sre.name,
				sre.mop,
				sre.creation,
				why,
			)
	checks["reservations_on_operations_after_submit"] = found

	# -- MOP Logs written on the row operations by anyone but the stale Issue -------------------
	found, legit_logs = [], {}
	for log in _co_select_in(
		"""
		SELECT name, manufacturing_operation AS mop, voucher_type, voucher_no, creation
		FROM `tabMOP Log`
		WHERE manufacturing_work_order IN %(mwos)s AND is_cancelled = 0
			AND creation > %(after)s AND manufacturing_operation IN %(mops)s
		""",
		"mwos",
		mwos,
		{"after": after, "mops": tuple(mops) or ("",)},
	):
		if log.voucher_type == "Employee IR" and log.voucher_no == eir:
			continue
		blocks, why = _co_mop_log_verdict(log, ctx)
		if blocks:
			found.append(
				{
					"mop_log": log.name,
					"manufacturing_operation": log.mop,
					"voucher": f"{log.voucher_type} {log.voucher_no}",
					"created": _co_norm(log.creation),
					"why": why,
				}
			)
			continue
		key = (log.voucher_type, log.voucher_no, log.mop)
		entry = legit_logs.setdefault(
			key, {"rows": 0, "first": log.creation, "why": why}
		)
		entry["rows"] += 1
		entry["first"] = min(entry["first"], log.creation)
	for (voucher_type, voucher_no, mop), entry in sorted(legit_logs.items()):
		note(
			"other_mop_logs_after_submit",
			"MOP Log",
			f"{voucher_type} {voucher_no}",
			mop,
			entry["first"],
			entry["why"],
			rows=entry["rows"],
		)
	checks["other_mop_logs_after_submit"] = found

	# -- the work orders themselves -------------------------------------------------------------
	checks["drafts_on_work_orders"] = [
		{"doctype": "Employee IR", "name": n}
		for n in _co_select_in(
			"""
			SELECT DISTINCT o.parent FROM `tabEmployee IR Operation` o
			INNER JOIN `tabEmployee IR` e ON e.name = o.parent
			WHERE o.parenttype = 'Employee IR' AND e.docstatus = 0
				AND o.manufacturing_work_order IN %(mwos)s
			""",
			"mwos",
			mwos,
			pluck=True,
		)
	] + [
		{"doctype": "Department IR", "name": n}
		for n in sorted(
			{
				d.department_ir
				for rows in dir_by_mwo.values()
				for d in rows
				if cint(d.docstatus) == 0
			}
		)
	]
	checks["draft_unresolved_eod_stock_entries"] = _co_unresolved_eod_drafts(mwos, mops)
	operation_row = (
		_co_one(
			"SELECT tree_no_reqd FROM `tabDepartment Operation` WHERE name = %(op)s",
			{"op": spec.get("operation")},
		)
		if spec.get("operation")
		else None
	)
	checks["casting_tree_required"] = (
		[spec.get("operation")]
		if operation_row and cint(operation_row.tree_no_reqd)
		else []
	)
	checks["cancel_warehouses_missing"] = _co_cancel_warehouses_missing(spec)
	checks["legitimate_later_artefacts"] = sorted(
		legitimate, key=lambda r: (cstr(r["created"]), r["check"], cstr(r["name"]))
	)
	return {key: [_co_jsonable(r) for r in rows] for key, rows in checks.items()}


def co_blocking_required(required):
	"""The blocking part of a ``co_required_absent`` result (drops the informational keys)."""
	return {
		k: v for k, v in (required or {}).items() if k not in CO_REQUIRED_ABSENT_INFO
	}


def _co_jsonable(row):
	if isinstance(row, dict):
		return {
			k: _co_jsonable(v) if isinstance(v, (dict, list)) else _co_norm(v)
			for k, v in row.items()
		}
	if isinstance(row, list):
		return [_co_jsonable(v) for v in row]
	return _co_norm(row)


def _co_cancel_warehouses_missing(spec):
	"""The supported cancel throws unless both Manufacturing warehouses resolve (employee_ir.py)."""
	missing = []
	if not _co_one(
		"""
		SELECT name FROM `tabWarehouse`
		WHERE disabled = 0 AND department = %(dep)s AND warehouse_type = 'Manufacturing'
		LIMIT 1
		""",
		{"dep": spec.get("department")},
	):
		missing.append(f"department warehouse for {spec.get('department')}")
	if spec.get("subcontracting") == "Yes":
		found = _co_one(
			"""
			SELECT name FROM `tabWarehouse`
			WHERE disabled = 0 AND company = %(company)s AND subcontractor = %(sub)s
				AND warehouse_type = 'Manufacturing'
			LIMIT 1
			""",
			{"company": spec.get("company"), "sub": spec.get("subcontractor")},
		)
		label = f"subcontractor warehouse for {spec.get('subcontractor')}"
	else:
		found = _co_one(
			"""
			SELECT name FROM `tabWarehouse`
			WHERE disabled = 0 AND employee = %(emp)s AND warehouse_type = 'Manufacturing'
			LIMIT 1
			""",
			{"emp": spec.get("employee")},
		)
		label = f"employee warehouse for {spec.get('employee')}"
	if not found:
		missing.append(label)
	return missing


def co_build_stale_issue_case(employee_ir):
	"""Everything the reviewed repair of ONE stale Employee Issue needs. Read-only; see above."""
	blockers, review = [], []
	case = {
		"case_id": f"C:{employee_ir}",
		"family": "C",
		"employee_ir": employee_ir,
		"status": None,
		"blockers": blockers,
		"needs_review": review,
	}
	head = _co_one(
		f"SELECT {_co_cols(CO_EIR_FIELDS)} FROM `tabEmployee IR` WHERE name = %(name)s",
		{"name": employee_ir},
	)
	if not head:
		blockers.append("Employee IR not found.")
		return _co_seal(case)
	if head.type != "Issue":
		blockers.append(
			f"{employee_ir} is a {head.type}; only Employee Issues are family C."
		)
	if cint(head.docstatus) != 1:
		blockers.append(
			f"{employee_ir} has docstatus {head.docstatus}; only a submitted Issue can be repaired "
			"(a cancelled one may already have been repaired -- run the repair in plan mode)."
		)
	rows = _co_select(
		f"""
		SELECT {_co_cols(CO_EIR_ROW_FIELDS)} FROM `tabEmployee IR Operation`
		WHERE parent = %(name)s AND parenttype = 'Employee IR'
		ORDER BY idx
		""",
		{"name": employee_ir},
	)
	if not rows:
		blockers.append(f"{employee_ir} has no rows.")
	if blockers:
		case["expected"] = {"employee_ir": _co_norm_row(head, CO_EIR_FIELDS)}
		return _co_seal(case)

	mops = sorted(
		{r.manufacturing_operation for r in rows if r.manufacturing_operation}
	)
	mop_state = {
		r.name: r
		for r in _co_select_in(
			f"SELECT {_co_cols(CO_MOP_FIELDS)} FROM `tabManufacturing Operation` "
			"WHERE name IN %(mops)s",
			"mops",
			mops,
		)
	}
	mwos = sorted(
		{r.manufacturing_work_order for r in rows if r.manufacturing_work_order}
		| {
			m.manufacturing_work_order
			for m in mop_state.values()
			if m.manufacturing_work_order
		}
	)
	mwo_state = {
		r.name: r
		for r in _co_select_in(
			f"SELECT {_co_cols(CO_MWO_FIELDS)} FROM `tabManufacturing Work Order` "
			"WHERE name IN %(mwos)s",
			"mwos",
			mwos,
		)
	}
	families = _co_families(mwos)
	eir_by_mop = _co_employee_ir_rows(mops)
	versions = _co_versions(mops)
	time_logs = {}
	for t in _co_select_in(
		f"""
		SELECT {_co_cols(CO_TIME_LOG_FIELDS)} FROM `tabManufacturing Operation Time Log`
		WHERE parent IN %(mops)s AND parenttype = 'Manufacturing Operation'
			AND parentfield = 'time_logs'
		""",
		"mops",
		mops,
	):
		time_logs.setdefault(t.parent, []).append(t)
	for found in time_logs.values():
		found.sort(key=lambda t: cint(t.idx))
	voucher_logs = _co_select_in(
		f"""
		SELECT {_co_cols(CO_MOP_LOG_FIELDS)} FROM `tabMOP Log`
		WHERE manufacturing_work_order IN %(mwos)s AND voucher_type = 'Employee IR'
			AND voucher_no = %(eir)s
		""",
		"mwos",
		mwos,
		{"eir": employee_ir},
	)
	totals = co_voucher_log_totals(employee_ir)
	if totals["total"] != len(voucher_logs):
		blockers.append(
			f"{totals['total'] - len(voucher_logs)} MOP Log row(s) of {employee_ir} sit outside its "
			"rows' work orders; the supported cancel would cancel them too."
		)

	submitted_on = head.issue_submitted_on
	subcontracting = head.subcontracting == "Yes"
	restore_fields = (
		CO_RESTORE_FIELDS_SUBCONTRACTOR
		if subcontracting
		else CO_RESTORE_FIELDS_EMPLOYEE
	)
	if subcontracting:
		review.append(
			"Subcontracting Issue: subcontractor / for_subcontracting targets need a manual look."
		)

	row_reports, restore, evidence, changes, delete_tls = [], {}, {}, {}, {}
	legit_docs, allowed = set(), set()
	plans = []
	for row in rows:
		mop = row.manufacturing_operation
		m = mop_state.get(mop)
		label = f"Row {row.idx} ({mop})"
		if not m:
			blockers.append(f"{label}: the operation does not exist.")
			continue
		mwo = m.manufacturing_work_order
		if row.manufacturing_work_order and row.manufacturing_work_order != mwo:
			blockers.append(
				f"{label}: row work order {row.manufacturing_work_order} != {mwo}."
			)
		w = mwo_state.get(mwo)
		fam = families.get(mwo, [])
		kind, legit, successors = _co_issue_row_kind(
			employee_ir, submitted_on, mop, eir_by_mop.get(mop), fam
		)
		if kind == "current":
			blockers.append(
				f"{label}: {employee_ir} issued this operation while it was current -- it is the "
				"legitimate Issue for this row, so cancelling the whole document would undo real "
				"work. Needs a row-level repair, not this manifest."
			)
		if not successors:
			blockers.append(
				f"{label}: the operation has no successor; it was never finished."
			)
		if w and w.manufacturing_operation == mop:
			blockers.append(
				f"{label}: the operation is still work order {mwo}'s current one."
			)
		receives = [
			r
			for r in eir_by_mop.get(mop, [])
			if r.type == "Receive" and cint(r.docstatus) == 1
		]
		minted = None
		for s in successors:
			rec = next((r for r in receives if r.employee_ir == s.employee_ir), None)
			if rec:
				minted = (s, rec)
				break
		if legit:
			legit_docs.add(legit.employee_ir)
			allowed.add(legit.employee_ir)
		if minted:
			allowed.add(minted[1].employee_ir)
		plans.append((row, m, mwo, kind, legit, successors, minted))

	legit_rows = _co_employee_ir_rows_of(legit_docs)

	for row, m, mwo, kind, legit, successors, minted in plans:
		mop = m.name
		label = f"Row {row.idx} ({mop})"
		tls = time_logs.get(mop, [])
		mop_versions = versions.get(mop, [])
		target, why = {}, {}

		successor, receive = (
			minted if minted else (successors[0] if successors else None, None)
		)
		if successor is not None:
			target["status"] = "Finished"
			why["status"] = (
				f"{successor.name} was minted from it on {_co_norm(successor.creation)} by "
				f"{'Employee IR Receive ' + receive.employee_ir if receive else 'Department IR Issue ' + cstr(successor.department_issue_id)}"
				f"; a finished operation's status is Finished."
			)
		else:
			target["status"] = _co_norm(m.status)
			why["status"] = "no successor (blocked)"
		if successor is not None and receive is None:
			review.append(
				f"{label}: successor {successor.name} was not minted by an Employee IR Receive of "
				"this operation; operation / holder targets need a manual look."
			)

		if legit:
			target["operation"] = legit.operation
			why[
				"operation"
			] = f"operation of {legit.employee_ir}, the Issue that issued it while current"
		elif receive:
			target["operation"] = receive.operation
			why["operation"] = f"operation of Receive {receive.employee_ir}"
		else:
			target["operation"] = m.operation
			why[
				"operation"
			] = "no legitimate Issue or Receive found; kept as currently stored"
			review.append(f"{label}: operation target kept as stored ({m.operation}).")

		if subcontracting:
			if legit and legit.subcontracting == "Yes":
				target["subcontractor"] = legit.subcontractor
				why["subcontractor"] = f"subcontractor of {legit.employee_ir}"
			else:
				target["subcontractor"] = m.subcontractor
				why["subcontractor"] = "kept as currently stored"
			target["for_subcontracting"] = cint(m.for_subcontracting)
			why["for_subcontracting"] = "value before the cancel (the cancel forces 1)"
		elif receive:
			target["employee"] = receive.employee
			why[
				"employee"
			] = f"written by Receive {receive.employee_ir} (Receive.employee on finish)"
		elif legit:
			target["employee"] = legit.employee
			why["employee"] = f"employee of {legit.employee_ir}"
		else:
			target["employee"] = m.employee
			why[
				"employee"
			] = "no legitimate Issue or Receive found; kept as currently stored"
			review.append(f"{label}: employee target kept as stored ({m.employee}).")

		issues_of_mop = _co_submitted_issues(eir_by_mop.get(mop))
		opener = {
			t.name: (
				_co_time_log_opener(t.from_time, issues_of_mop) or frappe._dict()
			).get("employee_ir")
			for t in tls
		}
		stale_tls = [t for t in tls if not t.to_time and opener[t.name] == employee_ir]
		other_open = [t for t in tls if not t.to_time and opener[t.name] != employee_ir]
		if len(stale_tls) > 1:
			blockers.append(
				f"{label}: {len(stale_tls)} open time logs fall inside {head.name}'s submit; "
				"cannot tell which one it opened."
			)
		if other_open:
			blockers.append(
				f"{label}: open time log(s) {', '.join(t.name for t in other_open)} were not opened "
				f"by {head.name} (opened by: "
				+ ", ".join(
					cstr(opener[t.name] or "no Issue submit") for t in other_open
				)
				+ ")."
			)
		delete_tls[mop] = [t.name for t in stale_tls]
		tl_evidence = []
		for t in stale_tls:
			added = _co_version_adding_row(mop_versions, "time_logs", t.name)
			tl_evidence.append(
				{
					"time_log": t.name,
					"from_time": _co_norm(t.from_time),
					"opened_seconds_after_submit": _co_seconds(
						t.from_time, submitted_on
					),
					"added_by_version": added.name if added else None,
					"version_owner": added.owner if added else None,
				}
			)
		if not stale_tls:
			review.append(
				f"{label}: no open time log of {head.name} found (nothing to delete)."
			)

		legit_tls = (
			[t for t in tls if opener[t.name] == legit.employee_ir] if legit else []
		)
		if legit and len(legit_tls) == 1:
			lt = legit_tls[0]
			added = _co_version_adding_row(mop_versions, "time_logs", lt.name)
			target["start_time"] = _co_norm(lt.from_time)
			why["start_time"] = (
				f"from_time of time log {lt.name}, opened by {legit.employee_ir}'s submit "
				f"({_co_norm(legit.issue_submitted_on)})"
				+ (f"; added by Version {added.name}" if added else "")
			)
		elif legit:
			target["start_time"] = _co_norm(m.start_time)
			why[
				"start_time"
			] = f"time log of {legit.employee_ir} not identified (blocked)"
			blockers.append(
				f"{label}: {len(legit_tls)} time logs match {legit.employee_ir}'s submit; "
				"start_time cannot be proven."
			)
		else:
			target["start_time"] = None
			why["start_time"] = (
				"no Issue ever issued the operation while it was current; the only start was "
				f"{head.name}'s, which the repair removes"
			)
			review.append(f"{label}: start_time target is empty (no legitimate Issue).")

		target["started_time"] = None
		cleared = _co_versions_changing(
			mop_versions, "started_time", to_none=True, before=submitted_on
		)
		why["started_time"] = (
			"cleared when the operation was finished: "
			+ ", ".join(
				f"Version {c['version']} ({c['created']}, {c['owner']})"
				for c in cleared
			)
			if cleared
			else (
				"already empty"
				if not m.started_time
				else "a finished operation has no running timer (reset_timer_value on Finished)"
			)
		)
		target["finish_time"] = _co_norm(m.finish_time)
		why[
			"finish_time"
		] = "not written by the Issue's submit nor by its cancel; kept exactly as stored"

		if legit:
			legit_last = (legit_rows.get(legit.employee_ir) or [None])[-1]
			value = (
				_co_norm(flt(legit_last.rpt_wt_issue))
				if legit_last
				else _co_norm(m.rpt_wt_issue)
			)
			target["rpt_wt_issue"] = value
			why["rpt_wt_issue"] = (
				f"{legit.employee_ir} wrote its LAST row's rpt_wt_issue (row "
				f"{legit_last.idx if legit_last else '?'}) to every operation it issued "
				"(shared values dict in on_submit_issue_new)"
			)
		else:
			target["rpt_wt_issue"] = _co_norm(m.rpt_wt_issue)
			why["rpt_wt_issue"] = "no legitimate Issue; kept as currently stored"

		target = {f: target.get(f) for f in restore_fields}
		why = {f: why.get(f) for f in restore_fields}
		why["time_logs_to_delete"] = tl_evidence
		why["post_submit_versions"] = [
			{"version": v.name, "owner": v.owner, "created": _co_norm(v.creation)}
			for v in mop_versions
			if submitted_on and v.creation > submitted_on
		]
		restore[mop] = target
		evidence[mop] = why
		current = _co_norm_row(m, restore_fields)
		changes[mop] = {
			f: {"current": current[f], "target": target[f]}
			for f in restore_fields
			if current[f] != target[f]
		}
		if delete_tls[mop]:
			changes[mop]["time_logs_deleted"] = delete_tls[mop]
		row_reports.append(
			{
				"row": cint(row.idx),
				"manufacturing_operation": mop,
				"manufacturing_work_order": mwo,
				"kind": kind,
				"legitimate_issue": legit.employee_ir if legit else None,
				"legitimate_issue_submitted_on": _co_norm(legit.issue_submitted_on)
				if legit
				else None,
				"minting_receive": receive.employee_ir if receive else None,
				"successor": successor.name if successor is not None else None,
				"successor_created": _co_norm(successor.creation)
				if successor is not None
				else None,
				"work_order_pointer": (mwo_state.get(mwo) or frappe._dict()).get(
					"manufacturing_operation"
				),
				"operation_status_now": m.status,
			}
		)

	active = [log for log in voucher_logs if not cint(log.is_cancelled)]
	proofs = {}
	for mop in mops:
		synced = [
			log
			for log in active
			if log.manufacturing_operation == mop and cint(log.is_synced)
		]
		if synced:
			proof = _co_eod_sync_proof(
				mop_state[mop].manufacturing_work_order, mop, synced
			)
			proofs[mop] = proof
			if proof["verdict"] != "proven":
				blockers.append(
					f"{mop}: {len(synced)} log(s) of {employee_ir} were already synced by EOD and the "
					f"EOD Sync Log proof is {proof['verdict']} -- stop for a stock review."
				)
	stray = [
		log.name for log in voucher_logs if log.manufacturing_operation not in mop_state
	]
	if stray:
		blockers.append(
			f"MOP Log row(s) {', '.join(stray)} of {employee_ir} name other operations."
		)

	spec = {
		"employee_ir": employee_ir,
		"submitted_on": _co_norm(submitted_on),
		"operations": mops,
		"work_orders": mwos,
		"allowed_documents": sorted(allowed),
		"company": head.company,
		"department": head.department,
		"operation": head.operation,
		"employee": head.employee,
		"subcontracting": head.subcontracting,
		"subcontractor": head.subcontractor,
	}
	required = co_required_absent(spec)
	for check, found in co_blocking_required(required).items():
		if not found:
			continue
		reasons = sorted({cstr(f.get("why")) for f in found if f.get("why")})
		blockers.append(
			f"required-absent check {check}: {len(found)} found"
			+ (f" ({'; '.join(reasons)})" if reasons else "")
			+ (
				" (list the Stock Entries in delete_draft_stock_entries after review, or "
				"remove them by hand)"
				if check == "draft_unresolved_eod_stock_entries"
				else ""
			)
		)
	if required.get("legitimate_later_artefacts"):
		review.append(
			f"{len(required['legitimate_later_artefacts'])} later artefact(s) on the row "
			"operations were attributed to the reviewed history and do not block; confirm "
			"required_absent.legitimate_later_artefacts."
		)

	case.update(
		{
			"submitted_on": _co_norm(submitted_on),
			"submitted_by": head.modified_by,
			"created_by": head.owner,
			"expected": {
				"employee_ir": _co_norm_row(head, CO_EIR_FIELDS),
				"employee_ir_rows": [_co_norm_row(r, CO_EIR_ROW_FIELDS) for r in rows],
				"operations": {
					k: _co_norm_row(v, CO_MOP_FIELDS) for k, v in mop_state.items()
				},
				"work_orders": {
					k: _co_norm_row(v, CO_MWO_FIELDS) for k, v in mwo_state.items()
				},
				"time_logs": {
					mop: [
						_co_norm_row(t, CO_TIME_LOG_FIELDS)
						for t in time_logs.get(mop, [])
					]
					for mop in mops
				},
				"mop_logs": {
					log.name: _co_norm_row(log, CO_MOP_LOG_FIELDS)
					for log in voucher_logs
				},
				"voucher_mop_logs": totals,
			},
			"rows": row_reports,
			"actions": {
				"cancel_employee_ir": employee_ir,
				"mop_logs_to_cancel": sorted(log.name for log in active),
				"time_logs_to_delete": delete_tls,
				"restore_fields": list(restore_fields),
				"restore": restore,
			},
			"evidence": evidence,
			"changes_vs_current": changes,
			"required_absent_spec": spec,
			"required_absent": required,
			"eod_sync_proof": proofs,
			"weights_preview": _co_weights_preview(
				mwos, mops, [log.name for log in active]
			),
			"side_effects": [
				"Employee IR cancel (supported path): MOP Log rows of the voucher get is_cancelled=1, "
				"weights are recomputed, the employee's MSL tracking table is refreshed, a casting "
				"tree (none here unless flagged) is unlinked.",
				"The cancel deletes the Issue's own open time log with a plain DELETE (no Version); a "
				"listed row still present afterwards is dropped by the restore save. The repair "
				"Comment on each operation lists every deleted time-log row either way.",
				"Each restored Manufacturing Operation is saved as a document: a Version records the "
				"field changes; its on_update refreshes the Parent Manufacturing Order weights and the "
				"Item CAM weight row, as every internal save does.",
				"A Comment with the ticket and the before/after values is added to the Employee IR, "
				"each operation and each work order.",
			],
		}
	)
	return _co_seal(case)


# Everything the reviewer approves, including every verdict the builder reached. Editing any of
# it -- for example clearing a blocker by hand -- breaks the seal. ``review_acknowledged`` is the
# reviewer's own input and is deliberately NOT sealed.
CO_SEALED_KEYS = (
	"employee_ir",
	"status",
	"blockers",
	"needs_review",
	"rows",
	"expected",
	"actions",
	"required_absent_spec",
	"eod_sync_proof",
)


def co_case_sha256(case):
	"""Hash of what the reviewer approves (``CO_SEALED_KEYS``)."""
	payload = {key: case.get(key) for key in CO_SEALED_KEYS}
	return hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
	).hexdigest()


def _co_seal(case):
	case["status"] = "blocked" if case.get("blockers") else "ready"
	case["case_sha256"] = co_case_sha256(case)
	return case


def co_voucher_log_totals(employee_ir):
	"""``{"total", "active", "synced_active"}`` MOP Log rows of an Employee IR voucher.

	One statement; without a voucher index this scans `tabMOP Log` once (~0.5 s on kggk-prod).
	"""
	row = _co_one(
		"""
		SELECT COUNT(*) AS total, IFNULL(SUM(is_cancelled = 0), 0) AS active,
			IFNULL(SUM(is_cancelled = 0 AND is_synced = 1), 0) AS synced_active
		FROM `tabMOP Log`
		WHERE voucher_type = 'Employee IR' AND voucher_no = %(eir)s
		""",
		{"eir": employee_ir},
	)
	return {
		"total": cint(row.total) if row else 0,
		"active": cint(row.active) if row else 0,
		"synced_active": cint(row.synced_active) if row else 0,
	}


def co_manifest(cases, *, production_site=None, read_only=None):
	return {
		"manifest_version": CO_MANIFEST_VERSION,
		"kind": CO_MANIFEST_KIND,
		"site": frappe.local.site,
		"production_site": production_site or frappe.local.site,
		"built_on": _co_norm(now_datetime()),
		"built_by": frappe.session.user,
		"builder": "jewellery_erpnext.mop_lineage_audit.build_stale_issue_manifest",
		"app_git_head": get_deployment_parity_record().get("git_head"),
		"read_only": read_only,
		"reviewed_by": None,
		"reviewed_on": None,
		"review_notes": None,
		"delete_draft_stock_entries": [],
		"how_to_apply": [
			"1. Re-build this manifest on the production site right before the repair window.",
			"2. Review every case: blockers must be empty; check every needs_review note and copy "
			"it verbatim into that case's review_acknowledged list (rehearse / apply refuse "
			"otherwise); read evidence, changes_vs_current, eod_sync_proof and weights_preview. "
			"Never edit anything else: the case is sealed (case_sha256).",
			"3. Take a Frappe Cloud backup, then fill reviewed_by / reviewed_on.",
			"4. jewellery_erpnext.patches.repair_current_operation_conflicts.execute "
			"mode=plan (read-only), then mode=apply with ticket=<ticket> as Administrator, "
			"outside the 19:00 EOD window.",
			"5. Re-run audit_current_operation_conflicts; then a selective EOD for any work order "
			"the EOD hold kept back.",
		],
		"cases": cases,
	}


@frappe.whitelist()
def build_stale_issue_manifest(employee_ir, production_site=None):
	"""Reviewable repair manifest for ONE stale Employee IR Issue (family C). Read-only.

	Returns the manifest (one case). ``production_site`` names the site the manifest is meant
	for when it is built on a copy; it defaults to the current site. The repair refuses to
	rehearse on it and refuses to apply anywhere else than ``site``.

	bench --site <site> execute jewellery_erpnext.mop_lineage_audit.build_stale_issue_manifest \\
		--kwargs "{'employee_ir': 'EMP-IR-Labh-2026-43405'}"
	"""
	frappe.only_for("System Manager")
	name = cstr(employee_ir).strip()
	if not name:
		frappe.throw("employee_ir is required")
	guard = _ReadOnlyAudit()
	with guard:
		case = co_build_stale_issue_case(name)
	return co_manifest(
		[case], production_site=production_site, read_only=guard.summary()
	)


def save_stale_issue_manifests(employee_irs, out_dir, production_site=None):
	"""bench-execute helper: one manifest file per Employee IR in ``out_dir``. No DB writes.

	Not whitelisted -- it writes files on the server. Returns a summary per file.
	"""
	frappe.only_for("System Manager")
	os.makedirs(out_dir, exist_ok=True)
	summary = []
	for name in _co_names(employee_irs):
		manifest = build_stale_issue_manifest(name, production_site=production_site)
		path = os.path.join(out_dir, f"{name}.json")
		with open(path, "w") as handle:
			json.dump(manifest, handle, indent=1, default=str)
			handle.write("\n")
		case = manifest["cases"][0]
		summary.append(
			{
				"employee_ir": name,
				"path": path,
				"status": case["status"],
				"blockers": case["blockers"],
				"needs_review": case["needs_review"],
				"case_sha256": case["case_sha256"],
			}
		)
	return summary


# ---------------------------------------------------------------------------------------------
# current state of a manifest case, and the two comparisons the repair decides from
# ---------------------------------------------------------------------------------------------


def co_read_case_state(case):
	"""Current DB values in the shape of ``case["expected"]`` (plain reads).

	The repair calls this AFTER locking the Employee IR, operation and work-order rows, so in its
	transaction these reads are the first consistent reads and see every commit made before the
	locks were granted.
	"""
	exp = case["expected"]
	name = case["employee_ir"]
	mops = sorted(exp.get("operations") or {})
	mwos = sorted(exp.get("work_orders") or {})
	head = _co_one(
		f"SELECT {_co_cols(CO_EIR_FIELDS)} FROM `tabEmployee IR` WHERE name = %(name)s",
		{"name": name},
	)
	rows = _co_select(
		f"""
		SELECT {_co_cols(CO_EIR_ROW_FIELDS)} FROM `tabEmployee IR Operation`
		WHERE parent = %(name)s AND parenttype = 'Employee IR' ORDER BY idx
		""",
		{"name": name},
	)
	state = {
		"employee_ir": _co_norm_row(head, CO_EIR_FIELDS) if head else None,
		"employee_ir_rows": [_co_norm_row(r, CO_EIR_ROW_FIELDS) for r in rows],
		"operations": {
			r.name: _co_norm_row(r, CO_MOP_FIELDS)
			for r in _co_select_in(
				f"SELECT {_co_cols(CO_MOP_FIELDS)} FROM `tabManufacturing Operation` "
				"WHERE name IN %(mops)s",
				"mops",
				mops,
			)
		},
		"work_orders": {
			r.name: _co_norm_row(r, CO_MWO_FIELDS)
			for r in _co_select_in(
				f"SELECT {_co_cols(CO_MWO_FIELDS)} FROM `tabManufacturing Work Order` "
				"WHERE name IN %(mwos)s",
				"mwos",
				mwos,
			)
		},
		"time_logs": {mop: [] for mop in mops},
		"mop_logs": {
			r.name: _co_norm_row(r, CO_MOP_LOG_FIELDS)
			for r in _co_select_in(
				f"SELECT {_co_cols(CO_MOP_LOG_FIELDS)} FROM `tabMOP Log` WHERE name IN %(logs)s",
				"logs",
				sorted(exp.get("mop_logs") or {}),
			)
		},
		"voucher_mop_logs": co_voucher_log_totals(name),
	}
	for t in _co_select_in(
		f"""
		SELECT {_co_cols(CO_TIME_LOG_FIELDS)} FROM `tabManufacturing Operation Time Log`
		WHERE parent IN %(mops)s AND parenttype = 'Manufacturing Operation'
			AND parentfield = 'time_logs'
		""",
		"mops",
		mops,
	):
		state["time_logs"].setdefault(t.parent, []).append(
			_co_norm_row(t, CO_TIME_LOG_FIELDS)
		)
	for found in state["time_logs"].values():
		found.sort(key=lambda t: cint(t["idx"]))
	return state


def _co_diff_row(diffs, path, expected, actual):
	if expected is None and actual is None:
		return
	if actual is None:
		diffs.append({"path": path, "expected": "(row exists)", "actual": "(missing)"})
		return
	if expected is None:
		diffs.append({"path": path, "expected": "(no row)", "actual": "(row exists)"})
		return
	for field, value in expected.items():
		if actual.get(field) != value:
			diffs.append(
				{
					"path": f"{path}.{field}",
					"expected": value,
					"actual": actual.get(field),
				}
			)


def co_compare_expected(case, state):
	"""Every expected value that no longer holds, as ``[{path, expected, actual}]``."""
	exp = case["expected"]
	diffs = []
	_co_diff_row(diffs, "employee_ir", exp.get("employee_ir"), state.get("employee_ir"))
	exp_rows = {r["name"]: r for r in exp.get("employee_ir_rows") or []}
	cur_rows = {r["name"]: r for r in state.get("employee_ir_rows") or []}
	for name in sorted(set(exp_rows) | set(cur_rows)):
		_co_diff_row(
			diffs, f"employee_ir_rows.{name}", exp_rows.get(name), cur_rows.get(name)
		)
	for section in ("operations", "work_orders", "mop_logs"):
		cur = state.get(section) or {}
		for name, row in sorted((exp.get(section) or {}).items()):
			_co_diff_row(diffs, f"{section}.{name}", row, cur.get(name))
	for mop, rows in sorted((exp.get("time_logs") or {}).items()):
		exp_tl = {r["name"]: r for r in rows}
		cur_tl = {r["name"]: r for r in (state.get("time_logs") or {}).get(mop, [])}
		for name in sorted(set(exp_tl) | set(cur_tl)):
			_co_diff_row(
				diffs, f"time_logs.{mop}.{name}", exp_tl.get(name), cur_tl.get(name)
			)
	exp_totals = exp.get("voucher_mop_logs") or {}
	cur_totals = state.get("voucher_mop_logs") or {}
	for key in ("total", "active", "synced_active"):
		if key in exp_totals and exp_totals.get(key) != cur_totals.get(key):
			diffs.append(
				{
					"path": f"voucher_mop_logs.{key}",
					"expected": exp_totals.get(key),
					"actual": cur_totals.get(key),
				}
			)
	return diffs


def co_compare_applied(case, state, *, include_pointers=True):
	"""Every way the current state differs from the REPAIRED one (empty = already applied).

	``include_pointers`` also requires every work order to still point where it pointed when
	the manifest was built -- right for the post-verify inside the repair transaction, wrong for
	recognising an old repair after the work orders have legitimately moved on.
	"""
	exp = case["expected"]
	act = case["actions"]
	diffs = []
	head = state.get("employee_ir") or {}
	if cint(head.get("docstatus")) != 2:
		diffs.append(
			{
				"path": "employee_ir.docstatus",
				"expected": 2,
				"actual": head.get("docstatus"),
			}
		)
	totals = state.get("voucher_mop_logs") or {}
	if cint(totals.get("active")):
		diffs.append(
			{
				"path": "voucher_mop_logs.active",
				"expected": 0,
				"actual": totals.get("active"),
			}
		)
	for name in act.get("mop_logs_to_cancel") or []:
		row = (state.get("mop_logs") or {}).get(name)
		if not row or cint(row.get("is_cancelled")) != 1:
			diffs.append(
				{
					"path": f"mop_logs.{name}.is_cancelled",
					"expected": 1,
					"actual": row.get("is_cancelled") if row else "(missing)",
				}
			)
	for mop, names in (act.get("time_logs_to_delete") or {}).items():
		present = {r["name"] for r in (state.get("time_logs") or {}).get(mop, [])}
		for name in names:
			if name in present:
				diffs.append(
					{
						"path": f"time_logs.{mop}.{name}",
						"expected": "(deleted)",
						"actual": "(present)",
					}
				)
		for kept in exp.get("time_logs", {}).get(mop, []):
			if kept["name"] not in names and kept["name"] not in present:
				diffs.append(
					{
						"path": f"time_logs.{mop}.{kept['name']}",
						"expected": "(kept)",
						"actual": "(missing)",
					}
				)
	for mop, target in (act.get("restore") or {}).items():
		cur = (state.get("operations") or {}).get(mop) or {}
		for field, value in target.items():
			if cur.get(field) != value:
				diffs.append(
					{
						"path": f"operations.{mop}.{field}",
						"expected": value,
						"actual": cur.get(field),
					}
				)
	for mwo, row in (exp.get("work_orders") or {}).items() if include_pointers else ():
		cur = (state.get("work_orders") or {}).get(mwo) or {}
		if cur.get("manufacturing_operation") != row.get("manufacturing_operation"):
			diffs.append(
				{
					"path": f"work_orders.{mwo}.manufacturing_operation",
					"expected": row.get("manufacturing_operation"),
					"actual": cur.get("manufacturing_operation"),
				}
			)
	return diffs
