# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Keep hand-made Stock Entries from draining metal that open Tree Numbers are still owed.

A casting tree's MSL warehouse is the *employee's*, shared by every tree for that operator, and
the tree ledger (``Tree Material Detail.pending_qty``) is only a claim on it. The tree's own legs
(Issue / Receive / Process Loss, all stamped ``custom_tree_number``) and the casting Employee IR
draws keep stock and claims moving together. A manual Material Transfer out of that warehouse does
not: it lowers the stock and leaves every claim standing, and the shortfall surfaces much later on
whichever tree happens to close last -- as "Tree Material Not Available" on a tree that did nothing
wrong, which can then neither Receive nor be written off through ``submit_tree``.

Observed in production: MAT-STE-66673 moved 20.86 g of M-G-22KT-91.75-Y from
"Casting KGJPL - 00294 WH - KGJPL" back to Waxing RM, MAT-STE-75009 put back 40.074 g, and the
warehouse was left 7.0 g short of its open trees until KGJPL-TR-26-00361 hit it on Receive.

So a MANUAL entry (``auto_created`` unset, no tree, no Employee IR) may move only the metal at
such a warehouse that no open tree is owed. Submitting a move out, or cancelling a move in, that
would leave less than the open trees' pending is refused. Inflows always pass -- they are how a
short warehouse is topped back up. System flows are left alone: they carry their own ledgers.

Compared per ``(warehouse, item)`` in stock UOM, the same unit
``tree_batch_reconciliation.report`` calls the warehouse gap. Ownership tiers are not split here.

Both sides of the comparison must be current, not this transaction's REPEATABLE READ snapshot. The
snapshot is taken early (loading the entry), so a tree Issue committed since then -- stock AND
pending into the same warehouse -- would show in the locked Bin but not in a plain read of the
claims, and its newly owed metal would look free. Every tree flow posts its stock under the
warehouse's Bin before it writes ``pending_qty``, and the guard runs holding that Bin, so the
committed claims cannot move under it; they only have to be read past the snapshot. Hence the
claims are a locking read, ``NOWAIT`` because it comes after the Bin (lock_order: tree before Bin):
it never waits, so it cannot close a cycle with a tree flow, and a row being written right now
refuses the entry as busy instead.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking import (
	is_msl_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)

# Statuses that still owe metal back. "Submitted" is terminal and "Draft" has not issued yet.
OPEN_STATUSES = (
	tree_balance.STATUS_ISSUED,
	tree_balance.STATUS_PARTIALLY_RECEIVED,
	tree_balance.STATUS_RECEIVED,
)


def guard_tree_pool(doc, method=None):
	"""``before_submit``: refuse a manual entry that would take tree-owed metal out of an MSL.

	Must run after ``prelock_bins``: the Bin it reads is then already held FOR UPDATE, so two
	concurrent manual entries cannot both pass against the same stock.
	"""
	_check(doc, cancelling=False)


def guard_tree_pool_on_cancel(doc, method=None):
	"""``before_cancel``: same rule for the reversal -- cancelling a manual move INTO an MSL takes
	that metal back out. Must run after ``prelock_bins_on_cancel`` for the same reason."""
	_check(doc, cancelling=True)


def _is_manual(doc):
	return not (
		cint(doc.get("auto_created"))
		or doc.get("custom_tree_number")
		or doc.get("employee_ir")
	)


def _net_reductions(doc, cancelling):
	"""``{(warehouse, item_code): qty}`` this entry takes OUT, net of what it puts back in.

	Netted per pair so a row that moves metal within one warehouse (or out and straight back)
	is not mistaken for a draw. Positive magnitudes only.
	"""
	sign = -1 if cancelling else 1
	net = {}
	for row in doc.get("items") or []:
		qty = flt(row.get("transfer_qty")) or flt(row.get("qty")) * flt(
			row.get("conversion_factor") or 1
		)
		if row.get("s_warehouse"):
			key = (row.s_warehouse, row.item_code)
			net[key] = net.get(key, 0.0) - sign * qty
		if row.get("t_warehouse"):
			key = (row.t_warehouse, row.item_code)
			net[key] = net.get(key, 0.0) + sign * qty

	eps = tree_balance.pending_eps()
	return {key: -q for key, q in net.items() if q < -eps}


def _msl_index_name():
	"""Name of the index on ``Tree Number.msl_warehouse`` (``search_index``), or ``None`` before
	``migrate`` has added it. Resolved by column: Frappe 16 names it ``<field>_index``."""
	try:
		index = frappe.db.get_column_index("tabTree Number", "msl_warehouse")
	except Exception:
		return None
	return index.get("Key_name") if index else None


def _open_tree_claims(pairs):
	"""``{(warehouse, item_code): [(tree, pending)]}`` still owed by open trees at those pairs.

	Only MSL warehouses are read. Which warehouses those are comes from the Warehouse (employee +
	Raw Material, the rule a tree's first Issue resolves its ``msl_warehouse`` by), never from the
	trees themselves: a snapshot showing no tree there yet would otherwise skip a warehouse a tree
	has just started issuing into.

	A ``LOCK IN SHARE MODE NOWAIT`` read over the ``msl_warehouse`` index range: it returns the
	latest committed rows, never waits (it runs after the Bin locks; see the module docstring),
	and raises ``QueryTimeoutError`` on a row another transaction is writing. FORCE INDEX and
	STRAIGHT_JOIN keep the shared locks on these warehouses' trees and their material rows: left
	to itself the optimizer drives from a full scan of ``tabTree Material Detail`` and would hold
	every tree's rows on the site until commit. Without the index (before ``migrate``) it falls
	back to the old plain read.
	"""
	warehouses = sorted({wh for wh, _item in pairs if is_msl_warehouse(wh)})
	if not warehouses:
		return {}

	index = _msl_index_name()
	force_index, lock = (
		(f"FORCE INDEX (`{index}`)", "LOCK IN SHARE MODE NOWAIT") if index else ("", "")
	)
	rows = frappe.db.sql(
		f"""
		SELECT tn.msl_warehouse, tmd.item_code, tn.name AS tree, tmd.pending_qty
		FROM `tabTree Number` tn {force_index}
		STRAIGHT_JOIN `tabTree Material Detail` tmd ON tmd.parent = tn.name
		WHERE tn.status IN %(statuses)s
		  AND tn.msl_warehouse IN %(warehouses)s
		  AND tmd.item_code IN %(items)s
		  AND tmd.pending_qty > %(eps)s
		ORDER BY tn.name
		{lock}
		""",
		{
			"statuses": OPEN_STATUSES,
			"warehouses": tuple(warehouses),
			"items": tuple({item for _wh, item in pairs}),
			"eps": tree_balance.pending_eps(),
		},
		as_dict=True,
	)
	claims = {}
	for r in rows:
		key = (r.msl_warehouse, r.item_code)
		if key in pairs:
			claims.setdefault(key, []).append((r.tree, flt(r.pending_qty)))
	return claims


def _check(doc, cancelling):
	if not _is_manual(doc):
		return

	reductions = _net_reductions(doc, cancelling)
	if not reductions:
		return

	try:
		claims = _open_tree_claims(reductions)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		frappe.throw(
			_(
				"A Tree Number at {0} is being updated by another user right now. Try again in a "
				"moment."
			).format(
				", ".join(
					frappe.bold(wh) for wh in sorted({wh for wh, _i in reductions})
				)
			),
			title=_("Busy"),
		)
	if not claims:
		return

	prec = tree_balance.qty_precision()
	eps = tree_balance.pending_eps()
	for (warehouse, item_code), trees in sorted(claims.items()):
		owed = flt(sum(q for _t, q in trees), prec)
		# Locking read: the Bin is already ours (prelock), and only a locking read sees the latest
		# committed qty rather than this transaction's older snapshot.
		actual = flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": item_code, "warehouse": warehouse},
				"actual_qty",
				for_update=True,
			),
			prec,
		)
		out = flt(reductions[(warehouse, item_code)], prec)
		if flt(actual - out, prec) >= flt(owed - eps, prec):
			continue

		action = (
			_("Cancelling {0} would take {1} of {2} back out of {3}.")
			if cancelling
			else _("{0} would take {1} of {2} out of {3}.")
		).format(doc.name, frappe.bold(out), item_code, frappe.bold(warehouse))
		frappe.throw(
			action
			+ " "
			+ _(
				"The warehouse holds {0}, and open trees are still owed {1} of it ({2}), so at most "
				"{3} can be moved by hand. Return a tree's metal with Receive Material on the tree "
				"instead."
			).format(
				actual,
				frappe.bold(owed),
				", ".join(f"{t}: {flt(q, prec)}" for t, q in trees),
				frappe.bold(max(flt(actual - owed, prec), 0.0)),
			),
			title=_("Tree Metal Would Go Short"),
		)
