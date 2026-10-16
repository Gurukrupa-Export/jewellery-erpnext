"""Work order <-> operation link integrity.

THE RULE
--------
A Manufacturing Operation (MOP) belongs to exactly one Manufacturing Work Order (MWO): its own
``manufacturing_work_order``, set when the MOP is created and never rewritten afterwards. Every record that
stores BOTH an MWO and a MOP -- an Employee IR / Department IR operation row, a Stock Entry header or row, a
Stock Reservation Entry, a MOP Log row -- must store the MOP's own MWO. The MOP is the anchor: it carries the
``previous_mop`` chain, the successors and the ledger, so the work order is always derived from it, never the
other way round.

WHY IT EXISTS (kggk-prod, 2026-09-11 and 2026-10-09)
-------------------------------------------------
``Employee IR Operation.manufacturing_work_order`` was an editable ``fetch_if_empty`` cell that the server never
re-derived, and nothing compared it with the operation. Three receive rows were submitted with a sibling's (or an
unrelated) work order next to the right operation. Everything keyed by work order then followed the wrong MWO --
the Stock Entry header, the reservation, the MOP Log stamps, the current-operation pointer, the repeat flag --
while everything keyed by operation followed the MOP. Make Receive Entry then consumed the sibling's reservation
and two quantity-1 pieces ended with their cast metal booked on the wrong half.

KILL SWITCH
-----------
``"mwo_mop_link_guard": "warn"`` in site_config turns every check in this module into an Error Log entry plus an
orange message instead of an exception. Key absent (or any other value): enforce. The row message is the same
string as ``current_operation_guard._row_problems`` on kggk_uat, so both guards read the same and share a
translation.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, escape_html

GUARD_KEY = "mwo_mop_link_guard"
MAX_MESSAGE_LINES = 25


class MWOMOPLinkError(frappe.ValidationError):
	"""A record pairs a Manufacturing Operation with a work order that does not own it."""


def guard_mode():
	return cstr(frappe.conf.get(GUARD_KEY) or "enforce").strip().lower()


def enforcing():
	"""False only in the kill switch's warn mode."""
	return guard_mode() != "warn"


def same(a, b):
	"""Names compare like MariaDB does (case-insensitive collation), ignoring stray whitespace."""
	return cstr(a).strip().casefold() == cstr(b).strip().casefold()


def bold(value):
	return frappe.bold(escape_html(cstr(value))) if cstr(value) else _("(blank)")


def mop_owners(mops):
	"""``{mop: owning work order}`` for every existing MOP in ``mops``: one query, blanks ignored.

	A MOP that does not exist is simply absent from the result, so callers decide whether a missing operation is
	their problem (a Link field already validated it) or not.
	"""
	names = sorted({cstr(m).strip() for m in mops or () if cstr(m).strip()})
	if not names:
		return {}
	return dict(
		frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ("in", names)},
			fields=["name", "manufacturing_work_order"],
			as_list=True,
		)
	)


def owner_of(mop):
	"""The work order that owns ``mop``, or None when the MOP is blank or does not exist."""
	mop = cstr(mop).strip()
	if not mop:
		return None
	return frappe.db.get_value(
		"Manufacturing Operation", mop, "manufacturing_work_order"
	)


def row_problem(idx, mop, owner, row_mwo):
	return _("Row {0}: operation {1} belongs to work order {2}, not {3}.").format(
		idx, bold(mop), bold(owner), bold(row_mwo)
	)


def no_owner_problem(mop, exists, idx=None):
	"""Why ``mop`` has no owner: it does not exist, or (``exists``) it was saved without a work order.

	Two different repairs, so the message says which: a missing operation is a wrong name, an operation without a
	work order is a broken Manufacturing Operation record. ``idx`` makes it a row message.
	"""
	if idx:
		text = (
			_("Row {0}: operation {1} has no work order.")
			if exists
			else _("Row {0}: operation {1} does not exist.")
		)
		return text.format(idx, bold(mop))
	text = (
		_("Operation {0} has no work order.")
		if exists
		else _("Operation {0} does not exist.")
	)
	return text.format(bold(mop))


def check_rows(
	rows,
	*,
	mwo_field="manufacturing_work_order",
	mop_field="manufacturing_operation",
	fill_blank=True,
):
	"""Return one message per row whose work order does not own its operation.

	A row with an operation but no work order gets the owner filled in when ``fill_blank`` (the row is a Document
	or a dict). Rows without an operation, and operations that do not exist, are skipped: existence belongs to the
	Link validator or to the caller.

	Operations are matched case-insensitively, as MariaDB matches them: a row written without link validation
	(``ignore_links``, an import) keeps the case it was typed in, while ``mop_owners`` answers with the stored name.
	"""
	rows = list(rows or ())
	owners = {
		cstr(mop).casefold(): owner
		for mop, owner in mop_owners(row.get(mop_field) for row in rows).items()
	}
	problems = []
	for row in rows:
		mop = cstr(row.get(mop_field)).strip()
		owner = owners.get(mop.casefold()) if mop else None
		if not owner:
			continue
		row_mwo = cstr(row.get(mwo_field)).strip()
		if not row_mwo:
			if fill_blank:
				_set(row, mwo_field, owner)
			continue
		if not same(row_mwo, owner):
			problems.append(row_problem(row.get("idx") or "?", mop, owner, row_mwo))
	return problems


def report(problems, doc=None, title=None):
	"""Raise one aggregated error for ``problems`` -- or, in warn mode, log it and show it in orange.

	Rows are joined with ``<br>`` because the Submission Queue banner shows only the last line of a traceback.
	"""
	messages = list(dict.fromkeys(p for p in problems or () if p))
	if not messages:
		return
	if len(messages) > MAX_MESSAGE_LINES:
		hidden = len(messages) - MAX_MESSAGE_LINES
		messages = messages[:MAX_MESSAGE_LINES] + [
			_("... and {0} more problem(s).").format(hidden)
		]
	text = "<br>".join(messages)
	title = title or _("Work Order / Operation Mismatch")

	if enforcing():
		frappe.throw(text, exc=MWOMOPLinkError, title=title)

	reference_name = None
	if isinstance(doc, Document):
		reference_name = None if doc.is_new() else doc.name
	elif isinstance(doc, dict):
		reference_name = doc.get("name")
	frappe.log_error(
		title=_("MWO-MOP link guard (warn mode): {0} {1}").format(
			doc.get("doctype") if doc is not None else "", reference_name or ""
		),
		message=text,
		reference_doctype=doc.get("doctype") if doc is not None else None,
		reference_name=reference_name,
	)
	frappe.msgprint(text, indicator="orange", title=title)


def assert_rows(doc, rows, **kwargs):
	"""``check_rows`` + ``report`` in one call; returns nothing, raises in enforce mode."""
	report(check_rows(rows, **kwargs), doc=doc)


def refetch_from_work_order(rows, fields, mwo_field="manufacturing_work_order"):
	"""Re-read ``fields`` (``{row field: work order field}``) from each row's work order, in one query.

	Frappe validates a row's links in field order, and ``manufacturing_work_order`` comes BEFORE
	``manufacturing_operation`` in both IR row tables. So on a draft save the fields fetched FROM the work order are
	read off the work order the client posted, and only then does the operation's fetch replace that work order
	with the operation's owner: a row posted with a sibling's work order keeps the sibling's fetched values
	(Employee IR ``is_finding_mwo``, Department IR ``parent_manufacturing_order``) next to the owner. Called once the
	row's work order is final, this makes them describe that work order again. For a row whose work order was never
	replaced the values are the ones Frappe just fetched, so nothing changes. Rows without a work order, or whose
	work order does not exist, are left alone. (Moving the operation above the work order in ``field_order`` would
	fix the order too, but it also swaps the two grid columns the floor reads every day.)
	"""
	rows = [row for row in rows or () if cstr(row.get(mwo_field)).strip()]
	if not rows or not fields:
		return
	names = sorted({cstr(row.get(mwo_field)).strip() for row in rows})
	values = {
		cstr(source.get("name")).casefold(): source
		for source in frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ("in", names)},
			fields=["name", *dict.fromkeys(fields.values())],
		)
	}
	for row in rows:
		source = values.get(cstr(row.get(mwo_field)).strip().casefold())
		if source is None:
			continue
		for row_field, source_field in fields.items():
			_set(row, row_field, source.get(source_field))


def set_current_operation(mop, expected_mwo=None, doc=None):
	"""Point the OWNER of ``mop`` at it (``Manufacturing Work Order.manufacturing_operation``) and return the owner.

	The pointer is always written on the work order that owns the operation, never on a caller-supplied one: the
	callers used to write ``row.manufacturing_work_order``, which is exactly how a mismatched row moved a sibling's
	pointer and left the real owner stuck on a finished operation. ``expected_mwo`` is the work order the caller
	believes it is acting on; a mismatch is reported (raised in enforce mode, logged in warn mode, in which case the
	owner is still the one updated).

	An operation with no owner -- one that does not exist, or one saved without a work order -- cannot say whose
	pointer moves. It is reported the same way, so the kill switch covers it too; in warn mode the caller's
	``expected_mwo`` takes the pointer, which is what every caller wrote before this check existed, and nothing is
	written when the caller named none. Returns the work order written, or None.
	"""
	owner = owner_of(mop)
	if not owner:
		mop = cstr(mop).strip()
		exists = bool(mop) and bool(frappe.db.exists("Manufacturing Operation", mop))
		report([no_owner_problem(mop, exists)], doc=doc)
		# Warn mode only: enforce mode raised above.
		owner = cstr(expected_mwo).strip()
		if not owner:
			return None
	elif expected_mwo and not same(expected_mwo, owner):
		report(
			[
				_(
					"Operation {0} belongs to work order {1}, not {2}; refusing to move the current operation "
					"of the wrong work order."
				).format(bold(mop), bold(owner), bold(expected_mwo))
			],
			doc=doc,
		)
	frappe.db.set_value(
		"Manufacturing Work Order", owner, "manufacturing_operation", mop
	)
	return owner


def _set(row, fieldname, value):
	# frappe._dict answers every attribute (missing ones read as None), so test for dict first.
	if isinstance(row, dict):
		row[fieldname] = value
	else:
		row.set(fieldname, value)
