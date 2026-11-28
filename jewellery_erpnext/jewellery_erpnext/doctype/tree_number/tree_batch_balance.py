# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Batch-wise view of a Tree Number's metal at its MSL warehouse. Read-only.

The tree ledger (``Tree Material Detail``) is kept per item, and that stays the unit Receive
settles in: batch identity does not survive the employee's shared MSL warehouse. Tree-tagged Work
Order draws take whichever batch the warehouse holds -- KGJPL-TR-26-00356 drew 24.669 g of a
batch it never issued -- which is why Receive pools same-ownership batches
(``tree_stock_entry._tree_fallback_batches``) instead of insisting on the tree's own.

What nobody could see was where a Receive was going to run short. "Tree Material Not Available,
short by 7.0" on KGJPL-TR-26-00361 was a WAREHOUSE shortage: the open trees at Casting KGJPL -
00294 WH were owed 7 g more than it held, and the last tree to close took the whole gap. So this
view puts two things side by side:

  * per batch: what this tree's own Stock Entries moved into and out of the MSL warehouse, what
    the warehouse holds of it now, and how much of that this tree may take;
  * per warehouse: what it holds against what every open tree there is still owed.

Shown on the Tree Number form (``tree_number.get_batch_balance``) and appended to the shortfall
error (``tree_stock_entry._batch_wise_section``). Worked out from the Stock Entries and the batch
ledger on every call; nothing is stored, so there is nothing to drift.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.ownership_priority import (
	batch_priority_map,
	batch_sort_key,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events import (
	tree_stock_entry as tse,
)
from jewellery_erpnext import tree_batch_reconciliation


def tree_batch_moves(tree_name, item_code, msl_wh):
	"""``{batch_no: (issued, taken_out)}`` -- what this tree's own Stock Entries moved into and out
	of ``msl_wh``.

	The rows ``tree_stock_entry._tree_netted_owed`` nets, kept apart and uncapped, so a batch the
	tree drew without ever issuing shows up as such instead of netting away. The tree's entries are
	read by name first: ``custom_tree_number`` has no index, and driving the join from Stock Entry
	Detail scans that whole table on every form load.
	"""
	# An unsaved tree has no entries -- and a None filter would match every untagged one.
	if not tree_name:
		return {}
	names = frappe.get_all(
		"Stock Entry",
		filters={"custom_tree_number": tree_name, "docstatus": 1},
		pluck="name",
	)
	if not names:
		return {}

	rows = frappe.db.sql(
		"""
		SELECT sed.batch_no,
			SUM(CASE WHEN sed.t_warehouse = %(msl)s THEN sed.qty ELSE 0 END) AS issued,
			SUM(CASE WHEN sed.s_warehouse = %(msl)s THEN sed.qty ELSE 0 END) AS taken_out
		FROM `tabStock Entry Detail` sed
		WHERE sed.parent IN %(names)s
		  AND sed.item_code = %(item_code)s
		  AND IFNULL(sed.batch_no, '') != ''
		  AND (sed.t_warehouse = %(msl)s OR sed.s_warehouse = %(msl)s)
		GROUP BY sed.batch_no
		""",
		{"names": names, "item_code": item_code, "msl": msl_wh},
		as_dict=True,
	)
	return {r.batch_no: (flt(r.issued), flt(r.taken_out)) for r in rows}


def _owner_label(inventory_type, customer):
	return f"{inventory_type} / {customer}" if customer else inventory_type


def batch_balance(tree, item_code, msl_wh, usable=None):
	"""One item's batch-wise balance for ``tree`` at ``msl_wh``, as a dict ``render_html`` reads.

	``rows`` holds every batch the tree moved there or the warehouse holds now, in consume order.
	``usable`` is ``{batch_no: qty}`` this tree may take: the shortfall error passes the pool the
	allocator actually offered. Without it, a batch counts in full when it shares an ownership tier
	the tree issued, the same rule ``_tree_fallback_batches`` applies -- an upper bound, since every
	open tree at the warehouse competes for the same metal. The warehouse totals say how far that
	competition is short.

	``mismatch`` marks a tree whose batch columns do not add back up to its ledger pending: some of
	its metal left the warehouse on entries not tagged to the tree, so that part cannot be shown
	per batch. Older trees, whose Work Order draws were not tagged yet, are mostly like that (231
	of 350 submitted trees on prod, 2026-10-26); every open tree with pending added up.
	"""
	prec = tse._se_precision()
	eps = tree_balance.pending_eps()

	moves = tree_batch_moves(tree.name, item_code, msl_wh)
	held = {}
	for r in tse._warehouse_batch_balances(item_code, msl_wh):
		qty = flt(r.qty, prec)
		if qty > eps:
			held[r.batch_no] = qty

	# The allocator's pool is read at the entry's posting time, so a batch can be in it that the
	# ledger balance above does not show; keep it, or its share would vanish from the total.
	batches = list(dict.fromkeys([*moves, *held, *(usable or {})]))
	ranks = batch_priority_map(batches)
	batches.sort(key=lambda b: batch_sort_key(b, ranks.get(b), "consume"))

	tiers = None
	if usable is None:
		tiers = tse._tree_issued_tiers([b for b, (i, _o) in moves.items() if i > eps])

	rows = []
	for batch_no in batches:
		meta = ranks.get(batch_no)
		owner = normalize_ownership(
			meta.inventory_type if meta else None,
			meta.customer if meta else None,
			batch_no=batch_no,
			item_code=item_code,
		)
		issued, taken_out = moves.get(batch_no, (0.0, 0.0))
		in_warehouse = held.get(batch_no, 0.0)
		if usable is not None:
			take = flt(usable.get(batch_no), prec)
		else:
			take = in_warehouse if owner in tiers else 0.0
		rows.append(
			frappe._dict(
				batch_no=batch_no,
				owner=_owner_label(*owner),
				issued=flt(issued, prec),
				taken_out=flt(taken_out, prec),
				in_warehouse=in_warehouse,
				usable=take,
			)
		)

	md = next((d for d in tree.material_details if d.item_code == item_code), None)
	pending = flt(md.pending_qty, prec) if md else 0.0
	issued_total = flt(sum(r.issued for r in rows), prec)
	taken_out_total = flt(sum(r.taken_out for r in rows), prec)
	net = flt(issued_total - taken_out_total, prec)

	open_trees = [
		(o.tree.name, flt(o.md.pending_qty, prec))
		for o in tree_batch_reconciliation._open_rows(
			msl_warehouse=msl_wh, item_code=item_code
		)
	]
	held_total = flt(sum(held.values()), prec)
	owed_total = flt(sum(p for _t, p in open_trees), prec)

	return frappe._dict(
		tree=tree.name,
		item_code=item_code,
		msl_warehouse=msl_wh,
		rows=rows,
		issued=issued_total,
		taken_out=taken_out_total,
		in_warehouse=held_total,
		usable=flt(sum(r.usable for r in rows), prec),
		pending=pending,
		net=net,
		mismatch=abs(net - pending) > eps,
		open_trees=open_trees,
		owed=owed_total,
		gap=flt(owed_total - held_total, prec),
	)


def render_html(balance):
	"""The table and the warehouse line, as HTML for a form dashboard section or an error.

	Every value is escaped: batch, tree and warehouse names are user-entered.
	"""
	prec = tse._se_precision()
	eps = tree_balance.pending_eps()
	right = '<td class="text-right">'

	def num(value):
		return escape_html(str(flt(value, prec)))

	def pill(text, colour):
		return f'<span class="indicator-pill {colour}">{escape_html(text)}</span>'

	body = "".join(
		"<tr>"
		f"<td>{escape_html(r.batch_no)}</td>"
		f"<td>{escape_html(r.owner)}</td>"
		f"{right}{num(r.issued)}</td>"
		f"{right}{num(r.taken_out)}</td>"
		f"{right}{num(r.in_warehouse)}</td>"
		f"{right}{num(r.usable)}</td>"
		"</tr>"
		for r in balance.rows
	)
	body += (
		"<tr>"
		f"<td><b>{escape_html(_('Total'))}</b></td><td></td>"
		f"{right}<b>{num(balance.issued)}</b></td>"
		f"{right}<b>{num(balance.taken_out)}</b></td>"
		f"{right}<b>{num(balance.in_warehouse)}</b></td>"
		f"{right}<b>{num(balance.usable)}</b></td>"
		"</tr>"
	)
	header = "".join(
		f'<th class="text-right">{escape_html(label)}</th>'
		for label in (
			_("Issued by this tree"),
			_("Taken out by this tree"),
			_("In warehouse now"),
			_("Usable for this tree"),
		)
	)
	table = (
		'<div style="overflow-x:auto"><table class="table table-bordered table-sm">'
		f"<thead><tr><th>{escape_html(_('Batch'))}</th>"
		f"<th>{escape_html(_('Ownership'))}</th>{header}</tr></thead>"
		f"<tbody>{body}</tbody></table></div>"
	)

	owed_line = escape_html(
		_("This tree is owed {0} of {1}.").format(
			flt(balance.pending, prec), balance.item_code
		)
	)
	if balance.mismatch:
		owed_line += " " + pill(
			_(
				"The batch columns come to {0}: part of this tree's metal left on entries not "
				"tagged to the tree"
			).format(balance.net),
			"gray",
		)

	claims = ", ".join(
		f"<b>{escape_html(t)}</b>: {num(p)}"
		if t == balance.tree
		else f"{escape_html(t)}: {num(p)}"
		for t, p in balance.open_trees
	)
	warehouse_line = escape_html(
		_("{0} holds {1}; the open trees there are owed {2} in all").format(
			balance.msl_warehouse,
			flt(balance.in_warehouse, prec),
			flt(balance.owed, prec),
		)
	)
	if claims:
		warehouse_line += f" ({claims})"
	if balance.gap > eps:
		warehouse_line += (
			". "
			+ pill(_("Short by {0}").format(flt(balance.gap, prec)), "red")
			+ " "
			+ escape_html(
				_(
					"Whichever tree receives last will be refused that much until the metal is "
					"put back into this warehouse."
				)
			)
		)
	else:
		warehouse_line += ". " + pill(_("Enough for all open trees"), "green")

	return f"{table}<p>{owed_line}</p><p>{warehouse_line}</p>"


def render_tree(tree):
	"""Every material row of ``tree`` that Receive can still settle, i.e. an open tree's rows with
	pending. Empty otherwise: there is nothing left to fall short, and on closed trees the batch
	columns mostly do not add up (see ``batch_balance``), which would read as an error."""
	if not tree.get("msl_warehouse"):
		return ""
	if tree.get("status") not in tree_batch_reconciliation.OPEN_STATUSES:
		return ""
	eps = tree_balance.pending_eps()
	blocks = []
	for md in tree.material_details:
		if not md.item_code or flt(md.pending_qty) <= eps:
			continue
		blocks.append(
			f"<p><b>{escape_html(md.item_code)}</b></p>"
			+ render_html(batch_balance(tree, md.item_code, tree.msl_warehouse))
		)
	return "".join(blocks)
