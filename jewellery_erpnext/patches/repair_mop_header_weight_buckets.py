"""Repair Manufacturing Operation header weight buckets that drifted from the MOP Log ledger.

``MOPLog.validate`` used to stamp ``{prefix}_wt`` on the Manufacturing Operation with the
saved row's own ``qty_after_transaction``. That field is the *family-wide* tier -- every
``F-`` item shares the ``finding`` bucket -- but only the last row written by
``create_mop_log_for_stock_transfer_to_mo`` ever held it correctly. The clone writers
(``create_mop_log_for_department_ir``, ``creste_mop_log_for_employee_ir``,
``create_mop_log_for_employee_ir_receive``) copy it verbatim per row, and
``update_new_mop_wtg`` decremented it by a per-``(item, batch)`` loss. The stamp was an
absolute set_value, so the header became last-writer-wins and every sibling row's
movement was silently dropped.

Observed on ``MOP-7Q48F``: the previous operation closed with findings 0.622 (SOP) and
1.353 (PSS), sharing a family total of 1.975. Employee IR ``EMP-IR-Labh-2026-04585``
booked losses of 0.014 and 0.030. The SOP clone wrote 1.992 - 0.014 = 1.978, the PSS
clone wrote 1.975 - 0.030 = 1.945, and PSS saved last -- so ``finding_wt`` read 1.945
against a true batch-tier sum of 0.608 + 1.323 = 1.931. Equivalently 1.945 == 0.622 +
1.323: the SOP finding was carried at its PRE-loss weight. ``gross_wt`` inflated to 6.234
against the 6.220 the Employee IR itself received, hiding 0.014 g of booked production
loss.

Both writers are fixed. ``MOPLog.validate`` now derives the bucket from the batch tier via
``recalculate_manufacturing_operation_weights``, and ``update_new_mop_wtg`` accumulates the
family and item tiers as running totals. This script repairs headers already written.

**It only repairs what the ledger governs.** For each drifted operation it recomputes only
the weight families that still have a surviving MOP Log row after the Work Order Refining
cutoff. Two classes are therefore left alone, by construction rather than by a skip list:

* A family whose every row pre-dates a Refining Entry drops out entirely, so a header the
  refining zero-out deliberately set to 0 is never rebuilt from metal that has physically
  left for the refinery. ``MOP-946HU`` (header 0, raw ledger 3.73) and ``MOP-09I6V``
  (header 0, raw ledger 3.53) stay at 0.
* A family with no MOP Log rows at all is untouched, so the diamond/gemstone weights that
  ``create_manufacturing_operation`` seeds from the BOM before any stone is issued survive.

FG operations are skipped and reported: their header is authored by
``ManufacturingWorkOrder.sync_mwo_weights``, which force-writes the FG MOP from the
MWO-wide aggregate, not from that operation's own ledger.

The detector is the same reconciliation the verification query runs, so a re-run after a
successful pass finds nothing -- the patch is idempotent and writes no MOP Log rows, only
``frappe.db.set_value`` on Manufacturing Operation. Unlike
``repair_mwo_wide_mop_log_balances`` it appends no ledger rows, so the MOP Log EOD lock
validator does not apply and it is safe to run inside an EOD window.

Registered in patches.txt, so ``bench migrate`` applies it. To preview instead:

    bench --site kg-gk execute \\
        jewellery_erpnext.patches.repair_mop_header_weight_buckets.execute \\
        --kwargs "{'dry_run': True}"

Scope to specific operations:

    bench --site kg-gk execute \\
        jewellery_erpnext.patches.repair_mop_header_weight_buckets.execute \\
        --kwargs "{'dry_run': True, 'mops': ['MOP-7Q48F']}"
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	FIELD_MAP,
	drop_pre_refining_rows,
	get_current_mop_balance_rows,
	recalculate_manufacturing_operation_weights,
)

TOLERANCE = 0.0005

# Header field written for each item-code family. D/G carry two more buckets
# (``_wt_in_gram``, ``_pcs``) which recalculate_... rewrites alongside ``_wt``;
# comparing ``_wt`` alone is enough to detect the drift.
BUCKET_FIELD = {prefix: f"{prefix}_wt" for prefix in FIELD_MAP.values()}


def _raw_ledger_totals(mops=None):
	"""``{mop: {prefix: total}}`` from the latest non-cancelled row per (item, batch).

	One grouped query over the whole table so the scan does not cost a round trip per
	operation. The refining cutoff is applied later, per candidate, because it needs a
	Refining Entry lookup that is only worth paying for on an operation that actually
	drifted.
	"""
	conditions = ""
	params: dict = {}
	if mops:
		conditions = "AND l.manufacturing_operation IN %(mops)s"
		params["mops"] = tuple(mops)

	rows = frappe.db.sql(
		f"""
		SELECT l.manufacturing_operation AS mop,
		       SUBSTRING(l.item_code, 1, 1) AS prefix_char,
		       SUM(l.qty_after_transaction_batch_based) AS ledger
		FROM `tabMOP Log` l
		INNER JOIN (
			SELECT manufacturing_operation, item_code, batch_no, MAX(creation) AS mx
			FROM `tabMOP Log`
			WHERE is_cancelled = 0 AND IFNULL(manufacturing_operation, '') <> ''
			GROUP BY manufacturing_operation, item_code, batch_no
		) latest
		  ON  latest.manufacturing_operation = l.manufacturing_operation
		  AND latest.item_code <=> l.item_code
		  AND latest.batch_no  <=> l.batch_no
		  AND latest.mx         = l.creation
		WHERE l.is_cancelled = 0 {conditions}
		GROUP BY l.manufacturing_operation, SUBSTRING(l.item_code, 1, 1)
		""",
		params,
		as_dict=True,
	)

	totals: dict = {}
	for row in rows:
		prefix = FIELD_MAP.get(row.prefix_char)
		if not prefix:
			continue
		totals.setdefault(row.mop, {})[prefix] = flt(row.ledger, 3)
	return totals


def _post_cutoff_totals(mop_name, mwo):
	"""``{prefix: total}`` counting only rows that survive the MWO's refining cutoff."""
	rows = drop_pre_refining_rows(
		get_current_mop_balance_rows(
			mop_name,
			include_fields=[
				"item_code",
				"batch_no",
				"qty_after_transaction_batch_based",
				"creation",
			],
		),
		mwo,
	)
	totals: dict = {}
	for row in rows:
		prefix = FIELD_MAP.get((row.get("item_code") or "")[:1])
		if not prefix:
			continue
		totals[prefix] = flt(
			flt(totals.get(prefix)) + flt(row.get("qty_after_transaction_batch_based")),
			3,
		)
	return totals


def detect(mops=None):
	"""Operations whose header buckets disagree with the ledger.

	Returns ``(corrections, review)``. A correction names the families to recompute --
	only those the surviving ledger actually governs.
	"""
	raw = _raw_ledger_totals(mops)
	if not raw:
		return [], []

	headers = {
		d.name: d
		for d in frappe.db.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", list(raw)]},
			fields=["name", "for_fg", "manufacturing_work_order", "manufacturing_order"]
			+ sorted(set(BUCKET_FIELD.values())),
			limit_page_length=0,
		)
	}

	corrections, review = [], []
	for mop_name, ledger in raw.items():
		header = headers.get(mop_name)
		if not header:
			continue

		drifted = {
			prefix: (flt(header.get(BUCKET_FIELD[prefix]), 3), total)
			for prefix, total in ledger.items()
			if abs(flt(header.get(BUCKET_FIELD[prefix]), 3) - total) > TOLERANCE
		}
		if not drifted:
			continue

		if cint(header.for_fg):
			review.append(
				{
					"mop": mop_name,
					"reason": "FG operation -- header authored by sync_mwo_weights",
					"drift": drifted,
				}
			)
			continue

		# Re-measure against the refining cutoff: a family whose rows all pre-date a
		# Refining Entry is not ledger-governed any more and must keep its zeroed header.
		survivors = _post_cutoff_totals(mop_name, header.manufacturing_work_order)
		families = []
		for prefix, (stored, _raw_total) in drifted.items():
			if prefix not in survivors:
				review.append(
					{
						"mop": mop_name,
						"reason": (
							f"{prefix}: every ledger row pre-dates the refining cutoff "
							f"-- leaving header at {stored}"
						),
						"drift": {prefix: drifted[prefix]},
					}
				)
				continue
			if abs(stored - survivors[prefix]) > TOLERANCE:
				families.append(prefix)

		if families:
			corrections.append(
				{
					"mop": mop_name,
					"manufacturing_work_order": header.manufacturing_work_order,
					"manufacturing_order": header.manufacturing_order,
					"families": sorted(families),
					"before": {
						p: flt(header.get(BUCKET_FIELD[p]), 3) for p in families
					},
					"after": {p: survivors[p] for p in families},
				}
			)

	corrections.sort(key=lambda c: c["mop"])
	review.sort(key=lambda r: r["mop"])
	return corrections, review


def _reroll_parents(corrections):
	"""Re-run the roll-ups that inherited the wrong figure.

	``sync_mwo_weights`` sums the latest MOP per sibling MWO onto the FG MWO and its FG
	MOP; ``set_pmo_weight_details_in_bulk`` sums MWO weights onto the PMO. Both are
	normally driven by document events that ``frappe.db.set_value`` bypasses, so they are
	re-run explicitly here -- but only where the FG MWO already carries weights, i.e.
	where the Tagging sync has already run. Firing it earlier would seed an FG MWO with
	figures it is not meant to hold yet; those pick up the corrected values on their own.
	"""
	rerolled = []
	pmos = {c["manufacturing_order"] for c in corrections if c["manufacturing_order"]}
	for pmo in sorted(pmos):
		fg_mwo = frappe.db.get_value(
			"Manufacturing Work Order",
			{"manufacturing_order": pmo, "for_fg": 1, "docstatus": 1},
			["name", "gross_wt"],
			as_dict=True,
		)
		if not fg_mwo or not flt(fg_mwo.gross_wt):
			continue
		frappe.get_doc("Manufacturing Work Order", fg_mwo.name).sync_mwo_weights()
		fg_mop = frappe.db.get_value(
			"Manufacturing Operation",
			{"manufacturing_work_order": fg_mwo.name},
			"name",
			order_by="creation desc",
		)
		if fg_mop:
			frappe.get_doc(
				"Manufacturing Operation", fg_mop
			).set_pmo_weight_details_in_bulk()
		rerolled.append(pmo)
	return rerolled


def execute(dry_run=False, mops=None):
	corrections, review = detect(mops)

	print(
		f"[repair-mop-buckets] {len(corrections)} operation(s) to recompute, "
		f"{len(review)} needing review."
	)
	for c in corrections:
		for prefix in c["families"]:
			print(
				"  {mop}  {field}  {before} -> {after}  (delta {delta})".format(
					mop=c["mop"],
					field=BUCKET_FIELD[prefix],
					before=c["before"][prefix],
					after=c["after"][prefix],
					delta=flt(c["after"][prefix] - c["before"][prefix], 3),
				)
			)
	for r in review:
		print(f"  REVIEW {r['mop']}: {r['reason']}")

	if dry_run:
		print("[repair-mop-buckets] DRY RUN — nothing written.")
		return {"corrections": corrections, "review": review}

	if not corrections:
		print("[repair-mop-buckets] Nothing to repair.")
		return {"corrections": [], "review": review}

	for c in corrections:
		recalculate_manufacturing_operation_weights(
			c["mop"], prefixes=tuple(c["families"])
		)
	rerolled = _reroll_parents(corrections)
	frappe.db.commit()

	print(
		f"[repair-mop-buckets] Repaired {len(corrections)} operation(s); "
		f"re-rolled {len(rerolled)} parent order(s)."
	)
	return {"corrections": corrections, "review": review, "rerolled": rerolled}
