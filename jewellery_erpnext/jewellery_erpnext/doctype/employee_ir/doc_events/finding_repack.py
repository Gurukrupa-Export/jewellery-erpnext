# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Finding repack for an Employee IR Receive.

Gate: ``eir.type == "Receive"`` AND the EIR's ``Department Operation`` carries
``is_finding_repack_requirement``.

Some casting operations pour findings (clasps, jump rings, bails) out of the very metal that was
issued onto the work order's Tree Number. Each ``Employee IR Operation`` row names up to two of
them -- ``finding_item1`` / ``finding_wt1`` and ``finding_item2`` / ``finding_wt2`` -- and this
module turns that into stock:

  1. A ``Repack`` Stock Entry CONSUMES the tree's metal item at the tree's MSL warehouse and
     PRODUCES each finding item back into that same MSL warehouse, under a freshly created batch.
  2. The tree's ``material_details`` ledger is charged for what was consumed (``receive_qty``),
     so ``pending_qty`` drops by exactly the finding weight -- a tree with 14 g pending that
     yields 2 g of findings is left with 12 g.
  3. The produced finding rows are handed back to ``main_slip_inject`` so they ride the
     ``Material Transfer (WORK ORDER)`` Stock Entry from MSL to the department warehouse. That is
     what lifts the Manufacturing Operation's ``finding_wt`` (and through it ``gross_wt``): the
     MOP Log bridge buckets a weight by the item code's first character (``FIELD_MAP`` in
     ``mop_log.py``), so an ``F`` row on a voucher carrying ``manufacturing_operation`` lands in
     the finding bucket and nothing else has to write it.

**Batch provenance is the Tree Number Receive flow's, not FIFO.** The MSL warehouse belongs to the
EMPLOYEE and pools metal from every tree that operator has worked, so warehouse-wide FIFO would
hand back whichever batch happens to be oldest -- possibly another tree's, possibly another
customer's. ``_allocate_tree_legs`` (the allocator behind the Tree Number "Receive Material"
button) is reused verbatim: this tree's own issued batches first, then, only if those fall short,
same-``(inventory_type, customer)``-tier substitutes, and a hard throw when neither can cover it.

**The Repack is deliberately ledger-invisible.** It carries no ``manufacturing_order`` /
``manufacturing_work_order`` on the header and no ``manufacturing_operation`` on any row. Those
two omissions are load-bearing, not oversights:

  * ``doc_events/stock_entry.onsubmit`` short-circuits a ``"Repack"`` that lacks the two header
    fields, so no Stock Reservation Entry is minted.
  * ``sync_mop_log_for_stock_entry`` skips rows with no ``manufacturing_operation``, so no MOP Log
    row is written.

Together they hold WHETHER OR NOT ``"Repack"`` is listed in MOP Settings' ``Stock Entry Type To
Reservation`` (it is absent on some sites and seeded by ``create_test_data`` on others). Without
them the finding weight would be counted twice on the operation -- once by this Repack and once by
the Material Transfer that carries the same metal onward. Provenance is kept instead through
``employee_ir`` / ``custom_eir_operation_row`` / ``custom_tree_number`` / ``auto_created``, which
is also what lets ``cancel_injections_for_eir`` reverse this entry with the injections.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
	_check_tree_draw,
	_metal_item,
	_tree_ledger_row,
	lock_tree,
	row_tree_name,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
	_allocate_tree_legs,
	_new_transfer_se,
	_resolve_msl_warehouse,
	_stamp_batch,
)

REPACK_STOCK_ENTRY_TYPE = "Repack"
FINDING_PREFIX = "F"

# (item field, weight field) pairs, in the order they are poured.
FINDING_FIELD_PAIRS = (
	("finding_item1", "finding_wt1"),
	("finding_item2", "finding_wt2"),
)


# ---------------------------------------------------------------------------
# Gate + row reading
# ---------------------------------------------------------------------------
def is_finding_repack_eir(eir):
	"""True when the EIR's operation repacks tree metal into findings.

	Mirrors ``tree_casting.is_casting_eir``: read off the Department Operation rather than the
	mirrored ``Employee IR.is_finding_repack_reqd``, so a document whose ``fetch_from`` never ran
	(an import, an API write, a draft saved before the flag was ticked) still resolves the live
	answer. The mirror exists for the client's ``depends_on``, not for the server.
	"""
	if not eir.get("operation"):
		return False
	return bool(
		frappe.db.get_value(
			"Department Operation", eir.operation, "is_finding_repack_requirement"
		)
	)


def finding_pairs(row):
	"""``[(item_code, qty)]`` for the finding slots filled on one Employee IR Operation row.

	Slots are independent: a row may carry one finding or two, and the second may be filled while
	the first is not. A half-filled slot (item without weight, or weight without item) is NOT
	silently dropped here -- ``validate_finding_repack`` rejects it -- so this returns only the
	slots that are complete, and the validator is what guarantees nothing was lost on the way.
	"""
	pairs = []
	for item_field, wt_field in FINDING_FIELD_PAIRS:
		item = row.get(item_field)
		qty = flt(row.get(wt_field))
		if item and qty > 0:
			pairs.append((item, qty))
	return pairs


def _row_finding_total(row):
	return flt(
		sum(qty for _item, qty in finding_pairs(row)), tree_balance.qty_precision()
	)


def _has_any_finding_input(row):
	"""True when ANY of the four finding fields carries a value.

	Used by the "flag is off" guard, which must fire on a half-filled slot too: an operator who
	typed a weight against the wrong operation has to be told, not quietly ignored.
	"""
	return any(row.get(field) for pair in FINDING_FIELD_PAIRS for field in pair)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_finding_repack(eir):
	"""Reject anything the submit-time repack could not honour, at save time.

	Runs from ``EmployeeIR.validate``. The authoritative checks run again at submit under the tree
	row lock (``_check_tree_draw`` inside ``create_finding_repack_for_row``); this is the
	fail-fast copy so the operator learns before pressing Submit.
	"""
	if eir.get("type") != "Receive":
		_reject_finding_input_when_unavailable(
			eir,
			_("Finding items can only be repacked on a Receive Employee IR."),
		)
		return

	if not is_finding_repack_eir(eir):
		_reject_finding_input_when_unavailable(
			eir,
			_(
				"Operation <b>{0}</b> does not have <b>Is Finding Repack Requirement</b> ticked, "
				"so no finding repack can be created for it."
			).format(eir.get("operation") or "-"),
		)
		return

	# Aggregate per (tree, metal item) BEFORE comparing against the ledger: two rows on the same
	# tree must not each pass a check that the pair would fail together.
	draws = {}
	for row in eir.employee_ir_operations:
		_validate_row_slots(row)
		pairs = finding_pairs(row)
		if not pairs:
			continue

		tree_name = row_tree_name(row)
		if not tree_name:
			frappe.throw(
				_(
					"Row #{0} ({1}): a finding repack draws metal from the work order's casting "
					"tree, but this work order is not on one."
				).format(row.idx, row.manufacturing_work_order or "-"),
				title=_("No Tree Number"),
			)

		for item_code, _qty in pairs:
			_validate_finding_item(row, item_code)

		metal_item = _metal_item(
			frappe.get_cached_doc(
				"Manufacturing Work Order", row.manufacturing_work_order
			)
		)
		if not metal_item:
			frappe.throw(
				_(
					"Row #{0} ({1}): the work order's metal item could not be resolved from its "
					"metal type / touch / purity / colour, so there is nothing to repack."
				).format(row.idx, row.manufacturing_work_order or "-")
			)

		bucket = draws.setdefault(tree_name, {})
		bucket[metal_item] = flt(bucket.get(metal_item, 0.0)) + _row_finding_total(row)

	_validate_draws_against_trees(draws)


def _reject_finding_input_when_unavailable(eir, reason):
	"""Throw when finding values are filled on an EIR that can never repack them.

	Silently ignoring them would lose stock: the operator believes the findings were booked, the
	tree keeps the metal as pending, and the operation never gains the weight.
	"""
	for row in eir.employee_ir_operations:
		if _has_any_finding_input(row):
			frappe.throw(
				_("Row #{0}: {1} Clear the finding item / weight fields.").format(
					row.idx, reason
				),
				title=_("Finding Repack Not Available"),
			)


def _validate_row_slots(row):
	"""Both halves of a finding slot must be filled, or neither."""
	for item_field, wt_field in FINDING_FIELD_PAIRS:
		item = row.get(item_field)
		qty = flt(row.get(wt_field))
		if item and qty <= 0:
			frappe.throw(
				_("Row #{0}: <b>{1}</b> is set but its weight is zero.").format(
					row.idx, item
				)
			)
		if qty > 0 and not item:
			frappe.throw(
				_(
					"Row #{0}: a finding weight of {1} is entered with no finding item."
				).format(row.idx, qty)
			)


def _validate_finding_item(row, item_code):
	"""The repack target must be an ``F`` variant.

	Load-bearing, not cosmetic: ``mop_log.FIELD_MAP`` buckets a weight by the item code's FIRST
	CHARACTER, so a metal or diamond item here would silently land in the wrong bucket on the
	Manufacturing Operation and corrupt its weight breakdown.
	"""
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if variant_of != FINDING_PREFIX:
		frappe.throw(
			_(
				"Row #{0}: <b>{1}</b> is not a Finding item (its Variant Of is <b>{2}</b>, "
				"expected <b>{3}</b>). Only finding items can be repacked here — anything else "
				"would be booked into the wrong weight bucket on the Manufacturing Operation."
			).format(row.idx, item_code, variant_of or _("not set"), FINDING_PREFIX),
			title=_("Not a Finding Item"),
		)


def _validate_draws_against_trees(draws):
	"""Every ``(tree, metal item)`` draw must fit inside what that tree still has outstanding."""
	prec = tree_balance.qty_precision()
	eps = tree_balance.pending_eps()
	for tree_name in sorted(draws):
		tree = frappe.get_doc("Tree Number", tree_name)
		for item_code, draw in draws[tree_name].items():
			draw = flt(draw, prec)
			if draw <= eps:
				continue
			md = _tree_ledger_row(tree, item_code)
			available = tree_balance.available_to_draw(md, prec)
			if draw - available <= eps:
				continue
			frappe.throw(
				_(
					"Tree <b>{0}</b> (item <b>{1}</b>) has only {2} outstanding, but the finding "
					"weights on this Employee IR need {3}. Issue more material to the tree, or "
					"reduce the finding weights."
				).format(tree_name, item_code, flt(available, prec), draw),
				title=_("Not Enough Tree Material"),
			)


# ---------------------------------------------------------------------------
# Repack
# ---------------------------------------------------------------------------
def create_finding_repack_for_row(eir, row):
	"""Post the finding Repack for one Employee IR Operation row.

	Returns the produced rows for the Material Transfer (WORK ORDER) leg --
	``[{item_code, qty, batch_no, inventory_type, customer}]`` -- or ``[]`` when this row has no
	findings (or the operation is not a finding-repack one), so the caller can hand them to
	``inject_extra_metal_for_eir_receive`` unconditionally.
	"""
	if eir.get("type") != "Receive" or not is_finding_repack_eir(eir):
		return []

	pairs = finding_pairs(row)
	if not pairs:
		return []

	existing = _existing_finding_repack(eir.name, row.name)
	if existing:
		# Idempotent on (eir, row): a retry after a partial failure must not pour the findings a
		# second time. Keyed the same way main_slip_inject keys its injections.
		#
		# The already-produced rows are REPLAYED rather than swallowed. If the Repack submitted
		# and the Material Transfer that carries the findings onward then failed, returning
		# nothing here would strand them in the MSL warehouse with no voucher left to move them;
		# the transfer's own batch-level idempotency decides whether they still need carrying.
		return _produced_rows_of(existing)

	tree_name = row_tree_name(row)
	if not tree_name:
		frappe.throw(
			_("Row #{0} ({1}): no Tree Number to draw finding metal from.").format(
				row.idx, row.manufacturing_work_order or "-"
			)
		)

	# Parent control row before tabSeries / tabBin (lock_order canonical position 1). The Employee
	# IR receive already locked every tree it touches up front; taking it again here is free and
	# keeps this function correct when called on its own.
	lock_tree(tree_name)
	tree = frappe.get_doc("Tree Number", tree_name)
	_reject_if_tree_submitted(tree)

	msl_wh = tree.get("msl_warehouse") or _resolve_msl_warehouse(tree)
	_validate_msl_matches_eir(eir, tree, msl_wh)

	mwo = frappe.get_cached_doc(
		"Manufacturing Work Order", row.manufacturing_work_order
	)
	metal_item = _metal_item(mwo)
	if not metal_item:
		frappe.throw(
			_(
				"Row #{0} ({1}): the work order's metal item could not be resolved."
			).format(row.idx, row.manufacturing_work_order or "-")
		)

	prec = tree_balance.qty_precision()
	total = flt(sum(qty for _item, qty in pairs), prec)

	# Authoritative ledger check, under the tree row lock taken above. validate_finding_repack
	# already ran, but it read the ledger without the lock and before any other voucher in flight
	# had settled — a concurrent tree receive between save and submit would slip past it.
	_check_tree_draw(eir, tree, metal_item, total)

	se = _new_finding_repack_se(eir, row, tree, msl_wh)

	# The Tree Number Receive button's own allocator: this tree's issued batches first, then
	# same-ownership-tier substitutes, then a throw. `loss=0` -- a finding repack writes nothing
	# off, so the loss pass has nothing to take and the returned loss allocation is empty.
	consume_alloc, _loss_alloc, ranks = _allocate_tree_legs(
		se, tree, metal_item, msl_wh, total, 0
	)

	produced = _append_finding_rows(
		se, pairs, metal_item, msl_wh, consume_alloc, ranks, prec
	)

	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	_charge_tree(tree, metal_item, total, prec)

	return produced


def _new_finding_repack_se(eir, row, tree, msl_wh):
	"""Header for the finding Repack.

	``_new_transfer_se`` is reused so ``company`` (derived from the warehouse being moved, not the
	tree, for multi-company safety), ``manufacturer``, ``custom_tree_number`` and ``auto_created``
	are all set exactly as every other tree-sourced entry sets them; only the purpose flips.

	``manufacturing_order`` / ``manufacturing_work_order`` are deliberately NOT set -- see the
	module docstring. ``custom_eir_operation_row`` carries the row link instead, which is what
	makes the entry idempotent and traceable without making it ledger-visible.
	"""
	se = _new_transfer_se(tree, REPACK_STOCK_ENTRY_TYPE, company_wh=msl_wh)
	se.purpose = "Repack"
	se.employee_ir = eir.name
	se.custom_eir_operation_row = row.name
	if eir.subcontracting == "Yes":
		se.subcontractor = eir.subcontractor
	else:
		se.employee = eir.employee
	return se


def _append_finding_rows(se, pairs, metal_item, msl_wh, consume_alloc, ranks, prec):
	"""Append consume(metal)/produce(finding) rows and return the produced rows' descriptors.

	The consume allocation is walked as a STREAM: each finding item takes its weight off the front,
	so the batches a finding is poured from are the batches the tree actually owes, in the order
	the allocator ranked them.

	A finding whose slice spans two ownership tiers produces one row per tier, each with its own
	batch. That split is not an edge case to paper over: the minted batch reads ``inventory_type``
	/ ``customer`` straight off its produce row, so folding a customer's metal and the company's
	into one batch would silently change who owns it.
	"""
	stream = [[batch_no, flt(qty, prec)] for batch_no, qty in consume_alloc]
	produced = []

	for item_code, wanted in pairs:
		remaining = flt(wanted, prec)
		# {owner: [(batch_no, qty)]} -- consumes for this finding, grouped by who owns them.
		by_owner = {}
		for slot in stream:
			if remaining <= 0:
				break
			take = flt(min(slot[1], remaining), prec)
			if take <= 0:
				continue
			owner = _owner_of(slot[0], ranks, metal_item)
			by_owner.setdefault(owner, []).append((slot[0], take))
			slot[1] = flt(slot[1] - take, prec)
			remaining = flt(remaining - take, prec)

		if remaining > tree_balance.pending_eps():
			# Unreachable while _allocate_tree_legs holds its contract (it throws on a shortfall
			# rather than under-delivering), but pouring a finding lighter than the operator asked
			# for would be a silent stock error, so it is refused rather than trusted.
			frappe.throw(
				_(
					"Finding {0}: only {1} of the {2} requested could be allocated from the "
					"tree's metal."
				).format(item_code, flt(wanted - remaining, prec), flt(wanted, prec))
			)

		for owner, slices in by_owner.items():
			inventory_type, customer = owner
			produce_qty = flt(sum(qty for _b, qty in slices), prec)
			for batch_no, qty in slices:
				_append_consume_row(
					se, metal_item, qty, msl_wh, batch_no, inventory_type, customer
				)
			batch_no = _create_finding_batch(se, item_code, inventory_type, customer)
			_append_produce_row(
				se, item_code, produce_qty, msl_wh, batch_no, inventory_type, customer
			)
			produced.append(
				{
					"item_code": item_code,
					"qty": produce_qty,
					"batch_no": batch_no,
					"inventory_type": inventory_type,
					"customer": customer,
				}
			)

	return produced


def _owner_of(batch_no, ranks, item_code):
	meta = ranks.get(batch_no)
	return normalize_ownership(
		meta.inventory_type if meta else None,
		meta.customer if meta else None,
		batch_no=batch_no,
		item_code=item_code,
	)


def _append_consume_row(
	se, metal_item, qty, msl_wh, batch_no, inventory_type, customer
):
	"""Metal out of MSL, on a pre-resolved batch.

	Row flags mirror ``tree_stock_entry._append_repack_loss_pair``'s consume leg. The batch is
	stamped here rather than left to the FIFO helper on purpose: a pre-stamped row takes
	``_expand_source_rows_for_fifo``'s early exit, which is what keeps the tree's own allocation
	from being overwritten by warehouse-wide FIFO.
	"""
	se.append(
		"items",
		_stamp_batch(
			{
				"item_code": metal_item,
				"qty": qty,
				"transfer_qty": qty,
				"conversion_factor": 1,
				"s_warehouse": msl_wh,
				"t_warehouse": None,
				"uom": "Gram",
				"stock_uom": "Gram",
				"pcs": "1",
				"use_serial_batch_fields": 1,
			},
			batch_no,
			inventory_type,
			customer,
		),
	)


def _append_produce_row(se, item_code, qty, msl_wh, batch_no, inventory_type, customer):
	"""The finding, into the SAME MSL warehouse the metal came out of.

	Producing into MSL rather than the department warehouse is what lets the Material Transfer
	(WORK ORDER) leg pick the finding up: that transfer sources from the employee's Raw Material
	warehouse, so a finding minted anywhere else could not ride it.

	``set_basic_rate_manually`` opts the produce row out of ERPNext's Repack rate pooling, exactly
	as the loss engine's produce rows do; ``CustomStockEntry.set_basic_rate`` then assigns the
	rate centrally from the consumed rows so the metal's value moves onto the finding instead of
	vanishing from the ledger.
	"""
	uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Gram"
	se.append(
		"items",
		_stamp_batch(
			{
				"item_code": item_code,
				"qty": qty,
				"transfer_qty": qty,
				"conversion_factor": 1,
				"s_warehouse": None,
				"t_warehouse": msl_wh,
				"uom": uom,
				"stock_uom": uom,
				"pcs": "1",
				"is_finished_item": 1,
				"set_basic_rate_manually": 1,
				"use_serial_batch_fields": 1,
			},
			batch_no,
			inventory_type,
			customer,
		),
	)


def _create_finding_batch(se, item_code, inventory_type, customer):
	"""Mint the finding's batch UP FRONT and return its id (``None`` for a non-batched item).

	Created here rather than left to the submit-time bundle machinery because the caller has to
	hand this exact batch to the Material Transfer leg that moves the finding onward; a batch that
	only exists after submit would have to be guessed at or read back.

	Mirrors ``manufacturing_operation._create_scrap_batch`` minus its Unused/Loose marker: the
	company is stamped explicitly because ``get_batch_company_abbr`` otherwise falls back to the
	SESSION user's default company (wrong on a multi-company site, fatal in a background job), and
	the ownership pair because this Stock Entry is ``auto_created`` -- ``CustomStockEntry.update_batches``
	skips its generic batch backfill, and ``doc_events/stock_entry`` would default the batch to
	Regular Stock, quietly turning a customer's metal into the company's.
	"""
	if not frappe.db.get_value("Item", item_code, "has_batch_no"):
		return None

	batch = frappe.new_doc("Batch")
	batch.item = item_code
	if se.get("company"):
		batch.custom_company = se.company
	if se.get("employee"):
		batch.custom_employee = se.employee
	if inventory_type:
		batch.custom_inventory_type = inventory_type
		batch.custom_customer = customer
	batch.insert(ignore_permissions=True)
	return batch.name


def _existing_finding_repack(eir_name, row_name):
	return frappe.db.get_value(
		"Stock Entry",
		{
			"employee_ir": eir_name,
			"custom_eir_operation_row": row_name,
			"stock_entry_type": REPACK_STOCK_ENTRY_TYPE,
			"auto_created": 1,
			"docstatus": ["!=", 2],
		},
		"name",
	)


def _produced_rows_of(se_name):
	"""Rebuild the Material Transfer descriptors from an already-posted finding Repack.

	The produce rows are the ones with a target and no source -- the same shape test
	``stamp_produce_rows_from_consumes`` uses to tell the two legs apart.
	"""
	rows = frappe.db.sql(
		"""
		SELECT item_code, qty, batch_no, inventory_type, customer
		FROM `tabStock Entry Detail`
		WHERE parent = %s
		  AND IFNULL(s_warehouse, '') = ''
		  AND IFNULL(t_warehouse, '') != ''
		ORDER BY idx
		""",
		(se_name,),
		as_dict=True,
	)
	return [
		{
			"item_code": r.item_code,
			"qty": flt(r.qty),
			"batch_no": r.batch_no,
			"inventory_type": r.inventory_type,
			"customer": r.customer,
		}
		for r in rows
	]


def _reject_if_tree_submitted(tree):
	"""A manually-submitted tree is terminal; a finding repack must not reopen it."""
	if tree.get("status") == tree_balance.STATUS_SUBMITTED:
		frappe.throw(
			_(
				"Tree {0} is submitted (locked); no finding repack can be booked against it. "
				"Cancel the tree's submission first."
			).format(tree.name),
			title=_("Tree Locked"),
		)


def _validate_msl_matches_eir(eir, tree, msl_wh):
	"""The tree's MSL must be the warehouse this receive is actually returning from.

	``tree.employee`` is a static copy of the ISSUE Employee IR's employee, so a receive booked
	against a different operator would consume the issuing operator's metal and then transfer the
	findings out of a warehouse this receive never touched. That is a real mis-post, not a
	cosmetic mismatch, so it is refused rather than silently reconciled.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
		_resolve_source_warehouse_raw_material,
	)

	eir_msl = _resolve_source_warehouse_raw_material(eir)
	if eir_msl and msl_wh and eir_msl != msl_wh:
		frappe.throw(
			_(
				"Tree {0} holds its metal in <b>{1}</b>, but this Employee IR receives into "
				"<b>{2}</b>. The finding repack would consume one operator's metal and hand the "
				"findings to another. Receive the tree with the employee it was issued to."
			).format(tree.name, msl_wh, eir_msl),
			title=_("Tree Warehouse Mismatch"),
		)


def _charge_tree(tree, metal_item, qty, prec):
	"""Book the consumed metal on the tree ledger so ``pending_qty`` drops by it.

	``receive_qty`` is the only column written, the same one
	``tree_casting.update_tree_on_receive`` writes -- a finding repack draws tree metal exactly
	the way a work-order gain injection does.

	``pending_qty`` is deliberately NOT written here: it is derived on every save by
	``TreeNumber.validate`` (``calculate_material_pending``), and a second writer is exactly what
	used to make the call paths drift apart.
	"""
	if flt(qty, prec) <= 0:
		return
	md = _tree_ledger_row(tree, metal_item)
	md.receive_qty = flt(flt(md.receive_qty) + flt(qty), prec)
	tree.status = tree_balance.tree_status(tree)
	tree.flags.ignore_permissions = True
	tree.save()


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
def finding_draw_by_tree(eir):
	"""``{tree_name: {metal_item: qty}}`` this Employee IR's findings drew out of each tree.

	Derived from the row fields rather than stored, the same way ``tree_draw_by_tree`` derives the
	gain: the finding weights are immutable once the document is submitted, so replaying them is
	exact and there is no second copy to fall out of step.

	Returns magnitudes only -- the cancel path negates the result, never the inputs.
	"""
	if eir.get("type") != "Receive" or not is_finding_repack_eir(eir):
		return {}

	prec = tree_balance.qty_precision()
	trees = {}
	for row in eir.employee_ir_operations:
		total = _row_finding_total(row)
		if total <= 0:
			continue
		tree_name = row_tree_name(row)
		if not tree_name or not row.manufacturing_work_order:
			continue
		metal_item = _metal_item(
			frappe.get_cached_doc(
				"Manufacturing Work Order", row.manufacturing_work_order
			)
		)
		if not metal_item:
			continue
		bucket = trees.setdefault(tree_name, {})
		bucket[metal_item] = flt(bucket.get(metal_item, 0.0) + total, prec)
	return trees


def reverse_finding_draw_on_trees(eir):
	"""Give back to each tree what this Employee IR's findings took, on cancel.

	The Repack Stock Entries themselves are cancelled by ``cancel_injections_for_eir`` (they are
	``auto_created`` and carry ``employee_ir``); this reverses only the ledger side.
	"""
	draws = finding_draw_by_tree(eir)
	if not draws:
		return

	prec = tree_balance.qty_precision()
	# Deterministic order (lock_order RULE A), matching update_tree_on_receive.
	for tree_name in sorted(draws):
		lock_tree(tree_name)
		tree = frappe.get_doc("Tree Number", tree_name)
		for metal_item, qty in draws[tree_name].items():
			md = _tree_ledger_row(tree, metal_item)
			# Floored: giving metal back is a credit, and a negative "received" is nonsense.
			md.receive_qty = max(0.0, flt(flt(md.receive_qty) - flt(qty), prec))
		tree.status = tree_balance.tree_status(tree)
		tree.flags.ignore_permissions = True
		tree.save()


def lock_finding_repack_trees(eir):
	"""Lock every tree this Employee IR's findings draw from, in name order, up front.

	``tree_casting.lock_trees_for_eir`` covers only CASTING operations (``tree_no_reqd``), and a
	finding-repack operation need not be one: ``row_tree_name`` falls back to the work order's
	``tree_number``, which survives past casting, so a downstream operation can legitimately draw
	from a tree its own flag says nothing about. Without this the tree row lock would be taken
	inside the row loop, i.e. AFTER ``lock_bins`` — the parent-control-row-last interleaving that
	lock_order exists to prevent, and a textbook MariaDB 1213 cycle against a concurrent Tree
	Number button holding the tree while it waits on those same Bins.

	Returns the locked tree names (mostly useful for tests/logging).
	"""
	if eir.get("type") != "Receive" or not is_finding_repack_eir(eir):
		return []

	names = sorted(
		{
			row_tree_name(row)
			for row in eir.employee_ir_operations
			if finding_pairs(row) and row_tree_name(row)
		}
	)
	for name in names:
		lock_tree(name)
	return names


def finding_bin_pairs(eir, msl_wh, dept_wh):
	"""``[(item_code, warehouse)]`` every Bin this feature touches, for the receive's pre-lock.

	The Employee IR receive locks all its Bins in one sorted sequence before any Stock Entry is
	built (lock_order RULE B); a Bin this feature reaches only at submit time would be acquired
	out of that sequence and reintroduce the deadlock the pre-lock exists to prevent.
	"""
	if eir.get("type") != "Receive" or not is_finding_repack_eir(eir):
		return []

	pairs = []
	for row in eir.employee_ir_operations:
		row_pairs = finding_pairs(row)
		if not row_pairs:
			continue
		if row.manufacturing_work_order:
			metal_item = _metal_item(
				frappe.get_cached_doc(
					"Manufacturing Work Order", row.manufacturing_work_order
				)
			)
			if metal_item and msl_wh:
				pairs.append((metal_item, msl_wh))
		for item_code, _qty in row_pairs:
			for warehouse in (msl_wh, dept_wh):
				if warehouse:
					pairs.append((item_code, warehouse))
	return pairs
