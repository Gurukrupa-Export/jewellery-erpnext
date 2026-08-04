# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

"""Casting-tree handling for Employee IR.

A "casting" operation is any Department Operation flagged ``tree_no_reqd`` (the
same flag Main Slip uses via ``is_tree_reqd``). For those operations the EIR
drives a Tree Number:

  * Issue   -> auto-create ONE Tree Number, link every MWO in the EIR to it, and
              seed the Material Details ledger with the issued metal per item.
  * Receive -> add received / loss metal to the same ledger and move the tree
              status Issued -> Partially Received -> Received.

Physical stock and loss still move through the existing EIR engine
(loss_stock_entry / main_slip_inject / MOP Log / SRE); the Tree Number's
Material Details table is a per-item qty ledger layered on top — it does NOT
create any parallel stock entries.

Grouping rules (confirmed with user):
  * One Tree == exactly the MWO set in one Casting Issue EIR.
  * All MWOs on one tree must share metal type / touch / purity / colour.
  * A casting group is re-issued all-or-nothing: an Issue EIR that touches a tree's
    ``casting_group`` must carry EVERY work order cast together. This is enforced at
    submit by ``validate_casting_group_complete`` (which offers the "Load Full Casting
    Tree" helper). A whole-group re-issue is allowed directly — regardless of the prior
    tree's received state — so there is no cancel-first requirement; a partial re-issue
    is what gets rejected (naming the missing members).
    This rule is GATED by ``MOP Settings.enforce_full_casting_tree_reissue`` and ships
    OFF, so by default a partial re-issue IS allowed; tick the box to enforce it. The
    "Load Full Casting Tree" button is available regardless of the setting.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.utils import get_item_from_attribute

METAL_ATTRS = ("metal_type", "metal_touch", "metal_purity", "metal_colour")

_SETTINGS = "MOP Settings"
_FULL_TREE_REISSUE_FLAG = "enforce_full_casting_tree_reissue"


def full_casting_tree_reissue_enforced():
	"""True when the all-or-nothing casting re-issue rule is switched ON (default OFF).

	The switch deliberately lives on the ``MOP Settings`` Single rather than site_config (the
	app's usual flag home) because managed / cloud sites cannot edit ``site_config.json`` -- an
	admin needs to be able to enable / roll back this rule from the UI WITHOUT a code change.
	``MOP Settings`` is an app-owned doctype, so the field ships via ``bench migrate``; unlike a
	custom field it needs no patch and cannot fall into the disabled-``after_migrate`` trap.

	Default OFF costs nothing to provision: a site that has never saved MOP Settings has no
	``tabSingles`` row for the field, and ``get_single_value`` casts that missing value by
	fieldtype (``cast("Check", None)`` -> ``cint(None)`` -> ``0``). The read is ``0``, not
	``None`` and not ``"0"``, so the rule is inert until an admin ticks the box -- no patch, no
	backfill, no ``create_test_data`` seeding.
	"""
	return bool(cint(frappe.db.get_single_value(_SETTINGS, _FULL_TREE_REISSUE_FLAG)))


def _se_precision():
	"""Qty precision for the tree Material Details ledger — pinned to Stock Entry Detail.transfer_qty (3).

	Thin alias kept so existing callers/tests keep working; the canonical definition lives in
	``tree_material_balance`` alongside the pending formula and the status machine.
	"""
	return tree_balance.qty_precision()


def _pending_eps():
	"""Tolerance for 'fully received' comparisons: half the smallest representable qty (0.0005 at prec 3).

	Alias for ``tree_material_balance.pending_eps``.
	"""
	return tree_balance.pending_eps()


def is_casting_eir(eir):
	"""True when the EIR's operation requires a tree (e.g. Casting)."""
	if not eir.operation:
		return False
	return bool(
		frappe.db.get_value("Department Operation", eir.operation, "tree_no_reqd")
	)


def _mwo_rows(eir):
	"""[(employee_ir_operation row, MWO doc)] for rows carrying a real MWO."""
	rows = []
	for row in eir.employee_ir_operations:
		if not row.manufacturing_work_order:
			continue
		mwo = frappe.get_cached_doc(
			"Manufacturing Work Order", row.manufacturing_work_order
		)
		rows.append((row, mwo))
	return rows


def _metal_item(mwo):
	return get_item_from_attribute(
		mwo.metal_type, mwo.metal_touch, mwo.metal_purity, mwo.metal_colour
	)


def casting_issue_qty_by_item(rows):
	"""{metal_item: sum of MWO.metal_weight} — the metal committed to a casting tree.

	The casting metal is NOT on the operation at issue time (gold is cast in later,
	during Receive, via Main Slip), so the operation/MWO ``gross_wt`` is 0. The work
	order's planned metal lives in ``MWO.metal_weight`` (fetch_from
	``master_bom.total_metal_weight``); seed the Issue ledger from that instead. A MWO
	with no BOM metal weight contributes 0 (graceful fallback).
	"""
	out = {}
	for _row, mwo in rows:
		item = _metal_item(mwo)
		if not item:
			continue
		out[item] = out.get(item, 0) + flt(mwo.metal_weight)
	return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_casting_tree(eir):
	"""Same-metal enforcement for casting (tree) EIRs.

	All work orders cast on one tree must share metal type / touch / purity / colour. The
	all-or-nothing *re-issue* rule (a casting group must move together) is NOT enforced here — it is
	a submit-time concern owned by ``validate_casting_group_complete`` (gated, default OFF). Keeping
	it out of ``validate`` lets a whole-group re-issue go through directly (regardless of the prior
	tree's received state) and keeps partial drafts saveable while the operator assembles rows; only
	a *partial* re-issue is rejected, at submit, naming the missing members.

	The same-metal rule below is NOT gated — it is a physical constraint on what can share a
	crucible, not a policy about which work orders move together. A partial re-issue permitted by
	the setting must still be metal-homogeneous.
	"""
	if not is_casting_eir(eir):
		return

	rows = _mwo_rows(eir)
	if not rows:
		return

	# All MWOs on one tree must share metal type / touch / purity / colour.
	_ref_row, ref_mwo = rows[0]
	for _row, mwo in rows[1:]:
		for attr in METAL_ATTRS:
			# multicolour work orders may legitimately differ on colour
			if attr == "metal_colour" and (
				mwo.get("multicolour") or ref_mwo.get("multicolour")
			):
				continue
			if (mwo.get(attr) or None) != (ref_mwo.get(attr) or None):
				frappe.throw(
					_(
						"All work orders on one casting tree must share the same metal. "
						"MWO <b>{0}</b> ({1}=<b>{2}</b>) does not match MWO <b>{3}</b> ({1}=<b>{4}</b>)."
					).format(
						mwo.name, attr, mwo.get(attr), ref_mwo.name, ref_mwo.get(attr)
					)
				)


def _issue_eligibility_filters(department, subcontracting):
	"""MOP filter dict for an Issue — mirrors employee_ir.js scan_mwo / get_operations.

	Uses ``frappe.get_all`` filter semantics (ifnull-aware) on purpose so this matches the
	client exactly and the "Load Full Casting Tree" button can never disagree with the
	completeness validator (both go through this one filter).
	"""
	filters = {
		"department": department,
		"operation": ["is", "not set"],
		"status": ["in", ["Not Started"]],
		"department_ir_status": ["not in", ["In-Transit", "Revert"]],
	}
	if subcontracting == "Yes":
		filters["employee"] = ["is", "not set"]
	else:
		filters["subcontractor"] = ["is", "not set"]
	return filters


def eligible_casting_group_mops(department, subcontracting, groups):
	"""Issue-eligible MOP rows for every MWO sharing one of ``groups`` (its casting tree)."""
	groups = [g for g in (groups or []) if g]
	if not groups:
		return []
	mwos = frappe.get_all(
		"Manufacturing Work Order", {"casting_group": ["in", groups]}, pluck="name"
	)
	if not mwos:
		return []
	filters = _issue_eligibility_filters(department, subcontracting)
	filters["manufacturing_work_order"] = ["in", mwos]
	return frappe.get_all(
		"Manufacturing Operation",
		filters,
		[
			"name as manufacturing_operation",
			"manufacturing_work_order",
			"gross_wt",
			"diamond_wt",
			"diamond_pcs",
			"gemstone_wt",
			"gemstone_pcs",
		],
	)


def validate_casting_group_complete(eir):
	"""No partial re-issue: an Issue touching a casting group must carry the WHOLE tree.

	Runs on submit only (wired from ``EmployeeIR.before_submit``) so partial drafts can still be
	saved while the operator scans / assembles rows. On the very first issue the MWOs have neither
	``casting_group`` nor ``tree_number`` yet (both are stamped by ``create_tree_on_issue`` on
	submit), so the check is a no-op there and only bites on a re-issue.

	When enabled this is the SOLE enforcer of the all-or-nothing re-issue rule
	(``validate_casting_tree`` no longer blocks in ``validate``), so the group key falls back to
	``tree_number`` for the (currently non-existent, but possible after a manual edit / import)
	case of a tree'd MWO that lacks a ``casting_group`` — the rule must never silently no-op for a
	work order that is on a tree.

	GATED, default OFF: fires only when ``MOP Settings.enforce_full_casting_tree_reissue`` is
	checked, so it can be enabled / rolled back WITHOUT a code change and soaked on a copy site
	first. Out of the box the rule does not run and a partial re-issue is allowed. The "Load Full
	Casting Tree" button stays available either way — it is a convenience, not a guard.
	``create_tree_on_issue`` also keeps stamping ``casting_group`` regardless of the flag, so
	grouping data accrues while the switch is off and turning it on later is not a cold start.

	Guard ORDER is load-bearing: the type / casting check runs BEFORE the settings read, never
	after. ``EmployeeIR.before_submit`` calls this for EVERY Issue EIR, casting or not, and
	``frappe.db.get_single_value`` resolves the field from meta and THROWS "Field ... does not
	exist" when it is absent. Reading the flag first would therefore break every Issue submit in
	the app on a site running this code before ``bench migrate`` installs the field; scoping first
	confines that window to casting Issues, where the feature actually lives.
	"""
	if eir.type != "Issue" or not is_casting_eir(eir):
		return

	if not full_casting_tree_reissue_enforced():
		return

	rows = _mwo_rows(eir)
	if not rows:
		return

	groups = {
		(mwo.get("casting_group") or mwo.get("tree_number"))
		for _row, mwo in rows
		if (mwo.get("casting_group") or mwo.get("tree_number"))
	}
	if not groups:
		return

	group_list = list(groups)
	# Members share either the casting_group (normal) or the tree_number (fallback); union both so a
	# group keyed by tree_number still resolves its full member set.
	required = set(
		frappe.get_all(
			"Manufacturing Work Order",
			{"casting_group": ["in", group_list]},
			pluck="name",
		)
	) | set(
		frappe.get_all(
			"Manufacturing Work Order",
			{"tree_number": ["in", group_list]},
			pluck="name",
		)
	)
	present = {
		row.manufacturing_work_order
		for row in eir.employee_ir_operations
		if row.manufacturing_work_order
	}
	missing = required - present
	if not missing:
		return

	# Split the missing members: those still at casting can be pulled in with the button; those
	# that have advanced past casting must be reversed back before this tree can be re-issued.
	addable = {
		m["manufacturing_work_order"]
		for m in eligible_casting_group_mops(eir.department, eir.subcontracting, groups)
	} & missing
	blocked = missing - addable

	parts = [
		_(
			"This casting tree must be re-issued in full — every work order cast together has to move together."
		)
	]
	if addable:
		parts.append(
			_(
				"Still at casting (use <b>Load Full Casting Tree</b> to add): <b>{0}</b>."
			).format(", ".join(sorted(addable)))
		)
	if blocked:
		parts.append(
			_(
				"Already past casting — reverse these back to casting first: <b>{0}</b>."
			).format(", ".join(sorted(blocked)))
		)
	frappe.throw(" ".join(parts), title=_("Incomplete casting-tree re-issue"))


def validate_casting_receive(eir):
	"""Guard a casting Receive EIR against the metal actually ISSUED onto its tree.

	Two independent things must hold for a gain (``received_gross_wt > gross_wt``):

	  1. It must be physically sourceable. ``inject_extra_metal_for_eir_receive`` mints the metal
	     out of the employee MSL warehouse and is gated on ``is_raw_material``; without that
	     gate nothing moves, so an unbacked gain is blocked (unchanged, long-standing rule).
	  2. It must be backed by tree stock. The gain is drawn from the very pool the tree "Issue
	     Material" button funds, so the tree must have that much outstanding. A draw with no issued
	     balance behind it is exactly the "receive without issue" defect and is blocked outright.

	A receive with no gain draws nothing from the tree and is never consulted against the ledger --
	this keeps ordinary receives working against historically over-drawn trees.

	Runs at ``validate()``; only Receive-type casting EIRs are guarded (cancel never calls validate).
	The authoritative re-check happens at submit in ``update_tree_on_receive``, under the tree row
	lock. This one is the fail-fast copy so the operator learns before pressing Submit.
	"""
	if eir.type != "Receive" or not is_casting_eir(eir):
		return

	_validate_unbacked_gain(eir)

	trees = tree_draw_by_tree(eir)
	if not trees:
		return

	for tree_name in sorted(trees):
		tree = frappe.get_doc("Tree Number", tree_name)
		for item, draw in trees[tree_name].items():
			_check_tree_draw(eir, tree, item, draw)


def _validate_unbacked_gain(eir):
	"""Block a gain that no Main Slip injection can physically source.

	Kept separate from the tree-balance guard because it is a different failure: here no metal
	moves at all, so the receive is wrong regardless of what the tree holds.
	"""
	if cint(getattr(eir, "is_raw_material", 0)):
		return

	eps = _pending_eps()
	for row in eir.employee_ir_operations:
		gain = flt(row.received_gross_wt) - flt(row.gross_wt)
		if gain > eps:
			frappe.throw(
				_(
					"Row #{0} ({1}): received {2} exceeds the operation's gross weight {3}, but this "
					"operation has no Main Slip to source the extra {4} from. Reduce the received "
					"weight."
				).format(
					getattr(row, "idx", "?"),
					row.manufacturing_work_order,
					flt(row.received_gross_wt),
					flt(row.gross_wt),
					flt(gain, _se_precision()),
				),
				title=_("Unbacked Gain"),
			)


def _tree_ledger_row(tree, item_code):
	"""The single ``material_details`` row for ``item_code``; never guess, never append.

	Silently skipping a missing row (the old behaviour) hid real mismatches: the metal was drawn
	from the tree but nothing was recorded against it.
	"""
	matches = [md for md in tree.material_details if md.item_code == item_code]
	if not matches:
		frappe.throw(
			_(
				"Tree {0} has no Material Details row for item {1}, so the metal drawn from it "
				"cannot be recorded. Add the item to the tree's Material Details first."
			).format(tree.name, item_code),
			title=_("Tree Material Item Missing"),
		)
	if len(matches) > 1:
		frappe.throw(
			_(
				"Tree {0} has {1} Material Details rows for item {2}. The receive cannot be "
				"attributed unambiguously — merge them into one row."
			).format(tree.name, len(matches), item_code),
			title=_("Ambiguous Tree Material Item"),
		)
	return matches[0]


def _check_tree_draw(eir, tree, item_code, draw):
	"""Throw unless ``draw`` fits within what the tree still has outstanding for ``item_code``."""
	eps = _pending_eps()
	prec = _se_precision()
	draw = flt(draw, prec)
	if draw <= eps:
		# Nothing is being taken from the tree — do not consult the ledger at all.
		return None

	md = _tree_ledger_row(tree, item_code)
	available = tree_balance.available_to_draw(md, prec)
	if draw - available <= eps:
		return md

	mwos = sorted(
		{
			row.manufacturing_work_order
			for row in eir.employee_ir_operations
			if row.manufacturing_work_order
		}
	)
	gross = sum(flt(row.gross_wt) for row in eir.employee_ir_operations)
	received = sum(flt(row.received_gross_wt) for row in eir.employee_ir_operations)
	frappe.throw(
		_(
			"Cannot receive {0} against Tree Number <b>{1}</b> (item <b>{2}</b>) because only {3} "
			"has been issued to the tree and is still outstanding.<br><br>"
			"Employee IR: <b>{4}</b><br>"
			"Work Order(s): <b>{5}</b><br>"
			"Gross weight on the operation(s): {6}<br>"
			"Received gross weight: {7}<br>"
			"Issued to tree: {8} &nbsp; Already received: {9} &nbsp; Loss: {10} &nbsp; "
			"Available: {11}<br><br>"
			"Issue the material to the tree before submitting this Employee IR Receive."
		).format(
			draw,
			tree.name,
			item_code,
			flt(available, prec),
			getattr(eir, "name", "") or "-",
			", ".join(mwos) or "-",
			flt(gross, prec),
			flt(received, prec),
			flt(md.issue_qty, prec),
			flt(md.receive_qty, prec),
			flt(md.loss_qty, prec),
			flt(available, prec),
		),
		title=_("Tree Material Not Issued"),
	)


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------
def create_tree_on_issue(eir):
	"""Create one Tree Number for the casting issue and link its MWOs."""
	if not is_casting_eir(eir):
		return None

	rows = _mwo_rows(eir)
	if not rows:
		return None

	_ref_row, ref_mwo = rows[0]

	tree = frappe.new_doc("Tree Number")
	tree.company = eir.company
	tree.manufacturer = eir.manufacturer
	tree.department = eir.department
	tree.operation = eir.operation
	tree.employee = eir.employee
	tree.employee_ir = eir.name
	# Status is derived from the ledger, never asserted. A freshly seeded tree holds no metal
	# yet (the rows below start at 0), so it is "Draft" until the Issue Material button funds it
	# — "Issued" now means "metal is on this tree", which is the whole point of the invariant.
	tree.status = tree_balance.STATUS_DRAFT
	# Pre-fill the Issue source with the department Raw Material warehouse so it is visible when the
	# operator opens the tree, instead of only being lazily back-filled on first Issue. Best-effort /
	# non-throwing (a missing dept RM warehouse must not block the casting Issue) — the strict
	# resolver runs later on the Issue-button path (tree_stock_entry._resolve_source_warehouse).
	tree.source_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"department": eir.department,
			"warehouse_type": "Raw Material",
			"disabled": 0,
			"is_group": 0,
		},
		"name",
	)
	for attr in METAL_ATTRS:
		tree.set(attr, ref_mwo.get(attr))

	# List the metal-item rows so the operator sees what to issue; issue_qty starts at 0
	# and is filled by the Tree Number "Issue Material" button (button-owned ledger for
	# casting). MWO.metal_weight (via casting_issue_qty_by_item) stays the planned reference.
	for item in casting_issue_qty_by_item(rows):
		tree.append(
			"material_details",
			{
				"item_code": item,
				"issue_qty": 0,
				"receive_qty": 0,
				"loss_qty": 0,
				"pending_qty": 0,
			},
		)

	tree.flags.ignore_permissions = True
	tree.insert()

	# One stable, opaque group id for everything cast together. Reuse an existing group if any
	# row already carries one (a re-issue); otherwise the fresh tree's name becomes the group.
	# Coalescing onto a single id — rather than only stamping empty rows — keeps a physically
	# single tree from carrying two group ids when a re-issued MWO is cast beside a brand-new one.
	group_id = (
		next(
			(
				mwo.get("casting_group")
				for _row, mwo in rows
				if mwo.get("casting_group")
			),
			None,
		)
		or tree.name
	)
	for _row, mwo in rows:
		updates = {"tree_number": tree.name}
		if mwo.get("casting_group") != group_id:
			updates["casting_group"] = group_id
		frappe.db.set_value("Manufacturing Work Order", mwo.name, updates)

	return tree.name


def unlink_tree_on_issue_cancel(eir):
	"""Cancelling a casting issue releases all its MWOs and removes the tree."""
	if not is_casting_eir(eir):
		return

	tree_name = frappe.db.get_value("Tree Number", {"employee_ir": eir.name}, "name")

	if tree_name:
		tree = frappe.get_doc("Tree Number", tree_name)
		has_activity = any(
			flt(r.receive_qty) or flt(r.loss_qty) for r in tree.material_details
		)
		if has_activity or tree.status in (
			"Partially Received",
			"Received",
			"Submitted",
		):
			frappe.throw(
				_(
					"Cannot cancel the issue for tree <b>{0}</b>: material has already "
					"been received. Cancel the receive Employee IR(s) first."
				).format(tree_name)
			)

	for row in eir.employee_ir_operations:
		if not row.manufacturing_work_order:
			continue
		current = frappe.db.get_value(
			"Manufacturing Work Order", row.manufacturing_work_order, "tree_number"
		)
		if current and (not tree_name or current == tree_name):
			frappe.db.set_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				"tree_number",
				None,
			)

	if tree_name:
		# The Issue Material button may have created physical Dept->MSL Stock Entries stamped
		# with this tree; cancel them so they aren't orphaned when the tree is deleted.
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
			cancel_tree_stock_entries,
		)

		cancel_tree_stock_entries(tree_name)
		frappe.delete_doc("Tree Number", tree_name, ignore_permissions=True, force=True)


# ---------------------------------------------------------------------------
# Receive
# ---------------------------------------------------------------------------
def _row_tree_and_item(row):
	"""(tree_name, metal_item) for one Employee IR Operation row.

	Prefers the tree pinned on the row at receive submit. Falling back to the live
	``MWO.tree_number`` is only safe going forward: ``create_tree_on_issue`` overwrites it on a
	re-issue, so a cancel that re-resolved it could reverse against a *different* tree — inflating
	one ledger while corrupting another.
	"""
	if not row.manufacturing_work_order:
		return None, None
	mwo = frappe.get_cached_doc(
		"Manufacturing Work Order", row.manufacturing_work_order
	)
	tree_name = getattr(row, "tree_number", None) or mwo.get("tree_number")
	if not tree_name:
		return None, None
	return tree_name, _metal_item(mwo)


def tree_draw_by_tree(eir):
	"""``{tree_name: {metal_item: qty}}`` — metal this receive draws OUT of each tree.

	The draw is the per-row gain::

	    draw_row = max(received_gross_wt - gross_wt, 0)

	which is exactly what ``inject_extra_metal_for_eir_receive`` mints out of the employee MSL
	(Raw Material) warehouse — the same pool the tree "Issue Material" button funds. Metal already
	on the operation (``gross_wt``) was never in the tree, and booked metal loss never leaves the
	MSL pool (with ``is_raw_material`` the Process Loss SE returns the metal item straight
	back into that warehouse), so neither is charged to the tree.

	Clamped PER ROW, never on the aggregate: the injection is minted inside the per-row loop, so
	``sum(max(...))`` is the real quantity moved. ``max(sum(...))`` would net one work order's gain
	against another's shortfall and under-charge the tree.

	Returns magnitudes only — the cancel path negates the *result*, never the inputs (negating
	first would push every gain through ``max(negative, 0)`` and silently reverse nothing).
	"""
	if not cint(getattr(eir, "is_raw_material", 0)):
		# No injection runs, so no metal leaves the MSL pool.
		return {}
	if (getattr(eir, "subcontracting", "No") or "No") == "Yes":
		# The injection sources from the SUBCONTRACTOR's Raw Material warehouse, a pool the tree
		# never owned (a tree's MSL is resolved from its Employee). Charging it would be fiction.
		return {}

	eps = _pending_eps()
	prec = _se_precision()
	trees = {}
	for row in eir.employee_ir_operations:
		tree_name, item = _row_tree_and_item(row)
		if not tree_name or not item:
			continue
		draw = flt(flt(row.received_gross_wt) - flt(row.gross_wt), prec)
		if draw <= eps:
			continue
		bucket = trees.setdefault(tree_name, {})
		bucket[item] = flt(bucket.get(item, 0.0) + draw, prec)
	return trees


def pin_tree_numbers_on_receive(eir):
	"""Stamp the resolved tree onto each operation row so cancel reverses the right one."""
	for row in eir.employee_ir_operations:
		if getattr(row, "tree_number", None):
			continue
		tree_name, _item = _row_tree_and_item(row)
		if tree_name:
			row.db_set("tree_number", tree_name, update_modified=False)


def update_tree_on_receive(eir, cancel=False):
	"""Apply (or reverse) this receive's tree draw on each linked Tree Number + set status.

	Only ``receive_qty`` is written. ``issue_qty`` stays button-owned and ``loss_qty`` belongs to
	the tree's own Receive/Submit legs, which already cap themselves at pending — so reversal is
	exact and the two paths can never double-count each other.
	"""
	if not is_casting_eir(eir):
		return

	trees = tree_draw_by_tree(eir)
	if not trees:
		return

	prec = _se_precision()

	# Deterministic order (lock_order RULE A) so two concurrent receives touching the same trees
	# take them in the same sequence.
	for tree_name in sorted(trees):
		lock_tree(tree_name)
		tree = frappe.get_doc("Tree Number", tree_name)
		# A manually-submitted tree is terminal; a casting receive must not silently reopen it.
		if tree.status == tree_balance.STATUS_SUBMITTED:
			frappe.throw(
				_(
					"Tree {0} is submitted (locked); no further receive can be booked against it. "
					"Cancel the tree's submission before receiving more material."
				).format(tree_name),
				title=_("Tree Locked"),
			)

		for item, draw in trees[tree_name].items():
			if cancel:
				# Reverse the magnitude computed at sign=+1. No availability check: giving metal
				# back to the tree is a credit, never a draw.
				md = _tree_ledger_row(tree, item)
				md.receive_qty = max(0.0, flt(flt(md.receive_qty) - flt(draw), prec))
			else:
				md = _check_tree_draw(eir, tree, item, draw)
				if md is None:
					continue
				md.receive_qty = flt(flt(md.receive_qty) + flt(draw), prec)

		tree.status = _tree_status(tree)
		tree.flags.ignore_permissions = True
		tree.save()


def lock_tree(tree_name):
	"""Take the Tree Number row lock (canonical position 1, before Series/Bin).

	``lock_order`` puts the parent control row first in the acquisition sequence. Locking the tree
	only at save time — after the Stock Entries have already taken Series and Bin locks — is the
	interleaving that produces MariaDB 1213, so callers acquire this up front.
	"""
	return frappe.db.get_value(
		"Tree Number", tree_name, "name", for_update=True, order_by=None
	)


def lock_trees_for_eir(eir):
	"""Lock every tree this Employee IR touches, in name order, before any Series/Bin lock."""
	if not is_casting_eir(eir):
		return []
	names = sorted(
		{
			tree
			for tree, _item in (
				_row_tree_and_item(row) for row in eir.employee_ir_operations
			)
			if tree
		}
	)
	for name in names:
		lock_tree(name)
	return names


def _tree_status(tree):
	"""Tree status from the whole ledger — see ``tree_material_balance.tree_status``."""
	return tree_balance.tree_status(tree)
