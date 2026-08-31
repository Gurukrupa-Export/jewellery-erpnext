# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Read-only projection of whether each open Tree Number can still close.

Companion to :mod:`tree_audit`, which checks the ledger's internal arithmetic. This one checks
the ledger against PHYSICAL stock: can the metal a tree is still owed actually be handed back
out of the employee's MSL warehouse, and from which batches?

The failure it exists to explain: an MSL warehouse is the *employee's*, shared by every tree for
that operator. All of them issue into it, and the untagged ``Material Transfer (WORK ORDER)``
draws consume it oldest-first, so an early tree's batch is routinely taken to zero by later
trees' operations -- each of which correctly drew its own share. Before the same-ownership-tier
fallback existed (``tree_stock_entry._tree_fallback_batches``) such a tree was permanently stuck:
it could neither Receive nor be written off through ``submit_tree``, because both settle through
the same batch allocator.

**This utility never writes.** There is no repair mode and it is not wired into any patch. What
it produces is the projection you read BEFORE letting operators run Receive, so you know which
trees close cleanly, which need a substitute batch, and which are genuinely short of metal and
must settle the remainder as loss.

Trees are simulated in name order against ONE shared pool, because that is how they really
compete: the first tree to receive takes the oldest batch. A tree shown as short is short only
under that ordering -- the aggregate shortfall for the warehouse is the number that does not
move, and it is reported separately.

Usage::

    bench --site <site> execute jewellery_erpnext.tree_batch_reconciliation.report
    bench --site <site> execute jewellery_erpnext.tree_batch_reconciliation.report \\
        --kwargs "{'msl_warehouse': 'Casting KGJPL - 00294 WH - KGJPL'}"
"""

import frappe
from frappe.utils import flt, nowdate, nowtime

from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
	capped_auto_batch_nos,
)
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

# Statuses that still owe metal back. "Submitted" is terminal and "Draft" has not issued yet.
OPEN_STATUSES = ("Issued", "Partially Received", "Received")

CLOSES_FROM_OWN = "CLOSES_FROM_OWN_BATCHES"
CLOSES_WITH_SUBSTITUTE = "CLOSES_WITH_SUBSTITUTE_BATCH"
SHORT_OF_METAL = "SHORT_OF_METAL"


def _open_rows(msl_warehouse=None, item_code=None, company=None):
	"""Every open ``(tree, item)`` still carrying pending, newest-first by warehouse."""
	filters = {"status": ["in", OPEN_STATUSES]}
	if msl_warehouse:
		filters["msl_warehouse"] = msl_warehouse
	if company:
		filters["company"] = company

	trees = frappe.get_all(
		"Tree Number",
		filters=filters,
		fields=["name", "status", "msl_warehouse", "company", "employee", "operation"],
		order_by="name",
	)
	if not trees:
		return []

	by_name = {t.name: t for t in trees}
	md_filters = {"parent": ["in", list(by_name)]}
	if item_code:
		md_filters["item_code"] = item_code

	eps = tree_balance.pending_eps()
	rows = []
	for md in frappe.get_all(
		"Tree Material Detail",
		filters=md_filters,
		fields=[
			"parent",
			"item_code",
			"issue_qty",
			"receive_qty",
			"loss_qty",
			"pending_qty",
		],
		order_by="parent, idx",
	):
		if flt(md.pending_qty) <= eps:
			continue
		tree = by_name[md.parent]
		if not tree.msl_warehouse:
			continue
		rows.append(frappe._dict(tree=tree, md=md))
	return rows


def _physical_pool(item_code, msl_warehouse):
	"""``[(batch_no, qty)]`` actually available at the warehouse, consume-ordered."""
	available = (
		capped_auto_batch_nos(
			frappe._dict(
				posting_date=nowdate(),
				posting_time=nowtime(),
				item_code=item_code,
				warehouse=msl_warehouse,
				for_stock_levels=False,
				consider_negative_batches=False,
			)
		)
		or []
	)
	pool = [
		(b.batch_no, flt(b.qty)) for b in available if b.batch_no and flt(b.qty) > 0
	]
	ranks = batch_priority_map([b for b, _q in pool])
	pool.sort(key=lambda bq: batch_sort_key(bq[0], ranks.get(bq[0]), "consume"))
	return pool, ranks


def _owner_of(batch_no, ranks, item_code):
	meta = ranks.get(batch_no)
	return normalize_ownership(
		meta.inventory_type if meta else None,
		meta.customer if meta else None,
		batch_no=batch_no,
		item_code=item_code,
	)


def report(msl_warehouse=None, item_code=None, company=None):
	"""Print the projection and return it as a list of dicts. Never writes."""
	rows = _open_rows(msl_warehouse, item_code, company)
	if not rows:
		print("No open Tree Number rows with pending qty for that filter.")
		return []

	eps = tree_balance.pending_eps()
	prec = tse._se_precision()

	# One shared pool per (warehouse, item) -- that is the unit the trees really compete over.
	groups = {}
	for r in rows:
		groups.setdefault((r.tree.msl_warehouse, r.md.item_code), []).append(r)

	out = []
	for (wh, item), members in sorted(groups.items()):
		pool, ranks = _physical_pool(item, wh)
		remaining = {b: q for b, q in pool}
		physical_total = flt(sum(remaining.values()), prec)
		pending_total = flt(sum(flt(m.md.pending_qty) for m in members), prec)

		print("")
		print("=" * 100)
		print(f"{wh}  |  {item}")
		print(
			f"  physical available {physical_total}   "
			f"open tree pending {pending_total}   "
			f"warehouse gap {flt(pending_total - physical_total, prec)}"
		)
		print("-" * 100)
		print(
			f"{'tree':<22}{'status':<20}{'pending':>10}{'own':>10}"
			f"{'substitute':>12}{'short':>10}  verdict"
		)

		for m in sorted(members, key=lambda x: x.tree.name):
			tree, md = m.tree, m.md
			need = flt(md.pending_qty, prec)

			owned = tse._tree_netted_owed(tree, md.item_code, wh)
			tiers = tse._tree_issued_tiers(owned)

			# Pass 1: the tree's own batches, capped at what is left of the shared pool.
			from_own = {}
			left = need
			for batch_no in sorted(
				owned, key=lambda b: batch_sort_key(b, ranks.get(b), "consume")
			):
				if left <= eps:
					break
				take = flt(
					min(flt(owned[batch_no]), flt(remaining.get(batch_no, 0.0)), left),
					prec,
				)
				if take <= eps:
					continue
				from_own[batch_no] = take
				remaining[batch_no] = flt(
					flt(remaining.get(batch_no, 0.0)) - take, prec
				)
				left = flt(left - take, prec)

			# Pass 2: same-ownership-tier substitutes from whatever the pool still holds.
			from_sub = {}
			for batch_no, _q in pool:
				if left <= eps:
					break
				# NOT `batch_no in from_own`: the residual of the tree's own batch is same-tier
				# metal like any other, and `remaining` has already had the own draw deducted.
				if flt(remaining.get(batch_no, 0.0)) <= eps:
					continue
				if _owner_of(batch_no, ranks, item) not in tiers:
					continue
				take = flt(min(flt(remaining[batch_no]), left), prec)
				if take <= eps:
					continue
				from_sub[batch_no] = take
				remaining[batch_no] = flt(flt(remaining[batch_no]) - take, prec)
				left = flt(left - take, prec)

			own_qty = flt(sum(from_own.values()), prec)
			sub_qty = flt(sum(from_sub.values()), prec)
			short = flt(left, prec)

			if short > eps:
				verdict = SHORT_OF_METAL
			elif sub_qty > eps:
				verdict = CLOSES_WITH_SUBSTITUTE
			else:
				verdict = CLOSES_FROM_OWN

			print(
				f"{tree.name:<22}{tree.status:<20}{need:>10}{own_qty:>10}"
				f"{sub_qty:>12}{short:>10}  {verdict}"
			)
			if from_sub:
				print(
					"".ljust(22)
					+ "substitutes: "
					+ ", ".join(f"{b}: {q}" for b, q in sorted(from_sub.items()))
				)

			out.append(
				{
					"tree": tree.name,
					"status": tree.status,
					"item_code": md.item_code,
					"msl_warehouse": wh,
					"pending": need,
					"from_own_batches": from_own,
					"from_substitute_batches": from_sub,
					"shortfall": short,
					"verdict": verdict,
				}
			)

		leftover = flt(sum(v for v in remaining.values() if v > eps), prec)
		print("-" * 100)
		print(
			f"  unclaimed metal left at this warehouse after all trees close: {leftover}"
		)

	short_total = flt(sum(r["shortfall"] for r in out), prec)
	subs = [r for r in out if r["verdict"] == CLOSES_WITH_SUBSTITUTE]
	print("")
	print("=" * 100)
	print(
		f"{len(out)} open rows | {len(subs)} need a substitute batch | "
		f"total metal that cannot be returned: {short_total}"
	)
	print("This is a projection only. Nothing was written.")
	return out
