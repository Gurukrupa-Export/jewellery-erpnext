"""Receive-state check: an Employee IR Receive may only receive operations that are with it.

WHY
---
A Receive row claims "this employee (or subcontractor) hands back this operation, done at this
Department Operation". The desk only ever offers operations for which that is true --
``employee_ir.js`` ``scan_mwo`` / ``get_operations`` filter a Receive on status On Hold / WIP /
QC Completed, this department, this operation and this employee (subcontractor) -- but nothing on
the server re-checked it. A pasted row, an edited cell or an API write could therefore receive an
operation that was issued to somebody else, never issued at all, or sits in another department, and
the receive then finished it, minted its successor and moved its weight on the strength of a claim
nobody had verified. An Issue turned into a Receive after its rows were loaded, with one work-order
cell re-pointed afterwards, went through exactly like that.

This is the Receive branch of ``current_operation_guard._state_problem`` on kggk_uat, with the same
wording, without the guard's pointer / successor / draft rules. One deliberate difference: the
employee is compared only on an in-house Receive. A subcontracting Issue never stamps an employee on
the operation (``on_submit_issue_new`` writes the subcontractor instead), so comparing one there could
only refuse a legitimate receive.

WHEN
----
On insert and on submit -- ``EmployeeIR.validate`` runs on both, including the Submission Queue
worker. A re-save of an existing draft judges only the rows it adds (every row when a header field
the desk filter reads has changed): an open draft may legitimately go stale -- its operation put On
Hold or into QC while it waited -- and refusing the re-save would stop the operator from editing it.
Submit is where the claim has to be true.

Reported through ``mwo_mop_link.report``, so the ``"mwo_mop_link_guard": "warn"`` kill switch in
site_config turns a refusal into an Error Log entry plus an orange message.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_mop_link import (
	bold,
	report,
	same,
)

# The statuses employee_ir.js offers to a Receive (scan_mwo / get_operations), and
# current_operation_guard.RECEIVABLE_STATUSES on kggk_uat.
RECEIVABLE_STATUSES = ("On Hold", "WIP", "QC Completed")

# Header fields the desk filter reads. A draft re-save that changes one of them re-judges every row
# (employee_ir.js clears the table when any of them changes, so its rows are all new anyway).
FILTER_HEADER_FIELDS = (
	"type",
	"department",
	"operation",
	"employee",
	"subcontracting",
	"subcontractor",
)

OPERATION_FIELDS = (
	"name",
	"manufacturing_work_order",
	"status",
	"department",
	"operation",
	"employee",
	"subcontractor",
)


def validate_receive_state(doc):
	"""Refuse (in warn mode: log) Receive rows whose operation this document cannot receive."""
	if doc.get("type") != "Receive":
		return
	rows = rows_to_check(doc)
	if not rows:
		return

	names = sorted({_mop(row) for row in rows})
	# Keyed case-insensitively, the way MariaDB matched them: a row saved without link
	# validation keeps whatever case it was typed in.
	operations = {
		m.name.casefold(): m
		for m in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ("in", names)},
			fields=list(OPERATION_FIELDS),
		)
	}
	problems = []
	for row in rows:
		m = operations.get(_mop(row).casefold())
		if not m:
			continue  # a missing operation is the Link validator's to report
		problem = state_problem(doc, row.get("idx") or "?", m)
		if problem:
			problems.append(problem)
	report(problems, doc=doc, title=_("Operation Cannot Be Received"))


def rows_to_check(doc):
	"""The rows to judge on this save: every row on insert and on submit; on a draft re-save, only
	the rows the save adds -- or every row when a header field the filter reads changed."""
	rows = [row for row in doc.get("employee_ir_operations") or [] if _mop(row)]
	if doc.is_new() or cint(doc.docstatus) == 1:
		return rows
	before = doc.get_doc_before_save()
	if not before or any(
		not same(before.get(f), doc.get(f)) for f in FILTER_HEADER_FIELDS
	):
		return rows
	stored = {
		_mop(row).casefold() for row in before.get("employee_ir_operations") or []
	}
	return [row for row in rows if _mop(row).casefold() not in stored]


def state_problem(doc, idx, m):
	"""``None`` when operation ``m`` (a Manufacturing Operation row) is receivable by ``doc``, else
	one message naming every field that does not match -- kggk_uat's wording, so both share a
	translation."""
	problems = []
	status = cstr(m.status)
	if status not in RECEIVABLE_STATUSES:
		problems.append(_("status {0}").format(status or _("blank")))
	if not same(m.department, doc.get("department")):
		problems.append(_("department {0}").format(bold(m.department)))
	if not same(m.operation, doc.get("operation")):
		problems.append(_("operation {0}").format(bold(m.operation)))
	if doc.get("subcontracting") == "Yes":
		if doc.get("subcontractor") and not same(
			m.subcontractor, doc.get("subcontractor")
		):
			problems.append(_("subcontractor {0}").format(bold(m.subcontractor)))
	elif doc.get("employee") and not same(m.employee, doc.get("employee")):
		problems.append(_("employee {0}").format(bold(m.employee)))
	if not problems:
		return None
	return _(
		"Row {0}: {1} of work order {2} does not match this Receive ({3})."
	).format(idx, bold(m.name), bold(m.manufacturing_work_order), ", ".join(problems))


def _mop(row):
	return cstr(row.get("manufacturing_operation")).strip()
