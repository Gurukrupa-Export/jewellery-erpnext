# Copyright (c) 2026, Nirali and contributors
# SPDX-License-Identifier: MIT
"""Data backfill: FG MWO gross_wt / FG MOP gross_wt.

Re-derives ``gross_wt`` on every ``for_fg=1`` Manufacturing Work Order from
the MWO's own component buckets using the canonical round-of-sum:

    gross_wt = flt(net_wt + finding_wt + carat_to_gram(diamond_wt)
                   + carat_to_gram(gemstone_wt) + other_wt, 3)

This ships the corrected ``sync_mwo_weights`` logic
(manufacturing_work_order.py) to FG MWOs written before the fix, whose
``gross_wt`` was stored as the SQL SUM of sibling MOP headers (each rounded
independently) and drifted +0.001 at a carat rounding boundary. It also
propagates corrected values to the FG MWO's Manufacturing Operation so that
SNC submission picks up correct weights.

**It repairs only sub-milligram rounding drift.** Each invariant is judged
independently -- a ``gross_wt`` already canonical does not block repairing a
stale ``diamond_wt_in_gram`` or a stale FG MOP -- and a correction larger than
``ROUNDING_DRIFT_CEILING`` is reported for review and never written. The FG
MOP target mirrors the runtime ``sync_mwo_weights``: the MWO's linked
``manufacturing_operation`` first, latest-by-creation fallback only when that
is blank.

Idempotent: the detector is the invariant itself, so a second pass finds
nothing. Writes only ``frappe.db.set_value``.

Registered in patches.txt, so ``bench migrate`` applies it. To preview::

    bench --site <site> execute \\
        jewellery_erpnext.patches.fix_mwo_gross_weight.execute \\
        --kwargs "{'dry_run': True}"
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.utils import carat_to_gram

# Stored fields can carry unrounded residue below 1 mg (the old FG BOM builder
# wrote ``diamond_weight / 5`` raw, leaving up to 0.0004 g of carat residue;
# MWO fields are stored at 3 dp). Canonical values are float-identical when ever
# they need no change, so a one-tenth-milligram bar separates "already equal"
# from "has unrounded residue / 3 dp drift worth snapping".
TOLERANCE = 0.0001

# A correction bigger than this is not carat rounding drift; it is a different
# defect and is reported for review, never written.
ROUNDING_DRIFT_CEILING = 0.001

BATCH = 500

# (field, corrected value) pairs judged independently on MWO and FG MOP.
_FIELD_CORRECTIONS = ("gross_wt", "diamond_wt_in_gram", "gemstone_wt_in_gram")


def _mwo_pages(batch_size):
	"""Yield pages of FG MWO names, paginated by name."""
	start = ""
	while True:
		rows = frappe.db.get_all(
			"Manufacturing Work Order",
			filters={
				"for_fg": 1,
				"name": (">", start),
			},
			fields=["name"],
			order_by="name asc",
			limit_page_length=batch_size,
		)
		if not rows:
			break
		yield [r.name for r in rows]
		start = rows[-1].name


def _family_corrections(mwo):
	"""Return the canonical {field: value} for the round-of-sum weights."""
	gross = flt(
		flt(mwo.get("net_wt"))
		+ flt(mwo.get("finding_wt"))
		+ carat_to_gram(flt(mwo.get("diamond_wt")))
		+ carat_to_gram(flt(mwo.get("gemstone_wt")))
		+ flt(mwo.get("other_wt")),
		3,
	)
	return {
		"gross_wt": gross,
		"diamond_wt_in_gram": carat_to_gram(flt(mwo.get("diamond_wt"))),
		"gemstone_wt_in_gram": carat_to_gram(flt(mwo.get("gemstone_wt"))),
	}


def _field_deltas(stored, corrections):
	"""Return {field: raw delta} where |stored - corrected| exceeds TOLERANCE.

	The delta is the unclamped float difference, not ``flt(delta, 3)`` -- clamping
	the delta would round a sub-milligram residue (0.8512 - 0.851) to zero and
	miss it. The calling code applies the rounding-drift ceiling to
	``flt(delta, 3)``.
	"""
	issues = {}
	for field in _FIELD_CORRECTIONS:
		raw = corrections[field] - flt(stored.get(field))
		if abs(raw) > TOLERANCE:
			issues[field] = raw
	return issues


def _detect(batch_size=BATCH):
	"""Returns ``(corrections, review)`` for FG MWOs.

	A correction names the fields to fix; anything exceeding the rounding-drift
	ceiling (or unresolved) is reported for review, never auto-written.
	"""
	corrections, review = [], []

	for page in _mwo_pages(batch_size):
		mwos = frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ("in", page)},
			fields=[
				"name",
				"net_wt",
				"finding_wt",
				"diamond_wt",
				"gemstone_wt",
				"other_wt",
				"diamond_wt_in_gram",
				"gross_wt",
				"manufacturing_operation",
			],
		)
		for mwo in mwos:
			corrections_map = _family_corrections(mwo)
			mwo_issues = _field_deltas(mwo, corrections_map)

			# MWO never stores gemstone_wt_in_gram (no column); drop it there.
			mwo_issues.pop("gemstone_wt_in_gram", None)

			# Split out any correction that is not rounding-sized.
			reviews, safe = [], {}
			for field, delta in mwo_issues.items():
				clamped = flt(delta, 3)
				if abs(clamped) > ROUNDING_DRIFT_CEILING:
					reviews.append(f"{field} {clamped} g exceeds the ceiling")
				else:
					safe[field] = delta
			mwo_issues = safe

			# A gross_wt beyond the ceiling makes the canonical gross itself
			# suspect, so the MOP gross mirror is not auto-written either.
			gross_suspect = any(r.startswith("gross_wt") for r in reviews)

			# FG MOP target mirrors runtime sync_mwo_weights: linked op first,
			# latest-by-creation fallback only when blank.
			fg_mop = mwo.get("manufacturing_operation")
			if not fg_mop:
				fg_mop = frappe.db.get_value(
					"Manufacturing Operation",
					{"manufacturing_work_order": mwo.name},
					"name",
					order_by="creation desc",
				)

			mop_issues = {}
			if fg_mop:
				mop = frappe.db.get_value(
					"Manufacturing Operation",
					fg_mop,
					["gross_wt", "diamond_wt_in_gram", "gemstone_wt_in_gram"],
					as_dict=True,
				)
				if not mop:
					review.append(
						{
							"mwo": mwo.name,
							"reason": (
								f"linked Manufacturing Operation {fg_mop} is missing"
							),
						}
					)
					fg_mop = None
				else:
					for field, delta in _field_deltas(mop, corrections_map).items():
						clamped = flt(delta, 3)
						if abs(clamped) > ROUNDING_DRIFT_CEILING:
							reviews.append(f"MOP {field} {clamped} g exceeds the ceiling")
						elif field == "gross_wt" and gross_suspect:
							reviews.append("MOP gross_wt left for review with MWO")
						else:
							mop_issues[field] = delta

			for reason in reviews:
				review.append({"mwo": mwo.name, "reason": reason})

			if not mwo_issues and not mop_issues:
				continue

			corrections.append(
				{
					"mwo": mwo.name,
					"mwo_issues": mwo_issues,
					"mop": fg_mop,
					"mop_issues": mop_issues,
					"corrections_map": corrections_map,
				}
			)

	return corrections, review


def execute(dry_run=False):
	corrections, review = _detect()

	print(
		f"[fix-mwo-gross-wt] {len(corrections)} MWO(s) to repair, "
		f"{len(review)} needing review."
	)
	for c in corrections:
		fields = sorted(set(c["mwo_issues"]) | set(c["mop_issues"]))
		print(
			"  {mwo}  fields={fields}  mop={mop}  gross={gross}".format(
				mwo=c["mwo"],
				fields=", ".join(fields),
				mop=c["mop"],
				gross=c["corrections_map"]["gross_wt"],
			)
		)
	for r in review:
		print(f"  REVIEW {r['mwo']}: {r['reason']}")

	if dry_run:
		print("[fix-mwo-gross-wt] DRY RUN -- nothing written.")
		return {"corrections": corrections, "review": review}

	if not corrections:
		print("[fix-mwo-gross-wt] Nothing to repair.")
		return {"corrections": [], "review": review}

	for c in corrections:
		if c["mwo_issues"]:
			frappe.db.set_value(
				"Manufacturing Work Order",
				c["mwo"],
				{field: c["corrections_map"][field] for field in c["mwo_issues"]},
				update_modified=False,
			)
		if c["mop"] and c["mop_issues"]:
			frappe.db.set_value(
				"Manufacturing Operation",
				c["mop"],
				{field: c["corrections_map"][field] for field in c["mop_issues"]},
				update_modified=False,
			)
	frappe.db.commit()

	print(f"[fix-mwo-gross-wt] Repaired {len(corrections)} MWO/MOP set(s).")
	return {"corrections": corrections, "review": review}
