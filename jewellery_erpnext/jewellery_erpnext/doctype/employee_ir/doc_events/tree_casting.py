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
	"""Qty precision for the tree Material Details ledger — pinned to Stock Entry Detail.transfer_qty (3)."""
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def _pending_eps():
	"""Tolerance for 'fully received' comparisons: half the smallest representable qty (0.0005 at prec 3).

	Single source of truth for the receive/loss tolerance used by the over-receive caps AND by
	``_tree_status``. Without it, ``received + loss == issued`` lands ``pending_qty`` on
	floating-point dust (e.g. ``3 - 2.9 - 0.1`` ≈ 8e-17, a hair ABOVE zero), so a strict
	``pending_qty <= 0`` would never flip the tree to "Received".
	"""
	return (10 ** -_se_precision()) / 2


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


def _mwo_loss_dict(eir):
	"""Total booked metal loss per MWO from both loss tables."""
	loss = {}
	for row in (eir.manually_book_loss_details or []) + (
		eir.employee_loss_details or []
	):
		if row.variant_of in ("M", "F"):
			loss.setdefault(row.manufacturing_work_order, 0)
			loss[row.manufacturing_work_order] += flt(row.proportionally_loss)
	return loss


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
	"""Guard a casting Receive EIR against the metal COMMITTED to its work orders (the gross weight).

	The tree's "issued" baseline is the operations' ``gross_wt`` — the metal the work orders were
	expected to return — NOT the button-owned ``issue_qty``. So an operator can receive back the
	committed metal without first pressing the tree "Issue Material" button:

	  * ``received <= gross`` (normal receive, with loss): always allowed (``recv + loss == gross``).
	  * ``received > gross`` (a gain): the excess ``received - gross`` is drawn from the tree. It is
	    physically sourced by ``inject_extra_metal_for_eir_receive`` (gated ``is_main_slip_required``);
	    without a Main Slip there is nothing to source it from, so an unbacked gain is blocked.
	  * ``recv <= gross`` but ``recv + loss > gross``: a genuine loss over-booking — still blocked.

	Runs at ``validate()``; only Receive-type casting EIRs are guarded (cancel never calls validate).
	"""
	if eir.type != "Receive" or not is_casting_eir(eir):
		return

	trees = _aggregate_receive_by_tree(eir, sign=1)
	if not trees:
		return

	eps = _pending_eps()
	main_slip_backed = bool(cint(getattr(eir, "is_main_slip_required", 0)))

	for tree_name, items in trees.items():
		for item, delta in items.items():
			recv, loss, gross = (
				flt(delta["recv"]),
				flt(delta["loss"]),
				flt(delta["gross"]),
			)
			if recv <= eps and loss <= eps:
				continue
			if recv - gross > eps:
				# GAIN: received more than committed. The excess is drawn from the tree and must be
				# physically sourced by the Main Slip injection; block an unbacked gain.
				if not main_slip_backed:
					frappe.throw(
						_(
							"Tree {0}, Item {1}: received {2} exceeds the committed gross weight {3}, "
							"and this operation has no Main Slip to draw the extra from the tree. "
							"Reduce the received weight."
						).format(tree_name, item, recv, gross)
					)
				continue
			if (recv + loss) - gross > eps:
				# received <= gross, but loss pushes the total over committed -> loss over-booked.
				frappe.throw(
					_(
						"Tree {0}, Item {1}: this receive books {2} (receive + loss) but only {3} "
						"(gross weight committed to the work orders) is available. Reduce the loss weight."
					).format(tree_name, item, recv + loss, gross)
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
	tree.status = "Issued"
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
def _aggregate_receive_by_tree(eir, sign=1):
	"""{tree_name: {metal_item: {"recv": qty, "loss": qty, "gross": qty}}} for this EIR's receive.

	Groups each operation row's ``received_gross_wt``, booked metal loss (``_mwo_loss_dict``) and the
	committed ``gross_wt`` (the metal the work orders were expected to return) by the MWO's tree +
	metal item. ``sign=-1`` reverses the contribution (cancel path). Shared by
	``validate_casting_receive`` (pre-submit guard) and ``update_tree_on_receive`` (ledger apply).
	``gross`` is the tree's "issued" baseline: a normal receive (received <= gross) satisfies
	``recv + loss == gross``; a gain (received > gross) draws the excess from the tree.
	"""
	mwo_loss = _mwo_loss_dict(eir)
	trees = {}
	for row in eir.employee_ir_operations:
		if not row.manufacturing_work_order:
			continue
		mwo = frappe.get_cached_doc(
			"Manufacturing Work Order", row.manufacturing_work_order
		)
		tree_name = mwo.get("tree_number")
		if not tree_name:
			continue
		item = _metal_item(mwo)
		if not item:
			continue
		bucket = trees.setdefault(tree_name, {})
		agg = bucket.setdefault(item, {"recv": 0, "loss": 0, "gross": 0})
		agg["recv"] += sign * flt(row.received_gross_wt)
		agg["loss"] += sign * flt(mwo_loss.get(row.manufacturing_work_order, 0))
		agg["gross"] += sign * flt(row.gross_wt)
	return trees


def update_tree_on_receive(eir, cancel=False):
	"""Add (or reverse) received / loss metal on each linked tree + set status."""
	if not is_casting_eir(eir):
		return

	sign = -1 if cancel else 1
	trees = _aggregate_receive_by_tree(eir, sign=sign)

	eps = _pending_eps()

	for tree_name, items in trees.items():
		tree = frappe.get_doc("Tree Number", tree_name)
		# A manually-submitted (locked) tree accepts no further receive/cancel — the manual
		# Submit is terminal, so a casting EIR receive must not silently reopen it.
		if tree.status == "Submitted":
			frappe.throw(
				_(
					"Tree {0} is submitted (locked); cannot book receive/loss against it. "
					"Reopen the tree first if this is intended."
				).format(tree_name)
			)
		for md in tree.material_details:
			delta = items.get(md.item_code)
			if not delta:
				continue
			# Defense-in-depth (forward path only): re-check against the committed gross baseline so a
			# concurrent Receive EIR that passed validate() independently cannot book a loss over the
			# committed metal. Mirrors validate_casting_receive: received<=gross is always fine, and a
			# gain (recv>gross) is left to the injection to source. Cancel (sign=-1) is skipped — its
			# deltas are negative and must reverse freely.
			if not cancel:
				recv, loss, gross = (
					flt(delta["recv"]),
					flt(delta["loss"]),
					flt(delta["gross"]),
				)
				if recv - gross <= eps and (recv + loss) - gross > eps:
					frappe.throw(
						_(
							"Tree {0}, Item {1}: this receive books {2} (receive + loss) but only {3} "
							"(gross weight committed) is available. Reduce the loss weight."
						).format(tree_name, md.item_code, recv + loss, gross)
					)
			md.receive_qty = flt(md.receive_qty) + delta["recv"]
			md.loss_qty = flt(md.loss_qty) + delta["loss"]
			# Effective issued baseline = max(button issue_qty, receive + loss): issue_qty stays
			# button-owned, so a never-button-issued tree floors to pending 0 (fully received, the
			# committed metal drawn from the tree) instead of a phantom negative. Cancel-safe because
			# issue_qty is never written here — reversing receive/loss recomputes pending exactly.
			md.pending_qty = max(
				0.0, flt(md.issue_qty) - flt(md.receive_qty) - flt(md.loss_qty)
			)
		tree.status = _tree_status(tree)
		tree.flags.ignore_permissions = True
		tree.save()


def _tree_status(tree):
	if not tree.material_details:
		return "Issued"
	received = any(
		flt(md.receive_qty) or flt(md.loss_qty) for md in tree.material_details
	)
	if not received:
		return "Issued"
	# "Received" requires EVERY row to be ENGAGED (issued via the button OR received/lost — the
	# committed metal can be drawn from the tree without a button issue, so issue_qty=0 with a
	# receive is a valid fully-received state) AND consumed (pending_qty <= eps). A never-touched
	# seed row (all zeros — e.g. an unreceived multicolour colour) must NOT flip a multi-item tree
	# to Received. The eps tolerance (same one the over-receive cap uses) makes
	# "received + loss == issued" read as fully received instead of getting stuck on float dust.
	eps = _pending_eps()
	fully = all(
		(flt(md.issue_qty) > 0 or flt(md.receive_qty) > 0 or flt(md.loss_qty) > 0)
		and flt(md.pending_qty) <= eps
		for md in tree.material_details
	)
	return "Received" if fully else "Partially Received"
