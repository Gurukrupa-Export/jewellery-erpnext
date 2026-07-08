"""Button-driven Issue / Receive Material for a Tree Number.

Implements the documented *Tree Number Material Tracking* flow. Two tree kinds:

  * **Standalone** tree (``employee_ir`` empty): both buttons post physical stock.
      - Issue Material   -> plain ``Material Transfer`` SE: Source WH -> MSL WH; ``issue_qty`` +=.
      - Receive Material -> plain ``Material Transfer`` SE: received MSL -> Dept RM,
                            operator loss MSL -> Dept Scrap; ``receive_qty`` / ``loss_qty`` +=.
  * **Casting** tree (``employee_ir`` set): the Issue button posts the physical Source -> MSL
    transfer and owns ``issue_qty``; the Receive button is **RECORD-ONLY** — it updates
    ``receive_qty`` and auto-books the remaining pending as **dust** (``loss_qty``), capped at
    the issued qty, but posts NO Stock Entry. The Casting Employee IR still performs the physical
    receive (Main-Slip injection + process loss) and keeps the manufacturing weight ledger; the
    EIR-side ``update_tree_on_receive`` is skipped (``employee_ir.py``) so the button owns the
    tree ledger with no double-count.

Notes:
  * **Ledger-invisible SEs.** The SE type is plain ``Material Transfer`` (NOT the ``(WORK ORDER)``
    variant), absent from MOP Settings' ``Stock Entry Type To Reservation``, so
    ``doc_events/stock_entry.onsubmit`` skips reservation + MOP Log. ``auto_created = 1`` also
    bypasses the WORK-ORDER / metal-property validations in ``before_validate``.
  * Warehouse resolution reuses existing app resolvers (``Warehouse`` custom fields
    ``employee`` / ``department``); MSL is the employee's Raw-Material warehouse.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	_apply_fifo_batches_to_stock_entry,
	_resolve_department_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
	_tree_status,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	_get_department_rm_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins,
	preallocate_series_for_docs,
)

MATERIAL_TRANSFER = "Material Transfer"


# ---------------------------------------------------------------------------
# Warehouse resolution
# ---------------------------------------------------------------------------
def _is_casting_tree(tree):
	"""EIR-seeded casting trees (employee_ir set) run the button-owned casting flow:
	the Receive button auto-books the remaining pending as dust. Standalone trees
	(employee_ir empty) keep the operator-entered loss behaviour."""
	return bool(tree.get("employee_ir"))


def _resolve_msl_warehouse(tree):
	"""The tree's MSL warehouse = the employee's Raw-Material warehouse."""
	if not tree.employee:
		frappe.throw(
			_(
				"Set Employee on Tree {0} to resolve its MSL (Raw Material) warehouse."
			).format(tree.name)
		)
	wh = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "employee": tree.employee, "warehouse_type": "Raw Material"},
		"name",
	)
	if not wh:
		frappe.throw(
			_("No Raw Material (MSL) warehouse found for Employee {0}.").format(
				tree.employee
			)
		)
	return wh


def _resolve_source_warehouse(tree, source_warehouse=None):
	"""Issue source: explicit arg -> tree.source_warehouse -> dept Manufacturing WH."""
	source_wh = source_warehouse or tree.get("source_warehouse")
	if not source_wh:
		if not tree.department:
			frappe.throw(
				_("Set Department on Tree {0} to resolve a Source warehouse.").format(
					tree.name
				)
			)
		source_wh = _resolve_department_warehouse(tree.department)
	if not source_wh:
		frappe.throw(
			_(
				"Could not resolve a Source warehouse (Department {0} Manufacturing)."
			).format(tree.department)
		)
	return source_wh


def _resolve_scrap_warehouse(department):
	"""Dept Scrap warehouse for the loss leg; require exactly one per department."""
	if not department:
		frappe.throw(_("Department is required to resolve a Scrap warehouse."))
	rows = frappe.db.get_all(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Scrap"},
		["name"],
	)
	if not rows:
		frappe.throw(
			_("No Scrap warehouse found for Department {0}.").format(department)
		)
	if len(rows) > 1:
		frappe.throw(
			_(
				"Multiple Scrap warehouses found for Department {0}: {1}. "
				"Configure exactly one Scrap warehouse per department."
			).format(department, ", ".join(r.name for r in rows))
		)
	return rows[0].name


# ---------------------------------------------------------------------------
# Stock Entry / ledger helpers
# ---------------------------------------------------------------------------
def _new_transfer_se(tree):
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = MATERIAL_TRANSFER
	se.purpose = "Material Transfer"
	se.company = tree.company
	se.custom_tree_number = tree.name
	se.auto_created = 1
	return se


def _append_item(se, item_code, qty, s_warehouse, t_warehouse):
	se.append(
		"items",
		{
			"item_code": item_code,
			"qty": qty,
			"s_warehouse": s_warehouse,
			"t_warehouse": t_warehouse,
			"uom": "Gram",
			"use_serial_batch_fields": 1,
		},
	)


def _ledger_row(tree, item_code):
	for md in tree.material_details:
		if md.item_code == item_code:
			return md
	return tree.append(
		"material_details",
		{
			"item_code": item_code,
			"issue_qty": 0,
			"receive_qty": 0,
			"loss_qty": 0,
			"pending_qty": 0,
		},
	)


def _recompute_pending(md):
	md.pending_qty = flt(md.issue_qty) - flt(md.receive_qty) - flt(md.loss_qty)


def _se_precision():
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


# ---------------------------------------------------------------------------
# Public operations (called from the Tree Number controller methods)
# ---------------------------------------------------------------------------
def issue_material(tree, item_code, qty, source_warehouse=None):
	"""Issue `qty` of `item_code` from the Source WH into the tree's MSL WH."""
	frappe.has_permission("Tree Number", "write", tree, throw=True)

	if not item_code:
		frappe.throw(_("Select an Item to issue."))
	qty = flt(qty, _se_precision())
	if qty <= 0:
		frappe.throw(
			_("Issue Qty must be greater than zero (precision {0}).").format(
				_se_precision()
			)
		)

	source_wh = _resolve_source_warehouse(tree, source_warehouse)
	msl_wh = tree.get("msl_warehouse") or _resolve_msl_warehouse(tree)
	if source_wh == msl_wh:
		frappe.throw(
			_("Source and MSL warehouse cannot be the same ({0}).").format(source_wh)
		)

	se = _new_transfer_se(tree)
	_append_item(se, item_code, qty, source_wh, msl_wh)

	# Canonical lock order: series (pos 2) -> bins (pos 3) before the stock posting.
	preallocate_series_for_docs(se)
	lock_bins([(item_code, source_wh), (item_code, msl_wh)])

	_apply_fifo_batches_to_stock_entry(se)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	md = _ledger_row(tree, item_code)
	md.issue_qty = flt(md.issue_qty) + qty
	_recompute_pending(md)
	# Back-fill the resolved warehouses (None-safe: the fields may be unmigrated on a site
	# that hasn't run `bench migrate` yet; frappe ignores unknown-field sets on save).
	if not tree.get("source_warehouse"):
		tree.source_warehouse = source_wh
	if not tree.get("msl_warehouse"):
		tree.msl_warehouse = msl_wh
	tree.status = _tree_status(tree)
	tree.save(ignore_permissions=True)
	return se.name


def receive_material(tree, rows):
	"""Receive / book loss for the given `rows` (list of {item_code, receive_qty, loss_qty}).

	Standalone trees: operator enters both receive_qty and loss_qty.
	Casting trees (employee_ir set): operator enters receive_qty only; the remaining
	``pending - receive`` is auto-booked as **dust** (loss) to Dept-Scrap, finalizing the
	item (``pending -> 0``). ``receive_qty`` is capped at the item's pending (= issued).
	"""
	frappe.has_permission("Tree Number", "write", tree, throw=True)

	if isinstance(rows, str):
		rows = json.loads(rows)
	if not rows:
		frappe.throw(_("Nothing to receive."))

	prec = _se_precision()
	casting = _is_casting_tree(tree)
	eps = (10**-prec) / 2
	by_item = {md.item_code: md for md in tree.material_details}

	# Validate + collect intent (no side effects yet).
	plan = []
	for r in rows:
		item = r.get("item_code")
		recv = flt(r.get("receive_qty"))
		md = by_item.get(item)
		if not item or recv <= 0:
			# Casting requires a receive qty; standalone may book loss-only.
			if casting or flt(r.get("loss_qty")) <= 0:
				continue
		if not md:
			frappe.throw(
				_("Item {0} is not on this tree's Material Details.").format(item)
			)
		if casting:
			# receive can't exceed the pending (= issued − already received).
			if recv - flt(md.pending_qty) > eps:
				frappe.throw(
					_("Item {0}: Receive ({1}) exceeds pending/issued ({2}).").format(
						item, recv, md.pending_qty
					)
				)
			# Everything not received becomes dust for this item.
			loss = max(flt(md.pending_qty) - recv, 0.0)
		else:
			loss = flt(r.get("loss_qty"))
			if (recv + loss) - flt(md.pending_qty) > eps:
				frappe.throw(
					_(
						"Item {0}: Receive ({1}) + Loss ({2}) exceeds Pending ({3})."
					).format(item, recv, loss, md.pending_qty)
				)
		plan.append({"md": md, "item": item, "recv": recv, "loss": loss})

	if not plan:
		frappe.throw(_("Enter a Receive Qty on at least one item."))

	se_name = None
	if not casting:
		# Standalone trees: post the physical Material Transfer (MSL -> Dept RM, loss -> Scrap).
		# Casting trees are RECORD-ONLY here — the Employee IR Receive already moves the physical
		# cast metal and keeps the manufacturing weight ledger; this button only records the tree
		# ledger (receive / dust), and update_tree_on_receive is skipped on the EIR side.
		msl_wh = tree.get("msl_warehouse") or _resolve_msl_warehouse(tree)
		dept_rm = _get_department_rm_warehouse(tree.department)
		scrap_wh = None
		if any(flt(p["loss"], prec) > 0 for p in plan):
			scrap_wh = _resolve_scrap_warehouse(tree.department)

		se = _new_transfer_se(tree)
		bin_pairs = []
		for p in plan:
			recv = flt(p["recv"], prec)
			loss = flt(p["loss"], prec)
			if recv > 0:
				_append_item(se, p["item"], recv, msl_wh, dept_rm)
				bin_pairs += [(p["item"], msl_wh), (p["item"], dept_rm)]
			if loss > 0:
				_append_item(se, p["item"], loss, msl_wh, scrap_wh)
				bin_pairs += [(p["item"], msl_wh), (p["item"], scrap_wh)]

		if not se.get("items"):
			frappe.throw(
				_(
					"Receive / Loss quantities all round to zero at precision {0}."
				).format(prec)
			)

		preallocate_series_for_docs(se)
		lock_bins(bin_pairs)

		_apply_fifo_batches_to_stock_entry(se)
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		se_name = se.name

	for p in plan:
		md = p["md"]
		md.receive_qty = flt(md.receive_qty) + p["recv"]
		md.loss_qty = flt(md.loss_qty) + p["loss"]
		_recompute_pending(md)
	tree.status = _tree_status(tree)
	tree.save(ignore_permissions=True)
	return se_name


def cancel_tree_stock_entries(tree):
	"""Cancel every submitted Material Transfer SE this tree created (reversal/cleanup)."""
	tree_name = tree.name if hasattr(tree, "name") else tree
	names = frappe.db.get_all(
		"Stock Entry",
		{"custom_tree_number": tree_name, "auto_created": 1, "docstatus": 1},
		pluck="name",
	)
	for name in names:
		frappe.get_doc("Stock Entry", name).cancel()
	return names
