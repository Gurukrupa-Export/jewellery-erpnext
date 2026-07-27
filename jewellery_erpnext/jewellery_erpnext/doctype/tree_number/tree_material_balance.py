# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Canonical arithmetic for the Tree Number ``material_details`` ledger.

One source of truth for precision, the pending formula, the row invariants and the
status state machine. Every writer -- ``TreeNumber.validate``, the Issue/Receive
buttons in ``tree_stock_entry``, and the casting Employee IR layer in
``tree_casting`` -- goes through here so the four paths cannot drift apart again.

The ledger models one physical pool: the tree's MSL (employee Raw Material)
warehouse.

    issue_qty    metal moved INTO the pool     (Dept RM -> MSL, Issue Material)
    receive_qty  metal drawn OUT to product    (MSL -> WO via the casting Employee
                                                IR gain injection, or MSL -> Dept RM
                                                via the Receive Material button)
    loss_qty     metal written off out of pool (MSL -> Dept Scrap)
    pending_qty  what is still sitting in the pool

so ``receive_qty + loss_qty <= issue_qty`` is a physical constraint, not a policy:
you cannot take more out of the pool than was put in. ``pending_qty`` is therefore
kept UNFLOORED -- a negative reading means the ledger is over-drawn and must stay
visible rather than being clamped to zero.
"""

import frappe
from frappe import _
from frappe.utils import flt

STATUS_DRAFT = "Draft"
STATUS_ISSUED = "Issued"
STATUS_PARTIALLY_RECEIVED = "Partially Received"
STATUS_RECEIVED = "Received"
STATUS_SUBMITTED = "Submitted"

QTY_FIELDS = ("issue_qty", "receive_qty", "loss_qty")

# Item Variant Attribute names -> the Tree Number field holding the same value.
#
# Metal COLOUR is deliberately absent. A Tree Number carries ONE metal_colour, copied from the
# first work order, but a multicolour tree legitimately holds one ledger row per colour -- live
# tree GEPL-TR-26-00109 has both M-G-18KT-75.4-Y and -P while its own colour reads "Pink". Checking
# colour would reject the Yellow row on exactly the trees the feature exists for.
# ``validate_casting_tree`` exempts colour for the same reason.
METAL_ATTRIBUTE_FIELDS = {
	"Metal Type": "metal_type",
	"Metal Touch": "metal_touch",
	"Metal Purity": "metal_purity",
}


def qty_precision():
	"""Qty precision for the ledger -- pinned to ``Stock Entry Detail.transfer_qty`` (3).

	Read from the live DocField rather than hardcoded so the ledger, the Stock Entries it
	mirrors and the test assertions all move together if the field is ever re-pinned.
	"""
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def pending_eps():
	"""Tolerance for 'fully consumed' comparisons: half the smallest representable qty.

	0.0005 at precision 3. Without it ``receive + loss == issue`` lands ``pending_qty`` on
	floating-point dust (``3 - 2.9 - 0.1`` is ~8e-17, a hair ABOVE zero) and a strict
	``pending <= 0`` would never flip a tree to "Received".
	"""
	return (10 ** -qty_precision()) / 2


def _zero_normalised(value):
	"""Collapse ``-0.0`` to ``0.0`` so the ledger never stores a negative zero."""
	return 0.0 if value == 0 else value


def _get(obj, field, default=None):
	"""Read a field off a Frappe Document/child row or a plain object.

	The helpers are called both with real Documents and with lightweight stand-ins, so go
	through ``.get`` when it exists and fall back to attribute access otherwise.
	"""
	getter = getattr(obj, "get", None)
	if callable(getter):
		try:
			value = getter(field)
		except TypeError:
			value = None
		if value is not None:
			return value
	return getattr(obj, field, default)


def calculate_pending(issue_qty, receive_qty, loss_qty, precision=None):
	"""Pending = Issue - Receive - Loss, rounded to the ledger precision.

	Deliberately UNFLOORED: an over-drawn row must read negative so the audit and the
	operator can see it. The write paths prevent new negatives; clamping here would only
	hide the ones that already exist.
	"""
	if precision is None:
		precision = qty_precision()
	pending = flt(
		flt(issue_qty, precision)
		- flt(receive_qty, precision)
		- flt(loss_qty, precision),
		precision,
	)
	return _zero_normalised(pending)


def recompute_row_pending(row, precision=None):
	"""Set ``row.pending_qty`` from the row's own quantities."""
	row.pending_qty = calculate_pending(
		_get(row, "issue_qty"),
		_get(row, "receive_qty"),
		_get(row, "loss_qty"),
		precision,
	)
	return row.pending_qty


def row_violation(row, precision=None):
	"""How far ``receive + loss`` overshoots ``issue`` on this row (0.0 when balanced)."""
	if precision is None:
		precision = qty_precision()
	overshoot = flt(
		flt(_get(row, "receive_qty"), precision)
		+ flt(_get(row, "loss_qty"), precision)
		- flt(_get(row, "issue_qty"), precision),
		precision,
	)
	return max(0.0, overshoot)


def available_to_draw(row, precision=None):
	"""Qty still drawable from this row, floored at 0.

	The floor matters: 155 legacy rows on the live site store a negative pending. Without
	it every ordinary zero-draw receive against those trees would compute a nonsense
	"capacity" and block.
	"""
	pending = calculate_pending(
		_get(row, "issue_qty"),
		_get(row, "receive_qty"),
		_get(row, "loss_qty"),
		precision,
	)
	return max(0.0, pending)


def validate_row_balance(doc, precision=None, previous_violations=None):
	"""Enforce the ledger invariants on every ``material_details`` row.

	Non-negative quantities are absolute. The ``receive + loss <= issue`` rule is enforced
	**non-worseningly**: a row that already violated it before this save may be saved again
	unchanged, but may never violate it further. Without that allowance the 154 historically
	over-drawn rows would become unsavable, which would in turn block ``submit_tree``,
	``reverse_tree_stock_entries`` and every Employee IR cancel that touches them. New and
	worsening violations are rejected outright; the legacy ones surface through the audit.
	"""
	if precision is None:
		precision = qty_precision()
	eps = pending_eps()
	previous_violations = previous_violations or {}

	seen_items = {}
	for row in _get(doc, "material_details") or []:
		for field in QTY_FIELDS:
			if flt(_get(row, field), precision) < -eps:
				frappe.throw(
					_("Row #{0} ({1}): {2} cannot be negative ({3}).").format(
						row.idx,
						row.item_code,
						frappe.unscrub(field),
						flt(_get(row, field), precision),
					),
					title=_("Invalid Tree Material Balance"),
				)

		if row.item_code in seen_items:
			frappe.throw(
				_(
					"Item {0} appears on rows #{1} and #{2} of Tree {3}. Each material item "
					"may hold only one ledger row."
				).format(row.item_code, seen_items[row.item_code], row.idx, doc.name),
				title=_("Duplicate Tree Material Row"),
			)
		seen_items[row.item_code] = row.idx

		violation = row_violation(row, precision)
		was = flt(previous_violations.get(row.name, 0.0), precision)
		if violation - was > eps:
			frappe.throw(
				_(
					"Row #{0} ({1}): Receive Qty ({2}) plus Loss Qty ({3}) exceeds Issue Qty "
					"({4}) on Tree {5}. Issue material to the tree before receiving it back."
				).format(
					row.idx,
					row.item_code,
					flt(_get(row, "receive_qty"), precision),
					flt(_get(row, "loss_qty"), precision),
					flt(_get(row, "issue_qty"), precision),
					doc.name,
				),
				title=_("Tree Receive Exceeds Issue"),
			)


def stored_row_violations(doc, precision=None):
	"""``{row name: violation}`` as currently persisted, for the non-worsening check."""
	if not _get(doc, "name") or _get(doc, "__islocal"):
		return {}
	rows = frappe.get_all(
		"Tree Material Detail",
		filters={"parent": doc.name, "parenttype": "Tree Number"},
		fields=["name", "issue_qty", "receive_qty", "loss_qty"],
	)
	return {r.name: row_violation(r, precision) for r in rows}


def tree_status(tree):
	"""Derive the Tree Number status from the whole ledger.

	    Draft               nothing has moved at all
	    Issued              metal issued, none returned or lost yet
	    Partially Received  metal issued and partly consumed, some still pending
	    Received            metal issued and every row fully consumed

	``Submitted`` is terminal and owned by ``TreeNumber.submit_tree``; it is never derived
	here. A tree with no issued metal can never read "Received" -- that is precisely the
	defect this state machine exists to prevent.
	"""
	rows = _get(tree, "material_details") or []
	if not rows:
		return STATUS_DRAFT

	eps = pending_eps()
	precision = qty_precision()

	total_issue = sum(flt(_get(md, "issue_qty"), precision) for md in rows)
	moved = any(
		flt(_get(md, "receive_qty"), precision) > eps
		or flt(_get(md, "loss_qty"), precision) > eps
		for md in rows
	)

	if total_issue <= eps:
		# Nothing was ever issued. Without issued metal there is nothing to be "Received";
		# any receive/loss sitting here is an over-draw for the audit to flag.
		return STATUS_PARTIALLY_RECEIVED if moved else STATUS_DRAFT

	if not moved:
		return STATUS_ISSUED

	# "Received" needs EVERY row both ENGAGED (issued onto the tree) and consumed
	# (pending within dust tolerance). A never-issued seed row -- e.g. an unreceived
	# multicolour colour -- must not flip a multi-item tree to Received while its metal
	# is still to come.
	fully = all(
		flt(_get(md, "issue_qty"), precision) > eps
		and calculate_pending(
			_get(md, "issue_qty"),
			_get(md, "receive_qty"),
			_get(md, "loss_qty"),
			precision,
		)
		<= eps
		for md in rows
	)
	return STATUS_RECEIVED if fully else STATUS_PARTIALLY_RECEIVED


# ---------------------------------------------------------------------------
# Same-metal rule
# ---------------------------------------------------------------------------
def item_metal_attributes(item_codes):
	"""``{item_code: {"Metal Type": v, "Metal Touch": v, "Metal Purity": v}}``.

	One query for every item asked about -- ``TreeNumber.validate`` runs on every save, so a
	per-row lookup here would quietly become N+1. Follows the read shape already used by
	``jewellery_erpnext.query.get_item_code``.
	"""
	if isinstance(item_codes, str):
		item_codes = [item_codes]
	item_codes = [i for i in dict.fromkeys(item_codes) if i]
	if not item_codes:
		return {}

	out = {i: {} for i in item_codes}
	for row in frappe.db.get_all(
		"Item Variant Attribute",
		filters={
			"parent": ["in", item_codes],
			"attribute": ["in", list(METAL_ATTRIBUTE_FIELDS)],
		},
		fields=["parent", "attribute", "attribute_value"],
	):
		out.setdefault(row.parent, {})[row.attribute] = row.attribute_value
	return out


def _norm(value):
	"""Attribute values are Attribute Value link names -- compare as trimmed strings.

	Deliberately NOT numeric: "75.4" and "75.40" are two different Attribute Values, and coercing
	them to floats would silently accept a purity the master data treats as distinct.
	"""
	return (value or "").strip()


def tree_metal_attributes(tree):
	"""``{attribute name: value}`` the tree constrains, skipping blanks.

	Empty for the bare Tree Numbers Main Slip creates (no employee, no metal, no ledger) -- there
	is nothing to match against, so those trees are left unconstrained.
	"""
	out = {}
	for attribute, fieldname in METAL_ATTRIBUTE_FIELDS.items():
		value = _norm(_get(tree, fieldname))
		if value:
			out[attribute] = value
	return out


def validate_item_matches_tree_metal(
	tree, item_code, attributes_by_item=None, row_idx=None
):
	"""Only metal of the tree's own type / touch / purity may go onto that tree.

	Casting melts one alloy per crucible, so an item of a different touch or purity has no business
	on the tree -- and ``_ledger_row`` would silently open a NEW material_details line for it,
	turning a mis-pick into a second ledger the operator never asked for.

	An item that does not declare the attribute is rejected rather than assumed compatible: it
	cannot be shown to match. On the live site every such item is a master alloy (M-AL,
	M-Alloy 381, ...) that belongs in the melt, not on a tree.
	"""
	wanted = tree_metal_attributes(tree)
	if not wanted or not item_code:
		return

	if attributes_by_item is None:
		attributes_by_item = item_metal_attributes(item_code)
	found = attributes_by_item.get(item_code) or {}

	where = _("Row #{0}: ").format(row_idx) if row_idx else ""
	for attribute, tree_value in wanted.items():
		item_value = _norm(found.get(attribute))
		if not item_value:
			frappe.throw(
				_(
					"{0}Item {1} does not declare a {2}, so it cannot be matched against Tree {3} "
					"({2} {4}). Only metal of the same Metal Type, Metal Touch and Metal Purity may "
					"be issued to a tree."
				).format(where, item_code, attribute, _get(tree, "name"), tree_value),
				title=_("Metal Does Not Match Tree"),
			)
		if item_value != tree_value:
			frappe.throw(
				_(
					"{0}Item {1} has {2} <b>{3}</b> but Tree {4} is {2} <b>{5}</b>. Only metal of "
					"the same Metal Type, Metal Touch and Metal Purity may be issued to a tree."
				).format(
					where,
					item_code,
					attribute,
					item_value,
					_get(tree, "name"),
					tree_value,
				),
				title=_("Metal Does Not Match Tree"),
			)


def validate_material_details_metal(tree):
	"""Every ledger row must carry metal matching the tree. One query for all rows."""
	rows = _get(tree, "material_details") or []
	if not rows or not tree_metal_attributes(tree):
		return
	attributes_by_item = item_metal_attributes([r.item_code for r in rows])
	for row in rows:
		validate_item_matches_tree_metal(
			tree, row.item_code, attributes_by_item=attributes_by_item, row_idx=row.idx
		)
