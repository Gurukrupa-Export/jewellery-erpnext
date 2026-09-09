# Copyright (c) 2026, Nirali and contributors
# SPDX-License-Identifier: MIT
"""Data backfill: FG BOM gross_weight / Serial No custom_gross_wt.

Re-derives ``gross_weight`` on every BOM that has at least one Serial No
referencing it (via ``Serial No.custom_bom_no``), then mirrors the corrected
value into each linked Serial No's ``custom_gross_wt``.

The previous ``create_finished_goods_bom`` stored carat→gram values unrounded
(``x / 5``), which drifted +0.001 from the MWO/SNC value at a rounding
boundary.  The fixed builder now uses ``carat_to_gram + flt(..., 3)``; this
patch ships that correction to records written before the fix.

**It repairs only sub-milligram rounding drift.** BOM and Serial No are judged
independently -- a BOM already canonical does not block repairing a stale
Serial No (``custom_gross_wt`` is a stored ``fetch_from`` copy that does not
refresh when the BOM changes), and vice versa -- and a correction larger than
``ROUNDING_DRIFT_CEILING`` is reported for review and never written.

Idempotent: the detector is the invariant itself, so a second pass finds
nothing. Writes only ``frappe.db.set_value``.

Registered in patches.txt, so ``bench migrate`` applies it. To preview::

    bench --site <site> execute \\
        jewellery_erpnext.patches.fix_bom_gross_weight.execute \\
        --kwargs "{'dry_run': True}"
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.utils import carat_to_gram

# The old FG BOM builder stored carat->gram raw with ``/ 5`` (no rounding),
# leaving up to 0.0004 g of residue on the gram twins and gross_weight. Canonical
# values are float-identical when they need no change, so a one-tenth-milligram
# bar separates "already equal" from "has unrounded residue / drift worth
# snapping" without reacting to binary float noise.
TOLERANCE = 0.0001

# A correction bigger than this is not carat rounding drift; it is a different
# defect and is reported for review, never written.
ROUNDING_DRIFT_CEILING = 0.001

BATCH = 500

_BOM_WEIGHT_FIELDS = [
	"diamond_weight",
	"gemstone_weight",
	"metal_weight",
	"finding_weight_",
	"other_weight",
	"total_diamond_weight_in_gms",
	"total_gemstone_weight_in_gms",
	"gross_weight",
]


def _bom_serial_pages(batch_size):
	"""Yield pages of (serial_no, custom_bom_no) pairs, paginated by serial name."""
	start = ""
	while True:
		rows = frappe.db.get_all(
			"Serial No",
			filters={
				"custom_bom_no": ("is", "set"),
				"name": (">", start),
			},
			fields=["name", "custom_bom_no"],
			order_by="name asc",
			limit_page_length=batch_size,
		)
		if not rows:
			break
		yield rows
		start = rows[-1].name


def _family_corrections(bom):
	"""Return {field: value} for the corrected BOM weights."""
	diamond_gram = carat_to_gram(flt(bom.get("diamond_weight")))
	gemstone_gram = carat_to_gram(flt(bom.get("gemstone_weight")))
	gross = flt(
		flt(bom.get("metal_weight"))
		+ flt(bom.get("finding_weight_"))
		+ diamond_gram
		+ gemstone_gram
		+ flt(bom.get("other_weight")),
		3,
	)
	return {
		"total_diamond_weight_in_gms": diamond_gram,
		"total_gemstone_weight_in_gms": gemstone_gram,
		"gross_weight": gross,
	}


def _field_deltas(stored, corrections):
	"""Return {field: raw delta} where |stored - corrected| exceeds TOLERANCE.

	The delta is the unclamped float difference, not ``flt(delta, 3)`` -- clamping
	the delta would round a sub-milligram unrounded residue (31.1992 - 31.199) to
	zero and miss the exact BOM artifact this patch repairs. The calling code
	applies the rounding-drift ceiling to ``flt(delta, 3)``.
	"""
	issues = {}
	for field, corrected in corrections.items():
		raw = corrected - flt(stored.get(field))
		if abs(raw) > TOLERANCE:
			issues[field] = raw
	return issues


def _detect(batch_size=BATCH):
	"""Returns ``(bom_corrections, serial_corrections, review)``.

	A serial correction is ``{serial_no: bom_name}`` when the BOM is either
	repaired or already canonical but the Serial No mirror is stale. Anything
	exceeding the rounding-drift ceiling is reported for review, never written.
	"""
	serials_by_bom = {}
	for page in _bom_serial_pages(batch_size):
		for row in page:
			serials_by_bom.setdefault(row.custom_bom_no, []).append(row.name)

	bom_corrections, serial_corrections, review = [], {}, []
	bom_list = sorted(serials_by_bom)

	for i in range(0, len(bom_list), batch_size):
		batch = bom_list[i : i + batch_size]
		boms = frappe.get_all(
			"BOM",
			filters={"name": ("in", batch)},
			fields=_BOM_WEIGHT_FIELDS,
		)
		for bom in boms:
			corrections_map = _family_corrections(bom)
			bom_issues = _field_deltas(bom, corrections_map)

			# Split out any correction that is not rounding-sized.
			reviews, safe = [], {}
			for field, delta in bom_issues.items():
				clamped = flt(delta, 3)
				if abs(clamped) > ROUNDING_DRIFT_CEILING:
					reviews.append(f"BOM {field} {clamped} g exceeds the ceiling")
				else:
					safe[field] = delta
			bom_issues = safe

			for reason in reviews:
				review.append({"bom": bom.name, "reason": reason})

			if bom_issues:
				bom_corrections.append(
					{
						"bom": bom.name,
						"issues": bom_issues,
						"corrections_map": corrections_map,
					}
				)

			# Serial mirror judged independently of the BOM, but only when the
			# canonical gross is trusted. A BOM beyond the ceiling makes the
			# canonical value itself suspect, so its serials go to review too.
			serial_names = serials_by_bom.get(bom.name, [])
			if reviews:
				for name in serial_names:
					review.append(
						{"bom": bom.name, "reason": "serial left for review with BOM"}
					)
				continue

			stale_serials = []
			if serial_names:
				stale_serials = frappe.db.get_all(
					"Serial No",
					filters={
						"name": ("in", serial_names),
						"custom_gross_wt": (
							"!=",
							corrections_map["gross_weight"],
						),
					},
					fields=["name"],
				)
			for r in stale_serials:
				serial_corrections[r.name] = corrections_map["gross_weight"]

	return bom_corrections, serial_corrections, review


def execute(dry_run=False):
	bom_corrections, serial_corrections, review = _detect()

	print(
		f"[fix-bom-gross-wt] {len(bom_corrections)} BOM(s) to repair, "
		f"{len(serial_corrections)} Serial No(s) to mirror, "
		f"{len(review)} needing review."
	)
	for c in bom_corrections:
		print(
			"  BOM {bom}  fields={fields}  gross={gross}".format(
				bom=c["bom"],
				fields=", ".join(sorted(c["issues"])),
				gross=c["corrections_map"]["gross_weight"],
			)
		)
	for r in review:
		print(f"  REVIEW {r['bom']}: {r['reason']}")

	if dry_run:
		print("[fix-bom-gross-wt] DRY RUN -- nothing written.")
		return {
			"bom_corrections": bom_corrections,
			"serial_corrections": serial_corrections,
			"review": review,
		}

	if not bom_corrections and not serial_corrections:
		print("[fix-bom-gross-wt] Nothing to repair.")
		return {
			"bom_corrections": [],
			"serial_corrections": {},
			"review": review,
		}

	for c in bom_corrections:
		frappe.db.set_value(
			"BOM",
			c["bom"],
			{field: c["corrections_map"][field] for field in c["issues"]},
			update_modified=False,
		)

	for i in range(0, len(serial_corrections), BATCH):
		chunk = list(serial_corrections.items())[i : i + BATCH]
		for sn, gross in chunk:
			frappe.db.set_value(
				"Serial No", sn, "custom_gross_wt", gross, update_modified=False
			)
	frappe.db.commit()

	print(
		f"[fix-bom-gross-wt] Repaired {len(bom_corrections)} BOM(s) and "
		f"{len(serial_corrections)} Serial No(s)."
	)
	return {
		"bom_corrections": bom_corrections,
		"serial_corrections": serial_corrections,
		"review": review,
	}
