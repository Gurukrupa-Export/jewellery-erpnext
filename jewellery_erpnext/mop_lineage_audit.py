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

**Work order <-> operation link integrity (records pairing a MOP with a work order that does not
own it; read-only, System Manager):**

    bench --site <site> execute jewellery_erpnext.mop_lineage_audit.audit_mwo_mop_consistency --kwargs "{'limit': 50}"
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import frappe
from frappe.utils import add_to_date, cint, flt, get_datetime, sbool

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


# ----------------------------------------------------------------------------------------------
# Work order <-> operation link integrity (MWO <-> MOP)
# ----------------------------------------------------------------------------------------------
#
# A Manufacturing Operation belongs to exactly one work order: its own ``manufacturing_work_order``,
# set when the operation is created and never rewritten (see ``doc_events/mwo_mop_link.py``). Every
# record that stores BOTH must store the operation's own work order. The detectors below find the
# records that do not. ``patches/repair_mwo_mop_link_labels.py`` plans its writes from the SAME
# ``mwo_mop_findings()`` output, so the audit and the repair cannot disagree about what is wrong.
#
# Cost: MOP Log has no index on ``manufacturing_operation`` on production (only
# ``mop_mwo_creation_idx`` and ``mop_eod_sync_idx``), and Stock Entry Detail stores the operation and
# work order as Small Text. So every detector is one sequential pass joined to Manufacturing
# Operation / Work Order by PRIMARY KEY -- no range join, no per-row subquery. The only time-window
# lookup (``mwo_mop_make_receive_foreign_sre``) is seeded from the handful of mis-keyed reservations
# and probes ``tabStock Entry.creation``, which is indexed.

MWO_MOP_OPEN_STATUSES = ("Not Started", "On Hold", "WIP", "QC Pending", "QC Completed")
MWO_MOP_CLOSED_MWO_STATUSES = ("Completed", "Closed", "Cancelled", "Stopped")
# A MOP Log row created more than this long BEFORE its operation was not written for it: the
# operation's short name was re-issued over rows a deleted operation left behind. A few seconds of
# slack because a row and its operation can be written in one request whose clock reads straddle a
# second boundary.
MWO_MOP_NAME_REUSE_SECONDS = 5
MAKE_RECEIVE_ENTRY_TYPE = "Material Receive (WORK ORDER)"


def _sorted(rows: list, *keys: str) -> list:
	"""Order a small result in Python instead of SQL. An ORDER BY on the driving table of a full
	scan makes MariaDB filesort the WHOLE table before the join -- minutes on the MOP Log under load
	-- while what comes back is a handful of rows."""
	return sorted(
		rows,
		key=lambda row: tuple(
			(row.get(k) is None, row.get(k) if row.get(k) is not None else 0)
			for k in keys
		),
	)


def _mwo_mop_open_condition(alias: str) -> str:
	"""SQL predicate: ``alias`` is live work -- an open status and not a cancel's Revert leftover.

	kggk_uat keeps the operation a cancelled Department Issue / Employee Receive minted, marked
	``department_ir_status = "Revert"`` with status Not Started: history, not an open operation. The
	column (and the option) exist on every line, so the exclusion applies wherever the column does.
	"""
	statuses = ", ".join(frappe.db.escape(status) for status in MWO_MOP_OPEN_STATUSES)
	condition = f"{alias}.status IN ({statuses})"
	if frappe.db.has_column("Manufacturing Operation", "department_ir_status"):
		condition += f" AND IFNULL({alias}.department_ir_status, '') != 'Revert'"
	return condition


def mwo_mop_reservation_stock_entry_types() -> list[str]:
	"""Stock Entry types that reserve stock against the HEADER work order on submit.

	The list ``doc_events/stock_entry.py`` reads before ``stock_reservation_entry_for_mwo``: the
	reservation takes its work order from the header and its operation from the row, so for these
	types every row's operation must belong to the header work order, not just to its own row label.
	"""
	return frappe.db.get_all(
		"Stock Entry Type To Reservation",
		filters={"parent": "MOP Settings"},
		pluck="stock_entry_type_to_reservation",
	)


def mwo_mop_ir_row_mismatches() -> list[dict]:
	"""Employee IR / Department IR operation rows (parent not cancelled) whose work order is not the
	owner of their operation. A blank row work order counts too: the repair fills it from the owner.

	Carries the owner's PMO and finding flag so the repair can re-derive the row fields that are
	``fetch_from`` the work order (DIR ``parent_manufacturing_order``, EIR ``is_finding_mwo``).

	``is_name_reuse`` marks a row whose operation was created more than ``MWO_MOP_NAME_REUSE_SECONDS``
	AFTER its document (a child row's ``creation`` is its parent's): the operation's short name was
	re-issued over one the document really named, or the row was added to a draft older than the
	operation. Either way the owner is not known to be the row's -- the repair reports it instead of
	relabelling it (the MOP Log rule, ``mwo_mop_log_findings``).
	"""
	seconds = cint(MWO_MOP_NAME_REUSE_SECONDS)
	return frappe.db.sql(
		f"""
		SELECT 'Employee IR' AS parenttype, p.name AS parent, p.docstatus AS parent_docstatus,
			p.type AS ir_type, p.operation, r.name AS row_name, r.idx,
			r.manufacturing_operation AS mop, r.manufacturing_work_order AS row_mwo,
			m.manufacturing_work_order AS owner, NULL AS row_pmo, w.manufacturing_order AS owner_pmo,
			r.is_finding_mwo AS row_is_finding_mwo, w.is_finding_mwo AS owner_is_finding_mwo,
			(p.creation < m.creation - INTERVAL {seconds} SECOND) AS is_name_reuse
		FROM `tabEmployee IR Operation` r
		INNER JOIN `tabEmployee IR` p ON p.name = r.parent
		INNER JOIN `tabManufacturing Operation` m ON m.name = r.manufacturing_operation
		LEFT JOIN `tabManufacturing Work Order` w ON w.name = m.manufacturing_work_order
		WHERE r.parenttype = 'Employee IR'
		  AND p.docstatus < 2
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND IFNULL(r.manufacturing_work_order, '') != m.manufacturing_work_order
		UNION ALL
		SELECT 'Department IR', p.name, p.docstatus, p.type, NULL, r.name, r.idx,
			r.manufacturing_operation, r.manufacturing_work_order, m.manufacturing_work_order,
			r.parent_manufacturing_order, w.manufacturing_order, NULL, NULL,
			(p.creation < m.creation - INTERVAL {seconds} SECOND)
		FROM `tabDepartment IR Operation` r
		INNER JOIN `tabDepartment IR` p ON p.name = r.parent
		INNER JOIN `tabManufacturing Operation` m ON m.name = r.manufacturing_operation
		LEFT JOIN `tabManufacturing Work Order` w ON w.name = m.manufacturing_work_order
		WHERE r.parenttype = 'Department IR'
		  AND p.docstatus < 2
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND IFNULL(r.manufacturing_work_order, '') != m.manufacturing_work_order
		ORDER BY parenttype, parent, idx
		""",
		as_dict=True,
	)


def mwo_mop_se_header_mismatches() -> list[dict]:
	"""Non-cancelled Stock Entries whose header operation belongs to another work order than the
	header's. Headers with an operation but no work order (plain Repacks) are not pairings, so they
	are not reported. Process Loss headers are held to this rule too: their header pair is taken
	from the first loss row, so it is consistent by construction.

	``is_name_reuse``: the operation is more than ``MWO_MOP_NAME_REUSE_SECONDS`` newer than the entry
	(see :func:`mwo_mop_ir_row_mismatches`), so the repair leaves the entry to a person.
	"""
	seconds = cint(MWO_MOP_NAME_REUSE_SECONDS)
	rows = frappe.db.sql(
		f"""
		SELECT se.name, se.docstatus, se.stock_entry_type, se.purpose, se.auto_created,
			se.employee_ir, se.custom_eir_operation_row, se.manufacturing_operation AS mop,
			se.manufacturing_work_order AS mwo, m.manufacturing_work_order AS owner,
			se.manufacturing_order AS pmo, w.manufacturing_order AS owner_pmo, se.custom_tree_number,
			(se.creation < m.creation - INTERVAL {seconds} SECOND) AS is_name_reuse
		FROM `tabStock Entry` se
		INNER JOIN `tabManufacturing Operation` m ON m.name = se.manufacturing_operation
		LEFT JOIN `tabManufacturing Work Order` w ON w.name = m.manufacturing_work_order
		WHERE se.docstatus < 2
		  AND IFNULL(se.manufacturing_work_order, '') != ''
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND m.manufacturing_work_order != se.manufacturing_work_order
		""",
		as_dict=True,
	)
	return _sorted(rows, "name")


def mwo_mop_se_row_mismatches(reservation_types: list[str] | None = None) -> list[dict]:
	"""Stock Entry Detail rows whose operation belongs to another work order. Two rules, ``rule``
	says which one fired (``row`` wins when both do):

	* ``row`` -- the owner differs from the row's own work order, or from the header's when the row
	  carries none (``COALESCE``). A Process Loss row is compared with its OWN label only: those
	  entries are multi-work-order by design and their header names just the first loss row's.
	* ``reservation_header`` -- for a reservation type (see
	  :func:`mwo_mop_reservation_stock_entry_types`), the owner differs from the HEADER work order,
	  because the reservation is keyed by the header. Repack counts only with a header work order
	  AND order, the same condition the submit hook applies. Process Loss is never held to it.

	The joined operation must exist: production stores these row fields as Small Text, so nothing
	ever validated them as links.
	"""
	if reservation_types is None:
		reservation_types = mwo_mop_reservation_stock_entry_types()
	reservation_types = tuple(t for t in reservation_types if t and t != "Process Loss")

	expected = """CASE WHEN se.stock_entry_type = 'Process Loss'
			THEN NULLIF(d.custom_manufacturing_work_order, '')
			ELSE COALESCE(
				NULLIF(d.custom_manufacturing_work_order, ''), NULLIF(se.manufacturing_work_order, '')
			)
		END"""
	header_rule = "0"
	params: dict = {}
	if reservation_types:
		header_rule = """(se.stock_entry_type IN %(reservation_types)s
			AND IFNULL(se.manufacturing_work_order, '') != ''
			AND (se.stock_entry_type != 'Repack' OR IFNULL(se.manufacturing_order, '') != '')
			AND m.manufacturing_work_order != se.manufacturing_work_order)"""
		params["reservation_types"] = reservation_types

	rows = frappe.db.sql(
		f"""
		SELECT se.name AS stock_entry, se.docstatus, se.stock_entry_type, se.auto_created,
			se.employee_ir, se.custom_eir_operation_row, d.name AS row_name, d.idx,
			d.manufacturing_operation AS mop, d.custom_manufacturing_work_order AS row_mwo,
			se.manufacturing_work_order AS header_mwo, m.manufacturing_work_order AS owner,
			IF(IFNULL(m.manufacturing_work_order != {expected}, 0), 'row', 'reservation_header') AS rule
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` d ON d.parent = se.name AND d.parenttype = 'Stock Entry'
		INNER JOIN `tabManufacturing Operation` m ON m.name = d.manufacturing_operation
		WHERE se.docstatus < 2
		  AND IFNULL(d.manufacturing_operation, '') != ''
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND (m.manufacturing_work_order != {expected} OR {header_rule})
		""",
		params,
		as_dict=True,
	)
	return _sorted(rows, "stock_entry", "idx")


def mwo_mop_sre_mismatches() -> list[dict]:
	"""Stock Reservation Entries whose work order is not the owner of their operation, active and
	cancelled alike (``docstatus`` tells them apart; cancelled ones are history)."""
	rows = frappe.db.sql(
		"""
		SELECT s.name, s.docstatus, s.status, s.manufacturing_work_order AS sre_mwo,
			s.manufacturing_operation AS mop, m.manufacturing_work_order AS owner,
			s.voucher_type, s.voucher_no, s.voucher_detail_no, s.from_voucher_no, s.item_code,
			s.warehouse, s.reserved_qty, s.delivered_qty, s.creation, s.modified
		FROM `tabStock Reservation Entry` s
		INNER JOIN `tabManufacturing Operation` m ON m.name = s.manufacturing_operation
		WHERE IFNULL(s.manufacturing_operation, '') != ''
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND IFNULL(s.manufacturing_work_order, '') != m.manufacturing_work_order
		""",
		as_dict=True,
	)
	return _sorted(rows, "creation", "name")


def mwo_mop_log_findings() -> dict:
	"""Live MOP Log rows that do not carry their operation's work order, from ONE pass over the
	ledger, split three ways:

	* ``stamps`` -- the operation exists and the stamp is not its owner. Repairable in place.
	* ``name_reuse`` -- the row is older than its operation by more than
	  ``MWO_MOP_NAME_REUSE_SECONDS``: the short operation name was re-issued over a deleted
	  operation's rows, whatever they are stamped with. Never repaired here -- relabelling would hand
	  someone else's metal to this operation; it needs a decision on the ledger itself.
	* ``orphans`` -- the operation does not exist any more (purged). Counted only.
	"""
	seconds = cint(MWO_MOP_NAME_REUSE_SECONDS)
	rows = frappe.db.sql(
		f"""
		SELECT l.name, l.manufacturing_operation AS mop, l.manufacturing_work_order AS stamp,
			m.manufacturing_work_order AS owner, l.voucher_type, l.voucher_no, l.item_code,
			l.batch_no, l.is_synced, l.creation, m.creation AS mop_creation,
			(m.name IS NULL) AS is_orphan,
			(m.name IS NOT NULL AND l.creation < m.creation - INTERVAL {seconds} SECOND) AS is_name_reuse,
			(w.name IS NOT NULL) AS stamp_exists
		FROM `tabMOP Log` l
		LEFT JOIN `tabManufacturing Operation` m ON m.name = l.manufacturing_operation
		LEFT JOIN `tabManufacturing Work Order` w ON w.name = l.manufacturing_work_order
		WHERE l.is_cancelled = 0
		  AND IFNULL(l.manufacturing_operation, '') != ''
		  AND (
			m.name IS NULL
			OR l.creation < m.creation - INTERVAL {seconds} SECOND
			OR (
				IFNULL(m.manufacturing_work_order, '') != ''
				AND IFNULL(l.manufacturing_work_order, '') != m.manufacturing_work_order
			)
		  )
		""",
		as_dict=True,
	)

	stamps: list[dict] = []
	name_reuse: list[dict] = []
	orphan_rows = 0
	orphan_mops: set = set()
	for row in _sorted(rows, "mop", "creation", "name"):
		is_orphan = cint(row.pop("is_orphan"))
		is_name_reuse = cint(row.pop("is_name_reuse"))
		stamp_exists = cint(row.pop("stamp_exists"))
		if is_orphan:
			orphan_rows += 1
			orphan_mops.add(row.mop)
		elif is_name_reuse:
			row["stamp_exists"] = stamp_exists
			name_reuse.append(row)
		else:
			stamps.append(row)
	return {
		"stamps": stamps,
		"name_reuse": name_reuse,
		"orphans": {"rows": orphan_rows, "operations": len(orphan_mops)},
	}


def mwo_mop_pointer_foreign() -> list[dict]:
	"""Work orders whose current-operation pointer names an operation owned by ANOTHER work order,
	any docstatus (a split child copies its parent's pointer)."""
	rows = frappe.db.sql(
		"""
		SELECT w.name AS mwo, w.docstatus, w.status, w.split_from,
			w.manufacturing_operation AS pointer, m.manufacturing_work_order AS pointer_owner,
			m.status AS pointer_status
		FROM `tabManufacturing Work Order` w
		INNER JOIN `tabManufacturing Operation` m ON m.name = w.manufacturing_operation
		WHERE IFNULL(m.manufacturing_work_order, '') != ''
		  AND m.manufacturing_work_order != w.name
		""",
		as_dict=True,
	)
	return _sorted(rows, "docstatus", "mwo")


def mwo_mop_pointer_stale() -> list[dict]:
	"""Submitted, open work orders still pointing at their own FINISHED operation although that
	operation has an open successor of the same work order (``previous_mop``): the receive that
	minted the successor moved some other work order's pointer instead.

	One entry per work order; ``successor`` is the newest open successor (``open_successors`` lists
	them all, newest first). Own Finished pointers WITHOUT a successor are not this defect (cancelled
	transfers, desk edits) and are not reported.
	"""
	rows = frappe.db.sql(
		f"""
		SELECT w.name AS mwo, w.status, w.manufacturing_operation AS pointer,
			p.status AS pointer_status, s.name AS successor, s.status AS successor_status
		FROM `tabManufacturing Work Order` w
		INNER JOIN `tabManufacturing Operation` p
			ON p.name = w.manufacturing_operation AND p.manufacturing_work_order = w.name
		INNER JOIN `tabManufacturing Operation` s
			ON s.manufacturing_work_order = w.name AND s.previous_mop = p.name
		WHERE w.docstatus = 1
		  AND IFNULL(w.status, '') NOT IN %(closed)s
		  AND p.status = 'Finished'
		  AND {_mwo_mop_open_condition("s")}
		ORDER BY w.name, s.creation DESC, s.name DESC
		""",
		{"closed": MWO_MOP_CLOSED_MWO_STATUSES},
		as_dict=True,
	)
	by_mwo: dict = {}
	for row in rows:
		entry = by_mwo.get(row.mwo)
		if entry is None:
			entry = by_mwo[row.mwo] = frappe._dict(
				mwo=row.mwo,
				status=row.status,
				pointer=row.pointer,
				pointer_status=row.pointer_status,
				successor=row.successor,
				successor_status=row.successor_status,
				open_successors=[],
			)
		entry.open_successors.append(row.successor)
	return list(by_mwo.values())


def mwo_mop_pointer_on_non_submitted() -> dict:
	"""Draft and discarded work orders that carry a current-operation pointer.

	Neither should: a draft has no operations of its own yet, and Discard (docstatus 0 -> 2 without
	``on_cancel``) leaves status and pointer as they were. A properly cancelled work order (status
	Cancelled) keeping its OWN last pointer is history, so it is only counted
	(``cancelled_own_pointer``); one keeping a foreign pointer is still reported.
	"""
	rows = frappe.db.sql(
		"""
		SELECT w.name AS mwo, w.docstatus, w.status, w.split_from,
			w.manufacturing_operation AS pointer, m.manufacturing_work_order AS pointer_owner,
			IFNULL(m.manufacturing_work_order = w.name, 0) AS pointer_is_own
		FROM `tabManufacturing Work Order` w
		LEFT JOIN `tabManufacturing Operation` m ON m.name = w.manufacturing_operation
		WHERE w.docstatus != 1
		  AND IFNULL(w.manufacturing_operation, '') != ''
		""",
		as_dict=True,
	)
	findings: list[dict] = []
	cancelled_own = 0
	for row in _sorted(rows, "docstatus", "mwo"):
		row["pointer_is_own"] = cint(row.pointer_is_own)
		if (
			cint(row.docstatus) == 2
			and row.status == "Cancelled"
			and row.pointer_is_own
		):
			cancelled_own += 1
			continue
		findings.append(row)
	return {"rows": findings, "cancelled_own_pointer": cancelled_own}


def mwo_mop_casting_receive_without_tree() -> list[dict]:
	"""Submitted casting Receive rows (Department Operation ``tree_no_reqd``) labelled with another
	work order than their operation's, with no ``tree_number`` although the operation was issued on a
	tree.

	A casting receive resolves its tree from the ROW work order (``resolve_receive_tree_numbers``),
	so a row labelled with a sibling's work order resolved none: the row and its injection Stock
	Entry lost the tree, and the receive charged no tree for it. The authority is the casting Issue
	row of the same operation, which pinned the tree at submit. ``issue_tree`` is that tree when the
	Issue rows agree on exactly one, else None (``issue_trees`` lists them) -- an ambiguous row is
	report-only. A tree-less row whose label is right is not this defect and is not listed.

	``received_gross_wt``, ``gross_wt``, ``is_raw_material`` and ``subcontracting`` let the repair
	report what the tree never recorded (``tree_casting.tree_draw_by_tree``'s per-row rule).
	"""
	candidates = frappe.db.sql(
		"""
		SELECT p.name AS parent, p.operation, p.is_raw_material, p.subcontracting, r.name AS row_name,
			r.idx, r.manufacturing_operation AS mop, r.manufacturing_work_order AS row_mwo,
			m.manufacturing_work_order AS owner, r.gross_wt, r.received_gross_wt
		FROM `tabEmployee IR Operation` r
		INNER JOIN `tabEmployee IR` p ON p.name = r.parent
		INNER JOIN `tabDepartment Operation` o ON o.name = p.operation
		INNER JOIN `tabManufacturing Operation` m ON m.name = r.manufacturing_operation
		WHERE r.parenttype = 'Employee IR'
		  AND p.docstatus = 1
		  AND p.type = 'Receive'
		  AND o.tree_no_reqd = 1
		  AND IFNULL(r.tree_number, '') = ''
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND IFNULL(r.manufacturing_work_order, '') != m.manufacturing_work_order
		ORDER BY p.name, r.idx
		""",
		as_dict=True,
	)
	if not candidates:
		return []

	issue_trees: dict = {}
	for row in frappe.db.sql(
		"""
		SELECT DISTINCT r.manufacturing_operation AS mop, r.tree_number
		FROM `tabEmployee IR Operation` r
		INNER JOIN `tabEmployee IR` p ON p.name = r.parent
		WHERE r.parenttype = 'Employee IR'
		  AND p.docstatus = 1
		  AND p.type = 'Issue'
		  AND IFNULL(r.tree_number, '') != ''
		  AND r.manufacturing_operation IN %(mops)s
		""",
		{"mops": tuple(sorted({row.mop for row in candidates}))},
		as_dict=True,
	):
		issue_trees.setdefault(row.mop, set()).add(row.tree_number)

	out: list[dict] = []
	for row in candidates:
		trees = sorted(issue_trees.get(row.mop) or ())
		if not trees:
			continue
		row["issue_trees"] = trees
		row["issue_tree"] = trees[0] if len(trees) == 1 else None
		out.append(row)
	return out


def mwo_mop_subcontracting_log_mismatches() -> list[dict]:
	"""Subcontracting Logs whose work order is not the owner of their operation (report-only:
	they copy the Stock Entry header they were written from)."""
	if not frappe.db.table_exists("Subcontracting Log"):
		return []
	rows = frappe.db.sql(
		"""
		SELECT l.name, l.docstatus, l.reference_doctype, l.reference_docname,
			l.manufacturing_work_order_name AS mwo, l.manufacturing_operation_name AS mop,
			m.manufacturing_work_order AS owner, l.settlement_required
		FROM `tabSubcontracting Log` l
		INNER JOIN `tabManufacturing Operation` m ON m.name = l.manufacturing_operation_name
		WHERE IFNULL(l.manufacturing_operation_name, '') != ''
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND IFNULL(l.manufacturing_work_order_name, '') != m.manufacturing_work_order
		""",
		as_dict=True,
	)
	return _sorted(rows, "name")


def mwo_mop_process_loss_exclusions() -> dict:
	"""How many Process Loss rows the header rule deliberately does NOT judge: rows whose operation
	belongs to another work order than the header (which names only the first loss row's). Reported
	so the exemption stays visible instead of silent."""
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS n_rows, COUNT(DISTINCT se.name) AS n_stock_entries
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` d ON d.parent = se.name AND d.parenttype = 'Stock Entry'
		INNER JOIN `tabManufacturing Operation` m ON m.name = d.manufacturing_operation
		WHERE se.docstatus < 2
		  AND se.stock_entry_type = 'Process Loss'
		  AND IFNULL(se.manufacturing_work_order, '') != ''
		  AND IFNULL(m.manufacturing_work_order, '') != ''
		  AND m.manufacturing_work_order != se.manufacturing_work_order
		""",
		as_dict=True,
	)[0]
	return {"rows": cint(row.n_rows), "stock_entries": cint(row.n_stock_entries)}


def mwo_mop_make_receive_foreign_sre(sre_rows: list[dict] | None = None) -> list[dict]:
	"""Make Receive Entries that consumed a reservation whose operation belongs to another work order.

	Make Receive Entry lists and checks reservations by work order only, so a reservation keyed
	(header work order, a sibling's operation) passes and the sibling's metal is received. Seeded
	from the mis-keyed CANCELLED reservations (consumption cancels them inside the receive's own
	request): for each, the Material Receive (WORK ORDER) entries of the same work order, item and
	source warehouse created between 10 s before and 5 s after the cancellation.
	"""
	if sre_rows is None:
		sre_rows = mwo_mop_sre_mismatches()
	out: list[dict] = []
	for sre in sre_rows:
		if (
			cint(sre.get("docstatus")) != 2
			or not sre.get("modified")
			or not sre.get("sre_mwo")
		):
			continue
		cancelled_on = get_datetime(sre["modified"])
		for hit in frappe.db.sql(
			"""
			SELECT se.name AS stock_entry, se.docstatus AS stock_entry_docstatus, se.creation,
				se.manufacturing_operation AS stock_entry_mop, d.name AS row_name, d.qty
			FROM `tabStock Entry` se
			INNER JOIN `tabStock Entry Detail` d
				ON d.parent = se.name AND d.parenttype = 'Stock Entry'
			WHERE se.creation BETWEEN %(start)s AND %(end)s
			  AND se.stock_entry_type = %(stock_entry_type)s
			  AND se.docstatus IN (1, 2)
			  AND se.manufacturing_work_order = %(mwo)s
			  AND d.item_code = %(item_code)s
			  AND d.s_warehouse = %(warehouse)s
			ORDER BY se.creation, d.idx
			""",
			{
				"start": add_to_date(cancelled_on, seconds=-10),
				"end": add_to_date(cancelled_on, seconds=5),
				"stock_entry_type": MAKE_RECEIVE_ENTRY_TYPE,
				"mwo": sre["sre_mwo"],
				"item_code": sre.get("item_code"),
				"warehouse": sre.get("warehouse"),
			},
			as_dict=True,
		):
			out.append(
				frappe._dict(
					sre=sre["name"],
					sre_mwo=sre["sre_mwo"],
					sre_mop=sre.get("mop"),
					sre_mop_owner=sre.get("owner"),
					item_code=sre.get("item_code"),
					warehouse=sre.get("warehouse"),
					sre_cancelled_on=sre["modified"],
					**hit,
				)
			)
	return out


def mwo_mop_findings(include_expensive: bool = False) -> dict:
	"""Every MWO <-> MOP detector, raw and uncapped: the single input of both
	:func:`audit_mwo_mop_consistency` and ``patches/repair_mwo_mop_link_labels``.

	``include_expensive`` adds the whole-ledger negative-balance sweep (the definition
	:func:`audit_negative_batch_balances` already owns) and the Make Receive Entry trace.
	"""
	sre = mwo_mop_sre_mismatches()
	findings = {
		"ir_rows": mwo_mop_ir_row_mismatches(),
		"se_headers": mwo_mop_se_header_mismatches(),
		"se_rows": mwo_mop_se_row_mismatches(),
		"sre": sre,
		"mop_log": mwo_mop_log_findings(),
		"pointer_foreign": mwo_mop_pointer_foreign(),
		"pointer_stale": mwo_mop_pointer_stale(),
		"pointer_on_non_submitted": mwo_mop_pointer_on_non_submitted(),
		"casting_receive_without_tree": mwo_mop_casting_receive_without_tree(),
		"subcontracting_log": mwo_mop_subcontracting_log_mismatches(),
		"process_loss_multi_mwo": mwo_mop_process_loss_exclusions(),
	}
	if include_expensive:
		findings["negative_latest_balances"] = audit_negative_batch_balances(limit=0)
		findings[
			"make_receive_consumed_foreign_sre"
		] = mwo_mop_make_receive_foreign_sre(sre)
	return findings


def mwo_mop_counts(findings: dict) -> dict:
	"""``{class: count}`` for a :func:`mwo_mop_findings` result -- the counts the audit reports."""
	counts = {
		"ir_rows": len(findings["ir_rows"]),
		"se_headers": len(findings["se_headers"]),
		"se_rows": len(findings["se_rows"]),
		"sre": len(findings["sre"]),
		"mop_log_stamps": len(findings["mop_log"]["stamps"]),
		"mop_log_name_reuse": len(findings["mop_log"]["name_reuse"]),
		"mop_log_orphans": findings["mop_log"]["orphans"]["rows"],
		"pointer_foreign": len(findings["pointer_foreign"]),
		"pointer_stale": len(findings["pointer_stale"]),
		"pointer_on_non_submitted": len(findings["pointer_on_non_submitted"]["rows"]),
		"casting_receive_without_tree": len(findings["casting_receive_without_tree"]),
		"subcontracting_log": len(findings["subcontracting_log"]),
		"process_loss_multi_mwo": findings["process_loss_multi_mwo"]["rows"],
	}
	for key in ("negative_latest_balances", "make_receive_consumed_foreign_sre"):
		if key in findings:
			counts[key] = len(findings[key])
	return counts


def _tally(rows, key) -> dict:
	tally: dict = {}
	for row in rows:
		value = key(row) if callable(key) else row.get(key)
		tally[value] = tally.get(value, 0) + 1
	return tally


def _count_name_reuse(rows) -> int:
	"""Rows whose operation is newer than their document (``is_name_reuse``): reported, never repaired."""
	return sum(1 for row in rows if cint(row.get("is_name_reuse")))


def _se_origin(row) -> str:
	if not cint(row.get("auto_created")):
		return "manual"
	return "eir_injection" if row.get("custom_eir_operation_row") else "other_auto"


@frappe.whitelist()
def audit_mwo_mop_consistency(
	limit: int = 200, include_expensive: bool = False
) -> dict:
	"""Read-only census of records that pair a Manufacturing Operation with a work order that does
	not own it. Returns ``{class: {"count": n, "rows": [...]}}``: ``rows`` is capped at ``limit``
	per class (``limit=0`` returns everything), ``count`` never is; some classes add a breakdown.

	Classes: ``ir_rows`` and ``se_headers`` (each with ``name_reuse``: rows whose operation is newer
	than their document), ``se_rows``, ``sre`` (``active`` / ``cancelled``),
	``mop_log_stamps``, ``mop_log_name_reuse``, ``mop_log_orphans`` (count only),
	``pointer_foreign`` (``by_docstatus``), ``pointer_stale``, ``pointer_on_non_submitted``,
	``casting_receive_without_tree``, ``subcontracting_log`` and ``process_loss_multi_mwo`` (the
	exemption, count only); with ``include_expensive`` also ``negative_latest_balances`` and
	``make_receive_consumed_foreign_sre``. System Manager only: it reads every work order.

	Every detector is a full pass over its table (see the section comment): seconds on a site the
	size of kggk-prod, but on a ledger of millions of MOP Log rows the whole census can outlast a web
	request. There, run it with ``bench execute`` (module docstring) instead of System Console::

	    frappe.call(
	        "jewellery_erpnext.mop_lineage_audit.audit_mwo_mop_consistency", {limit: 50}
	    )
	"""
	frappe.only_for("System Manager")
	limit = max(cint(limit), 0)
	findings = mwo_mop_findings(include_expensive=bool(sbool(include_expensive)))

	def bucket(rows, **extra):
		rows = list(rows or ())
		return {"count": len(rows), "rows": rows[:limit] if limit else rows, **extra}

	sre = findings["sre"]
	mop_log = findings["mop_log"]
	non_submitted = findings["pointer_on_non_submitted"]
	process_loss = findings["process_loss_multi_mwo"]
	out = {
		"ir_rows": bucket(
			findings["ir_rows"],
			by_parenttype=_tally(findings["ir_rows"], "parenttype"),
			name_reuse=_count_name_reuse(findings["ir_rows"]),
		),
		"se_headers": bucket(
			findings["se_headers"],
			by_origin=_tally(findings["se_headers"], _se_origin),
			name_reuse=_count_name_reuse(findings["se_headers"]),
		),
		"se_rows": bucket(
			findings["se_rows"], by_rule=_tally(findings["se_rows"], "rule")
		),
		"sre": bucket(
			sre,
			active=sum(1 for row in sre if cint(row.docstatus) == 1),
			cancelled=sum(1 for row in sre if cint(row.docstatus) == 2),
		),
		"mop_log_stamps": bucket(mop_log["stamps"]),
		"mop_log_name_reuse": bucket(
			mop_log["name_reuse"],
			operations=len({row.mop for row in mop_log["name_reuse"]}),
		),
		"mop_log_orphans": {
			"count": mop_log["orphans"]["rows"],
			"operations": mop_log["orphans"]["operations"],
			"rows": [],
		},
		"pointer_foreign": bucket(
			findings["pointer_foreign"],
			by_docstatus=_tally(findings["pointer_foreign"], "docstatus"),
		),
		"pointer_stale": bucket(findings["pointer_stale"]),
		"pointer_on_non_submitted": bucket(
			non_submitted["rows"],
			cancelled_own_pointer=non_submitted["cancelled_own_pointer"],
		),
		"casting_receive_without_tree": bucket(
			findings["casting_receive_without_tree"]
		),
		"subcontracting_log": bucket(findings["subcontracting_log"]),
		"process_loss_multi_mwo": {
			"count": process_loss["rows"],
			"stock_entries": process_loss["stock_entries"],
			"rows": [],
		},
	}
	if "negative_latest_balances" in findings:
		negatives = findings["negative_latest_balances"]
		out["negative_latest_balances"] = bucket(
			negatives,
			operations=len({row["manufacturing_operation"] for row in negatives}),
		)
		out["make_receive_consumed_foreign_sre"] = bucket(
			findings["make_receive_consumed_foreign_sre"]
		)
	return out


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
	# Cheap classes only: the expensive negative-balance sweep is already in this output above.
	out["mwo_mop_consistency"] = audit_mwo_mop_consistency()
	return out
