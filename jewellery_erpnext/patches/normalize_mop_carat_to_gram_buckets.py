"""Normalize the carat->gram twins on Manufacturing Operation headers.

``recalculate_manufacturing_operation_weights`` used to build
``{diamond,gemstone}_wt_in_gram`` by rounding EVERY ``(item_code, batch_no)`` MOP Log
row to 3 dp and then summing::

	buckets[f"{prefix}_wt_in_gram"] += flt(qty * 0.2, 3)   # per-row round, THEN sum

so the gram twin stopped being a function of the carat bucket beside it. Observed on
``MOP-050YL``: two diamond rows of 0.497 ct and 0.067 ct rounded to 0.099 + 0.013 =
0.112 g, where the 0.564 ct total converts to ``flt(0.1128, 3)`` = 0.113 g. Its previous
operation ``MOP-0W5T5`` carries the identical two rows and reads 0.113, because
``create_operation_for_next_dept``'s ``copy_doc`` carried the value forward and no
diamond-family row was ever saved against it to trigger the recompute.

``update_wt_detail`` folds the gram twin into ``gross_wt``, so the half-milligram became
a visible 0.001 g shortfall against ``prev_gross_wt`` -- a process loss with no physical
cause, which the next Employee IR then refuses to submit until somebody books it. The
drift runs both ways: ``MOP-IN870`` rounded UP and opened as an unbacked gain instead.

The writer is fixed -- every carat->gram site now goes through
``jewellery_erpnext.utils.carat_to_gram``, which converts the summed carats once. This
script repairs headers already written.

**It repairs only sub-milligram rounding drift, and only where the ledger governs.**
For each candidate it recomputes the affected stone families from the MOP Log through
the normal ``recalculate_manufacturing_operation_weights`` path, so ``gross_wt`` and
``prev_gross_wt`` are re-derived by ``update_wt_detail`` rather than hand-written. Four
classes are reported instead of repaired:

* A family with no surviving MOP Log row after the Work Order Refining cutoff. Its
  header was authored outside MOP Log -- by the MWO->MOP seed, or by ``copy_doc`` from
  the previous operation -- and recomputing would zero real stone weight.
* A correction larger than ``ROUNDING_DRIFT_CEILING``. That is not rounding drift; on
  this bench it is the Tagging operations whose ``gemstone_wt_in_gram`` is stale against
  a ``gemstone_wt`` that ``sync_mwo_weights`` refreshed without it (the Manufacturing
  Work Order doctype has no ``gemstone_wt_in_gram`` column). Those are finished goods
  whose weights already reached Product Certification, so moving them is a business
  decision, not a migration.
* An operation whose CARAT bucket also disagrees with the ledger. That is
  ``repair_mop_header_weight_buckets``'s job, and it must run first.
* FG operations and ``Finished`` operations. An FG header is authored by
  ``sync_mwo_weights``, not by its own ledger.

Parent roll-ups are deliberately NOT re-run. ``_reroll_parents`` in
``repair_mop_header_weight_buckets`` reaches exactly the FG surface this script is scoped
to leave alone, and the operations this repairs are mid-flow -- their PMO/FG rollup has
not happened yet and will pick up the corrected figure on its own.

Idempotent: the detector is the invariant itself, so a second pass finds nothing. Writes
only ``frappe.db.set_value`` on Manufacturing Operation and appends no MOP Log rows, so
the MOP Log EOD lock validator does not apply and it is safe inside an EOD window.

Registered in patches.txt, so ``bench migrate`` applies it. To preview instead::

	bench --site kg-gk execute \\
		jewellery_erpnext.patches.normalize_mop_carat_to_gram_buckets.execute \\
		--kwargs "{'dry_run': True}"

Scope to specific operations::

	bench --site kg-gk execute \\
		jewellery_erpnext.patches.normalize_mop_carat_to_gram_buckets.execute \\
		--kwargs "{'dry_run': True, 'mops': ['MOP-050YL']}"
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	recalculate_manufacturing_operation_weights,
)
from jewellery_erpnext.patches.repair_mop_header_weight_buckets import (
	_post_cutoff_totals,
)
from jewellery_erpnext.utils import carat_to_gram

TOLERANCE = 0.0005

# Half a milligram either way is what per-row rounding of a 3 dp field can produce. A
# correction bigger than this is a different defect and is reported, never written.
ROUNDING_DRIFT_CEILING = 0.001

STONE_PREFIXES = ("diamond", "gemstone")


def _candidates(mops=None):
	"""Headers where a gram twin disagrees with ``carat_to_gram`` of its carat bucket."""
	conditions = ""
	params: dict = {}
	if mops:
		conditions = "AND name IN %(mops)s"
		params["mops"] = tuple(mops)

	return frappe.db.sql(
		f"""
		SELECT name, for_fg, status, manufacturing_work_order, manufacturing_order,
			   diamond_wt, diamond_wt_in_gram, gemstone_wt, gemstone_wt_in_gram,
			   gross_wt, prev_gross_wt
		FROM `tabManufacturing Operation`
		WHERE (
				  ABS(ROUND(diamond_wt  * 0.2, 3) - diamond_wt_in_gram)  > %(tol)s
			   OR ABS(ROUND(gemstone_wt * 0.2, 3) - gemstone_wt_in_gram) > %(tol)s
			  )
			  {conditions}
		""",
		dict(params, tol=TOLERANCE),
		as_dict=True,
	)


def detect(mops=None):
	"""Returns ``(corrections, review)``.

	A correction names the stone families to recompute; everything else is reported with
	the reason it was left alone.
	"""
	corrections, review = [], []

	for header in _candidates(mops):
		drifted = {
			prefix: (
				flt(header.get(f"{prefix}_wt_in_gram"), 3),
				carat_to_gram(header.get(f"{prefix}_wt")),
			)
			for prefix in STONE_PREFIXES
			if abs(
				flt(header.get(f"{prefix}_wt_in_gram"), 3)
				- carat_to_gram(header.get(f"{prefix}_wt"))
			)
			> TOLERANCE
		}
		if not drifted:
			continue

		if cint(header.for_fg):
			review.append(
				{
					"mop": header.name,
					"reason": "FG operation -- header authored by sync_mwo_weights",
					"drift": drifted,
				}
			)
			continue

		if header.status == "Finished":
			review.append(
				{
					"mop": header.name,
					"reason": "operation already Finished -- weights have left the floor",
					"drift": drifted,
				}
			)
			continue

		survivors = _post_cutoff_totals(header.name, header.manufacturing_work_order)

		families, before, after, reasons = [], {}, {}, []
		for prefix, (stored_gram, _derived) in drifted.items():
			ledger_ct = survivors.get(prefix)
			if ledger_ct is None:
				reasons.append(
					f"{prefix}: no surviving MOP Log row -- header authored outside the ledger"
				)
				continue

			carat_delta = flt(ledger_ct - flt(header.get(f"{prefix}_wt"), 3), 3)
			if abs(carat_delta) > TOLERANCE:
				reasons.append(
					f"{prefix}: carat bucket itself drifts from the ledger by {carat_delta} ct "
					"-- run repair_mop_header_weight_buckets first"
				)
				continue

			new_gram = carat_to_gram(ledger_ct)
			gram_delta = flt(new_gram - stored_gram, 3)
			if abs(gram_delta) > ROUNDING_DRIFT_CEILING:
				reasons.append(
					f"{prefix}: correction {gram_delta} g exceeds the "
					f"{ROUNDING_DRIFT_CEILING} g rounding-drift ceiling"
				)
				continue

			families.append(prefix)
			before[prefix] = stored_gram
			after[prefix] = new_gram

		if reasons:
			review.append(
				{"mop": header.name, "reason": "; ".join(reasons), "drift": drifted}
			)
		if not families:
			continue

		corrections.append(
			{
				"mop": header.name,
				"manufacturing_work_order": header.manufacturing_work_order,
				"manufacturing_order": header.manufacturing_order,
				"families": families,
				"before": before,
				"after": after,
				"gross_wt": flt(header.gross_wt, 3),
				"prev_gross_wt": flt(header.prev_gross_wt, 3),
			}
		)

	return corrections, review


def execute(dry_run=False, mops=None):
	corrections, review = detect(mops)

	print(
		f"[normalize-carat-gram] {len(corrections)} operation(s) to recompute, "
		f"{len(review)} needing review."
	)
	for c in corrections:
		for prefix in c["families"]:
			print(
				"  {mop}  {field}  {before} -> {after}  (delta {delta})  "
				"gross_wt {gross} vs prev {prev}".format(
					mop=c["mop"],
					field=f"{prefix}_wt_in_gram",
					before=c["before"][prefix],
					after=c["after"][prefix],
					delta=flt(c["after"][prefix] - c["before"][prefix], 3),
					gross=c["gross_wt"],
					prev=c["prev_gross_wt"],
				)
			)
	for r in review:
		print(f"  REVIEW {r['mop']}: {r['reason']}")

	if dry_run:
		print("[normalize-carat-gram] DRY RUN -- nothing written.")
		return {"corrections": corrections, "review": review}

	if not corrections:
		print("[normalize-carat-gram] Nothing to repair.")
		return {"corrections": [], "review": review}

	for c in corrections:
		# The normal path: rewrites the family from the ledger and lets
		# update_wt_detail re-derive gross_wt and prev_gross_wt.
		recalculate_manufacturing_operation_weights(
			c["mop"], prefixes=tuple(c["families"])
		)
	frappe.db.commit()

	print(f"[normalize-carat-gram] Repaired {len(corrections)} operation(s).")
	return {"corrections": corrections, "review": review}
