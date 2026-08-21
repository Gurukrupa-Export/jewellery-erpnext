"""Partial-receipt ledger for Product Certification.

An Issue document can be answered by more than one Receive — production already does
this (``KG-PHS-24-00023`` carries three) — but nothing used to track or cap it. This
module is the single source of truth for that rollup:

* ``received_weight`` / ``pending_weight`` on each **Issue** ``Product Details`` row,
  derived from the ``total_weight`` of every SUBMITTED Receive row pointing at it, and
* ``receive_status`` on the Issue parent (Not Received / Partially Received /
  Fully Received).

Both are derived, never authored — every writer recomputes from the submitted Receives
rather than applying a delta, so submit, cancel and amend all converge on the same
answer without a reversal path to get wrong.

The tolerance is deliberately the same idiom the Tree Number ledger uses
(``employee_ir/doc_events/tree_casting.py``): ``received == issued`` routinely lands on
floating-point dust (``30 - 12 - 18`` is not exactly 0), so a strict ``<= 0`` would
leave a fully-received Issue stuck on "Partially Received" forever.
"""

from collections import defaultdict

import frappe
from frappe.utils import flt

NOT_RECEIVED = "Not Received"
PARTIALLY_RECEIVED = "Partially Received"
FULLY_RECEIVED = "Fully Received"


def se_precision():
	"""Weight precision for the receipt ledger — pinned to Stock Entry Detail.transfer_qty (3)."""
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def pending_eps():
	"""Tolerance for 'fully received' comparisons: half the smallest representable qty (0.0005 at prec 3)."""
	return (10 ** -se_precision()) / 2


MATCH_FIELDS = (
	"serial_no",
	"item_code",
	"manufacturing_work_order",
	"parent_manufacturing_order",
	"tree_no",
)


def _match_filters(row):
	"""The identity of a Product Details row, as ``validate_items`` already defines it.

	Blank-as-None is load-bearing: ``frappe.db.get_value`` treats ``""`` as a literal
	empty string and would miss rows where the column is NULL.
	"""
	return {
		"serial_no": row.get("serial_no") or None,
		"item_code": row.get("item_code"),
		"manufacturing_work_order": row.get("manufacturing_work_order") or None,
		"parent_manufacturing_order": row.get("parent_manufacturing_order") or None,
		"tree_no": row.get("tree_no") or None,
	}


def match_identity(row):
	"""``_match_filters`` as a hashable tuple — the PROBE side of an in-Python match.

	One definition, two consumers (``validate_items`` and the issue-row index below), so
	neither can drift from the filter dict.
	"""
	filters = _match_filters(row)
	return tuple(filters[field] for field in MATCH_FIELDS)


def stored_identity(row):
	"""The same tuple for a row read back from the database — the STORED side.

	Deliberately un-normalised. The filter dict this replaces sent a blank as ``None``,
	which the query builder turns into ``IS NULL``, so a column holding ``""`` never
	matched a blank row. Normalising both sides would quietly start matching them.
	"""
	return tuple(row.get(field) for field in MATCH_FIELDS)


ISSUE_ROW_FIELDS = (
	"name",
	"total_weight",
	"received_weight",
	"pending_weight",
	*MATCH_FIELDS,
)


def get_issue_rows(issue_name):
	"""Every Product Details row of an Issue, in grid order.

	One read shared by the ledger's three consumers -- the row index, the pending map and
	the status rollup -- which used to fetch the same rows separately.
	"""
	if not issue_name:
		return []

	return frappe.get_all(
		"Product Details",
		filters={"parent": issue_name, "parenttype": "Product Certification"},
		fields=list(ISSUE_ROW_FIELDS),
		order_by="idx asc",
	)


def build_issue_row_index(issue_rows):
	"""``{identity tuple: row name}`` for resolving hand-built Receive rows.

	First row wins, which is the single-row answer the per-row ``get_value`` gave; ordering
	by ``idx`` is what makes "first" the same row on every call.
	"""
	index = {}
	for row in issue_rows:
		index.setdefault(stored_identity(row), row.name)
	return index


def resolve_issue_row(issue_name, row, index=None):
	"""Name of the Issue's Product Details row that ``row`` (a Receive row) settles.

	``issue_row`` is set by the "Create Receiving" mapper and is authoritative. Rows
	typed or scanned by hand fall back to the same identity tuple ``validate_items``
	uses, so a hand-built Receive still lands on the right ledger row.

	Pass ``index`` (from ``build_issue_row_index``) to resolve without a query -- the
	fallback used to cost one read per unmapped row, on both sides of the rollup. Without
	it the behaviour is unchanged, so any other caller still works.
	"""
	if row.get("issue_row"):
		return row.get("issue_row")
	if not issue_name:
		return None

	if index is not None:
		return index.get(match_identity(row))

	filters = _match_filters(row)
	filters["parent"] = issue_name
	filters["parenttype"] = "Product Certification"
	return frappe.db.get_value("Product Details", filters, "name")


def get_received_map(issue_name, exclude=None, issue_rows=None):
	"""``{issue_pd_row_name: weight}`` booked by submitted Receives against ``issue_name``.

	``exclude`` drops one Product Certification from the tally — the document being
	cancelled or amended, whose own rows must not count towards the pending it is
	being validated against.

	``issue_rows`` is the Issue's own rows when the caller already has them; the index they
	build resolves hand-built Receive rows in memory instead of one query each.
	"""
	received = defaultdict(float)
	if not issue_name:
		return received

	filters = {
		"receive_against": issue_name,
		"type": "Receive",
		"docstatus": 1,
	}
	if exclude:
		filters["name"] = ["!=", exclude]

	receives = frappe.get_all("Product Certification", filters=filters, pluck="name")
	if not receives:
		return received

	rows = frappe.get_all(
		"Product Details",
		filters={"parent": ["in", receives], "parenttype": "Product Certification"},
		fields=[
			"parent",
			"issue_row",
			"item_code",
			"serial_no",
			"manufacturing_work_order",
			"parent_manufacturing_order",
			"tree_no",
			"total_weight",
		],
	)
	if issue_rows is None:
		issue_rows = get_issue_rows(issue_name)
	index = build_issue_row_index(issue_rows)

	for row in rows:
		key = resolve_issue_row(issue_name, row, index=index)
		if key:
			received[key] += flt(row.total_weight)

	return received


def get_pending_map(issue_name, exclude=None, issue_rows=None):
	"""``{issue_pd_row_name: (total_weight, pending_weight)}`` for an Issue document."""
	if issue_rows is None:
		issue_rows = get_issue_rows(issue_name)
	received = get_received_map(issue_name, exclude=exclude, issue_rows=issue_rows)
	precision = se_precision()

	pending = {}
	for row in issue_rows:
		total = flt(row.total_weight, precision)
		pending[row.name] = (
			total,
			flt(max(0.0, total - flt(received.get(row.name), precision)), precision),
		)
	return pending


def derive_status(rows, has_receipts):
	"""Status for an Issue given its ``(total, pending)`` pairs.

	A document with no Product Details rows at all cannot be "Fully Received" off the
	back of an empty ``all()`` — it stays wherever its receipts put it.
	"""
	if not has_receipts:
		return NOT_RECEIVED
	if not rows:
		return PARTIALLY_RECEIVED

	eps = pending_eps()
	if all(pending <= eps for _total, pending in rows):
		return FULLY_RECEIVED
	return PARTIALLY_RECEIVED


def update_receive_status(issue_name):
	"""Recompute the receipt ledger on an Issue document. Idempotent."""
	if not issue_name:
		return

	current = frappe.db.get_value(
		"Product Certification", issue_name, ["type", "receive_status"], as_dict=1
	)
	if not current or current.type != "Issue":
		return

	issue_rows = get_issue_rows(issue_name)
	received = get_received_map(issue_name, issue_rows=issue_rows)
	precision = se_precision()

	rows = []
	for row in issue_rows:
		total = flt(row.total_weight, precision)
		received_weight = flt(received.get(row.name), precision)
		pending_weight = flt(max(0.0, total - received_weight), precision)
		rows.append((total, pending_weight))

		# Only write when the derived value actually moved. This routine is idempotent by
		# design — every submit, cancel and the backfill patch recompute the whole ledger —
		# so on a settled Issue every one of these UPDATEs was writing back what was
		# already there.
		if (
			flt(row.received_weight, precision) == received_weight
			and flt(row.pending_weight, precision) == pending_weight
		):
			continue

		# db_set on the child row: the Issue is submitted, and these are derived
		# columns — bumping `modified` would churn the parent's version history.
		frappe.db.set_value(
			"Product Details",
			row.name,
			{"received_weight": received_weight, "pending_weight": pending_weight},
			update_modified=False,
		)

	status = derive_status(rows, has_receipts=bool(received))
	if status != current.receive_status:
		frappe.db.set_value(
			"Product Certification",
			issue_name,
			"receive_status",
			status,
			update_modified=False,
		)
	return status


def validate_over_receipt(doc):
	"""Block a Receive from booking more weight than its Issue still has pending."""
	if doc.type != "Receive" or not doc.receive_against:
		return

	issue_rows = get_issue_rows(doc.receive_against)
	pending = get_pending_map(
		doc.receive_against, exclude=doc.name, issue_rows=issue_rows
	)
	if not pending:
		return

	eps = pending_eps()
	precision = se_precision()

	# Aggregate this document first: two rows settling the same Issue row must be
	# capped on their SUM, not row by row.
	index = build_issue_row_index(issue_rows)
	booked = defaultdict(float)
	first_idx = {}
	for row in doc.product_details:
		key = resolve_issue_row(doc.receive_against, row, index=index)
		if not key:
			continue
		row.issue_row = key
		booked[key] += flt(row.total_weight)
		first_idx.setdefault(key, row.idx)

	for key, weight in booked.items():
		if key not in pending:
			continue
		_total, row_pending = pending[key]
		if flt(weight, precision) - row_pending > eps:
			frappe.throw(
				frappe._(
					"Row #{0}: cannot receive {1} against {2} — only {3} is pending on that row."
				).format(
					first_idx.get(key),
					flt(weight, precision),
					doc.receive_against,
					row_pending,
				),
				title=frappe._("Over Receipt"),
			)
