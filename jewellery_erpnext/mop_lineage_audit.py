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
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import frappe
from frappe.utils import cint, flt

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
