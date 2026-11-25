"""Move work orders from Waxing to Manufacturing Plan & Management with Department IR.

One-off data patch, registered in patches.txt: the first `bench migrate` after the deploy
runs ``execute()`` once -- it adds MOP Log's ``mop_balance_idx`` if missing, then applies
the transfer. It never fails the migrate: rows that could not be moved are reported (Error
Log "Waxing to MPM transfer" + the CSV in File) and can be re-run from System Console with
``enqueue(mode="apply", ...)`` -- the transfer is idempotent.

The work orders are listed in ``transfer_waxing_to_mpm_mwos.txt`` next to this file (the
1,294 "Not exists in jwelex" rows of Cancel 2.xlsx). Each batch becomes one Department IR
Issue (Waxing -> Manufacturing Plan & Management) and its Receive -- the same pair an
operator makes in the desk (e.g. Department-IR-Labh-2026-08274 / -08275). Every document
goes through its normal insert/submit, so all Department IR validations and hooks run.

Modes
    plan      read-only; classifies every MWO as Ready / Already moved / Skipped (+ reason)
    rehearse  makes every Issue + Receive, then rolls each batch back (nothing is kept)
    apply     makes and commits every Issue + Receive, one transaction per batch

Run from System Console (as a System Manager):
    log(frappe.call("jewellery_erpnext.patches.transfer_waxing_to_mpm.enqueue", mode="plan"))
    log(frappe.call("jewellery_erpnext.patches.transfer_waxing_to_mpm.enqueue",
                    mode="apply", confirm="kggk-prod.frappe.cloud"))
rehearse/apply run as a background job on the long queue; the result is a private CSV in
File (transfer_waxing_to_mpm_<mode>_<timestamp>.csv) and an Error Log titled
"Waxing to MPM transfer".

Or on a bench:
    bench --site kggk-dummy execute jewellery_erpnext.patches.transfer_waxing_to_mpm.plan
    bench --site kggk-dummy execute jewellery_erpnext.patches.transfer_waxing_to_mpm.run \
        --kwargs "{'mode': 'rehearse', 'confirm': 'kggk-dummy'}"

Running apply again is safe: work orders already moved are reported as "Already in
Manufacturing Plan & Management" and not touched.

Expected result (plan on kggk-prod.frappe.cloud, 2026-11-24) -- see EXPECTED_COUNTS and
EXPECTED_NOT_TRANSFERRED below; every plan / rehearse / apply reports any drift from them:
    1,294 MWOs in the list
    1,280 Ready        -> moved Waxing -> Manufacturing Plan & Management
       14 not moved by a Department IR, by the MWO's department:
           Waxing - KGJPL                              5  (skipped, operation cannot be issued)
           Manufacturing Plan & Management  - KGJPL    7  (already there, nothing to do)
           Central - KGJPL                             1  (skipped)
           Model Making - KGJPL                        1  (skipped)
"""

import csv
import io
import json
import os
from collections import Counter

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

FROM_DEPARTMENT = "Waxing - KGJPL"
# The department's name has two spaces before the dash.
TO_DEPARTMENT = "Manufacturing Plan & Management  - KGJPL"
COMPANY = "KG GK Jewellers Private Limited"
MANUFACTURER = "Labh"
BATCH_SIZE = 100

# Result of `plan` on kggk-prod.frappe.cloud, 2026-11-24 (Cancel 2.xlsx, 1,294 filtered MWOs).
EXPECTED_COUNTS = {
	"Ready": 1280,  # Waxing -> Manufacturing Plan & Management
	"Skipped": 7,  # not transferred, see EXPECTED_NOT_TRANSFERRED
	"Already in Manufacturing Plan & Management": 7,  # nothing to do
}

# The 14 MWOs that will NOT go through a Department IR.
# (MWO, MWO department, current MOP, MOP department / status, reason)
EXPECTED_NOT_TRANSFERRED = {
	"Skipped": [
		# --- MWO in Waxing, but its operation cannot be issued (5)
		(
			"MWO-KGJPL-PE00428-001-6-91.75-Y-01",
			"Waxing - KGJPL",
			"MOP-2610-GX6Q4T",
			"Waxing - KGJPL / Finished",
			"operation already Finished in Waxing; an Issue takes only Not Started operations",
		),
		(
			"MWO-KGJPL-RI01202-003-1-91.75-Y-01",
			"Waxing - KGJPL",
			"MOP-2609-4HR4P4",
			"Waxing - KGJPL / Not Started",
			"already on draft Issue Department-IR-Labh-2026-08720 (Waxing -> MPM, prasenjit_d, "
			"2026-11-23); submit or delete that draft",
		),
		(
			"MWO-KGJPL-NE05090-002-3-91.75-Y-02",
			"Waxing - KGJPL",
			"MOP-8JH36",
			"Manufacturing Plan & Management  - KGJPL / In-Transit",
			"already issued to MPM on Department-IR-Labh-2026-01739, never received; make that Receive",
		),
		(
			"MWO-KGJPL-NE00935-002-1-91.75-Y-03",
			"Waxing - KGJPL",
			"MOP-YZ802",
			"Waxing - KGJPL / Finished",
			"MWO Completed (delivered and invoiced)",
		),
		(
			"MWO-KGJPL-MU00378-004-2-91.75-Y-01",
			"Waxing - KGJPL",
			"MOP-V2C74",
			"Waxing - KGJPL / Finished",
			"MWO Closed",
		),
		# --- MWO in a department other than Waxing (2)
		(
			"MWO-KGJPL-NE24412-001-1-91.75-Y-04",
			"Central - KGJPL",
			"MOP-2611-YGN48F",
			"Central - KGJPL / Not Started",
			"MWO is in Central, not Waxing",
		),
		(
			"MWO-KGJPL-MU02068-005-5-91.75-Y-03",
			"Model Making - KGJPL",
			"MOP-2611-BVW5X4",
			"Waxing - KGJPL / In-Transit",
			"MWO in Model Making; operation In-Transit into Waxing on Department-IR-Labh-2026-08368, "
			"not received",
		),
	],
	# --- MWO already in Manufacturing Plan & Management (7)
	"Already in Manufacturing Plan & Management": [
		(
			"MWO-KGJPL-PE00428-001-5-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-2610-ZUT0W8",
			"MPM / Finished",
			"already moved",
		),
		(
			"MWO-KGJPL-RI02163-049-1-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-6Z61X",
			"MPM / Not Started",
			"already moved",
		),
		(
			"MWO-KGJPL-RI02163-037-1-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-J958G",
			"MPM / Not Started",
			"already moved",
		),
		(
			"MWO-KGJPL-RI14911-001-1-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-N937V",
			"MPM / Not Started",
			"already moved",
		),
		(
			"MWO-KGJPL-EA00982-005-1-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-5XM22",
			"MPM / Not Started",
			"already moved",
		),
		(
			"MWO-KGJPL-EA01497-029-2-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-00RN4",
			"MPM / Not Started",
			"already moved",
		),
		(
			"MWO-KGJPL-EA01497-029-3-91.75-Y-01",
			"Manufacturing Plan & Management  - KGJPL",
			"MOP-7M31R",
			"MPM / Not Started",
			"already moved",
		),
	],
}

JOB_ID = "transfer_waxing_to_mpm"
REPORT_TITLE = "Waxing to MPM transfer"
MWO_LIST = os.path.join(
	os.path.dirname(os.path.abspath(__file__)), "transfer_waxing_to_mpm_mwos.txt"
)

READY = "Ready"
ALREADY = "Already in Manufacturing Plan & Management"
SKIPPED = "Skipped"
MOVED = "Moved"
REHEARSED = "Rehearsed OK"
FAILED = "Failed"

# MWO statuses that must not be transferred.
BLOCKED_MWO_STATUSES = ("Closed", "Completed", "Stopped", "Cancelled")
# PMO workflow states that refuse a transfer (close_open.BLOCKING_STATES) or are cancelled.
BLOCKED_PMO_STATES = ("Hold", "Hold (Draft)", "Closed", "Closed (Draft)", "Cancelled")
CHUNK = 500


# --------------------------------------------------------------------------- entry points


def execute():
	"""patches.txt entry point: index first, then apply on this site."""
	_ensure_mop_balance_index()
	try:
		summary = run(mode="apply", confirm=frappe.local.site)
	except Exception:
		# A data fix must never roll back a deploy. Whatever was committed stays; the
		# rest can be re-run from System Console with enqueue(mode="apply", ...).
		frappe.db.rollback()
		frappe.log_error(title=REPORT_TITLE + " (patch failed)")
		frappe.db.commit()
		print(f"{REPORT_TITLE}: failed, see Error Log '{REPORT_TITLE} (patch failed)'")
		return
	print(f"{REPORT_TITLE}: {summary['counts']} report={summary['report']}")


def _ensure_mop_balance_index():
	"""MOP Log's balance index, as in add_make_receive_entry_indexes.

	That patch is recorded as run on prod but the index is missing there, so every
	Department IR balance read full-scans MOP Log (~6 s per operation on 717k rows;
	with the index the whole transfer took 5.5 min on a restore of prod).
	"""
	from jewellery_erpnext.patches.add_make_receive_entry_indexes import _index_exists

	if _index_exists("tabMOP Log", "mop_balance_idx"):
		return
	frappe.db.sql_ddl(
		"ALTER TABLE `tabMOP Log` ADD INDEX `mop_balance_idx` "
		"(`manufacturing_operation`, `is_cancelled`, `item_code`, `batch_no`, `creation`)"
	)


@frappe.whitelist()
def enqueue(mode="plan", confirm=None, batch_size=BATCH_SIZE):
	"""System Console entry point. plan runs inline; rehearse/apply are queued."""
	frappe.only_for("System Manager")
	if mode == "plan":
		return plan()
	_check_mode(mode, confirm)
	frappe.enqueue(
		"jewellery_erpnext.patches.transfer_waxing_to_mpm.run",
		queue="long",
		timeout=4 * 3600,
		job_id=JOB_ID,
		deduplicate=True,
		mode=mode,
		confirm=confirm,
		batch_size=cint(batch_size) or BATCH_SIZE,
	)
	return _(
		"Queued {0} as job {1}. Look for the Error Log '{2}' and the CSV in File when it ends."
	).format(mode, JOB_ID, REPORT_TITLE)


def plan(mwo_list=None):
	"""Read-only. Returns the counts and every skipped / already-moved work order."""
	rows = _build_plan(_load_mwo_names(mwo_list))
	drift = _drift(rows)
	return {
		"site": frappe.local.site,
		"total": len(rows),
		"counts": dict(Counter(r["result"] for r in rows)),
		"matches_expected": not any(drift.values()),
		"drift": drift,
		"by_department": dict(
			Counter(
				r["mwo_department"] or "(no MWO)" for r in rows if r["result"] != READY
			)
		),
		"ready_holding_stock": [
			r["mwo"] for r in rows if r["result"] == READY and r["holds_stock"]
		],
		"skipped": [
			{"mwo": r["mwo"], "pmo": r["pmo"], "reason": r["reason"]}
			for r in rows
			if r["result"] == SKIPPED
		],
		"already_moved": [
			{"mwo": r["mwo"], "reason": r["reason"]}
			for r in rows
			if r["result"] == ALREADY
		],
	}


def run(mode="rehearse", confirm=None, batch_size=BATCH_SIZE, mwo_list=None):
	"""rehearse or apply. Called by the queued job, or directly with bench execute."""
	_check_mode(mode, confirm)
	batch_size = cint(batch_size) or BATCH_SIZE
	started = now_datetime()

	rows = _build_plan(_load_mwo_names(mwo_list))
	# Drift is judged on the plan, before any row's result turns into Moved / Failed.
	drift = _drift(rows)
	ready = [r for r in rows if r["result"] == READY]
	# Nothing the plan read is held open across the batches.
	frappe.db.rollback()

	queue = [ready[i : i + batch_size] for i in range(0, len(ready), batch_size)]
	while queue:
		batch = queue.pop(0)
		error = _transfer_batch(batch, mode)
		if error is None:
			continue
		if len(batch) > 1:
			# Split and retry, so one bad work order cannot hold back the other 99.
			half = len(batch) // 2
			queue[0:0] = [batch[:half], batch[half:]]
		else:
			batch[0].update(result=FAILED, reason=error)

	summary = {
		"site": frappe.local.site,
		"mode": mode,
		"started": str(started),
		"finished": str(now_datetime()),
		"total": len(rows),
		"counts": dict(Counter(r["result"] for r in rows)),
		"matches_expected": not any(drift.values()),
		"drift": drift,
		"issues": sorted({r["issue"] for r in rows if r.get("issue")}),
		"receives": sorted({r["receive"] for r in rows if r.get("receive")}),
	}
	summary["report"] = _save_report(rows, mode)
	frappe.log_error(
		title=REPORT_TITLE, message=json.dumps(summary, indent=1, default=str)
	)
	frappe.db.commit()
	return summary


# --------------------------------------------------------------------------- the transfer


def _transfer_batch(batch, mode):
	"""One Issue + its Receive for ``batch``. Returns None on success, else the error text.

	Each batch is its own transaction. A failure rolls the whole transaction back (not to
	a savepoint), which also drops any after-commit callbacks the failed submits queued,
	so nothing of a failed batch can run at the next commit. Every earlier batch is
	already committed (apply) or rolled back (rehearse), so a full rollback loses nothing.
	"""
	now = now_datetime()
	for attempt in range(3):
		try:
			issue = _make_issue(batch, now)
			receive = _make_receive(issue, batch, now)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			frappe.db.rollback()
			_clear_messages()
			if attempt == 2:
				return _error_text()
			continue
		except Exception:
			error = _error_text()
			frappe.db.rollback()
			_clear_messages()
			return error
		break

	new_mops = {
		row.manufacturing_work_order: row.manufacturing_operation
		for row in receive.department_ir_operation
	}
	if mode == "apply":
		frappe.db.commit()
	else:
		frappe.db.rollback()
	_clear_messages()

	for r in batch:
		r.update(
			result=MOVED if mode == "apply" else REHEARSED,
			reason="",
			issue=issue.name,
			receive=receive.name,
			new_mop=new_mops.get(r["mwo"]),
		)
	return None


def _make_issue(batch, now):
	issue = frappe.new_doc("Department IR")
	issue.update(
		{
			"type": "Issue",
			"company": COMPANY,
			"manufacturer": MANUFACTURER,
			"current_department": FROM_DEPARTMENT,
			"next_department": TO_DEPARTMENT,
			"transfer_type": "Next Department",
			"date_time": now,
		}
	)
	for r in batch:
		issue.append(
			"department_ir_operation",
			{
				"manufacturing_operation": r["mop"],
				"manufacturing_work_order": r["mwo"],
				"parent_manufacturing_order": r["pmo"],
			},
		)
	issue.flags.ignore_permissions = True
	issue.insert()
	issue.submit()
	return issue


def _make_receive(issue, batch, now):
	receive = frappe.new_doc("Department IR")
	receive.update(
		{
			"type": "Receive",
			"company": COMPANY,
			"manufacturer": MANUFACTURER,
			"current_department": TO_DEPARTMENT,
			"previous_department": FROM_DEPARTMENT,
			"receive_against": issue.name,
			"date_time": now,
		}
	)
	# The doctype's own "Get Operations" for a Receive: every operation the Issue put
	# In-Transit, in the Issue's order.
	receive.get_manufacturing_operations_from_department_ir(issue.name)
	received = {row.manufacturing_work_order for row in receive.department_ir_operation}
	missing = sorted({r["mwo"] for r in batch} - received)
	if missing or receive.current_department != TO_DEPARTMENT:
		frappe.throw(
			_("Issue {0} did not put every work order In-Transit to {1}: {2}").format(
				issue.name,
				TO_DEPARTMENT,
				", ".join(missing) or receive.current_department,
			)
		)
	receive.flags.ignore_permissions = True
	receive.insert()
	receive.submit()
	return receive


# --------------------------------------------------------------------------- the plan


def _build_plan(names):
	mwos = _get_all(
		"Manufacturing Work Order",
		names,
		[
			"name",
			"docstatus",
			"status",
			"department",
			"manufacturing_order",
			"manufacturing_operation",
			"is_finding_mwo",
		],
	)
	pmos = _get_all(
		"Parent Manufacturing Order",
		{m.manufacturing_order for m in mwos.values() if m.manufacturing_order},
		["name", "docstatus", "workflow_state"],
	)
	mops = _get_all(
		"Manufacturing Operation",
		{m.manufacturing_operation for m in mwos.values() if m.manufacturing_operation},
		[
			"name",
			"status",
			"department",
			"department_ir_status",
			"department_issue_id",
			"manufacturing_work_order",
		],
	)
	open_dirs = _open_documents(
		"Department IR",
		"Department IR Operation",
		"department_ir_operation",
		list(mops),
	)
	open_eirs = _open_documents(
		"Employee IR",
		"Employee IR Operation",
		"employee_ir_operations",
		list(mops),
		drafts_only=True,
	)
	stocked = _operations_holding_stock(list(mops))

	rows = []
	for name in names:
		mwo = mwos.get(name)
		mop = (
			mops.get(mwo.manufacturing_operation)
			if mwo and mwo.manufacturing_operation
			else None
		)
		pmo = (
			pmos.get(mwo.manufacturing_order)
			if mwo and mwo.manufacturing_order
			else None
		)
		result, reason = _classify(name, mwo, mop, pmo, open_dirs, open_eirs)
		rows.append(
			{
				"mwo": name,
				"pmo": mwo.manufacturing_order if mwo else "",
				"mwo_department": mwo.department if mwo else "",
				"mwo_status": mwo.status if mwo else "",
				"finding_mwo": cint(mwo.is_finding_mwo) if mwo else 0,
				"mop": mop.name
				if mop
				else (mwo.manufacturing_operation if mwo else ""),
				"mop_department": mop.department if mop else "",
				"mop_status": mop.status if mop else "",
				"holds_stock": int(bool(mop and mop.name in stocked)),
				"result": result,
				"reason": reason,
				"issue": "",
				"receive": "",
				"new_mop": "",
			}
		)
	return rows


def _classify(name, mwo, mop, pmo, open_dirs, open_eirs):
	if not mwo:
		return SKIPPED, "MWO not found"
	if mwo.docstatus != 1:
		return SKIPPED, "MWO is {0}".format(
			"Draft" if mwo.docstatus == 0 else "Cancelled"
		)
	if mwo.status in BLOCKED_MWO_STATUSES:
		return SKIPPED, f"MWO status is {mwo.status}"
	if not pmo:
		return SKIPPED, f"PMO {mwo.manufacturing_order or '(blank)'} not found"
	if pmo.docstatus != 1:
		return SKIPPED, f"PMO {pmo.name} is not submitted ({pmo.workflow_state})"
	if pmo.workflow_state in BLOCKED_PMO_STATES:
		return SKIPPED, f"PMO {pmo.name} is in {pmo.workflow_state}"
	if not mop:
		return SKIPPED, "MWO has no current operation"
	if mop.manufacturing_work_order != name:
		return (
			SKIPPED,
			f"current operation {mop.name} belongs to {mop.manufacturing_work_order}",
		)
	if mop.department_ir_status == "In-Transit":
		return SKIPPED, (
			f"current operation {mop.name} is In-Transit to {mop.department} on "
			f"{mop.department_issue_id}, which is not received yet; make its Receive instead"
		)
	if mwo.department == TO_DEPARTMENT and mop.department == TO_DEPARTMENT:
		return ALREADY, f"MWO and operation {mop.name} are already in {TO_DEPARTMENT}"
	if mwo.department != FROM_DEPARTMENT:
		return SKIPPED, f"MWO is in {mwo.department}, not {FROM_DEPARTMENT}"
	if mop.department != FROM_DEPARTMENT:
		return (
			SKIPPED,
			f"current operation {mop.name} is in {mop.department}, not {FROM_DEPARTMENT}",
		)
	if mop.status != "Not Started":
		return SKIPPED, (
			f"current operation {mop.name} is {mop.status}; a Department IR Issue takes "
			"only Not Started operations"
		)
	if mop.department_ir_status == "Revert":
		return SKIPPED, f"current operation {mop.name} is in Revert"
	if mop.name in open_dirs:
		return (
			SKIPPED,
			f"operation {mop.name} is already on Department IR Issue {open_dirs[mop.name]}",
		)
	if mop.name in open_eirs:
		return (
			SKIPPED,
			f"operation {mop.name} is on Employee IR Issue {open_eirs[mop.name]}",
		)
	return READY, ""


def _get_all(doctype, names, fields):
	names = [n for n in names if n]
	out = {}
	for i in range(0, len(names), CHUNK):
		for d in frappe.get_all(
			doctype, filters={"name": ["in", names[i : i + CHUNK]]}, fields=fields
		):
			out[d.name] = d
	return out


def _open_documents(
	parent_doctype, child_doctype, parentfield, mops, drafts_only=False
):
	"""{operation: document} for non-cancelled Issues (drafts too) holding the operation.

	The same rule as department_ir_utils.validate_duplicate, which would refuse the Issue.
	"""
	parent = frappe.qb.DocType(parent_doctype)
	child = frappe.qb.DocType(child_doctype)
	status = parent.docstatus == 0 if drafts_only else parent.docstatus != 2
	out = {}
	for i in range(0, len(mops), CHUNK):
		rows = (
			frappe.qb.from_(child)
			.join(parent)
			.on(child.parent == parent.name)
			.select(child.manufacturing_operation, parent.name, parent.docstatus)
			.where(
				(child.parenttype == parent_doctype)
				& (child.parentfield == parentfield)
				& (parent.type == "Issue")
				& status
				& child.manufacturing_operation.isin(mops[i : i + CHUNK])
			)
		).run(as_dict=True)
		for row in rows:
			state = "draft" if cint(row.docstatus) == 0 else "submitted"
			out.setdefault(row.manufacturing_operation, f"{row.name} ({state})")
	return out


def _operations_holding_stock(mops):
	"""Operations with a non-zero MOP Log balance: the EOD sync will post a stock move
	for these after the transfer. Informational only."""
	out = set()
	for i in range(0, len(mops), CHUNK):
		out.update(
			frappe.get_all(
				"MOP Log",
				filters={
					"manufacturing_operation": ["in", mops[i : i + CHUNK]],
					"is_cancelled": 0,
					"qty_after_transaction": ["!=", 0],
				},
				pluck="manufacturing_operation",
				distinct=True,
			)
		)
	return out


# --------------------------------------------------------------------------- drift


def _drift(rows):
	"""How the live plan differs from EXPECTED_COUNTS / EXPECTED_NOT_TRANSFERRED.

	Every value is empty when the site still looks exactly as it did when the lists were
	written. Informational only: apply still moves just the rows that are Ready live.
	"""
	counts = Counter(r["result"] for r in rows)
	count_changes = {
		result: {
			"expected": EXPECTED_COUNTS.get(result, 0),
			"live": counts.get(result, 0),
		}
		for result in set(EXPECTED_COUNTS) | set(counts)
		if EXPECTED_COUNTS.get(result, 0) != counts.get(result, 0)
	}

	expected = {
		entry[0]: result
		for result, entries in EXPECTED_NOT_TRANSFERRED.items()
		for entry in entries
	}
	live = {r["mwo"]: r for r in rows}
	newly_not_transferred = [
		{
			"mwo": r["mwo"],
			"result": r["result"],
			"department": r["mwo_department"],
			"reason": r["reason"],
		}
		for r in rows
		if r["result"] != READY and r["mwo"] not in expected
	]
	now_ready, changed_result = [], []
	for mwo, result in expected.items():
		row = live.get(mwo)
		if not row:
			changed_result.append(
				{"mwo": mwo, "expected": result, "live": "not in the MWO list"}
			)
		elif row["result"] == READY:
			now_ready.append(
				{"mwo": mwo, "expected": result, "department": row["mwo_department"]}
			)
		elif row["result"] != result:
			changed_result.append(
				{
					"mwo": mwo,
					"expected": result,
					"live": row["result"],
					"reason": row["reason"],
				}
			)
	return {
		"count_changes": count_changes,
		"newly_not_transferred": newly_not_transferred,
		"now_ready": now_ready,
		"changed_result": changed_result,
	}


# --------------------------------------------------------------------------- helpers


def _load_mwo_names(mwo_list=None):
	if mwo_list:
		names = json.loads(mwo_list) if isinstance(mwo_list, str) else list(mwo_list)
	else:
		with open(MWO_LIST, encoding="utf-8") as f:
			names = [
				line.strip()
				for line in f
				if line.strip() and not line.lstrip().startswith("#")
			]
	return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def _check_mode(mode, confirm):
	if mode not in ("rehearse", "apply"):
		frappe.throw(_("mode must be plan, rehearse or apply, not {0}").format(mode))
	if confirm != frappe.local.site:
		frappe.throw(
			_("{0} writes to this site: pass confirm='{1}' to go ahead.").format(
				mode, frappe.local.site
			)
		)


def _error_text():
	messages = []
	for raw in frappe.local.message_log or []:
		try:
			msg = json.loads(raw) if isinstance(raw, str) else raw
			messages.append(frappe.utils.strip_html(msg.get("message") or ""))
		except Exception:
			messages.append(str(raw))
	text = (
		"; ".join(m for m in messages if m)
		or frappe.get_traceback().strip().splitlines()[-1]
	)
	return text[:1000]


def _clear_messages():
	frappe.local.message_log = []
	frappe.clear_messages()


REPORT_FIELDS = (
	"mwo",
	"pmo",
	"result",
	"reason",
	"mwo_department",
	"mwo_status",
	"finding_mwo",
	"mop",
	"mop_department",
	"mop_status",
	"holds_stock",
	"issue",
	"receive",
	"new_mop",
)


def _save_report(rows, mode):
	buf = io.StringIO()
	writer = csv.DictWriter(buf, fieldnames=REPORT_FIELDS, extrasaction="ignore")
	writer.writeheader()
	order = {FAILED: 0, SKIPPED: 1, ALREADY: 2}
	writer.writerows(sorted(rows, key=lambda r: order.get(r["result"], 3)))
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": "transfer_waxing_to_mpm_{0}_{1}.csv".format(
				mode, now_datetime().strftime("%Y%m%d_%H%M%S")
			),
			"is_private": 1,
			"content": buf.getvalue(),
		}
	)
	file_doc.insert(ignore_permissions=True)
	return file_doc.file_url
