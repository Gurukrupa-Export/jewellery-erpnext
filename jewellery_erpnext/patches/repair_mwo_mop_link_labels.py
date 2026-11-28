"""Repair Patch 1 (generic): put each record's work order back next to its operation.

THE DEFECT
----------
A Manufacturing Operation (MOP) belongs to one Manufacturing Work Order (MWO): its own
``manufacturing_work_order``, never rewritten after creation. ``Employee IR Operation`` stored the work
order as an editable ``fetch_if_empty`` cell that nothing checked, so a receive row could pair an
operation with a SIBLING's (or an unrelated) work order. On submit, everything keyed by work order
copied the row's label -- the Stock Entry header and rows, the MOP Log stamps, the work order's
current-operation pointer, the repeat-receive flag, the casting tree -- while everything keyed by
operation followed the operation. Split work orders also inherited their parent's pointer. The code
fix (``doc_events/mwo_mop_link.py`` and its callers) stops new cases; this patch relabels the old ones.

WHAT IT CHANGES -- label repairs that hold whichever sibling physically holds the metal
-------------------------------------------------------------------------------------
The operation is the anchor (it carries the ``previous_mop`` chain, the successors and the ledger),
so every label is re-derived FROM the operation, never the other way round.

1. IR rows (Employee IR / Department IR, parent not cancelled): row work order := the operation's
   owner, with the row fields fetched from it (DIR ``parent_manufacturing_order``, EIR
   ``is_finding_mwo``). A relabelled casting Receive row left without a tree gets the tree its
   operation was issued on (the casting Issue row of the same operation); the header tree follows
   the rows. That row's receive charged NO tree when it was submitted, so the tree's ledger never
   recorded its weight -- and the row's tree is the one a cancel reverses. The receive must not be
   cancelled before the tree is corrected (``casting_tree_filled_never_charged`` lists the rows; the
   family ledger repair owns the correction).
2. Employee IR injection Stock Entries (``auto_created``, ``custom_eir_operation_row``, draft or
   submitted) whose header operation belongs to another work order: header work order, order (PMO)
   and, when blank, ``custom_tree_number`` from the source row; row work orders := the owner.
3. MOP Log live rows whose stamp is not their operation's owner: stamp := owner (one Comment per
   operation). Rows older than their operation (a re-issued short name) and rows of a purged
   operation are NOT touched -- relabelling them would hand someone else's metal to this one -- nor
   are the rows of a Stock Entry this patch reports without repairing (a manual entry, say): which
   work order holds that metal is the decision the entry is waiting for.
4. Pointers: (a) a draft or discarded work order pointing at another work order's operation (or at
   one that no longer exists) loses the pointer (a discarded one becomes Cancelled, a draft one
   Draft); (b) a submitted, open work order still pointing at its own Finished operation that has an
   open successor moves to the newest open successor. A Revert operation (kggk_uat keeps the one a
   cancelled Department Issue / Employee Receive minted, ``department_ir_status = "Revert"``) is
   never an open successor: a pointer is never moved onto one, and a Finished pointer whose only
   successors are Revert is not stale.
5. Repeat-receive flags made true only by a relabelled row are cleared (``is_repeat_receive`` 0,
   ``worker_performance`` blank), using the Employee IR's own ``get_repeat_work_orders``. A flag is
   never set.

Classes 1 and 2 also leave alone -- and report -- a record whose operation is newer than the
document (the operation's short name was re-issued, or a row was added to an older draft: the owner
is not known to be the record's), and a record whose OLD label still keys an active reservation
(the receive's cancel finds the reservation by that label, so relabelling would strand it).

WHAT IT NEVER CHANGES (reported, see ``report_only``)
---------------------------------------------------
Manual Stock Entries and their MOP Log rows (their header operation may be the wrong half -- a
person must decide), cancelled documents of any kind (history; a properly cancelled work order keeps
its pointer), active reservations, Subcontracting Logs, re-issued-name and purged-operation MOP Log
rows, Process Loss entries (multi-work-order by design), submitted work orders pointing at another
work order's operation, quantities, stock, and the ledger balances (class 1 explains the one place a
label decides what a later cancel reverses). The family-specific ledger repair needs the floor's
per-family decision and ships separately.

KGGK_UAT
--------
Independent of ``patches/repair_current_operation_conflicts.py`` (the manifest-driven repair of the
2026-10-05 stale Employee Issues, run by hand, not from patches.txt): that one decides which
operation is CURRENT and repairs reopened operations; this one only re-derives labels from the
operation and moves a pointer solely in the class-4(b) shape -- own Finished pointer, open
successor -- which a stale Employee Issue does not leave (it reopens an OLDER operation; the pointer
stays on the open current one). Should a reviewed manifest still name a work order this patch
touched, that repair's own drift check refuses the case and the manifest is rebuilt. Relabelling an
Employee Issue DRAFT row also moves which work order the draft holds under the current-operation
guard's outstanding-draft rule: to the operation's owner, the work order the draft really acts on.

SAFETY
------
* State-driven: detection runs at execution time through ``mop_lineage_audit.mwo_mop_findings`` --
  the same detectors the audit reports -- never against a hard-coded list, so a second run finds
  nothing left to do and changes nothing.
* Refuses (``frappe.ValidationError``) while the EOD MOP Log sync holds its lock or a stock
  reconciliation window is open: the deploy is retried outside those windows.
* One savepoint per case. Each planned value is re-read with a row lock first; a value that changed
  since detection skips the case ("drifted"). A failing case rolls back alone, goes to Error Log and
  does not fail the migrate -- with two exceptions that fail it ON PURPOSE, so the next migrate simply
  runs everything again: an error that ends the whole transaction (a deadlock, a lost connection)
  takes every savepoint and every earlier case with it, so it is re-raised as it is; and a class-5
  case that fails or drifts undoes the whole run first, because its verdict exists only in the run
  whose relabels produced it -- a later run finds the rows already relabelled and would never see it.
* Writes use ``frappe.db.set_value(..., update_modified=False)`` and add an Info Comment (old -> new)
  on the parent document, because Version history does not cover these fields.
* No commit: ``bench migrate`` commits after the patch; ``bench execute`` commits after the call.

HOW TO RUN
----------
Registered in patches.txt, so ``bench migrate`` applies it once per site. Preview (never writes):

    bench --site <site> execute jewellery_erpnext.patches.repair_mwo_mop_link_labels.run

or, from System Console (System Manager)::

    frappe.call("jewellery_erpnext.patches.repair_mwo_mop_link_labels.preview")

Re-apply by hand (e.g. after a refused or partly drifted migrate):

    bench --site <site> execute jewellery_erpnext.patches.repair_mwo_mop_link_labels.run --kwargs "{'dry_run': False}"

Each run makes one full pass per detector (twice for an apply: before and after), seconds on a site
the size of kggk-prod but a minute or more on a ledger of millions of MOP Log rows. A web request
(System Console) can time out there; ``bench execute`` cannot.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, escape_html

from jewellery_erpnext import mop_lineage_audit as audit
from jewellery_erpnext.jewellery_erpnext import stock_recon_window
from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_mop_link import mop_owners, same
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
	single_tree_or_none,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	get_repeat_work_orders,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import eod_lock

REPAIR_TAG = "repair-mwo-mop-link"
LOG_PREFIX = f"[{REPAIR_TAG}]"
# How many names a report-only line prints before it says "... and N more".
_PRINT_NAMES = 12


class CaseDrift(Exception):
	"""A planned value changed between detection and write: the case is skipped, never forced."""


def execute():
	"""patches.txt entry point (``bench migrate``)."""
	run(dry_run=False)


@frappe.whitelist()
def preview():
	"""Dry run for System Console / desk: exactly what :func:`run` would change here. Never writes."""
	frappe.only_for("System Manager")
	return run(dry_run=True)


def run(dry_run=True):
	"""Detect every case and report it (``dry_run``) or repair it. Returns the report.

	Report keys: ``dry_run``, ``before`` / ``after`` (``{class: count}``, the audit's numbers),
	``cases`` (one per document: ``class``, ``doctype``, ``name``, ``writes`` and, after an apply,
	``outcome``), ``applied``, ``drifted``, ``failed`` and ``report_only``.
	"""
	refuse_inside_locked_windows()

	findings = audit.mwo_mop_findings()
	plan = build_plan(findings)
	report = frappe._dict(
		dry_run=bool(dry_run),
		before=audit.mwo_mop_counts(findings),
		after=None,
		cases=[],
		applied=0,
		drifted=[],
		failed=[],
		report_only=plan.report_only,
	)

	if dry_run:
		repeat_cases = preview_repeat_flag_cases(plan)
		report.cases = [_public(case) for case in plan.cases + repeat_cases]
		_print_report(report)
		return report

	# The whole apply sits in one savepoint, so that a class-5 failure can undo it (below).
	run_savepoint = f"mwo_mop_link_run_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(run_savepoint)
	applied_relabels = []
	for case in plan.cases:
		_record(report, case, apply_case(case))
		if case.outcome == "applied":
			applied_relabels.extend(case.relabels)
	# After every row relabel has landed, so each verdict sees the corrected labels of ALL rows.
	repeat_cases = repeat_flag_cases(applied_relabels)
	for case in repeat_cases:
		_record(report, case, apply_case(case))

	unapplied = [case for case in repeat_cases if case.outcome != "applied"]
	if unapplied:
		# A repeat-flag verdict exists only in the run whose relabels produced it: the next run finds
		# the rows already relabelled, plans no class-5 case, and the stale flag drops out of every
		# report. So the whole run is undone instead, and the next run starts from scratch.
		frappe.db.rollback(save_point=run_savepoint)
		_print_report(report)
		names = ", ".join(f"{case.name} ({case.outcome})" for case in unapplied)
		print(
			f"{LOG_PREFIX} ROLLED BACK: repeat-flag case(s) {names}; nothing from this run was kept."
		)
		frappe.throw(
			_(
				"The work order / operation label repair could not clear the repeat-receive flag of {0}, "
				"so the whole repair was rolled back (see Error Log and the migrate output). Run bench "
				"migrate again."
			).format(names),
			frappe.ValidationError,
			title=_("Label Repair Rolled Back"),
		)
	frappe.db.release_savepoint(run_savepoint)

	report.after = audit.mwo_mop_counts(audit.mwo_mop_findings())
	_print_report(report)
	return report


# ----------------------------------------------------------------------------------------------
# refusal
# ----------------------------------------------------------------------------------------------


def refuse_inside_locked_windows():
	"""Raise while the EOD MOP Log sync holds its lock or a stock reconciliation window is open.

	The EOD sync reads the very labels this patch rewrites (it groups and marks ledger rows by work
	order), and both windows block stock movement for operators; a relabel landing mid-run could
	split one work order's sync across two keys. The migrate fails loudly and is simply re-run.
	"""
	if eod_lock.is_eod_sync_locked():
		frappe.throw(
			_(
				"The work order / operation label repair cannot run while the EOD MOP Log sync holds its "
				"lock. Deploy outside the EOD window: run bench migrate again once the sync has finished."
			),
			frappe.ValidationError,
			title=_("EOD Sync In Progress"),
		)
	open_windows = open_reconciliation_windows()
	if open_windows:
		frappe.throw(
			_(
				"The work order / operation label repair cannot run while a Stock Reconciliation window is "
				"open for department(s) {0}. Deploy outside the EOD window and the reconciliation windows, "
				"then run bench migrate again."
			).format(", ".join(open_windows)),
			frappe.ValidationError,
			title=_("Stock Reconciliation In Progress"),
		)


def open_reconciliation_windows():
	"""Departments whose stock reconciliation window is open right now (none while the feature is
	off). The non-throwing twin of ``stock_recon_window.validate_stock_movement_allowed``: same
	switch, same bypass flag, same per-department clock check (which handles windows that wrap past
	midnight). Site-wide on purpose -- a repair spans departments."""
	if not stock_recon_window._enabled() or stock_recon_window._bypassed():
		return []
	from_field, to_field = stock_recon_window._FROM_FIELD, stock_recon_window._TO_FIELD
	if not (
		frappe.db.has_column("Department", from_field)
		and frappe.db.has_column("Department", to_field)
	):
		return []
	departments = frappe.get_all(
		"Department",
		filters=[[from_field, "is", "set"], [to_field, "is", "set"]],
		pluck="name",
	)
	return sorted(
		d
		for d in departments
		if stock_recon_window._department_window_status(d) == "open"
	)


# ----------------------------------------------------------------------------------------------
# plan
# ----------------------------------------------------------------------------------------------


def _case(cls, doctype, name):
	"""One unit of work: the writes for ONE document, applied in one savepoint with one Comment.

	``doctype`` / ``name`` is the document the Comment lands on (the parent for child-row writes).
	``relabels`` (Employee IR rows of submitted Receives) feed the repeat-flag recheck.
	"""
	return frappe._dict(
		cls=cls,
		doctype=doctype,
		name=name,
		summary=[],
		writes=[],
		relabels=[],
		clear_cache=[],
		outcome=None,
		detail=None,
	)


def _write(doctype, name, field, old, new, label=None):
	return frappe._dict(
		doctype=doctype, name=name, field=field, old=old, new=new, label=label
	)


def _summarise(case, text):
	if text not in case.summary:
		case.summary.append(text)


def build_plan(findings=None):
	"""Cases for classes 1-4 plus the report-only lists, from one :func:`mwo_mop_findings` result.

	Class 5 (repeat flags) is not planned here: its verdicts depend on class 1 having relabelled the
	rows first (see :func:`repeat_flag_cases` / :func:`preview_repeat_flag_cases`).
	"""
	if findings is None:
		findings = audit.mwo_mop_findings()
	reserved = _active_reservation_keys(findings)
	ir_cases, tree_overlay, ir_held = _ir_cases(findings, reserved)
	se_cases, se_skipped = _stock_entry_cases(findings, tree_overlay, reserved)
	mop_log_cases, mop_log_held = _mop_log_cases(
		findings, _unrepaired_stock_entries(findings, se_cases)
	)
	pointer_cases, pointer_held = _pointer_cases(findings)
	report_only = _report_only(findings, se_cases, se_skipped)
	report_only.update(ir_held)
	report_only["mop_log_unrepaired_stock_entry"] = mop_log_held
	report_only["pointer_on_cancelled"] = pointer_held
	return frappe._dict(
		findings=findings,
		cases=ir_cases + se_cases + mop_log_cases + pointer_cases,
		report_only=report_only,
	)


def _fold(value):
	"""Names compare the way MariaDB compares them: trimmed, case-insensitively."""
	return cstr(value).strip().casefold()


def _active_reservation_keys(findings):
	"""``(work order, operation)`` pairs, folded, that key a SUBMITTED reservation pairing an operation
	with a work order that does not own it. ``findings["sre"]`` lists every such reservation, so a
	label the repair is about to replace is in here exactly when a live reservation still uses it."""
	return {
		(_fold(row.sre_mwo), _fold(row.mop))
		for row in findings["sre"]
		if cint(row.docstatus) == 1
	}


def _unrepaired_stock_entries(findings, se_cases):
	"""Folded names of the Stock Entries the audit reports but class 2 does not repair (manual,
	mixed, re-issued operation, reserved...): the MOP Log rows they wrote keep their stamps."""
	reported = {_fold(row.name) for row in findings["se_headers"]}
	reported |= {_fold(row.stock_entry) for row in findings["se_rows"]}
	return reported - {_fold(case.name) for case in se_cases}


def _ir_cases(findings, reserved):
	"""Class 1: one case per Employee IR / Department IR document.

	A row is held back (reported, never relabelled) when its operation is newer than its document
	(``is_name_reuse``: the owner is not known to be the row's) or when its OLD label still keys an
	active reservation (``reserved``): the receive's cancel looks the reservation up by the row's
	label, so relabelling the row would strand it.

	Returns ``(cases, tree_overlay, held)``: ``tree_overlay`` maps each row whose tree this class fills
	to that tree (class 2 stamps the injection entry from it), ``held`` the report-only lists.
	"""
	cases: dict = {}
	held = {
		"ir_rows_name_reuse": [],
		"ir_rows_active_reservation": [],
		"casting_tree_ambiguous": [],
		"casting_tree_filled_never_charged": [],
	}
	relabelled: set = set()

	def case_for(doctype, name):
		key = (doctype, name)
		if key not in cases:
			cases[key] = _case("ir_rows", doctype, name)
			cases[key].clear_cache.append(key)
		return cases[key]

	for row in findings["ir_rows"]:
		if cint(row.get("is_name_reuse")):
			held["ir_rows_name_reuse"].append(row.row_name)
			continue
		if (_fold(row.row_mwo), _fold(row.mop)) in reserved:
			held["ir_rows_active_reservation"].append(row.row_name)
			continue
		relabelled.add(row.row_name)
		case = case_for(row.parenttype, row.parent)
		child = f"{row.parenttype} Operation"
		label = _("row {0} ({1})").format(row.idx, row.mop)
		_summarise(
			case,
			_("each listed row now carries the work order that owns its operation"),
		)
		case.writes.append(
			_write(
				child,
				row.row_name,
				"manufacturing_work_order",
				row.row_mwo,
				row.owner,
				label,
			)
		)
		if row.parenttype == "Employee IR" and cint(row.row_is_finding_mwo) != cint(
			row.owner_is_finding_mwo
		):
			case.writes.append(
				_write(
					child,
					row.row_name,
					"is_finding_mwo",
					cint(row.row_is_finding_mwo),
					cint(row.owner_is_finding_mwo),
					label,
				)
			)
		if (
			row.parenttype == "Department IR"
			and row.owner_pmo
			and not same(row.row_pmo, row.owner_pmo)
		):
			case.writes.append(
				_write(
					child,
					row.row_name,
					"parent_manufacturing_order",
					row.row_pmo,
					row.owner_pmo,
					label,
				)
			)
		# Only a SUBMITTED RECEIVE row is "a completed cycle" to get_repeat_work_orders, so only its
		# relabel can change another receive's repeat verdict.
		if (
			row.parenttype == "Employee IR"
			and cint(row.parent_docstatus) == 1
			and row.ir_type == "Receive"
			and row.operation
		):
			case.relabels.append(
				frappe._dict(
					row_name=row.row_name,
					operation=row.operation,
					old=row.row_mwo,
					new=row.owner,
				)
			)

	tree_overlay: dict = {}
	filled_by_parent: dict = {}
	for row in findings["casting_receive_without_tree"]:
		if row.row_name not in relabelled:
			# Only a row this class relabels in the same case: a row held back above keeps its label,
			# and with it the tree its receive resolved.
			continue
		if not row.issue_tree:
			held["casting_tree_ambiguous"].append(row.row_name)
			continue
		case = case_for("Employee IR", row.parent)
		_summarise(
			case,
			_(
				"casting rows now carry the tree their operation was issued on. This receive charged no "
				"tree for them when it was submitted, so that tree never recorded their weight: "
				"cancelling the receive before the tree is corrected would reverse weight the tree "
				"never received"
			),
		)
		held["casting_tree_filled_never_charged"].append(
			{
				"name": row.row_name,
				"employee_ir": row.parent,
				"idx": row.idx,
				"tree": row.issue_tree,
				"gross_wt": row.gross_wt,
				"received_gross_wt": row.received_gross_wt,
			}
		)
		case.writes.append(
			_write(
				"Employee IR Operation",
				row.row_name,
				"tree_number",
				None,
				row.issue_tree,
				_("row {0} ({1})").format(row.idx, row.mop),
			)
		)
		tree_overlay[row.row_name] = row.issue_tree
		filled_by_parent.setdefault(row.parent, {})[row.row_name] = row.issue_tree

	# The header tree is the single tree every row agrees on (single_tree_or_none, the rule the
	# receive itself applies); a filled row can change that answer.
	for parent, filled in filled_by_parent.items():
		rows = frappe.get_all(
			"Employee IR Operation",
			filters={"parent": parent, "parenttype": "Employee IR"},
			fields=["name", "tree_number"],
		)
		header = frappe.db.get_value("Employee IR", parent, "tree_number")
		resolved = single_tree_or_none(
			filled.get(r.name) or r.tree_number for r in rows
		)
		if not same(header, resolved):
			cases[("Employee IR", parent)].writes.append(
				_write(
					"Employee IR", parent, "tree_number", header, resolved, _("header")
				)
			)

	return list(cases.values()), tree_overlay, held


def _stock_entry_cases(findings, tree_overlay, reserved):
	"""Class 2: one case per Employee IR injection Stock Entry whose header pair disagrees.

	Only entries whose every operation-bearing row belongs to the header operation's owner are
	relabelled; anything mixed is left for a person, and so is an entry whose operation is newer than
	the entry (``is_name_reuse``) or whose old labels still key an active reservation (``reserved``):
	the reservation was made from the entry's header and is released through that label (the
	receive's cancel looks it up by work order and operation), so the entry, its receive row and the
	reservation stay together under the old label until a person moves all three. Rows are written so
	that, after the header change, each row's owner equals ``COALESCE(row work order, header work
	order)``: a row label that disagrees with the owner is replaced, a blank one is left to fall back
	to the corrected header.
	"""
	eligible = [
		h
		for h in findings["se_headers"]
		if cint(h.auto_created)
		and h.custom_eir_operation_row
		and cint(h.docstatus) in (0, 1)
	]
	if not eligible:
		return [], []

	rows_by_entry: dict = {}
	for row in frappe.db.sql(
		"""
		SELECT parent, name, idx, manufacturing_operation AS mop,
			custom_manufacturing_work_order AS row_mwo
		FROM `tabStock Entry Detail`
		WHERE parenttype = 'Stock Entry' AND parent IN %(names)s
		""",
		{"names": tuple(h.name for h in eligible)},
		as_dict=True,
	):
		rows_by_entry.setdefault(row.parent, []).append(row)
	owners = mop_owners(row.mop for rows in rows_by_entry.values() for row in rows)
	source_trees = dict(
		frappe.get_all(
			"Employee IR Operation",
			filters={
				"name": ["in", sorted({h.custom_eir_operation_row for h in eligible})]
			},
			fields=["name", "tree_number"],
			as_list=True,
		)
	)

	cases, skipped = [], []
	for header in eligible:
		rows = sorted(rows_by_entry.get(header.name, []), key=lambda r: cint(r.idx))
		if cint(header.get("is_name_reuse")):
			skipped.append(
				frappe._dict(
					name=header.name,
					reason=_(
						"operation {0} is newer than the entry (its name was re-issued, or it was set "
						"later): the owner is not known to be the entry's"
					).format(header.mop),
				)
			)
			continue
		# A reservation is keyed by the HEADER work order and the row's operation.
		keys = {(_fold(header.mwo), _fold(header.mop))}
		keys |= {(_fold(header.mwo), _fold(r.mop)) for r in rows if cstr(r.mop).strip()}
		if keys & reserved:
			skipped.append(
				frappe._dict(
					name=header.name,
					reason=_(
						"an active reservation made from it is still keyed by work order {0}; the entry "
						"stays with it until a person moves both"
					).format(header.mwo),
				)
			)
			continue
		mixed = [
			r
			for r in rows
			if owners.get(cstr(r.mop).strip())
			and not same(owners[cstr(r.mop).strip()], header.owner)
		]
		if mixed:
			skipped.append(
				frappe._dict(
					name=header.name,
					reason=_("rows carry operations of other work orders: {0}").format(
						", ".join(cstr(r.mop) for r in mixed)
					),
				)
			)
			continue

		case = _case("se_headers", "Stock Entry", header.name)
		case.clear_cache.append(("Stock Entry", header.name))
		_summarise(
			case,
			_(
				"work order labels re-derived from operation {0}, owned by {1}; "
				"stock, ledger and reservations untouched"
			).format(header.mop, header.owner),
		)
		case.writes.append(
			_write(
				"Stock Entry",
				header.name,
				"manufacturing_work_order",
				header.mwo,
				header.owner,
				_("header"),
			)
		)
		if header.owner_pmo and not same(header.pmo, header.owner_pmo):
			case.writes.append(
				_write(
					"Stock Entry",
					header.name,
					"manufacturing_order",
					header.pmo,
					header.owner_pmo,
					_("header"),
				)
			)
		tree = tree_overlay.get(header.custom_eir_operation_row) or source_trees.get(
			header.custom_eir_operation_row
		)
		if tree and not cstr(header.custom_tree_number).strip():
			case.writes.append(
				_write(
					"Stock Entry",
					header.name,
					"custom_tree_number",
					header.custom_tree_number,
					tree,
					_("header"),
				)
			)
		for row in rows:
			if (
				owners.get(cstr(row.mop).strip())
				and cstr(row.row_mwo).strip()
				and not same(row.row_mwo, header.owner)
			):
				case.writes.append(
					_write(
						"Stock Entry Detail",
						row.name,
						"custom_manufacturing_work_order",
						row.row_mwo,
						header.owner,
						_("row {0} ({1})").format(row.idx, cstr(row.mop).strip()),
					)
				)
		cases.append(case)
	return cases, skipped


def _mop_log_cases(findings, unrepaired_entries):
	"""Class 3: one case per operation whose live MOP Log rows carry another work order's stamp.

	A row written by a Stock Entry this patch reports but does not repair (``unrepaired_entries``,
	folded names: a manual entry, a mixed one...) keeps its stamp and is reported instead: the entry
	still names the stamp's work order, and which work order holds that metal is exactly what the
	entry is waiting for a person to decide. Relabelling its ledger alone would make that decision
	silently. Returns ``(cases, held MOP Log names)``.
	"""
	cases: dict = {}
	held: list = []
	for row in findings["mop_log"]["stamps"]:
		if (
			row.voucher_type == "Stock Entry"
			and _fold(row.voucher_no) in unrepaired_entries
		):
			held.append(row.name)
			continue
		case = cases.get(row.mop)
		if case is None:
			case = cases[row.mop] = _case(
				"mop_log_stamps", "Manufacturing Operation", row.mop
			)
			_summarise(
				case,
				_(
					"MOP Log stamps re-derived from this operation's own work order {0}; "
					"quantities untouched"
				).format(row.owner),
			)
		case.writes.append(
			_write(
				"MOP Log",
				row.name,
				"manufacturing_work_order",
				row.stamp,
				row.owner,
				_("MOP Log {0} ({1} {2})").format(
					row.name, row.voucher_type, row.voucher_no
				),
			)
		)
	return list(cases.values()), held


def _pointer_cases(findings):
	"""Class 4: (a) drafts / discards pointing at another work order's operation, or at one that no
	longer exists; (b) stale own pointers with an open successor. A SUBMITTED work order pointing at
	another work order's operation is report-only: which of its own operations is current is a
	decision, not a label. A properly CANCELLED one (status Cancelled) is history and keeps whatever
	it points at; it is reported. Returns ``(cases, the cancelled work orders held back)``."""
	cases = []
	held: set = set()

	def cancelled(row):
		return cint(row.docstatus) == 2 and row.status == "Cancelled"

	for row in findings["pointer_foreign"]:
		if cint(row.docstatus) == 1:
			continue
		if cancelled(row):
			held.add(row.mwo)
			continue
		cases.append(
			_clear_pointer_case(
				"pointer_foreign",
				row,
				_(
					"current operation {0} belongs to work order {1}; "
					"a draft or discarded work order carries none"
				).format(row.pointer, row.pointer_owner),
			)
		)

	# The same verdict for a pointer nothing owns -- its operation was deleted (or carries no work
	# order): it is not this work order's either. ManufacturingWorkOrder.validate clears such a
	# pointer on a draft only in principle, because link validation refuses the save first.
	for row in findings["pointer_on_non_submitted"]["rows"]:
		if row.pointer_owner:
			continue
		if cancelled(row):
			held.add(row.mwo)
			continue
		cases.append(
			_clear_pointer_case(
				"pointer_missing_operation",
				row,
				_(
					"current operation {0} does not exist (or belongs to no work order); "
					"a draft or discarded work order carries none"
				).format(row.pointer),
			)
		)

	for row in findings["pointer_stale"]:
		case = _case("pointer_stale", "Manufacturing Work Order", row.mwo)
		_summarise(
			case,
			_(
				"current operation moved from its Finished operation {0} to its open successor {1}"
			).format(row.pointer, row.successor),
		)
		case.writes.append(
			_write(
				"Manufacturing Work Order",
				row.mwo,
				"manufacturing_operation",
				row.pointer,
				row.successor,
			)
		)
		cases.append(case)
	return cases, sorted(held)


def _clear_pointer_case(cls, row, summary):
	"""Clear a draft's or discarded work order's pointer, and give it the status it should read."""
	case = _case(cls, "Manufacturing Work Order", row.mwo)
	_summarise(case, summary)
	case.writes.append(
		_write(
			"Manufacturing Work Order",
			row.mwo,
			"manufacturing_operation",
			row.pointer,
			None,
		)
	)
	if cint(row.docstatus) == 2 and row.status != "Cancelled":
		# Discard (docstatus 0 -> 2) skips on_cancel, so the status was never moved.
		case.writes.append(
			_write(
				"Manufacturing Work Order",
				row.mwo,
				"status",
				row.status,
				"Cancelled",
			)
		)
	elif cint(row.docstatus) == 0 and row.status == "Not Started":
		# Copied from the split parent; a draft work order reads Draft.
		case.writes.append(
			_write("Manufacturing Work Order", row.mwo, "status", row.status, "Draft")
		)
	return case


def _report_only(findings, se_cases, se_skipped):
	"""What this patch deliberately leaves alone, so the migrate log says it out loud. (The class
	planners add their own held-back lists in :func:`build_plan`.)"""
	repaired = {case.name for case in se_cases}
	manual, other = set(), set()
	for row in findings["se_headers"]:
		(other if cint(row.auto_created) else manual).add(row.name)
	for row in findings["se_rows"]:
		(other if cint(row.auto_created) else manual).add(row.stock_entry)
	non_submitted = findings["pointer_on_non_submitted"]["rows"]
	return {
		"manual_stock_entries": sorted(manual),
		"other_stock_entries": sorted(other - repaired),
		"stock_entries_skipped": [dict(row) for row in se_skipped],
		"sre_active": sorted(r.name for r in findings["sre"] if cint(r.docstatus) != 2),
		"sre_cancelled": sorted(
			r.name for r in findings["sre"] if cint(r.docstatus) == 2
		),
		"subcontracting_log": sorted(r.name for r in findings["subcontracting_log"]),
		"mop_log_name_reuse": sorted(r.name for r in findings["mop_log"]["name_reuse"]),
		"mop_log_orphans": dict(findings["mop_log"]["orphans"]),
		"process_loss_multi_mwo": dict(findings["process_loss_multi_mwo"]),
		"pointer_foreign_submitted": sorted(
			r.mwo for r in findings["pointer_foreign"] if cint(r.docstatus) == 1
		),
		# Drafts / discards whose pointer is their own: not a borrowed pointer.
		"pointer_on_non_submitted_not_foreign": sorted(
			r.mwo for r in non_submitted if r.pointer_is_own
		),
	}


# ----------------------------------------------------------------------------------------------
# class 5: repeat-receive flags
# ----------------------------------------------------------------------------------------------


def flagged_receives(relabels):
	"""Submitted Receives flagged as repeats whose verdict a relabel can change.

	A relabel at operation X moves one completed cycle from the old work order to the new one, so the
	only verdicts it can change are those of submitted Receives at X carrying either work order.
	Returns ``[{name, operation, worker_performance}]``, one per receive, sorted by name.
	"""
	by_operation: dict = {}
	for relabel in relabels or ():
		by_operation.setdefault(relabel.operation, set()).update(
			mwo for mwo in (relabel.old, relabel.new) if cstr(mwo).strip()
		)

	receives: dict = {}
	for operation in sorted(by_operation):
		mwos = sorted(by_operation[operation])
		if not operation or not mwos:
			continue
		for eir in frappe.db.sql(
			"""
			SELECT DISTINCT p.name, p.operation, p.worker_performance
			FROM `tabEmployee IR` p
			INNER JOIN `tabEmployee IR Operation` r
				ON r.parent = p.name AND r.parenttype = 'Employee IR'
			WHERE p.docstatus = 1
			  AND p.type = 'Receive'
			  AND p.operation = %(operation)s
			  AND p.is_repeat_receive = 1
			  AND r.manufacturing_work_order IN %(mwos)s
			""",
			{"operation": operation, "mwos": tuple(mwos)},
			as_dict=True,
		):
			receives.setdefault(eir.name, eir)
	return [receives[name] for name in sorted(receives)]


def repeat_flag_cases(relabels):
	"""Class 5, computed on the CURRENT labels (call it after class 1 has written them).

	Each receive from :func:`flagged_receives` is re-asked through the Employee IR's own
	``get_repeat_work_orders``; one that no longer has a prior cycle is cleared. A receive that the
	relabel turns INTO a repeat is left alone: setting the flag would ask a question nobody was
	shown, and the worker-performance answer cannot be invented.
	"""
	cases = []
	for eir in flagged_receives(relabels):
		rows = frappe.get_all(
			"Employee IR Operation",
			filters={"parent": eir.name, "parenttype": "Employee IR"},
			fields=["manufacturing_operation", "manufacturing_work_order"],
			order_by="idx asc",
		)
		if get_repeat_work_orders(rows, eir.operation, eir.name):
			continue
		case = _case("repeat_flags", "Employee IR", eir.name)
		_summarise(
			case,
			_(
				"no longer a repeat receive: once the mislabelled rows were corrected, no other submitted "
				"Receive at {0} carries any of this receive's work orders"
			).format(eir.operation),
		)
		case.writes.append(_write("Employee IR", eir.name, "is_repeat_receive", 1, 0))
		if cstr(eir.worker_performance).strip():
			case.writes.append(
				_write(
					"Employee IR",
					eir.name,
					"worker_performance",
					eir.worker_performance,
					None,
				)
			)
		cases.append(case)
	return cases


def preview_repeat_flag_cases(plan):
	"""Class 5 for a dry run: what :func:`repeat_flag_cases` would answer after class 1.

	The verdict is the Employee IR's own query, and that query reads the database, so the planned
	row relabels are applied inside a savepoint, the verdicts collected and the savepoint ROLLED
	BACK before returning -- nothing survives, and the preview cannot drift from what an apply does.
	The UPDATEs touch only rows of submitted receives. On a read-only connection (maintenance mode)
	the verdicts cannot be asked; the receives that would be re-checked are reported instead.
	"""
	relabels = [relabel for case in plan.cases for relabel in case.relabels]
	if not relabels:
		return []
	if frappe.flags.read_only:
		plan.report_only["repeat_flags_to_recheck"] = [
			eir.name for eir in flagged_receives(relabels)
		]
		return []

	savepoint = "mwo_mop_link_preview"
	frappe.db.savepoint(savepoint)
	try:
		for relabel in relabels:
			frappe.db.sql(
				"""
				UPDATE `tabEmployee IR Operation`
				SET manufacturing_work_order = %(new)s
				WHERE name = %(row_name)s
				""",
				relabel,
			)
		return repeat_flag_cases(relabels)
	finally:
		frappe.db.rollback(save_point=savepoint)


# ----------------------------------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------------------------------


def apply_case(case):
	"""Apply one case inside its own savepoint; returns ``"applied"``, ``"drifted"`` or ``"failed"``.

	Every planned old value is re-read with a row lock first (``FOR UPDATE`` sees the latest committed
	value, not this transaction's snapshot). Anything else than the value detection saw means the row
	moved on since: the case is skipped rather than overwritten. An error that rolled back the whole
	transaction, savepoint included, is re-raised as it is (see the module docstring).
	"""
	savepoint = f"mwo_mop_link_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	try:
		for write in case.writes:
			current = frappe.db.get_value(
				write.doctype, write.name, write.field, for_update=True
			)
			if not _same_value(current, write.old):
				raise CaseDrift(
					_("{0} {1}: {2} is {3}, expected {4}").format(
						write.doctype,
						write.name,
						write.field,
						_shown(current),
						_shown(write.old),
					)
				)
		for (doctype, name), values in _grouped(case.writes).items():
			frappe.db.set_value(doctype, name, values, update_modified=False)
		for doctype, name in case.clear_cache:
			# set_value on a child row clears only the child's cache entry, not its parent's.
			frappe.clear_document_cache(doctype, name)
		add_comment(case)
	except CaseDrift as drift:
		frappe.db.rollback(save_point=savepoint)
		case.detail = cstr(drift)
		return "drifted"
	except Exception:
		if not _rolled_back_to(savepoint):
			# The error ended the whole transaction (a deadlock, a lost connection): the savepoint
			# went with it, and so did every case applied before this one. Carrying on would report
			# repairs that no longer exist, so the ORIGINAL error fails the run (bench migrate then
			# rolls back and is simply run again).
			raise
		case.detail = frappe.get_traceback()
		frappe.log_error(
			title=f"{REPAIR_TAG}: {case.doctype} {case.name}",
			message=case.detail,
			reference_doctype=case.doctype,
			reference_name=case.name,
		)
		return "failed"
	frappe.db.release_savepoint(savepoint)
	return "applied"


def _rolled_back_to(savepoint):
	"""Roll back to ``savepoint``; False when it no longer exists because the transaction it belonged
	to was rolled back underneath it."""
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:
		return False
	return True


def add_comment(case):
	"""Info Comment on the document: what changed, old -> new, and why."""
	lines = [f"<b>{REPAIR_TAG}</b>: {escape_html('; '.join(case.summary))}."]
	lines += [escape_html(_describe(write)) for write in case.writes]
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": case.doctype,
			"reference_name": case.name,
			"content": "<br>".join(lines),
		}
	).insert(ignore_permissions=True)


def _record(report, case, outcome):
	case.outcome = outcome
	entry = _public(case)
	report.cases.append(entry)
	if outcome == "applied":
		report.applied += 1
	else:
		report[outcome].append(
			{"doctype": case.doctype, "name": case.name, "detail": case.detail}
		)


def _grouped(writes):
	grouped: dict = {}
	for write in writes:
		grouped.setdefault((write.doctype, write.name), {})[write.field] = write.new
	return grouped


def _same_value(current, old):
	if isinstance(old, int) and not isinstance(old, bool):
		return cint(current) == old
	return same(current, old)


def _shown(value):
	return cstr(value) if cstr(value).strip() else _("(blank)")


def _describe(write):
	where = write.label or f"{write.doctype} {write.name}"
	return _("{0}: {1} {2} -> {3}").format(
		where, write.field, _shown(write.old), _shown(write.new)
	)


def _public(case):
	entry = {
		"class": case.cls,
		"doctype": case.doctype,
		"name": case.name,
		"writes": [
			{
				"doctype": w.doctype,
				"name": w.name,
				"field": w.field,
				"old": w.old,
				"new": w.new,
			}
			for w in case.writes
		],
	}
	if case.outcome:
		entry["outcome"] = case.outcome
	return entry


def _print_report(report):
	mode = "DRY RUN" if report.dry_run else "APPLY"
	print(f"{LOG_PREFIX} {mode}: before {_counts_line(report.before)}")
	for case in report.cases:
		outcome = f" [{case['outcome']}]" if case.get("outcome") else ""
		print(f"  {case['class']}: {case['doctype']} {case['name']}{outcome}")
		for write in case["writes"]:
			print(
				f"      {write['doctype']} {write['name']}: {write['field']} "
				f"{_shown(write['old'])} -> {_shown(write['new'])}"
			)
	for key, value in (report.report_only or {}).items():
		if not value:
			continue
		if isinstance(value, list):
			names = [cstr(v.get("name") if isinstance(v, dict) else v) for v in value]
			more = (
				f" ... and {len(names) - _PRINT_NAMES} more"
				if len(names) > _PRINT_NAMES
				else ""
			)
			value = f"{len(names)}: {', '.join(names[:_PRINT_NAMES])}{more}"
		print(f"  report-only {key}: {value}")
	if report.dry_run:
		print(
			f"{LOG_PREFIX} DRY RUN: {len(report.cases)} case(s) planned, nothing written."
		)
		return
	print(
		f"{LOG_PREFIX} applied {report.applied} case(s), drifted {len(report.drifted)}, "
		f"failed {len(report.failed)} (failures are in Error Log)."
	)
	print(f"{LOG_PREFIX} after {_counts_line(report.after)}")


def _counts_line(counts):
	return " ".join(f"{key}={value}" for key, value in (counts or {}).items())
