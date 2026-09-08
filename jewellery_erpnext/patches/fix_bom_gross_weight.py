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

Idempotent: re-running yields zero changes once values match.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.utils import carat_to_gram

BATCH = 500


def _correct_gross_weight(bom):
	"""Re-derive gross_weight from stored BOM fields using carat_to_gram."""
	diamond_gram = carat_to_gram(flt(bom.get("diamond_weight")))
	gemstone_gram = carat_to_gram(flt(bom.get("gemstone_weight")))
	other_weight = flt(bom.get("other_weight"))
	gross = flt(
		flt(bom.get("metal_weight"))
		+ flt(bom.get("finding_weight_"))
		+ diamond_gram
		+ gemstone_gram
		+ other_weight,
		3,
	)
	return gross


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


def execute():
	bom_fixes = 0
	serial_fixes = 0

	# Collect BOM names that have at least one FG serial referencing them.
	bom_names = set()
	for page in _bom_serial_pages(BATCH):
		for r in page:
			bom_names.add(r.custom_bom_no)

	if not bom_names:
		return

	bom_list = sorted(bom_names)

	for i in range(0, len(bom_list), BATCH):
		batch = bom_list[i : i + BATCH]
		boms = frappe.get_all(
			"BOM",
			filters={"name": ("in", batch)},
			fields=[
				"name",
				"diamond_weight",
				"gemstone_weight",
				"metal_weight",
				"finding_weight_",
				"total_diamond_weight_in_gms",
				"total_gemstone_weight_in_gms",
				"other_weight",
				"gross_weight",
			],
		)
		for bom in boms:
			corrected = _correct_gross_weight(bom)
			corrected_diamond = carat_to_gram(flt(bom.get("diamond_weight")))
			corrected_gemstone = carat_to_gram(flt(bom.get("gemstone_weight")))

			if (
				flt(bom.gross_weight) == corrected
				and flt(bom.get("total_diamond_weight_in_gms")) == corrected_diamond
				and flt(bom.get("total_gemstone_weight_in_gms"))
				== corrected_gemstone
			):
				continue

			frappe.db.set_value(
				"BOM",
				bom.name,
				{
					"total_diamond_weight_in_gms": corrected_diamond,
					"total_gemstone_weight_in_gms": corrected_gemstone,
					"gross_weight": corrected,
				},
				update_modified=False,
			)

			# Fix all Serial Nos linked to this BOM.
			serials = frappe.db.get_all(
				"Serial No",
				filters={"custom_bom_no": bom.name},
				fields=["name"],
			)
			if serials:
				for j in range(0, len(serials), BATCH):
					sn_batch = [s.name for s in serials[j : j + BATCH]]
					frappe.db.set_value(
						"Serial No",
						{"name": ("in", sn_batch)},
						"custom_gross_wt",
						corrected,
						update_modified=False,
					)
					serial_fixes += len(sn_batch)

			bom_fixes += 1

	frappe.db.commit()
