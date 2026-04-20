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
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import frappe


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
				'"voucher_type": "Department IR"' in text and '"voucher_no": self.name' in text
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
	return frappe.db.sql(
		"""
		SELECT DISTINCT manufacturing_operation
		FROM `tabDepartment IR Operation`
		WHERE parent = %(p)s AND IFNULL(manufacturing_operation,'') != ''
		""",
		{"p": receive_name},
		pluck="manufacturing_operation",
	) or []


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
		"snc_submitted_empty_source_table": out["templates"]["snc_submitted_empty_source_table"],
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
	base["stock_entry_legacy_balance_trace"] = get_stock_entry_legacy_balance_table_trace()
	base["proof_pack"]["archive_hint"] = (
		"Save this JSON from bench output to your ticket / evidence store; re-run after each deploy."
	)
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
		l for l in all_logs if l.get("item_code") and l["item_code"][0] in ("D", "G")
	]
	issue_logs = (
		_mop_log_rows("AND voucher_type = 'Employee IR' AND voucher_no = %s", (issue_voucher,))
		if issue_voucher
		else []
	)
	issue_diamond_logs = [
		l for l in issue_logs if l.get("item_code") and l["item_code"][0] in ("D", "G")
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
			"in_current_but_missing_from_issue_snapshot": sorted(current_keys - issue_keys),
		},
		"mop_log_diamond_rows": diamond_logs,
		"issue_voucher_diamond_rows": issue_diamond_logs,
		"stock_entry_diamond_lines": se_diamond_lines,
		"diagnosis": diagnosis or [
			"Diamond present uniformly in Stock Entry, MOP Log, Issue snapshot and header — "
			"investigate UI / Receive form rendering instead of data lineage."
		],
	}


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
	out["submission_queue_duplicates"] = audit_submission_queue_department_ir_duplicates()
	out["error_log_dir_fallback_recent"] = audit_error_log_dir_fallback(20)
	return out
