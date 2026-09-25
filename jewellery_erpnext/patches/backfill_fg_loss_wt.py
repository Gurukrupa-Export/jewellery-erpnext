"""Re-derive loss_wt on FG work orders written before F14.

``ManufacturingWorkOrder.sync_mwo_weights`` used to take the FG work order's ``loss_wt``
from the LATEST operation of each sibling work order. Loss is measured on the operation
that was received and the next operation starts at 0, so every FG header it wrote reads 0
loss. It now sums every loss (negative ``loss_wt``) over every sibling operation
(``cumulative_loss_wt``; a positive value is material coming in, such as the casting
receipt). This script applies the same rule to headers written before the fix.

It writes ``loss_wt`` only -- on the FG work order and on the FG operation that
``sync_mwo_weights`` writes to -- with ``frappe.db.set_value(update_modified=False)``. It
appends no MOP Log rows and touches no other weight, so it is safe inside an EOD window.

Skipped, by construction:

* FG work orders ``sync_mwo_weights`` has not run on yet (header ``gross_wt`` is 0). They
  pick up the new rule when it runs, as ``repair_mop_header_weight_buckets._reroll_parents``
  also assumes.
* FG work orders whose siblings have no operation: the sync never writes loss for them.

Listed for REVIEW and left alone: an FG operation whose ``loss_wt`` differs from its work
order's header. The sync writes both to the same figure, so a difference means another
writer (an Employee IR on the FG operation itself) owns it.

NOT registered in patches.txt: this changes historical figures, so it runs by hand in the
controlled remediation phase. It is a dry run unless told otherwise:

    bench --site <site> execute jewellery_erpnext.patches.backfill_fg_loss_wt.execute

    bench --site <site> execute jewellery_erpnext.patches.backfill_fg_loss_wt.execute \\
        --kwargs "{'dry_run': False}"

Scope to specific FG work orders with ``'mwos': ['MWO-...']``.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	cumulative_loss_wt,
)

TOLERANCE = 0.0005


def _fg_operation(fg):
	"""The operation sync_mwo_weights writes to: the header link, else the latest one."""
	if fg.manufacturing_operation:
		return fg.manufacturing_operation
	return frappe.db.get_value(
		"Manufacturing Operation",
		{"manufacturing_work_order": fg.name},
		"name",
		order_by="creation desc",
	)


def detect(mwos=None):
	"""``(changes, review, skipped)`` for every submitted FG work order in scope."""
	filters = {"for_fg": 1, "docstatus": 1}
	if mwos:
		filters["name"] = ["in", list(mwos)]
	fg_orders = frappe.get_all(
		"Manufacturing Work Order",
		filters=filters,
		fields=[
			"name",
			"manufacturing_order",
			"manufacturing_operation",
			"gross_wt",
			"loss_wt",
		],
		order_by="name",
	)

	changes, review, skipped = [], [], []
	for fg in fg_orders:
		if not flt(fg.gross_wt):
			skipped.append({"mwo": fg.name, "reason": "not synced yet (gross_wt 0)"})
			continue
		# The same siblings sync_mwo_weights reads.
		siblings = frappe.get_all(
			"Manufacturing Work Order",
			filters={
				"manufacturing_order": fg.manufacturing_order,
				"name": ["!=", fg.name],
				"for_fg": 0,
				"docstatus": 1,
			},
			pluck="name",
		)
		if not siblings or not frappe.db.exists(
			"Manufacturing Operation", {"manufacturing_work_order": ["in", siblings]}
		):
			skipped.append({"mwo": fg.name, "reason": "no sibling operation"})
			continue

		after = cumulative_loss_wt(siblings)
		before = flt(fg.loss_wt, 3)
		fg_mop = _fg_operation(fg)
		mop_loss = (
			flt(frappe.db.get_value("Manufacturing Operation", fg_mop, "loss_wt"), 3)
			if fg_mop
			else None
		)

		if fg_mop and abs(mop_loss - before) > TOLERANCE:
			review.append(
				{
					"mwo": fg.name,
					"mop": fg_mop,
					"reason": f"FG operation loss {mop_loss} differs from header {before}",
				}
			)
			continue
		if abs(after - before) <= TOLERANCE:
			continue
		changes.append(
			{
				"mwo": fg.name,
				"mop": fg_mop,
				"manufacturing_order": fg.manufacturing_order,
				"before": before,
				"after": after,
			}
		)
	return changes, review, skipped


def execute(dry_run=True, mwos=None):
	changes, review, skipped = detect(mwos)

	print(
		f"[backfill-fg-loss] {len(changes)} FG work order(s) to update, "
		f"{len(review)} needing review, {len(skipped)} skipped."
	)
	for c in changes:
		print(f"  {c['mwo']} / {c['mop']}  loss_wt {c['before']} -> {c['after']}")
	for r in review:
		print(f"  REVIEW {r['mwo']} / {r['mop']}: {r['reason']}")

	if dry_run:
		print("[backfill-fg-loss] DRY RUN — nothing written.")
		return {"changes": changes, "review": review, "skipped": skipped}

	for c in changes:
		frappe.db.set_value(
			"Manufacturing Work Order",
			c["mwo"],
			"loss_wt",
			c["after"],
			update_modified=False,
		)
		if c["mop"]:
			frappe.db.set_value(
				"Manufacturing Operation",
				c["mop"],
				"loss_wt",
				c["after"],
				update_modified=False,
			)
	frappe.db.commit()

	print(f"[backfill-fg-loss] Updated {len(changes)} FG work order(s).")
	return {"changes": changes, "review": review, "skipped": skipped}
