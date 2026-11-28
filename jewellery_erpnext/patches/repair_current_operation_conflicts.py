"""Reviewed, guarded repair of work-order current-operation damage (family C first).

WHAT IT REPAIRS
---------------
Family C of ``mop_lineage_audit.audit_current_operation_conflicts``: a stale Employee IR Issue --
a draft submitted after its work order had moved on (EMP-IR-Labh-2026-43405 on 2026-10-05) or a
second Issue of an operation that was already issued -- reopened finished operations, overwrote
their start fields, opened a time log and cloned pre-receive balances into MOP Logs.

The repair of one such Issue, in ONE transaction:

1. lock the Employee IR row (NOWAIT), then its operations, then its work orders, then its MOP
   Log rows (lock_order RULE D/E: the same order as a normal cancel);
2. compare every expected value recorded in the manifest -- ``modified`` included -- and abort
   with a field-level diff on any drift; an already-repaired case is reported and left alone,
   a half-repaired one aborts;
3. re-run the manifest's required-absent checks and the EOD Sync Log proof for already-synced
   logs;
4. cancel the Issue through the SUPPORTED path (``doc.cancel()``) with
   ``frappe.flags.current_operation_reviewed_repair = {("Employee IR", name)}`` so the cancel
   guard lets this one reviewed document through (doc_events/current_operation_guard.py);
   that sets its MOP Logs ``is_cancelled = 1``, recomputes weights and refreshes the MSL table;
5. AFTER the cancel -- whose Issue branch forces Not Started and clears operation / employee /
   start_time -- restore each operation to the reviewed targets through its document (a
   Version records the change), deleting the listed stale time-log rows by name (a listed row
   the cancel itself already removed counts as deleted);
6. recompute the operation weights, add a Comment with the before/after values on the
   Employee IR, every operation and every work order;
7. post-verify (exactly one open operation per work order and it is the pointer, or none when
   the pointer is closed; no active MOP Log of the voucher; targets in place; the scoped audit
   clean) and commit.

NOT registered in patches.txt -- it never runs on migrate.

MODES
-----
* ``plan`` (default) -- SELECT-only, inside a READ ONLY transaction: current-vs-expected diff,
  guard results and the exact writes ``apply`` would make. Safe on production.
* ``rehearse`` -- the full apply, then ROLLBACK. Refused on the manifest's ``production_site``
  and on any site whose site_config sets ``current_operation_repair_read_only``.
* ``apply`` -- requires the manifest's ``reviewed_by`` + ``reviewed_on``, a ``ticket``, the
  Administrator user and ``site == manifest.site``. One commit per Employee IR; stops at the
  first case that does not apply cleanly (earlier cases stay committed).

USAGE (take a backup first; never inside the 19:00 EOD window)
-----
    bench --site <site> execute \\
        jewellery_erpnext.patches.repair_current_operation_conflicts.execute \\
        --kwargs "{'manifest_path': '/path/EMP-IR-Labh-2026-43405.json'}"

    ... --kwargs "{'manifest_path': '...', 'mode': 'apply', 'ticket': 'INC-1234'}"

On Frappe Cloud (no shell): ``run(manifest=<json>, mode=..., ticket=...)`` is whitelisted --
System Manager may plan; rehearse / apply need Administrator.

Build manifests with ``mop_lineage_audit.build_stale_issue_manifest`` on the site they are for,
right before the repair window. Families A (Department Receive cancels) and B (duplicate-row
twins) need business sign-off and are not handled here.
"""

import json
import math
import time
import traceback

import frappe
from frappe.utils import cint, cstr, escape_html, flt, get_datetime, now_datetime

from jewellery_erpnext.mop_lineage_audit import (
	CO_MANIFEST_KIND,
	CO_MANIFEST_VERSION,
	CO_SEALED_KEYS,
	_co_eod_sync_proof,
	_co_norm,
	_co_select_in,
	_ReadOnlyAudit,
	audit_current_operation_conflicts,
	co_blocking_required,
	co_build_stale_issue_case,
	co_case_sha256,
	co_compare_applied,
	co_compare_expected,
	co_read_case_state,
	co_required_absent,
	co_voucher_log_totals,
)

MODES = ("plan", "rehearse", "apply")
# A site whose site_config sets this key only ever plans: rehearse and apply are refused there,
# whatever the manifest says (for example a reference copy the manifests are reviewed against).
READ_ONLY_SITE_KEY = "current_operation_repair_read_only"
LOCK_WAIT_SECONDS = 15
DIFF_LIMIT = 200
REVIEWED_REPAIR_FLAG = "current_operation_reviewed_repair"
IN_PROGRESS_FLAG = "current_operation_repair_in_progress"
_FALSE_WORDS = ("", "0", "false", "no", "off", "none")


def _truthy(value):
	"""site_config switches: anything but None / False / 0 / "", "0", "false", "no", "off"."""
	if value is None or value is False:
		return False
	return cstr(value).strip().lower() not in _FALSE_WORDS


class RepairAborted(Exception):
	"""A case cannot be applied as reviewed; nothing of it was written."""

	def __init__(self, reason, details=None):
		super().__init__(reason)
		self.reason = reason
		self.details = details


# ---------------------------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------------------------


def execute(manifest_path=None, mode="plan", ticket=None, manifest=None):
	"""Plan (default) / rehearse / apply the cases of one manifest. Returns a report dict."""
	mode = cstr(mode or "plan").strip().lower()
	if mode not in MODES:
		frappe.throw(f"mode must be one of {', '.join(MODES)}")
	data = _load_manifest(manifest_path, manifest)
	if data is None:
		return {
			"status": "nothing to do",
			"usage": "execute(manifest_path=<file> | manifest=<json>, mode='plan'|'rehearse'|"
			"'apply', ticket=<ticket for apply>)",
		}
	_validate_manifest(data)
	if mode == "plan":
		return _plan(data)
	return _write(data, mode, cstr(ticket).strip() or None)


@frappe.whitelist()
def run(manifest, mode="plan", ticket=None):
	"""Frappe Cloud entry point (no shell): System Manager may plan; rehearse/apply need
	Administrator, which ``_global_guards`` enforces."""
	frappe.only_for("System Manager")
	return execute(manifest=manifest, mode=mode, ticket=ticket)


def _load_manifest(manifest_path, manifest):
	if manifest:
		return json.loads(manifest) if isinstance(manifest, str) else manifest
	if manifest_path:
		with open(manifest_path) as handle:
			return json.load(handle)
	return None


def _validate_manifest(data):
	if not isinstance(data, dict) or data.get("kind") != CO_MANIFEST_KIND:
		frappe.throw(f"not a {CO_MANIFEST_KIND} manifest")
	if cint(data.get("manifest_version")) != CO_MANIFEST_VERSION:
		frappe.throw(f"unsupported manifest_version {data.get('manifest_version')}")
	cases = data.get("cases")
	if not isinstance(cases, list) or not cases:
		frappe.throw("manifest has no cases")
	for case in cases:
		for key in (
			"employee_ir",
			"expected",
			"actions",
			"case_sha256",
			"required_absent_spec",
		):
			if key not in case:
				frappe.throw(f"manifest case {case.get('case_id')} lacks {key}")


# ---------------------------------------------------------------------------------------------
# guards shared by every mode
# ---------------------------------------------------------------------------------------------


def _eod_locked():
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock import (
		is_eod_sync_locked,
	)

	return bool(is_eod_sync_locked())


def _open_recon_windows(data):
	"""Departments of the cases whose stock reconciliation window is open right now."""
	from jewellery_erpnext.jewellery_erpnext import stock_recon_window as srw

	if not srw._enabled():
		return []
	departments = set()
	for case in data["cases"]:
		spec = case.get("required_absent_spec") or {}
		if spec.get("department"):
			departments.add(spec["department"])
		for row in (case["expected"].get("operations") or {}).values():
			if row.get("department"):
				departments.add(row["department"])
	return sorted(d for d in departments if srw._department_window_status(d) == "open")


def _global_guards(data, mode, ticket):
	site = frappe.local.site
	guards = {
		"site": site,
		"manifest_site": data.get("site"),
		"production_site": data.get("production_site"),
		"user": frappe.session.user,
		"reviewed_by": data.get("reviewed_by"),
		"reviewed_on": data.get("reviewed_on"),
		"ticket": ticket,
		"eod_lock_held": _eod_locked(),
		"recon_window_open": _open_recon_windows(data),
		"read_only_site": _truthy(frappe.conf.get(READ_ONLY_SITE_KEY)),
	}
	problems = []
	if guards["eod_lock_held"]:
		problems.append(
			"EOD sync lock is held: no repair while an EOD run is in flight."
		)
	if guards["recon_window_open"]:
		problems.append(
			"stock reconciliation window open for "
			+ ", ".join(guards["recon_window_open"])
			+ "; the cancel would be rejected."
		)
	if mode in ("rehearse", "apply") and frappe.session.user != "Administrator":
		problems.append(
			f"{mode} runs as Administrator only (current user {frappe.session.user})."
		)
	if mode in ("rehearse", "apply") and guards["read_only_site"]:
		problems.append(
			f"{mode} is refused on {site}: its site_config sets {READ_ONLY_SITE_KEY}."
		)
	if mode == "rehearse" and site == data.get("production_site"):
		problems.append(
			f"rehearse is refused on {site}, the manifest's production site; rehearse on a "
			"copy of it instead."
		)
	if mode == "apply":
		if site != data.get("site"):
			problems.append(
				f"apply must run on the site the manifest was built on ({data.get('site')}), "
				f"not {site}; rebuild the manifest there."
			)
		if not (data.get("reviewed_by") and data.get("reviewed_on")):
			problems.append(
				"the manifest has no reviewed_by / reviewed_on: it was not approved."
			)
		else:
			try:
				reviewed = get_datetime(data["reviewed_on"])
				if data.get("built_on") and reviewed < get_datetime(data["built_on"]):
					problems.append(
						"reviewed_on predates built_on: the review is of another build."
					)
			except Exception:
				problems.append(
					f"reviewed_on {data.get('reviewed_on')!r} is not a date/time."
				)
		if not ticket:
			problems.append("apply needs a ticket (recorded in every Comment).")
	guards["problems"] = problems
	return guards


def _case_problems(data, case):
	"""Static reasons a case may not be written, from the manifest alone."""
	problems = []
	if co_case_sha256(case) != case.get("case_sha256"):
		problems.append(
			"the case was edited after it was built (sha256 mismatch); rebuild the manifest "
			"instead of editing it"
		)
	if case.get("blockers"):
		problems.append(
			"the manifest lists blockers for this case: " + "; ".join(case["blockers"])
		)
	if case.get("status") != "ready":
		problems.append(
			f"the case's status is {case.get('status')!r}; only a 'ready' case is repaired"
		)
	acknowledged = set(case.get("review_acknowledged") or [])
	pending = [n for n in case.get("needs_review") or [] if n not in acknowledged]
	if pending:
		problems.append(
			"needs_review note(s) not acknowledged -- check each one, then copy it verbatim "
			"into this case's review_acknowledged list: " + "; ".join(pending)
		)
	return problems


def _sealed_diff(reviewed, rebuilt):
	"""The sealed keys on which the reviewed case and the case built now disagree."""
	out = {}
	for key in CO_SEALED_KEYS:
		a = json.dumps(reviewed.get(key), sort_keys=True, default=str)
		b = json.dumps(rebuilt.get(key), sort_keys=True, default=str)
		if a != b:
			out[key] = (
				{"reviewed": reviewed.get(key), "now": rebuilt.get(key)}
				if key in ("status", "blockers", "needs_review")
				else "differs"
			)
	return out


def _required_problems(required, listed_for_deletion):
	"""Blocking findings of ``co_required_absent`` (its informational keys never block)."""
	listed = set(listed_for_deletion or [])
	problems = []
	for check, found in co_blocking_required(required).items():
		if check == "draft_unresolved_eod_stock_entries":
			found = [f for f in found if f.get("stock_entry") not in listed]
		if found:
			problems.append({"check": check, "found": found[:20]})
	return problems


def _eod_proof_problems(case, state):
	"""Synced logs are allowed only with a manifest proof that still holds (re-read now)."""
	problems = []
	proofs = case.get("eod_sync_proof") or {}
	logs = state.get("mop_logs") or {}
	synced = {}
	for name, row in logs.items():
		if cint(row.get("is_synced")) and not cint(row.get("is_cancelled")):
			synced.setdefault(row.get("manufacturing_operation"), []).append(name)
	for mop, names in sorted(synced.items()):
		proof = proofs.get(mop)
		if not proof or proof.get("verdict") != "proven":
			problems.append(
				f"{mop}: log(s) {', '.join(sorted(names))} are already synced and the manifest "
				"carries no proven EOD Sync Log proof -- stop for a stock review"
			)
			continue
		missing = set(names) - set(proof.get("synced_logs") or [])
		if missing:
			problems.append(
				f"{mop}: synced log(s) {', '.join(sorted(missing))} are not in the proof"
			)
			continue
		fresh = _co_eod_sync_proof(
			proof["manufacturing_work_order"],
			mop,
			[
				frappe._dict(name=n, creation=get_datetime(logs[n]["creation"]))
				for n in sorted(names)
			],
		)
		if fresh["verdict"] != "proven":
			problems.append(
				f"{mop}: EOD Sync Log proof no longer holds ({fresh['verdict']})"
			)
	return problems


# ---------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------


def _report_header(data, mode, ticket=None):
	return {
		"mode": mode,
		"site": frappe.local.site,
		"manifest_site": data.get("site"),
		"production_site": data.get("production_site"),
		"built_on": data.get("built_on"),
		"reviewed_by": data.get("reviewed_by"),
		"reviewed_on": data.get("reviewed_on"),
		"ticket": ticket,
		"started_on": _co_norm(now_datetime()),
	}


def _plan(data):
	audit = _ReadOnlyAudit()
	with audit:
		report = _report_header(data, "plan")
		report["guards"] = _global_guards(data, "plan", None)
		report["cases"] = [_plan_case(data, case) for case in data["cases"]]
	report["read_only"] = audit.summary()
	report["would_apply"] = (
		[]
		if report["guards"]["problems"]
		else [
			c["employee_ir"] for c in report["cases"] if c["verdict"] == "would_apply"
		]
	)
	report["status"] = "planned"
	return report


def _classify_state(case, state):
	applied = co_compare_applied(case, state, include_pointers=False)
	if not applied:
		return "already_applied", [], []
	expected = co_compare_expected(case, state)
	if not expected:
		return "matches_expected", expected, applied
	head = state.get("employee_ir") or {}
	if cint(head.get("docstatus")) == 2 or _any_target_met(case, state):
		return "partial", expected, applied
	return "drift", expected, applied


def _any_target_met(case, state):
	"""True when an effect only this repair produces is already in place (some voucher log is
	cancelled). A missing time log is NOT counted: someone else may have removed it, and that is
	drift, not a half-applied repair."""
	for name in case["actions"].get("mop_logs_to_cancel") or []:
		row = (state.get("mop_logs") or {}).get(name) or {}
		if cint(row.get("is_cancelled")):
			return True
	return False


def _plan_case(data, case):
	out = {
		"employee_ir": case["employee_ir"],
		"manifest_status": case.get("status"),
		"case_sha256": case.get("case_sha256"),
		"manifest_problems": _case_problems(data, case),
	}
	state = co_read_case_state(case)
	verdict, expected_diff, applied_diff = _classify_state(case, state)
	out["state"] = verdict
	out["diff_vs_expected"] = expected_diff[:DIFF_LIMIT]
	out["diff_vs_repaired"] = applied_diff[:DIFF_LIMIT]
	required = co_required_absent(case["required_absent_spec"])
	out["required_absent_problems"] = _required_problems(
		required, data.get("delete_draft_stock_entries")
	)
	out["legitimate_later_artefacts"] = (
		required.get("legitimate_later_artefacts") or []
	)[:DIFF_LIMIT]
	out["eod_proof_problems"] = _eod_proof_problems(case, state)
	out["writes_apply_would_make"] = _planned_writes(case, data)
	out["post_verify_blockers"] = _other_open_operations(case)
	clean = not (
		out["manifest_problems"]
		or out["required_absent_problems"]
		or out["eod_proof_problems"]
		or out["post_verify_blockers"]
	)
	if verdict == "already_applied":
		out["verdict"] = "already_applied"
	elif verdict == "matches_expected" and clean:
		out["verdict"] = "would_apply"
	else:
		out["verdict"] = "blocked"
	return out


def _planned_writes(case, data):
	act = case["actions"]
	return {
		"cancel": f"Employee IR {act['cancel_employee_ir']} (supported cancel, reviewed-repair flag)",
		"mop_logs_set_is_cancelled": act.get("mop_logs_to_cancel") or [],
		"time_logs_deleted": act.get("time_logs_to_delete") or {},
		"operations_restored": act.get("restore") or {},
		"weights_recomputed": sorted(act.get("restore") or {}),
		"draft_stock_entries_deleted": sorted(
			{
				f["stock_entry"]
				for f in (case.get("required_absent") or {}).get(
					"draft_unresolved_eod_stock_entries", []
				)
				if f.get("stock_entry")
				in set(data.get("delete_draft_stock_entries") or [])
			}
		),
		"comments_on": [
			f"Employee IR {case['employee_ir']}",
			*(f"Manufacturing Operation {m}" for m in sorted(act.get("restore") or {})),
			*(
				f"Manufacturing Work Order {w}"
				for w in sorted(case["expected"]["work_orders"])
			),
		],
	}


# ---------------------------------------------------------------------------------------------
# rehearse / apply
# ---------------------------------------------------------------------------------------------


class _NoNestedCommit:
	"""One transaction per case: any commit attempted inside the case body is an error.

	Patches the Database OBJECT (not the proxy) and restores exactly what was there before,
	including a test's own ``patch.object(frappe.db, "commit", ...)``.
	"""

	def __enter__(self):
		self._db = frappe.local.db
		self._had_own = "commit" in vars(self._db)
		self._own = vars(self._db).get("commit")

		def refuse(*args, **kwargs):
			raise RepairAborted(
				"a nested commit was attempted inside the repair transaction"
			)

		self._db.commit = refuse
		return self

	def __exit__(self, *exc):
		if self._had_own:
			self._db.commit = self._own
		else:
			del self._db.commit
		return False


def _write(data, mode, ticket):
	report = _report_header(data, mode, ticket)
	guards = _global_guards(data, mode, ticket)
	report["guards"] = guards
	if guards["problems"]:
		report["status"] = "refused"
		return report
	if cint(frappe.db.transaction_writes):
		report["status"] = "refused"
		guards["problems"].append("the caller's transaction has pending writes")
		return report
	results = []
	for case in data["cases"]:
		result = _write_case(data, case, mode, ticket)
		results.append(result)
		if mode == "apply" and result["status"] not in ("applied", "already_applied"):
			break
	report["cases"] = results
	ok = (
		("applied", "already_applied")
		if mode == "apply"
		else ("rehearsed", "already_applied")
	)
	report["status"] = (
		"ok"
		if all(r["status"] in ok for r in results)
		and len(results) == len(data["cases"])
		else "stopped"
	)
	return report


def _write_case(data, case, mode, ticket):
	name = case["employee_ir"]
	out = {"employee_ir": name, "mode": mode, "case_sha256": case.get("case_sha256")}
	problems = _case_problems(data, case)
	if problems:
		out.update(status="refused", problems=problems)
		return out
	if _eod_locked():
		out.update(status="refused", problems=["EOD sync lock is held"])
		return out

	frappe.db.rollback()  # a fresh transaction: its first reads come after the locks below
	frappe.flags[IN_PROGRESS_FLAG] = True
	try:
		with _NoNestedCommit():
			_lock_case(case)
			state = co_read_case_state(case)
			verdict, expected_diff, applied_diff = _classify_state(case, state)
			if verdict == "already_applied":
				raise _AlreadyApplied()
			if verdict != "matches_expected":
				raise RepairAborted(
					f"current state is '{verdict}', not the reviewed one",
					{
						"diff_vs_expected": expected_diff[:DIFF_LIMIT],
						"diff_vs_repaired": applied_diff[:DIFF_LIMIT],
					},
				)
			# The builder, run again now under the case's locks, must reach exactly the reviewed
			# case: same state, same verdicts (status, blockers, needs_review), same EOD proof.
			rebuilt = co_build_stale_issue_case(name)
			if rebuilt.get("case_sha256") != case.get("case_sha256"):
				raise RepairAborted(
					"the case built now, under the locks, differs from the reviewed one; rebuild "
					"and re-review the manifest",
					_sealed_diff(case, rebuilt),
				)
			found = co_required_absent(case["required_absent_spec"])
			required = _required_problems(found, data.get("delete_draft_stock_entries"))
			if required:
				raise RepairAborted("required-absent check failed", required)
			eod = _eod_proof_problems(case, state)
			if eod:
				raise RepairAborted("EOD Sync Log proof check failed", eod)

			_cancel_issue(name)
			deleted_rows = _restore_operations(case, state)
			_recompute_weights(case)
			dropped = _delete_listed_drafts(found, data)
			after = co_read_case_state(case)
			post = _post_verify(case, after)
			if post:
				raise RepairAborted("post-verify failed", post)
			_add_comments(case, ticket, mode, state, after, deleted_rows, dropped)
		if mode == "apply":
			frappe.db.commit()
			out["status"] = "applied"
		else:
			frappe.db.rollback()
			out["status"] = "rehearsed"
		out["restored"] = {
			mop: {f: after["operations"].get(mop, {}).get(f) for f in target}
			for mop, target in case["actions"]["restore"].items()
		}
		out["time_logs_deleted"] = case["actions"].get("time_logs_to_delete") or {}
		out["mop_logs_cancelled"] = case["actions"].get("mop_logs_to_cancel") or []
		out["draft_stock_entries_deleted"] = dropped
	except _AlreadyApplied:
		frappe.db.rollback()
		out["status"] = "already_applied"
	except RepairAborted as exc:
		frappe.db.rollback()
		out.update(status="aborted", reason=exc.reason, details=exc.details)
	except Exception as exc:
		frappe.db.rollback()
		out.update(
			status="failed",
			reason=f"{type(exc).__name__}: {exc}",
			traceback=traceback.format_exc()[-4000:],
		)
	finally:
		frappe.flags[IN_PROGRESS_FLAG] = False
	return out


class _AlreadyApplied(Exception):
	pass


def _lock_case(case):
	"""Employee IR (NOWAIT) -> operations -> work orders -> the voucher's MOP Log rows.

	Primary-key FOR UPDATE reads only (record locks, no gap locks). The Employee IR goes first
	because it is this repair's parent control row; operations then work orders is RULE E, the
	order the cancel guard itself takes; the MOP Log rows come last, as in the cancel's own
	primary-key ``UPDATE ... SET is_cancelled = 1``. All the case's rows share ONE wait budget of
	LOCK_WAIT_SECONDS, not one per row.
	"""
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_manufacturing_operations,
		lock_work_orders,
	)

	exp = case["expected"]
	deadline = time.monotonic() + LOCK_WAIT_SECONDS

	def wait():
		"""Seconds left of the case's budget (at least 1); 0 = NOWAIT once it is spent."""
		remaining = deadline - time.monotonic()
		return max(1, math.ceil(remaining)) if remaining > 0 else 0

	try:
		frappe.db.sql(
			"SELECT name FROM `tabEmployee IR` WHERE name = %s FOR UPDATE NOWAIT",
			(case["employee_ir"],),
		)
		lock_manufacturing_operations(sorted(exp.get("operations") or {}), wait=wait)
		lock_work_orders(sorted(exp.get("work_orders") or {}), wait=wait)
		for log in sorted(exp.get("mop_logs") or {}):
			left = wait()
			frappe.db.sql(
				"SELECT name FROM `tabMOP Log` WHERE name = %s FOR UPDATE "
				+ (f"WAIT {left}" if left else "NOWAIT"),
				(log,),
			)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		raise RepairAborted(
			"rows are locked by another transaction (busy); retry in a minute",
			repr(exc),
		) from exc


def _cancel_issue(name):
	"""The supported cancel, with the reviewed-repair flag for exactly this document."""
	previous = frappe.flags.get(REVIEWED_REPAIR_FLAG)
	frappe.flags[REVIEWED_REPAIR_FLAG] = set(previous or ()) | {("Employee IR", name)}
	try:
		doc = frappe.get_doc("Employee IR", name)
		doc.flags.ignore_permissions = True
		doc.cancel()
	finally:
		frappe.flags[REVIEWED_REPAIR_FLAG] = previous


def _db_value(meta, field, value):
	df = meta.get_field(field)
	if value is None or not df:
		return value
	if df.fieldtype in ("Datetime", "Date", "Time"):
		return get_datetime(value) if df.fieldtype == "Datetime" else value
	if df.fieldtype in ("Float", "Currency", "Percent"):
		return flt(value)
	if df.fieldtype in ("Int", "Check"):
		return cint(value)
	return value


def _restore_operations(case, before):
	"""Put each operation back to the reviewed targets, through its document.

	Runs AFTER the cancel, whose Issue branch forced Not Started and cleared operation /
	employee / start_time. ``flags.ignore_validation`` is the internal-writer path of
	ManufacturingOperation.validate; its only remaining work, ``set_start_finish_time``, is
	switched off for this save because the reviewed targets ARE the start / finish values (it
	would otherwise copy the last time log's to_time into finish_time). The removed time-log rows
	are dropped from the document, so the save deletes exactly them. The Employee Issue cancel
	itself already deletes the Issue's own open time log with a plain DELETE (no Version); the
	repair Comment records every deleted row either way.

	``before`` is the case state read under the case's locks before the cancel. Every listed
	time log must be in it (otherwise the data drifted after review). The cancel may already have
	deleted the Issue's own open time log; such a row counts as deleted, not as missing.
	"""
	act = case["actions"]
	meta = frappe.get_meta("Manufacturing Operation")
	deleted = {}
	for mop in sorted(act.get("restore") or {}):
		target = act["restore"][mop]
		drop = set((act.get("time_logs_to_delete") or {}).get(mop) or [])
		listed = {
			row["name"]: row
			for row in (before.get("time_logs") or {}).get(mop) or []
			if row["name"] in drop
		}
		missing = drop - set(listed)
		if missing:
			raise RepairAborted(
				f"{mop}: time log(s) {', '.join(sorted(missing))} vanished before the cancel"
			)
		deleted[mop] = [
			{
				k: listed[n].get(k)
				for k in ("name", "idx", "from_time", "to_time", "employee", "owner")
			}
			for n in sorted(listed, key=lambda n: cint(listed[n].get("idx")))
		]
		doc = frappe.get_doc("Manufacturing Operation", mop)
		for field, value in target.items():
			doc.set(field, _db_value(meta, field, value))
		if drop:
			doc.set("time_logs", [row for row in doc.time_logs if row.name not in drop])
		doc.flags.ignore_validation = True
		doc.flags.ignore_permissions = True
		doc.flags.ignore_links = True
		doc.flags.ignore_mandatory = True
		doc.set_start_finish_time = lambda: None
		doc.save(ignore_version=False)
		gone = _co_select_in(
			"SELECT name FROM `tabManufacturing Operation Time Log` WHERE name IN %(names)s",
			"names",
			drop,
			pluck=True,
		)
		if gone:
			raise RepairAborted(
				f"{mop}: time log(s) {', '.join(gone)} were not deleted"
			)
	return deleted


def _recompute_weights(case):
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		recalculate_manufacturing_operation_weights,
	)

	for mop in sorted(case["actions"].get("restore") or {}):
		recalculate_manufacturing_operation_weights(mop)


def _delete_listed_drafts(required, data):
	"""Delete the draft 'MOP EOD Sync (Unresolved)' Stock Entries the reviewer listed -- only
	when every one of their rows belongs to this case's work orders / operations. ``required``
	is the case's ``co_required_absent`` result, read under the case's locks."""
	listed = set(data.get("delete_draft_stock_entries") or [])
	found = {
		f["stock_entry"]: f
		for f in required.get("draft_unresolved_eod_stock_entries", [])
		if f.get("stock_entry") in listed
	}
	deleted = []
	for name, entry in sorted(found.items()):
		doc = frappe.get_doc("Stock Entry", name)
		if cint(doc.docstatus) != 0:
			raise RepairAborted(f"{name} is no longer a draft")
		if len(entry.get("matching_rows") or []) != cint(entry.get("rows")):
			raise RepairAborted(
				f"{name} also carries rows of other work orders; remove this case's rows by hand"
			)
		frappe.delete_doc("Stock Entry", name, ignore_permissions=True)
		deleted.append(name)
	return deleted


def _post_verify(case, after):
	"""What must hold before the commit; returns the problems (empty = commit)."""
	from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
		OPEN_STATUSES,
	)

	problems = list(co_compare_applied(case, after, include_pointers=True))
	mwos = sorted(case["expected"]["work_orders"])
	pointers = {
		mwo: row.get("manufacturing_operation")
		for mwo, row in (after.get("work_orders") or {}).items()
	}
	open_rows = _co_select_in(
		"""
		SELECT name, manufacturing_work_order AS mwo, status FROM `tabManufacturing Operation`
		WHERE manufacturing_work_order IN %(mwos)s AND status IN %(open)s
			AND IFNULL(department_ir_status, '') != 'Revert'
		""",
		"mwos",
		mwos,
		{"open": tuple(OPEN_STATUSES)},
	)
	for mwo in mwos:
		names = sorted(r.name for r in open_rows if r.mwo == mwo)
		if any(n != pointers.get(mwo) for n in names):
			problems.append(
				{
					"path": f"work_orders.{mwo}.open_operations",
					"expected": [pointers.get(mwo)] if names else [],
					"actual": names,
				}
			)
	totals = co_voucher_log_totals(case["employee_ir"])
	if totals["active"]:
		problems.append(
			{
				"path": "voucher_mop_logs.active",
				"expected": 0,
				"actual": totals["active"],
			}
		)
	audit = audit_current_operation_conflicts(mwos=mwos, limit=50)
	stale = [
		r["manufacturing_operation"]
		for r in audit["open_non_pointer"]["rows"]
		if r["manufacturing_operation"] in case["actions"]["restore"]
	]
	if stale:
		problems.append(
			{"path": "audit.open_non_pointer", "expected": [], "actual": stale}
		)
	open_logs = [
		r["time_log"]
		for r in audit["open_time_log_fingerprint"]["rows"]
		if r["manufacturing_operation"] in case["actions"]["restore"]
	]
	if open_logs:
		problems.append(
			{
				"path": "audit.open_time_log_fingerprint",
				"expected": [],
				"actual": open_logs,
			}
		)
	held = [
		r["mop_log"]
		for r in audit["eod_ambiguity"]["rows"]
		if r["manufacturing_operation"] in case["actions"]["restore"]
	]
	if held:
		problems.append({"path": "audit.eod_ambiguity", "expected": [], "actual": held})
	return problems


def _other_open_operations(case):
	"""Open, non-current operations of the case's work orders that this case does NOT close.

	The post-verify requires exactly one open operation per work order (the pointer), so these
	would make it fail; plan reports them up front.
	"""
	from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
		OPEN_STATUSES,
	)

	pointers = {
		mwo: row.get("manufacturing_operation")
		for mwo, row in case["expected"]["work_orders"].items()
	}
	rows = _co_select_in(
		"""
		SELECT name, manufacturing_work_order AS mwo FROM `tabManufacturing Operation`
		WHERE manufacturing_work_order IN %(mwos)s AND status IN %(open)s
			AND IFNULL(department_ir_status, '') != 'Revert'
		""",
		"mwos",
		sorted(pointers),
		{"open": tuple(OPEN_STATUSES)},
	)
	closing = set(case["actions"].get("restore") or {})
	return sorted(
		f"{r.mwo}: {r.name}"
		for r in rows
		if r.name != pointers.get(r.mwo) and r.name not in closing
	)


def _add_comments(case, ticket, mode, before, after, deleted_rows, dropped):
	sha = cstr(case.get("case_sha256"))[:12]
	header = (
		f"<b>Reviewed current-operation repair</b> ({escape_html(mode)}, ticket "
		f"{escape_html(cstr(ticket))}, manifest case {escape_html(sha)})"
	)

	def comment(doctype, name, payload):
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"comment_email": frappe.session.user,
				"reference_doctype": doctype,
				"reference_name": name,
				"content": header
				+ "<pre>"
				+ escape_html(json.dumps(payload, indent=1, default=str))
				+ "</pre>",
			}
		).insert(ignore_permissions=True)

	name = case["employee_ir"]
	comment(
		"Employee IR",
		name,
		{
			"before": {"docstatus": (before.get("employee_ir") or {}).get("docstatus")},
			"after": {"docstatus": (after.get("employee_ir") or {}).get("docstatus")},
			"mop_logs_cancelled": case["actions"].get("mop_logs_to_cancel") or [],
			"draft_stock_entries_deleted": dropped,
			"why": "stale Employee Issue (family C): it re-issued operations its work orders had "
			"already moved past; cancelled through the supported path under review.",
		},
	)
	for mop, target in sorted((case["actions"].get("restore") or {}).items()):
		comment(
			"Manufacturing Operation",
			mop,
			{
				"before": {
					f: (before["operations"].get(mop) or {}).get(f) for f in target
				},
				"after": {
					f: (after["operations"].get(mop) or {}).get(f) for f in target
				},
				"time_logs_deleted": deleted_rows.get(mop) or [],
				"evidence": (case.get("evidence") or {}).get(mop),
			},
		)
	for mwo, row in sorted(case["expected"]["work_orders"].items()):
		comment(
			"Manufacturing Work Order",
			mwo,
			{
				"current_operation": (after["work_orders"].get(mwo) or {}).get(
					"manufacturing_operation"
				),
				"unchanged": True,
				"closed_stale_operations": sorted(
					m
					for m, op in case["expected"]["operations"].items()
					if op.get("manufacturing_work_order") == mwo
				),
			},
		)
