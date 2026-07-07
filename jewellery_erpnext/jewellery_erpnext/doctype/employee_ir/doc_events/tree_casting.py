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
  * An MWO can belong to only one *active* (not yet fully Received) tree. To redo
    a tree you cancel its issue EIR — which releases all its MWOs — and re-issue
    every work order together. A single MWO cannot be issued onto an existing tree.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.utils import get_item_from_attribute

METAL_ATTRS = ("metal_type", "metal_touch", "metal_purity", "metal_colour")


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
	"""Same-metal enforcement + atomic-issue guard for casting (tree) EIRs."""
	if not is_casting_eir(eir):
		return

	rows = _mwo_rows(eir)
	if not rows:
		return

	# 1) All MWOs on one tree must share metal type / touch / purity / colour.
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

	# 2) Atomic issue: an MWO may belong to only one active (non-Received) tree.
	if eir.type == "Issue":
		for _row, mwo in rows:
			existing_tree = frappe.db.get_value(
				"Manufacturing Work Order", mwo.name, "tree_number"
			)
			if not existing_tree:
				continue
			status = frappe.db.get_value("Tree Number", existing_tree, "status")
			if status and status != "Received":
				frappe.throw(
					_(
						"MWO <b>{0}</b> is already issued to tree <b>{1}</b> ({2}). "
						"Cancel that tree's issue Employee IR and re-issue all work orders "
						"together — a single work order cannot be issued onto an existing tree."
					).format(mwo.name, existing_tree, status)
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

	# Every MWO in this batch gets the same tree_number -> one UPDATE.
	mwo_names = [mwo.name for _row, mwo in rows]
	if mwo_names:
		frappe.db.set_value(
			"Manufacturing Work Order",
			{"name": ["in", mwo_names]},
			"tree_number",
			tree.name,
		)

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
		if has_activity or tree.status in ("Partially Received", "Received"):
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
def update_tree_on_receive(eir, cancel=False):
	"""Add (or reverse) received / loss metal on each linked tree + set status."""
	if not is_casting_eir(eir):
		return

	sign = -1 if cancel else 1
	mwo_loss = _mwo_loss_dict(eir)

	# Group receive contributions: tree -> item -> {recv, loss}
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
		agg = bucket.setdefault(item, {"recv": 0, "loss": 0})
		agg["recv"] += sign * flt(row.received_gross_wt)
		agg["loss"] += sign * flt(mwo_loss.get(row.manufacturing_work_order, 0))

	for tree_name, items in trees.items():
		tree = frappe.get_doc("Tree Number", tree_name)
		for md in tree.material_details:
			delta = items.get(md.item_code)
			if not delta:
				continue
			md.receive_qty = flt(md.receive_qty) + delta["recv"]
			md.loss_qty = flt(md.loss_qty) + delta["loss"]
			md.pending_qty = flt(md.issue_qty) - flt(md.receive_qty) - flt(md.loss_qty)
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
	fully = all(flt(md.pending_qty) <= 0 for md in tree.material_details)
	return "Received" if fully else "Partially Received"
