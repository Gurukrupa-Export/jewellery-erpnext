"""Repair MOP Log balances inflated by the old MWO-wide running-balance sum.

``create_mop_log_for_stock_transfer_to_mo`` used to derive ``qty_after_transaction*``
from ``SUM(qty_change)`` over the whole **Manufacturing Work Order** rather than the
operation being written to. A MWO routinely strands residue on a finished operation --
metal returned short of the balance, a rework loop, a Work Order Refining Entry -- and
the MWO-wide sum folded that residue into the next operation's opening balance.

Observed on ``MWO-KGJPL-RI02163-054-1-91.75-Y-01``: MOP Log row ``s1iefq86og`` posted a
``qty_change`` of 3.210 (Stock Entry ``MAT-STE-05717``, the re-cast issue) and recorded a
balance of 3.220, because 0.010 stranded before ``RFN-MWO-26-00003`` was still in the MWO
sum. ``MOP-0K3Q4.gross_wt`` then read 3.220 against a received weight of 3.210.

The writer is fixed (``get_mop_opening_balances``). This script repairs rows already
written.

**Append-only.** It never updates or deletes an existing MOP Log row. Each correction is
a NEW row carrying the delta as ``qty_change`` and the corrected absolute balance, tagged
``row_name = REPAIR_ROW_TAG``, so history stays intact, the change is reversible by
cancelling the appended rows, and re-runs are no-ops. ``MOPLog.validate`` ->
``update_wt_detail`` then recomputes ``gross_wt`` through the normal path.

**It only repairs what it can prove.** The detector is
``mop_lineage_audit.audit_post_refining_contamination`` -- the same function the audit
reports, so audit and repair cannot disagree. Refining zeroes the MWO, so an operation
created entirely after that moment provably opens at 0 and its correct closing balance is
the sum of its own ``qty_change`` values. Operations that *straddle* the refining cutoff
have no derivable opening balance and are reported for manual review, never guessed at.

A genuinely stranded residue is left where it is, visible for write-off or return. This
corrects the *contamination of other operations*, not the disposal decision -- where a
stranded gram physically went is Central's call.

NOT registered in patches.txt -- run it manually.

Dry run (prints every proposed correction, writes nothing):

    bench --site gk execute \\
        jewellery_erpnext.patches.repair_mwo_wide_mop_log_balances.execute

Apply, one MWO at a time:

    bench --site gk execute \\
        jewellery_erpnext.patches.repair_mwo_wide_mop_log_balances.execute \\
        --kwargs "{'dry_run': False, 'mwos': ['MWO-KGJPL-RI02163-054-1-91.75-Y-01']}"

Take a backup first (``bench --site <site> backup``), and do not run while an EOD sync is
in flight: MOP Log carries the EOD lock validator on ``before_save``, so the inserts
would be rejected mid-window.
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
	get_last_mop_index,
	recalculate_manufacturing_operation_weights,
)
from jewellery_erpnext.mop_lineage_audit import (
	REPAIR_ROW_TAG,
	audit_post_refining_contamination,
)


def _current_pcs(mop, item_code, batch_no):
	"""PCS on the operation's latest row for this key; carried through unchanged.

	The defect was in the qty tiers only, so the correcting row must not disturb PCS --
	it restates whatever the ledger already holds.
	"""
	for bal in get_current_mop_balance_rows(
		mop,
		include_fields=["item_code", "batch_no", "pcs_after_transaction_batch_based"],
		keys=[(item_code, batch_no)],
	):
		if bal.get("item_code") == item_code and bal.get("batch_no") == batch_no:
			return cint(bal.get("pcs_after_transaction_batch_based"))
	return 0


def _append_correction(fix):
	"""Insert ONE correcting MOP Log row. Never mutates an existing row."""
	mop = fix["manufacturing_operation"]
	warehouses = frappe.db.get_value(
		"MOP Log",
		{
			"manufacturing_operation": mop,
			"item_code": fix["item_code"],
			"batch_no": fix["batch_no"],
			"is_cancelled": 0,
		},
		["from_warehouse", "to_warehouse"],
	) or (None, None)

	correct = flt(fix["expected"], 3)
	pcs = _current_pcs(mop, fix["item_code"], fix["batch_no"])

	ml = frappe.new_doc("MOP Log")
	ml.item_code = fix["item_code"]
	ml.batch_no = fix["batch_no"]
	ml.manufacturing_operation = mop
	ml.manufacturing_work_order = fix["manufacturing_work_order"]
	ml.from_warehouse = warehouses[0]
	ml.to_warehouse = warehouses[1]
	ml.voucher_type = "Manufacturing Operation"
	ml.voucher_no = mop
	ml.row_name = REPAIR_ROW_TAG

	ml.qty_change = flt(fix["delta"], 3)
	ml.pcs_change = 0
	# All three tiers land on the corrected absolute balance: they are per-operation
	# views of the same figure and this row IS the operation's new balance.
	ml.qty_after_transaction = correct
	ml.qty_after_transaction_item_based = correct
	ml.qty_after_transaction_batch_based = correct
	ml.pcs_after_transaction = pcs
	ml.pcs_after_transaction_item_based = pcs
	ml.pcs_after_transaction_batch_based = pcs

	ml.is_synced = 0
	ml.is_cancelled = 0
	ml.flow_index = (get_last_mop_index(mop) or 0) + 1
	ml.flags.ignore_permissions = True
	ml.insert(ignore_permissions=True)
	return ml.name


def execute(dry_run=True, mwos=None, limit=500, allow_increase=False):
	"""Report, and optionally repair, MOP Log balances inflated by the MWO-wide sum.

	``allow_increase`` gates corrections with a POSITIVE delta -- those raise a metal
	balance rather than removing phantom stock. This defect inflates balances, so a
	shortfall points at a different cause (a missed movement, a cancelled voucher), and
	inventing gold in the ledger to close it would hide that. Those are routed to manual
	review unless explicitly allowed.
	"""
	if isinstance(mwos, str):
		mwos = [mwos]

	findings = audit_post_refining_contamination(mwos=mwos, limit=cint(limit))
	if not findings:
		scope = ", ".join(mwos) if mwos else "all refined MWOs"
		print(
			f"[repair-mop-balance] No contamination found for {scope}. Nothing to do."
		)
		return {"corrections": [], "review": []}

	def _repairable(f):
		if f["straddles"] or f["already_repaired"]:
			return False
		return allow_increase or flt(f["delta"]) < 0

	corrections = [f for f in findings if _repairable(f)]
	review = [f for f in findings if not _repairable(f)]

	print(
		f"[repair-mop-balance] {len(corrections)} provable correction(s), "
		f"{len(review)} needing manual review."
	)
	for c in corrections:
		print(
			"  {mop}  {item}/{batch}  {actual} -> {expected}  (delta {delta})".format(
				mop=c["manufacturing_operation"],
				item=c["item_code"],
				batch=c["batch_no"] or "no-batch",
				actual=c["actual"],
				expected=c["expected"],
				delta=c["delta"],
			)
		)
	for r in review:
		if r["already_repaired"]:
			reason = "already repaired"
		elif r["straddles"]:
			reason = "straddles refining"
		else:
			reason = "would INCREASE balance; pass allow_increase=True"
		print(
			"  REVIEW ({reason}) {mop}  {item}/{batch}  ledger {actual}, "
			"post-refining movements sum to {expected}".format(
				reason=reason,
				mop=r["manufacturing_operation"],
				item=r["item_code"],
				batch=r["batch_no"] or "no-batch",
				actual=r["actual"],
				expected=r["expected"],
			)
		)

	if dry_run:
		print(
			"[repair-mop-balance] DRY RUN — nothing written. "
			"Pass dry_run=False to apply."
		)
		return {"corrections": corrections, "review": review}

	if not corrections:
		print("[repair-mop-balance] Nothing provable to write.")
		return {"corrections": [], "review": review}

	written = []
	for c in corrections:
		written.append(_append_correction(c))
	for mop in sorted({c["manufacturing_operation"] for c in corrections}):
		recalculate_manufacturing_operation_weights(mop)
	frappe.db.commit()

	print(f"[repair-mop-balance] Wrote {len(written)} correcting MOP Log row(s).")
	return {"corrections": corrections, "review": review, "written": written}
