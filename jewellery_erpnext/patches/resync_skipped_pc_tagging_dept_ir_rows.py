"""Replay the PC<->Tagging stock sync for Department IR rows that were silently skipped.

``pc_tagging_stock_sync._process_row`` creates one Stock Entry per Department IR child
row, but its duplicate-SE guard was scoped to the whole Department IR::

    existing_se = frappe.db.get_all(
        "Stock Entry", filters={"department_ir": dept_ir_doc.name, "docstatus": 1}, ...)
    if existing_se:
        frappe.log_error(...)   # log only -- the user saw nothing
        return

So on a multi-row Department IR row 1 created and submitted its Stock Entry, and every
later row matched row 1's entry and returned without moving any stock. The MOP Logs were
still written for those rows, so the logical ledger claimed the material had moved to the
next department while it was physically still sitting in the current one -- and the
follow-on Receive then failed the physical-stock fail-fast in ``_process_row``.

Measured on the kg-gk site before the fix: ``Department-IR-Labh-2026-00185`` (Issue,
Product Certification -> Tagging, 2 rows) produced only ``MAT-STE-00721``, covering row
idx1 (``MOP-75ZO8``). Row idx2 (``MOP-6P9U8``) kept 3.234 g of M-G-22KT-91.9-Y plus 0.36
and 0.248 ct of diamonds parked in ``Product Certification WO - KGJPL`` with no active
reservation, which blocked ``Department-IR-Labh-2026-00186``.

The guard is now row-scoped (keyed on ``Stock Entry Detail.manufacturing_operation``,
which is unique per Department IR row on both legs), so re-running the sync on an affected
document is a no-op for the rows already covered and completes the ones that were skipped.
This patch finds those documents and does exactly that.

Run standalone with a report first::

    bench --site <site> execute \
        jewellery_erpnext.patches.resync_skipped_pc_tagging_dept_ir_rows.execute \
        --kwargs "{'dry_run': True}"
"""

import frappe

# Department name prefixes that gate the PC<->Tagging scenarios in
# pc_tagging_stock_sync._resolve_scenario. Matched with LIKE because the stored value
# carries the company abbreviation suffix (e.g. "Product Certification - KGJPL").
_PC_LIKE = "Product Certification%"
_TAGGING_LIKE = "Tagging%"


def execute(dry_run=False):
	"""Re-run the PC<->Tagging sync for rows the Department-IR-wide guard skipped."""
	if not frappe.db.has_column("Stock Entry", "department_ir"):
		# Site never ran the Department IR stock flow; nothing to repair.
		return

	affected = _affected_department_irs()
	if not affected:
		frappe.log_error(
			"resync_skipped_pc_tagging_dept_ir_rows: no Department IR with uncovered "
			"MOP Log operations found. Nothing to do.",
			"PC Tagging Row Resync",
		)
		return

	repaired, failed = [], []

	for dept_ir_name, missing_mops in affected:
		if dry_run:
			repaired.append((dept_ir_name, sorted(missing_mops), "dry-run"))
			continue

		try:
			_resync(dept_ir_name)
		except Exception:
			# A row whose material has since moved on will hit the physical-stock
			# fail-fast. That is a genuine reconcile-first situation for a human, not a
			# reason to abort `bench migrate` for every other site and every other patch.
			frappe.db.rollback()
			failed.append((dept_ir_name, sorted(missing_mops)))
			frappe.log_error(
				frappe.get_traceback(),
				"PC Tagging Row Resync failed: {0}".format(dept_ir_name),
			)
			continue

		frappe.db.commit()  # nosemgrep - each document is repaired independently
		repaired.append((dept_ir_name, sorted(missing_mops), "resynced"))

	_report(repaired, failed, dry_run)
	return {"repaired": repaired, "failed": failed}


def _affected_department_irs():
	"""[(department_ir, {uncovered manufacturing_operation, ...})] for multi-row IRs.

	A row is "uncovered" when its MOP Logs exist but no submitted Stock Entry of the same
	Department IR carries a line for that operation -- exactly the footprint the skipped
	rows leave behind.
	"""
	candidates = frappe.db.sql(
		"""
		SELECT d.name
		FROM `tabDepartment IR` d
		JOIN `tabDepartment IR Operation` o ON o.parent = d.name
		WHERE d.docstatus = 1
		  AND (
		        (d.type = 'Issue'
		         AND d.current_department LIKE %(pc)s
		         AND d.next_department LIKE %(tagging)s)
		     OR (d.type = 'Receive'
		         AND d.previous_department LIKE %(pc)s
		         AND d.current_department LIKE %(tagging)s)
		  )
		GROUP BY d.name
		HAVING COUNT(o.name) > 1
		ORDER BY d.creation
		""",
		{"pc": _PC_LIKE, "tagging": _TAGGING_LIKE},
		pluck=True,
	)

	affected = []
	for dept_ir_name in candidates:
		log_mops = set(
			frappe.db.sql_list(
				"""
			SELECT DISTINCT manufacturing_operation FROM `tabMOP Log`
			WHERE voucher_type = 'Department IR' AND voucher_no = %s
			  AND is_cancelled = 0 AND manufacturing_operation IS NOT NULL
			""",
				(dept_ir_name,),
			)
		)
		if not log_mops:
			continue

		se_names = frappe.db.get_all(
			"Stock Entry",
			filters={"department_ir": dept_ir_name, "docstatus": 1},
			pluck="name",
		)
		if not se_names:
			# The sync never ran at all for this document -- a different failure mode
			# (validation throw, no reservations). Out of scope: replaying it here would
			# post stock for a document nobody has looked at.
			continue

		covered = set(
			frappe.db.get_all(
				"Stock Entry Detail",
				filters={"parent": ["in", se_names], "parenttype": "Stock Entry"},
				pluck="manufacturing_operation",
			)
		)
		missing = log_mops - covered
		if missing:
			affected.append((dept_ir_name, missing))

	return affected


def _resync(dept_ir_name):
	"""Re-run the sync for one Department IR; covered rows no-op via the row guard."""
	from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync import (
		process_pc_tagging_stock_sync,
	)

	process_pc_tagging_stock_sync(frappe.get_doc("Department IR", dept_ir_name))


def _report(repaired, failed, dry_run):
	lines = [
		"resync_skipped_pc_tagging_dept_ir_rows ({0})".format(
			"dry run" if dry_run else "applied"
		),
		"repaired: {0}".format(len(repaired)),
	]
	for name, mops, outcome in repaired:
		lines.append("  {0} [{1}] {2}".format(name, ", ".join(mops), outcome))
	lines.append("failed: {0}".format(len(failed)))
	for name, mops in failed:
		lines.append("  {0} [{1}] see its own Error Log".format(name, ", ".join(mops)))

	message = "\n".join(lines)
	print(message)  # noqa: T201 -- surfaces in the bench migrate / execute output
	frappe.log_error(message, "PC Tagging Row Resync")
