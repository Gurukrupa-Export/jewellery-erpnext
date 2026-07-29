# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Read-only integrity audit for the Tree Number material ledger.

Reports trees whose ``material_details`` violate the ledger invariants -- most importantly
metal recorded as received against a tree that was never issued, which is how
``GEPL-TR-26-00154`` reached ``issue 0 / receive 2.36`` at status "Received".

**This utility never writes.** It has no repair mode and is deliberately not wired into any
patch. Setting ``issue_qty = receive_qty`` to make the arithmetic balance would fabricate a
material issue that never happened -- on a precious-metal ledger that is worse than the
inconsistency it hides. Trees without issue evidence are classified for manual
reconciliation and left alone.

Usage::

    bench --site <site> execute jewellery_erpnext.tree_audit.run_tree_audit
    bench --site <site> execute jewellery_erpnext.tree_audit.run_tree_audit \\
        --kwargs "{'company': 'Gurukrupa Export Private Limited', 'limit': 50}"

or from the API / console::

    frappe.call("jewellery_erpnext.tree_audit.run_tree_audit", from_date="2026-07-01")
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)

# Classification of what could safely be done about a tree, if anyone ever chose to act.
SAFE_TO_RECALCULATE_PENDING = "SAFE_TO_RECALCULATE_PENDING"
SAFE_TO_REBUILD_FROM_STOCK_ENTRY = "SAFE_TO_REBUILD_FROM_STOCK_ENTRY"
MISSING_ISSUE_EVIDENCE = "MISSING_ISSUE_EVIDENCE"
AMBIGUOUS_MOVEMENT = "AMBIGUOUS_MOVEMENT"
MANUAL_RECONCILIATION_REQUIRED = "MANUAL_RECONCILIATION_REQUIRED"

DEFAULT_LIMIT = 500


def _issue_evidence(tree_names):
	"""``{tree: qty}`` of metal proven issued by submitted, tree-stamped Stock Entries.

	The tree "Issue Material" button stamps ``Stock Entry.custom_tree_number`` and moves
	Dept RM -> MSL. A submitted entry is the only authoritative proof that metal was issued;
	the ledger column alone is not (it is what we are auditing).
	"""
	if not tree_names:
		return {}
	rows = frappe.get_all(
		"Stock Entry",
		filters={
			"custom_tree_number": ["in", list(tree_names)],
			"docstatus": 1,
			"auto_created": 1,
		},
		fields=["name", "custom_tree_number"],
	)
	if not rows:
		return {}

	by_se = {r.name: r.custom_tree_number for r in rows}
	details = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", list(by_se)]},
		fields=["parent", "qty", "s_warehouse", "t_warehouse"],
	)
	evidence = {}
	for d in details:
		# Issue leg only: metal moving INTO the tree's MSL warehouse. The receive/loss legs of
		# the same tree flow the other way and must not count as issue evidence.
		if not d.t_warehouse:
			continue
		tree = by_se.get(d.parent)
		if tree:
			evidence.setdefault(tree, 0.0)
			evidence[tree] += flt(d.qty)
	return evidence


def _classify(issues, has_issue_qty, has_issue_evidence, pending_only):
	"""Pick the single most conservative classification for a tree."""
	if not issues:
		return None
	if pending_only:
		# Nothing but a stale pending_qty: re-deriving it from the stored quantities invents
		# nothing.
		return SAFE_TO_RECALCULATE_PENDING
	if not has_issue_qty:
		if has_issue_evidence:
			# A submitted issue Stock Entry exists but the ledger column is empty -- the ledger
			# could be rebuilt from the movement, not guessed.
			return SAFE_TO_REBUILD_FROM_STOCK_ENTRY
		return MISSING_ISSUE_EVIDENCE
	return MANUAL_RECONCILIATION_REQUIRED


@frappe.whitelist()
def run_tree_audit(
	dry_run=True,
	company=None,
	tree_number=None,
	from_date=None,
	to_date=None,
	limit=DEFAULT_LIMIT,
	verbose=True,
):
	"""Audit Tree Number material ledgers. Always read-only.

	``dry_run`` exists only so the signature reads honestly and a caller cannot *assume* a
	repair mode is available: passing ``dry_run=False`` raises rather than writing anything.
	"""
	frappe.only_for("System Manager")

	if not cint(dry_run):
		frappe.throw(
			_(
				"The Tree Number audit is read-only and has no repair mode. Fabricating an "
				"issue quantity to balance a ledger would invent a material movement that never "
				"happened; trees are classified for manual reconciliation instead."
			),
			title=_("Audit Is Read-Only"),
		)

	filters = {}
	if company:
		filters["company"] = company
	if tree_number:
		filters["name"] = tree_number
	if from_date and to_date:
		filters["posting_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["posting_date"] = [">=", from_date]
	elif to_date:
		filters["posting_date"] = ["<=", to_date]

	limit = cint(limit) or DEFAULT_LIMIT
	trees = frappe.get_all(
		"Tree Number",
		filters=filters,
		fields=["name", "status", "company", "employee", "department", "employee_ir"],
		order_by="name",
		limit_page_length=limit,
	)
	if not trees:
		return _empty_result(limit)

	tree_names = [t.name for t in trees]
	rows_by_tree = {}
	for row in frappe.get_all(
		"Tree Material Detail",
		filters={"parent": ["in", tree_names], "parenttype": "Tree Number"},
		fields=[
			"name",
			"parent",
			"idx",
			"item_code",
			"issue_qty",
			"receive_qty",
			"loss_qty",
			"pending_qty",
		],
		order_by="parent, idx",
	):
		rows_by_tree.setdefault(row.parent, []).append(row)

	evidence = _issue_evidence({t.name for t in trees if rows_by_tree.get(t.name)})
	live_eirs = _live_employee_irs({t.employee_ir for t in trees if t.employee_ir})

	eps = tree_balance.pending_eps()
	prec = tree_balance.qty_precision()

	findings = []
	counts = _new_counts()
	classifications = {}

	for tree in trees:
		rows = rows_by_tree.get(tree.name) or []
		if not rows:
			# Bare Main Slip-created trees carry no ledger at all -- out of scope, not a finding.
			counts["trees_without_material_rows"] += 1
			continue

		counts["trees_scanned"] += 1
		issues = []
		pending_only = True

		total_issue = sum(flt(r.issue_qty, prec) for r in rows)
		total_moved = sum(
			flt(r.receive_qty, prec) + flt(r.loss_qty, prec) for r in rows
		)

		seen_items = set()
		for row in rows:
			issues.extend(_row_issues(row, prec, eps, counts))
			if row.item_code in seen_items:
				issues.append(f"duplicate ledger row for item {row.item_code}")
				counts["duplicate_item_rows"] += 1
				pending_only = False
			seen_items.add(row.item_code)

		if any(not i.startswith("pending mismatch") for i in issues):
			pending_only = False

		issues.extend(_status_issues(tree, total_issue, total_moved, eps, counts))

		if tree.employee_ir and tree.employee_ir not in live_eirs:
			issues.append(
				f"linked Employee IR {tree.employee_ir} is missing or not submitted"
			)
			counts["orphaned_employee_ir"] += 1
			pending_only = False

		if not issues:
			continue

		counts["trees_with_findings"] += 1
		classification = _classify(
			issues,
			has_issue_qty=total_issue > eps,
			has_issue_evidence=flt(evidence.get(tree.name)) > eps,
			pending_only=pending_only,
		)
		classifications[classification] = classifications.get(classification, 0) + 1
		findings.append(
			{
				"tree_number": tree.name,
				"status": tree.status,
				"company": tree.company,
				"department": tree.department,
				"employee_ir": tree.employee_ir,
				"total_issue_qty": flt(total_issue, prec),
				"issue_evidence_qty": flt(evidence.get(tree.name), prec),
				"classification": classification,
				"issues": issues,
			}
		)

	result = {
		"dry_run": True,
		"writes_performed": 0,
		"limit": limit,
		"counts": counts,
		"classifications": classifications,
		"findings": findings,
	}
	if cint(verbose):
		print(_format_report(result))
	return result


def _new_counts():
	return {
		"trees_scanned": 0,
		"trees_without_material_rows": 0,
		"trees_with_findings": 0,
		"receive_without_issue": 0,
		"receive_plus_loss_over_issue": 0,
		"loss_over_issue": 0,
		"negative_qty": 0,
		"negative_pending": 0,
		"pending_mismatch": 0,
		"duplicate_item_rows": 0,
		"received_with_zero_issue": 0,
		"received_with_pending": 0,
		"issued_with_zero_issue": 0,
		"draft_with_movements": 0,
		"orphaned_employee_ir": 0,
	}


def _empty_result(limit):
	return {
		"dry_run": True,
		"writes_performed": 0,
		"limit": limit,
		"counts": _new_counts(),
		"classifications": {},
		"findings": [],
	}


def _live_employee_irs(names):
	if not names:
		return set()
	return set(
		frappe.get_all(
			"Employee IR",
			filters={"name": ["in", list(names)], "docstatus": 1},
			pluck="name",
		)
	)


def _row_issues(row, prec, eps, counts):
	"""Per-row invariant violations."""
	issues = []
	issue_qty = flt(row.issue_qty, prec)
	receive_qty = flt(row.receive_qty, prec)
	loss_qty = flt(row.loss_qty, prec)

	for field in tree_balance.QTY_FIELDS:
		if flt(row.get(field), prec) < -eps:
			issues.append(f"row #{row.idx} {row.item_code}: negative {field}")
			counts["negative_qty"] += 1

	if issue_qty <= eps and receive_qty > eps:
		issues.append(
			f"row #{row.idx} {row.item_code}: received {receive_qty} with nothing issued"
		)
		counts["receive_without_issue"] += 1

	if loss_qty - issue_qty > eps:
		issues.append(
			f"row #{row.idx} {row.item_code}: loss {loss_qty} exceeds issue {issue_qty}"
		)
		counts["loss_over_issue"] += 1

	if (receive_qty + loss_qty) - issue_qty > eps:
		issues.append(
			f"row #{row.idx} {row.item_code}: receive+loss {flt(receive_qty + loss_qty, prec)} "
			f"exceeds issue {issue_qty}"
		)
		counts["receive_plus_loss_over_issue"] += 1

	expected = tree_balance.calculate_pending(issue_qty, receive_qty, loss_qty, prec)
	if abs(flt(row.pending_qty, prec) - expected) > eps:
		issues.append(
			f"pending mismatch on row #{row.idx} {row.item_code}: "
			f"stored {flt(row.pending_qty, prec)}, expected {expected}"
		)
		counts["pending_mismatch"] += 1

	if flt(row.pending_qty, prec) < -eps:
		counts["negative_pending"] += 1

	return issues


def _status_issues(tree, total_issue, total_moved, eps, counts):
	"""Status values the ledger cannot support."""
	issues = []
	if tree.status == tree_balance.STATUS_RECEIVED and total_issue <= eps:
		issues.append("status 'Received' but nothing was ever issued")
		counts["received_with_zero_issue"] += 1
	if tree.status == tree_balance.STATUS_ISSUED and total_issue <= eps:
		issues.append("status 'Issued' but nothing was ever issued")
		counts["issued_with_zero_issue"] += 1
	if tree.status == tree_balance.STATUS_DRAFT and (
		total_issue > eps or total_moved > eps
	):
		issues.append("status 'Draft' but the ledger already has movements")
		counts["draft_with_movements"] += 1
	if (
		tree.status == tree_balance.STATUS_RECEIVED
		and (total_issue - total_moved) > eps
	):
		issues.append(
			f"status 'Received' but {flt(total_issue - total_moved, 3)} is still pending"
		)
		counts["received_with_pending"] += 1
	return issues


def _format_report(result):
	lines = [
		"",
		"Tree Number material-ledger audit (read-only)",
		"=" * 60,
	]
	for key, value in result["counts"].items():
		lines.append(f"  {key.replace('_', ' '):<40} {value:>8}")
	lines.append("-" * 60)
	if result["classifications"]:
		for key, value in sorted(result["classifications"].items()):
			lines.append(f"  {key:<40} {value:>8}")
		lines.append("-" * 60)
	lines.append(f"  {'writes performed':<40} {result['writes_performed']:>8}")
	lines.append("")

	findings = result["findings"]
	if findings:
		lines.append(
			f"First {min(len(findings), 20)} of {len(findings)} affected trees:"
		)
		for f in findings[:20]:
			lines.append(
				f"  {f['tree_number']} [{f['status']}] -> {f['classification']}"
			)
			for issue in f["issues"][:4]:
				lines.append(f"      - {issue}")
	lines.append("")
	return "\n".join(lines)
