"""Customer Goods Return — the "Settle" button.

A customer-goods-return Material Request transfers the customer's metal out of
``set_from_warehouse``. When that warehouse does not hold enough of the customer's
material at the required purity, the "Settle" button sources the shortfall INTO it,
exactly the way the ``Create SNC`` flow settles borrowed gold:

  * **Case 1 — already there.** ``set_from_warehouse`` holds enough. Nothing to do.
  * **Case 2 — same purity elsewhere.** Transfer the customer's gold in from the other
    warehouse, then transfer an equal quantity of REGULAR (company) gold back to it, so
    that warehouse ends whole in quantity — it lent the customer's gold and got company
    gold in its place.
  * **Case 3 — only a different purity elsewhere.** Convert that purity to the required
    one (customer gold) and, mirrored with regular gold, keep every purity/warehouse
    physically balanced, then the Case 2 transfer + reverse.

Scope is deliberately narrow (confirmed with the user): Settle ONLY tops up
``set_from_warehouse``. It does not submit the Material Request and does not create the
In-Transit / End-Transit entries — the user drives those with the existing buttons.

Like ``create_snc``, this runs synchronously on the button click and goes through the
canonical lock-order helpers. It reuses the SNC owner-batch finder (with its per-run
allocation guard) and the pre-submit consumable-balance assertion.
"""

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.sub_utils.snc import (
	PURITY_PRIORITY,
	_append_item,
	_consumable_batch_qty_map,
	_get_gold_items_for_purity,
	_get_item_purity,
	_get_purity_label,
	_get_raw_material_warehouses,
	_submit_consuming_stock_entry,
	find_owner_rm_warehouse,
)

CUSTOMER_GOODS = "Customer Goods"
REGULAR_STOCK = "Regular Stock"

# Warehouse-to-warehouse metal moves within the company. ``auto_created = 1`` makes
# CustomStockEntry.update_batches a no-op (no FIFO refetch, no department gate), so the
# batches we pre-fill move as-is; _submit_consuming_stock_entry still asserts the
# consumable balance up front.
#
# The transfer must be a DIRECT source -> target move, created and submitted in one
# entry -- NOT routed through a transit warehouse. On this site "Material Transfer
# (DEPARTMENT)" and "Customer Goods Transfer" both carry add_to_transit = 1 on the Stock
# Entry Type, which the Stock Entry field fetches (fetch_if_empty, default 0) so it can't
# be held at 0 while that type is used. The plain base "Material Transfer" type is
# add_to_transit = 0, giving the direct one-shot transfer.
TRANSFER_SE_TYPE = "Material Transfer"
REPACK_SE_TYPE = "Repack-Metal Conversion"

TOLERANCE = 0.001

# Sourcing outcomes recorded per row (for the response / logging).
LEVEL_AVAILABLE = "Available"
LEVEL_CONVERT_IN_PLACE = "ConvertInPlace"
LEVEL_SAME_PURITY = "SamePurity"
LEVEL_CONVERT = "Convert"


@frappe.whitelist()
def settle_material_request(mr_name):
	"""Top up ``set_from_warehouse`` for every short row of a return Material Request."""
	mr = frappe.get_doc("Material Request", mr_name)
	mr.check_permission("write")
	_validate_settleable(mr)

	plan = _build_plan(mr)

	actionable = [p for p in plan if p["level"] != LEVEL_AVAILABLE]
	if not actionable:
		return {
			"settled": [],
			"message": _("Source warehouse already holds enough — nothing to settle."),
		}

	_execute(mr, actionable)

	frappe.logger().info(
		f"cg_settle: settled {len(actionable)} row(s) on {mr.name} "
		f"({', '.join(str(p['row']) for p in actionable)})"
	)
	return {
		"settled": [p["row"] for p in actionable],
		"message": _("Settled {0} row(s).").format(len(actionable)),
	}


def _validate_settleable(mr):
	# Settle tops up the source warehouse with independent Stock Entries; it never
	# saves the Material Request, so it is valid on a draft OR a submitted MR — only a
	# cancelled one is rejected.
	if mr.docstatus == 2:
		frappe.throw(_("Settle is not available on a cancelled Material Request."))
	if mr.material_request_type != "Material Transfer":
		frappe.throw(_("Settle is only available for a Material Transfer."))
	if not mr.get("items"):
		frappe.throw(_("Material Request has no items to settle."))


# ---------------------------------------------------------------------------
# Planning (read-only): decide, per row, whether/where to source the shortfall.
# ---------------------------------------------------------------------------


def _build_plan(mr):
	company = mr.company
	all_rm = _get_raw_material_warehouses(company)

	# Batch-level reservations across every sourcing find in this run, so two rows can
	# never be handed the same source batch. Kept separate: how much of the PRE-EXISTING
	# target stock earlier rows have already earmarked (material sourced in for a row is
	# earmarked for that row, so it must not count as free for a later row).
	allocated = {}
	consumed_target = {}

	plan = []
	for row in mr.items:
		plan.append(_plan_row(row, mr, company, all_rm, allocated, consumed_target))
	return plan


def _plan_row(row, mr, company, all_rm, allocated, consumed_target):
	target = row.get("from_warehouse") or mr.get("set_from_warehouse")
	if not target:
		frappe.throw(
			_(
				"Row {0}: a source warehouse (Set From Warehouse) is required to Settle."
			).format(row.idx)
		)

	customer = row.get("customer") or mr.get("customer") or mr.get("_customer")
	if not customer:
		frappe.throw(
			_("Row {0}: a customer is required to settle customer goods.").format(
				row.idx
			)
		)

	item = row.item_code
	required = flt(row.qty, 3)

	have = _owner_qty_in_warehouse(customer, item, target, consumed_target)
	shortfall = flt(required - have, 3)

	base = {
		"row": row.idx,
		"item": item,
		"customer": customer,
		"target": target,
		"required": required,
		"shortfall": shortfall,
		# The batch the MR references. The MR's own transfer is batch-strict (the
		# MR -> Stock Entry mapper copies this onto the transfer row), so the sourced
		# material must land in THIS batch, not a fresh child batch. None => let the
		# child-batch minter name it (fallback).
		"mr_batch": row.get("batch_no"),
	}

	if shortfall <= TOLERANCE:
		# Case 1 — enough of the customer's material already in the target warehouse.
		_claim_target(consumed_target, customer, item, target, min(required, have))
		return dict(base, level=LEVEL_AVAILABLE)

	# Whatever the target already holds is earmarked for this row.
	_claim_target(consumed_target, customer, item, target, have)

	others = [w for w in all_rm if w != target]
	pure_short = _pure_weight(row, item, shortfall, required)

	# Convert-in-place — the customer's OWN convertible purity is already sitting IN the
	# target warehouse (the common return case: their 24KT was converted to 18KT for
	# manufacturing and never left Central). Convert it back in place: one repack, no
	# borrowing, no regular-gold balancing. Preferred over borrowing from elsewhere.
	# Aggregates across the customer's batches, since the converted metal is usually
	# spread over several.
	in_target = _gather_convertible(
		customer, item, pure_short, target, company, allocated
	)
	if in_target:
		# The customer conversion is systematic only (no physical melt), so mirror it with
		# regular gold (item -> item2 in the target) to keep each purity's system total
		# matching physical — the same balancing the cross-warehouse case does.
		reg = _reserve_regular(item, shortfall, company, target, allocated, row.idx)
		return dict(
			base,
			level=LEVEL_CONVERT_IN_PLACE,
			src=in_target,
			regular=reg,
			pure_short=pure_short,
		)

	# Case 2 — same purity available in another warehouse.
	src = find_owner_rm_warehouse(
		customer,
		item,
		shortfall,
		company=company,
		warehouses=others,
		allocated=allocated,
	)
	if src:
		reg = _reserve_regular(item, shortfall, company, target, allocated, row.idx)
		return dict(base, level=LEVEL_SAME_PURITY, src=src, regular=reg)

	# Case 3 — only a different purity available, in another warehouse; convert + balance.
	conv = find_owner_rm_warehouse(
		customer,
		item,
		pure_short,
		search_different_purity=True,
		company=company,
		warehouses=others,
		allocated=allocated,
	)
	if conv:
		# Regular gold of the REQUIRED purity in the target, to mirror the conversion so
		# each purity stays physically balanced (see _execute).
		reg = _reserve_regular(item, shortfall, company, target, allocated, row.idx)
		return dict(
			base, level=LEVEL_CONVERT, src=conv, regular=reg, pure_short=pure_short
		)

	frappe.throw(
		_(
			"Row {0}: need {1} of {2} for customer {3}, but it is not available in the "
			"source warehouse, in any other warehouse at this purity, or in a convertible "
			"purity."
		).format(row.idx, shortfall, item, customer)
	)


def _pure_weight(row, item, shortfall, required):
	pure_row = flt(row.get("custom_pure_qty") or 0, 3)
	if pure_row and required:
		# Scale the row's pure qty down to the shortfall proportion.
		return flt(pure_row * shortfall / required, 3)
	return flt(shortfall * flt(_get_item_purity(item)) / 100, 3)


def _reserve_regular(item, qty, company, target, allocated, row_idx):
	"""Reserve regular (company) gold of ``item`` in the target warehouse for a reverse
	leg. Missing regular gold fails the whole Settle rather than leaving a source
	warehouse short after the reverse cannot be booked."""
	reg = find_owner_rm_warehouse(
		None, item, qty, company=company, warehouses=[target], allocated=allocated
	)
	if not reg:
		frappe.throw(
			_(
				"Row {0}: {1} of Regular Stock {2} is needed in {3} to balance the "
				"borrowed gold, but is not available. Stock regular gold there and retry."
			).format(row_idx, qty, item, target)
		)
	return reg


def _owner_qty_in_warehouse(customer, item, warehouse, consumed_target):
	"""Consumable qty of ``customer``'s ``item`` in ``warehouse``, net of what earlier
	rows of this run already earmarked."""
	batches = frappe.get_all(
		"Batch",
		filters={"item": item, "custom_customer": customer, "disabled": 0},
		pluck="name",
	)
	total = 0.0
	if batches:
		cmap = _consumable_batch_qty_map(batches, item, [warehouse])
		total = sum(flt(q, 3) for q in cmap.values())
	claimed = flt(consumed_target.get((customer, item, warehouse), 0), 3)
	return max(flt(total, 3) - claimed, 0.0)


def _claim_target(consumed_target, customer, item, warehouse, qty):
	if qty <= 0:
		return
	key = (customer, item, warehouse)
	consumed_target[key] = flt(consumed_target.get(key, 0), 3) + flt(qty, 3)


def _gather_convertible(customer, item, pure_needed, warehouse, company, allocated):
	"""Gather enough of the customer's convertible purity in ONE warehouse to cover
	``pure_needed`` (pure weight), possibly across SEVERAL batches.

	The customer's converted metal is normally spread over multiple batches, so unlike
	the single-batch finder this accumulates batches of one candidate purity until the
	pure requirement is met. Returns
	``{"item_code", "purity", "warehouse", "rows": [{"batch_no", "qty"}]}`` (source qty in
	candidate-item units) and reserves each consumed batch in ``allocated``; ``None`` if no
	single candidate purity can cover it. Higher purities are tried first (PURITY_PRIORITY).
	"""
	pure_needed = flt(pure_needed, 3)
	if pure_needed <= TOLERANCE:
		return None

	required_label = _get_purity_label(item)
	for purity in PURITY_PRIORITY:
		if purity == required_label:
			continue
		for candidate in _get_gold_items_for_purity(purity, item):
			cand_purity = flt(_get_item_purity(candidate))
			if not cand_purity:
				continue
			rows, acc_pure = _accumulate_batches(
				customer, candidate, cand_purity, pure_needed, warehouse, allocated
			)
			if acc_pure >= pure_needed - TOLERANCE and rows:
				for r in rows:
					key = (r["batch_no"], warehouse)
					allocated[key] = flt(allocated.get(key, 0), 3) + r["qty"]
				return {
					"item_code": candidate,
					"purity": cand_purity,
					"warehouse": warehouse,
					"rows": rows,
				}
	return None


def _accumulate_batches(
	customer, candidate, cand_purity, pure_needed, warehouse, allocated
):
	"""Take the customer's ``candidate`` batches in ``warehouse`` (oldest first) until the
	pure requirement is met. Returns (rows, accumulated_pure) without reserving."""
	batches = frappe.get_all(
		"Batch",
		filters={"item": candidate, "custom_customer": customer, "disabled": 0},
		order_by="creation asc",
		pluck="name",
	)
	if not batches:
		return [], 0.0

	cmap = _consumable_batch_qty_map(batches, candidate, [warehouse])
	rows = []
	acc_pure = 0.0
	for batch in batches:
		consumable = flt(cmap.get((batch, warehouse), 0), 3)
		if consumable <= TOLERANCE:
			continue
		# Net reservations (get_batch_qty) and this run's earlier claims, so we never
		# promise stock that is reserved or already earmarked.
		netted = min(
			consumable,
			flt(
				get_batch_qty(batch_no=batch, warehouse=warehouse, item_code=candidate),
				3,
			),
		)
		avail = flt(netted - flt(allocated.get((batch, warehouse), 0), 3), 3)
		if avail <= TOLERANCE:
			continue
		need_units = flt((pure_needed - acc_pure) / (cand_purity / 100), 3)
		take = flt(min(avail, need_units), 3)
		if take <= TOLERANCE:
			continue
		rows.append({"batch_no": batch, "qty": take})
		acc_pure = flt(acc_pure + take * cand_purity / 100, 3)
		if acc_pure >= pure_needed - TOLERANCE:
			break
	return rows, acc_pure


# ---------------------------------------------------------------------------
# Execution (writes): mint + submit the Stock Entries the plan calls for.
# ---------------------------------------------------------------------------


def _execute(mr, actionable):
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		series_stubs,
	)

	# RULE B — pin the naming counters and every Bin this run touches, up front, in
	# canonical order, so a concurrent submit cannot interleave Series <-> Bin.
	preallocate_series_for_docs(
		*series_stubs(mr.company, TRANSFER_SE_TYPE, REPACK_SE_TYPE)
	)
	lock_bins(_bin_pairs(actionable))

	for entry in actionable:
		if entry["level"] == LEVEL_CONVERT_IN_PLACE:
			_settle_convert_in_place(mr, entry)
		elif entry["level"] == LEVEL_SAME_PURITY:
			_settle_same_purity(mr, entry)
		elif entry["level"] == LEVEL_CONVERT:
			_settle_convert(mr, entry)


def _bin_pairs(actionable):
	pairs = []
	for e in actionable:
		item, target, src = e["item"], e["target"], e["src"]
		pairs.append((item, target))
		pairs.append((item, src["warehouse"]))
		if e["level"] in (LEVEL_CONVERT, LEVEL_CONVERT_IN_PLACE):
			# the different-purity source item also moves / is consumed
			pairs.append((src["item_code"], src["warehouse"]))
			pairs.append((src["item_code"], target))
	return pairs


def _settle_convert_in_place(mr, entry):
	"""The customer's convertible purity is already in the target: convert it back there,
	then mirror the conversion with regular gold to keep physical stock balanced.

	Two repacks, both in the target, no transfers:
	  1. customer:  item2 -> item   (systematic; produced into the MR batch)
	  2. regular:   item  -> item2  (company gold; restores each purity's physical total,
	     since the customer conversion did not physically melt anything)
	"""
	conv = entry["src"]  # {item_code, warehouse: target, rows: [{batch_no, qty}]}
	item = entry["item"]
	item2 = conv["item_code"]
	target = entry["target"]
	shortfall = entry["shortfall"]
	# Total source consumed by the customer conversion; the regular mirror produces it
	# back so the item2 physical total is unchanged.
	source_total = flt(sum(flt(r["qty"], 3) for r in conv["rows"]), 3)

	# 1. Customer conversion: item2 (gathered batches) -> item, into the MR batch.
	_convert_multi(
		mr,
		source_item=item2,
		source_rows=conv["rows"],
		target_item=item,
		target_qty=shortfall,
		warehouse=target,
		customer=entry["customer"],
		target_batch=entry.get("mr_batch"),
	)
	# 2. Regular mirror: item (regular) -> item2 (regular), same quantities reversed.
	reg = entry["regular"]
	_convert(
		mr,
		source_item=item,
		source_qty=shortfall,
		source_batch=reg["batch_no"],
		target_item=item2,
		target_qty=source_total,
		warehouse=target,
		customer=None,
	)


def _settle_same_purity(mr, entry):
	item = entry["item"]
	qty = entry["shortfall"]
	target = entry["target"]
	src = entry["src"]
	reg = entry["regular"]
	mr_batch = entry.get("mr_batch")
	found_batch = src["batch_no"]

	# The MR transfer is batch-strict, so the customer's gold must arrive in the MR's
	# batch. A transfer can't relabel, so when the found batch differs, relabel it in the
	# source warehouse first (same-item repack: consume found -> produce mr_batch) and
	# move mr_batch in; otherwise transfer the found batch (already the MR batch) as-is.
	transfer_batch = found_batch
	if mr_batch and found_batch != mr_batch:
		transfer_batch = _convert(
			mr,
			source_item=item,
			source_qty=qty,
			source_batch=found_batch,
			target_item=item,
			target_qty=qty,
			warehouse=src["warehouse"],
			customer=entry["customer"],
			target_batch=mr_batch,
		)

	# Bring the customer's gold in from the lending warehouse.
	_transfer(
		mr, item, qty, transfer_batch, src["warehouse"], target, entry["customer"]
	)
	# Return an equal quantity of regular gold, so the lender ends whole.
	_transfer(mr, item, qty, reg["batch_no"], target, src["warehouse"], None)


def _settle_convert(mr, entry):
	"""Different-purity settle, balanced with regular gold.

	Net effect per the user's Case 3, achieved with four Stock Entries:

	  1. customer conversion in W:  item2 -> item        (customer's holding, pure-equiv)
	  2. regular conversion in T:   item  -> item2       (company gold)
	  3. customer transfer:         item   W -> T        (customer gets the required item)
	  4. regular transfer:          item2  T -> W        (W restored, now regular)

	Result: W's item2 quantity is unchanged (customer -> regular), T gains the customer's
	required-purity item in place of company gold. Provisional choreography anchored on
	create_snc — confirm against a real voucher on the cloud site (see plan open items).
	"""
	item = entry["item"]
	qty = entry["shortfall"]
	target = entry["target"]
	conv = entry["src"]  # {item_code: item2, batch_no, warehouse: W, qty: source_qty}
	reg = entry["regular"]  # regular `item` in target
	item2 = conv["item_code"]
	source_qty = flt(conv["qty"], 3)
	W = conv["warehouse"]

	# 1. Customer conversion in W: item2 -> item, produced INTO the MR's batch so the
	#    batch-strict MR transfer later finds it (falls back to an auto batch if the MR
	#    row carries none).
	produced_item_batch = _convert(
		mr,
		source_item=item2,
		source_qty=source_qty,
		source_batch=conv["batch_no"],
		target_item=item,
		target_qty=qty,
		warehouse=W,
		customer=entry["customer"],
		target_batch=entry.get("mr_batch"),
	)
	# 2. Regular conversion in target: item -> item2 (company gold).
	produced_item2_batch = _convert(
		mr,
		source_item=item,
		source_qty=qty,
		source_batch=reg["batch_no"],
		target_item=item2,
		target_qty=source_qty,
		warehouse=target,
		customer=None,
	)
	# 3. Customer transfer: item W -> target.
	_transfer(mr, item, qty, produced_item_batch, W, target, entry["customer"])
	# 4. Regular transfer: item2 target -> W.
	_transfer(mr, item2, source_qty, produced_item2_batch, target, W, None)


# ---------------------------------------------------------------------------
# Stock Entry builders
# ---------------------------------------------------------------------------


def _new_se(mr, se_type, purpose, from_wh, to_wh, customer):
	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": se_type,
			"purpose": purpose,
			"company": mr.company,
			"branch": mr.get("branch")
			or frappe.db.get_value("Warehouse", from_wh or to_wh, "custom_branch"),
			"from_warehouse": from_wh,
			"to_warehouse": to_wh,
			# Direct one-shot transfer, never routed through a transit warehouse.
			"add_to_transit": 0,
			"inventory_type": CUSTOMER_GOODS if customer else REGULAR_STOCK,
			"_customer": customer,
			"auto_created": 1,
		}
	)
	return se


def _transfer(mr, item, qty, batch, from_wh, to_wh, customer):
	inv = CUSTOMER_GOODS if customer else REGULAR_STOCK
	se = _new_se(mr, TRANSFER_SE_TYPE, "Material Transfer", from_wh, to_wh, customer)
	_append_item(
		se,
		{
			"item_code": item,
			"qty": flt(qty, 3),
			"batch_no": batch,
			"s_warehouse": from_wh,
			"t_warehouse": to_wh,
			"inventory_type": inv,
			"customer": customer,
		},
	)
	se.insert(ignore_permissions=True)
	_submit_consuming_stock_entry(se)
	return se.name


def _convert(
	mr,
	source_item,
	source_qty,
	source_batch,
	target_item,
	target_qty,
	warehouse,
	customer,
	target_batch=None,
):
	"""Repack one purity into another inside a single warehouse; return the target batch.

	``target_batch`` -- when given, the produced (inward) row is stamped with it, so
	``create_child_batches`` skips minting a fresh child batch (it only mints for
	produced rows with no batch) and the metal is produced into that existing batch. Used
	to land the sourced material in the batch the Material Request references.
	"""
	return _convert_multi(
		mr,
		source_item=source_item,
		source_rows=[{"batch_no": source_batch, "qty": flt(source_qty, 3)}],
		target_item=target_item,
		target_qty=target_qty,
		warehouse=warehouse,
		customer=customer,
		target_batch=target_batch,
	)


def _convert_multi(
	mr,
	source_item,
	source_rows,
	target_item,
	target_qty,
	warehouse,
	customer,
	target_batch=None,
):
	"""Repack one purity into another inside a single warehouse, consuming one or more
	source batches of ``source_item``; return the target batch.

	See ``_convert`` for the ``target_batch`` semantics. Multiple source rows let a
	conversion draw the requirement from several batches when it is spread across them.
	"""
	inv = CUSTOMER_GOODS if customer else REGULAR_STOCK
	se = _new_se(mr, REPACK_SE_TYPE, "Repack", warehouse, warehouse, customer)
	for sr in source_rows:
		_append_item(
			se,
			{
				"item_code": source_item,
				"qty": flt(sr["qty"], 3),
				"batch_no": sr["batch_no"],
				"s_warehouse": warehouse,
				"inventory_type": inv,
				"customer": customer,
			},
		)
	produced_row = {
		"item_code": target_item,
		"qty": flt(target_qty, 3),
		"t_warehouse": warehouse,
		"inventory_type": inv,
		"customer": customer,
	}
	if target_batch:
		produced_row["batch_no"] = target_batch
	_append_item(se, produced_row)
	se.insert(ignore_permissions=True)
	_submit_consuming_stock_entry(se)

	if target_batch:
		return target_batch

	produced = frappe.db.get_value(
		"Stock Entry Detail",
		{"parent": se.name, "item_code": target_item, "t_warehouse": warehouse},
		"batch_no",
	)
	if not produced:
		frappe.throw(
			_("{0} {1} did not create a target batch.").format(REPACK_SE_TYPE, se.name)
		)
	return produced
