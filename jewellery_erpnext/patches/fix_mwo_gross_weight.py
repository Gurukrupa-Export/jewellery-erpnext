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
independently) and drifted +0.001 at a carat rounding boundary.

Also propagates corrected values to the FG MWO's Manufacturing Operation
so that SNC submission picks up correct weights.

Idempotent: re-running yields zero changes once values match.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.utils import carat_to_gram

BATCH = 500


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


def execute():
	mwo_fixes = 0
	mop_fixes = 0

	for page in _mwo_pages(BATCH):
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
			],
		)
		for mwo in mwos:
			diamond_gram = carat_to_gram(flt(mwo.get("diamond_wt")))
			gemstone_gram = carat_to_gram(flt(mwo.get("gemstone_wt")))
			corrected_gross = flt(
				flt(mwo.get("net_wt"))
				+ flt(mwo.get("finding_wt"))
				+ diamond_gram
				+ gemstone_gram
				+ flt(mwo.get("other_wt")),
				3,
			)

			if flt(mwo.gross_wt) == corrected_gross:
				continue

			frappe.db.set_value(
				"Manufacturing Work Order",
				mwo.name,
				{
					"gross_wt": corrected_gross,
					"diamond_wt_in_gram": diamond_gram,
				},
				update_modified=False,
			)

			# Also fix the FG MOP linked to this MWO.
			fg_mop = frappe.db.get_value(
				"Manufacturing Operation",
				{"manufacturing_work_order": mwo.name},
				"name",
				order_by="creation desc",
			)
			if fg_mop:
				frappe.db.set_value(
					"Manufacturing Operation",
					fg_mop,
					{
						"gross_wt": corrected_gross,
						"diamond_wt_in_gram": diamond_gram,
						"gemstone_wt_in_gram": gemstone_gram,
					},
					update_modified=False,
				)
				mop_fixes += 1

			mwo_fixes += 1

	frappe.db.commit()
