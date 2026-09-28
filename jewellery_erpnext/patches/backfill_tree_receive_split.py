"""Backfill the provenance split of ``Tree Material Detail.receive_qty``.

``receive_qty`` records metal drawn back out of a casting tree, from two different
places: a casting Employee IR receive (the work-order gain, ``tree_casting``) and
the tree's own **Receive Material** button (``tree_stock_entry``). Both write the
same column, so a tree could not show which half was which. Three columns now carry
that, and this seeds them for trees that existed before the split:

    wo_receive_qty        the Employee IR half, recomputed from the receives
    manual_receive_qty    receive_qty - wo_receive_qty (whatever is left)
    wo_received_gross_wt  the full received gross weight of those Employee IR rows

``wo_receive_qty`` is recomputed from the source documents rather than guessed:
per submitted Receive ``Employee IR Operation`` row pinned to the tree, the draw is
``max(received_gross_wt - gross_wt, 0)``, clamped per row, exactly as
``tree_casting.tree_draw_by_tree`` computes it live. ``wo_received_gross_wt`` sums
``received_gross_wt`` over the same rows.

Two deliberate limits, both mirroring the live code:

  * The draw is only charged to the tree when the Employee IR had ``is_raw_material``
    set and was not subcontracted -- otherwise no metal left the tree's MSL pool. The
    gross weight is summed regardless, because the work order was received against
    the tree either way.
  * Rows are attributed exactly the way the live code does it
    (``tree_casting._row_tree_and_item``): ``Employee IR Operation.tree_number``
    first, then ``Manufacturing Work Order.tree_number`` where that pin is NULL --
    which means the row predates pinning (deb5a71e, 2026-07-29) and no pinned value
    is being overridden. Attributing on the pin alone silently inverted the split for
    every receive submitted between 55eee77a (2026-06-26, when ``receive_qty`` credits
    began) and pinning. Fallback-attributed rows are counted and reported, since on a
    work order re-issued since, the work order's current tree is a best guess.

``manual_receive_qty`` is a residual, floored at 0. On a healthy ledger it is exactly
the button's contribution; on one of the historically over-drawn rows the code
comments warn about it may floor, which is the honest answer -- there is no record
that would let us do better, and the residual must never go negative.

Writes child rows with ``frappe.db.set_value`` and never saves the parent:
``TreeNumber.validate`` would re-run ``validate_row_balance`` against those
over-drawn rows and recompute status on trees this patch has no business touching.
The three columns are informational and feed none of the ledger arithmetic.

Idempotent -- every value is recomputed from source, not incremented.

Ad-hoc, optionally bounded so a large site can be done in batches::

    bench --site <site> execute jewellery_erpnext.patches.backfill_tree_receive_split.execute
    bench --site <site> execute jewellery_erpnext.patches.backfill_tree_receive_split.execute \\
        --kwargs "{'from_date': '2026-01-01', 'to_date': '2026-06-30'}"
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)


def _receive_rows(tree_names):
	"""Submitted casting-Receive EIR rows belonging to any of ``tree_names``.

	Gated on ``Department Operation.tree_no_reqd`` because that is the LIVE eligibility
	rule: ``update_tree_on_receive`` opens with ``if not is_casting_eir(eir): return``,
	and ``is_casting_eir`` is exactly this flag on ``Employee IR.operation``. A receive
	booked at any other operation never touched a tree column, so the backfill must not
	invent one for it.

	The pin is NOT evidence of a casting receive and cannot stand in for this gate.
	``employee_ir.on_submit`` calls ``pin_tree_numbers_on_receive`` for EVERY Receive,
	ungated -- it stamps provenance for Stock Entry lineage, not tree arithmetic -- and
	that helper falls back to ``MWO.tree_number``. So a Pre Polish or Final Polish
	receive on a work order still carrying its casting tree gets ``eiro.tree_number``
	pinned to that tree. Without this join those rows read as pinned casting receives:
	their ``received_gross_wt`` inflates ``wo_received_gross_wt``, and on an EIR with
	``is_raw_material`` set their gain inflates ``wo_receive_qty`` -- neither of which
	the live path would ever have written. They would not even show in the fallback
	count, since the pin is present.

	Attribution mirrors the LIVE rule in ``tree_casting._row_tree_and_item``:
	``Employee IR Operation.tree_number`` first, then the work order's ``tree_number``.
	Matching the live rule is the whole point -- if the patch attributes more narrowly
	than the code that maintains these columns, the split it seeds disagrees with every
	subsequent write.

	Reading the pin alone is what made that happen. ``Employee IR Operation.tree_number``
	and ``pin_tree_numbers_on_receive`` arrived together in deb5a71e (2026-07-29), but
	``update_tree_on_receive`` has credited ``receive_qty`` since 55eee77a (2026-06-26).
	Every casting receive submitted in that five-week window has a NULL pin, so a
	pin-only query finds nothing for it, computes a draw of 0, and dumps the entire
	work-order draw into ``manual_receive_qty`` -- labelling a work-order receive as a
	tree-button return, the exact inverse of the truth. The halves still sum, so the
	dashboard's "unsplit" check cannot catch it either.

	A row with no pin necessarily predates pinning, and for those the live
	``MWO.tree_number`` is NOT reliable: a re-issue overwrites it, so following it blindly
	credits an old receive to whichever tree the work order sits on today. Three sources are
	therefore tried in order of how well each can be proven.

	  1. ``Employee IR Operation.tree_number`` -- the pin. Immutable once submitted.
	  2. The casting ISSUE that put this work order on a tree. ``create_tree_on_issue`` sets
	     ``Tree Number.employee_ir`` to the Issue EIR, and that document's own rows name every
	     work order cast on it. Both are submitted records, so unlike ``MWO.tree_number`` they
	     do not move when the work order is later re-issued. Where a work order has been cast
	     more than once, the most recent tree created at or before this receive is the one it
	     was on at the time -- which is precisely the re-issue case the fallback got wrong.
	  3. ``MWO.tree_number``, only when no issue record names the work order at all. Still a
	     best guess, still counted and logged separately, but now reached only by rows for
	     which no submitted evidence survives.

	Leaving an unresolved row out entirely is NOT an option, and is worse than a guess:
	``manual_receive_qty`` is derived as ``receive_qty - wo_receive_qty``
	(``tree_material_balance.derive_manual_receive``), so a row that is skipped does not
	abstain -- its whole quantity lands in ``manual_receive_qty``, labelling a work-order
	receive as a tree-button return. There is no third bucket to park it in.
	"""
	# The casting issue that put this work order on a tree, as of this receive. Correlated
	# rather than joined: it must be evaluated per row against that row's own receive date,
	# and it only ever runs for the minority of rows with no pin.
	issue_tree = """(
			SELECT t.name
			FROM `tabTree Number` t
			INNER JOIN `tabEmployee IR Operation` ti
				ON ti.parent = t.employee_ir
			   AND ti.parenttype = 'Employee IR'
			WHERE ti.manufacturing_work_order = eiro.manufacturing_work_order
			  AND t.creation <= eir.modified
			ORDER BY t.creation DESC
			LIMIT 1
		)"""

	return frappe.db.sql(
		"""
		SELECT
			COALESCE(eiro.tree_number, {issue_tree}, mwo.tree_number) AS tree_number,
			eiro.tree_number AS pinned_tree_number,
			{issue_tree} AS issue_tree_number,
			eiro.manufacturing_work_order,
			eiro.gross_wt,
			eiro.received_gross_wt,
			eir.is_raw_material,
			eir.subcontracting
		FROM `tabEmployee IR Operation` eiro
		INNER JOIN `tabEmployee IR` eir ON eir.name = eiro.parent
		INNER JOIN `tabDepartment Operation` dop ON dop.name = eir.operation
		LEFT JOIN `tabManufacturing Work Order` mwo
			ON mwo.name = eiro.manufacturing_work_order
		WHERE eiro.parenttype = 'Employee IR'
		  AND eir.docstatus = 1
		  AND eir.type = 'Receive'
		  AND dop.tree_no_reqd = 1
		  AND COALESCE(eiro.tree_number, {issue_tree}, mwo.tree_number) IN %(trees)s
		""".format(issue_tree=issue_tree),
		{"trees": tuple(tree_names)},
		as_dict=True,
	)


def _metal_item(mwo_name, cache):
	"""The metal item a work order's ledger row is keyed on, memoised."""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
		_metal_item as resolve,
	)

	if mwo_name not in cache:
		try:
			cache[mwo_name] = resolve(
				frappe.get_cached_doc("Manufacturing Work Order", mwo_name)
			)
		except Exception:
			# A work order that no longer resolves cannot be attributed; the receive
			# it belonged to still shows in receive_qty, it just cannot be split.
			cache[mwo_name] = None
	return cache[mwo_name]


CHUNK = 200


def _tree_names(from_date, to_date):
	filters = {}
	if from_date and to_date:
		filters["posting_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["posting_date"] = [">=", from_date]
	elif to_date:
		filters["posting_date"] = ["<=", to_date]
	return frappe.get_all("Tree Number", filters=filters, pluck="name")


def _split_chunk(tree_names, prec, eps, item_cache, stats):
	"""Recompute and write the three columns for one batch of trees."""
	# {tree: {metal_item: [draw, gross]}}
	totals = {}
	for row in _receive_rows(tree_names):
		if not row.manufacturing_work_order:
			continue
		if not row.pinned_tree_number:
			if row.issue_tree_number:
				# Reconstructed from the casting Issue that owned this work order.
				stats["reconstructed_rows"] += 1
			else:
				# No submitted issue names it; the live work order tree is all that is left.
				stats["fallback_rows"] += 1
		item = _metal_item(row.manufacturing_work_order, item_cache)
		if not item:
			stats["unresolved_items"] += 1
			continue

		bucket = totals.setdefault(row.tree_number, {}).setdefault(item, [0.0, 0.0])

		# Same gate as tree_draw_by_tree: without the Main Slip injection, and for a
		# subcontracted receive, no metal leaves this tree's pool.
		if cint(row.is_raw_material) and (row.subcontracting or "No") != "Yes":
			draw = flt(flt(row.received_gross_wt) - flt(row.gross_wt), prec)
			if draw > eps:
				bucket[0] = flt(bucket[0] + draw, prec)

		gross = flt(row.received_gross_wt, prec)
		if gross > 0:
			bucket[1] = flt(bucket[1] + gross, prec)

	ledger = frappe.get_all(
		"Tree Material Detail",
		filters={"parenttype": "Tree Number", "parent": ["in", tree_names]},
		fields=["name", "parent", "item_code", "receive_qty"],
	)

	for md in ledger:
		draw, gross = totals.get(md.parent, {}).get(md.item_code, (0.0, 0.0))
		# Never claim more of receive_qty for the work-order half than the column holds.
		wo_receive = min(flt(draw, prec), flt(md.receive_qty, prec))
		manual = max(0.0, flt(flt(md.receive_qty) - wo_receive, prec))

		frappe.db.set_value(
			"Tree Material Detail",
			md.name,
			{
				"wo_receive_qty": wo_receive,
				"manual_receive_qty": manual,
				"wo_received_gross_wt": flt(gross, prec),
			},
			update_modified=False,
		)

	return len(ledger)


def execute(from_date=None, to_date=None):
	tree_names = _tree_names(from_date, to_date)
	if not tree_names:
		frappe.logger().info("backfill_tree_receive_split: no trees in range")
		return

	prec = tree_balance.qty_precision()
	eps = tree_balance.pending_eps()
	item_cache = {}
	stats = {"reconstructed_rows": 0, "fallback_rows": 0, "unresolved_items": 0}

	# Chunked so neither IN clause grows without bound on a large site, and so a long
	# run commits as it goes instead of holding one enormous transaction.
	updated = 0
	for start in range(0, len(tree_names), CHUNK):
		updated += _split_chunk(
			tree_names[start : start + CHUNK], prec, eps, item_cache, stats
		)
		frappe.db.commit()

	# Never let the coverage caveats vanish into a success message. A reconstructed row is
	# attributed from submitted evidence; a fallback row is still a best guess; an unresolved
	# metal item is a receive that could not be split at all (its draw stays in
	# manual_receive_qty).
	summary = (
		f"backfill_tree_receive_split: split {updated} ledger row(s) "
		f"across {len(tree_names)} tree(s); "
		f"{stats['reconstructed_rows']} pre-pinning row(s) reconstructed from the "
		f"casting Issue; "
		f"{stats['fallback_rows']} row(s) attributed via "
		f"Manufacturing Work Order.tree_number (best guess); "
		f"{stats['unresolved_items']} row(s) had no resolvable metal item"
	)
	frappe.logger().info(summary)
	print(summary)
