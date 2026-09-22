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

	The fallback is applied ONLY where the pin is NULL, which is what keeps the module
	docstring's re-issue concern intact: a re-issue overwrites ``MWO.tree_number``, so
	following it blindly would credit a receive to whichever tree the work order sits on
	today. But a row with no pin necessarily predates pinning, so there is no pinned
	value being overridden -- the work order's tree is the only evidence left. Rows
	resolved that way are counted and logged rather than silently absorbed, because on a
	work order that HAS since been re-issued the fallback is a best guess.
	"""
	return frappe.db.sql(
		"""
		SELECT
			COALESCE(eiro.tree_number, mwo.tree_number) AS tree_number,
			eiro.tree_number AS pinned_tree_number,
			eiro.manufacturing_work_order,
			eiro.gross_wt,
			eiro.received_gross_wt,
			eir.is_raw_material,
			eir.subcontracting
		FROM `tabEmployee IR Operation` eiro
		INNER JOIN `tabEmployee IR` eir ON eir.name = eiro.parent
		LEFT JOIN `tabManufacturing Work Order` mwo
			ON mwo.name = eiro.manufacturing_work_order
		WHERE eiro.parenttype = 'Employee IR'
		  AND eir.docstatus = 1
		  AND eir.type = 'Receive'
		  AND COALESCE(eiro.tree_number, mwo.tree_number) IN %(trees)s
		""",
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
			# Attributed through the work order because the row predates pinning.
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
	stats = {"fallback_rows": 0, "unresolved_items": 0}

	# Chunked so neither IN clause grows without bound on a large site, and so a long
	# run commits as it goes instead of holding one enormous transaction.
	updated = 0
	for start in range(0, len(tree_names), CHUNK):
		updated += _split_chunk(
			tree_names[start : start + CHUNK], prec, eps, item_cache, stats
		)
		frappe.db.commit()

	# Never let the coverage caveats vanish into a success message: a fallback-attributed
	# row is a best guess, and an unresolved metal item is a receive that could not be
	# split at all (its draw stays in manual_receive_qty).
	summary = (
		f"backfill_tree_receive_split: split {updated} ledger row(s) "
		f"across {len(tree_names)} tree(s); "
		f"{stats['fallback_rows']} pre-pinning row(s) attributed via "
		f"Manufacturing Work Order.tree_number; "
		f"{stats['unresolved_items']} row(s) had no resolvable metal item"
	)
	frappe.logger().info(summary)
	print(summary)
