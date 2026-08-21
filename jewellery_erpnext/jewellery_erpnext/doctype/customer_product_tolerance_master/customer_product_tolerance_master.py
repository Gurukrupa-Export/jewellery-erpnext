# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.customer_product_tolerance_master.tolerance_utils import (
	NO_UPPER_BOUND,
	diamond_group_key,
	gemstone_group_key,
	group_tolerance_rows,
	metal_group_key,
)

# (child fieldname, label, from-field, to-field, group key) for every banded table.
_BANDED_TABLES = (
	(
		"metal_tolerance_table",
		"Metal Tolerance Table",
		"from_weight",
		"to_weight",
		metal_group_key,
	),
	(
		"diamond_tolerance_table",
		"Diamond Tolerance Table",
		"from_diamond",
		"to_diamond",
		diamond_group_key,
	),
	(
		"gemstone_tolerance_table",
		"Gemstone Tolerance Table",
		"from_diamond",
		"to_diamond",
		gemstone_group_key,
	),
)


class CustomerProductToleranceMaster(Document):
	def validate(self):
		self.validate_tolerance_bands()

	def validate_tolerance_bands(self):
		"""Reject bands that Parent Manufacturing Order could not resolve to one winner.

		Modelled on Refinery Price List.validate_slab_bands. Two rules only:

		* From must not exceed To (skipped when To is 0, which encodes "and above").
		* Two rows in the same group must not overlap, compared with STRICT inequality
		  so contiguous bands that merely touch at a boundary are fine -- live masters
		  express schedules as 3-5 / 5-10, and pick_tolerance_row resolves the shared
		  endpoint deterministically by taking the first covering row.

		Contiguity is deliberately NOT enforced: PTM-GJCU0009-00001 ships bands 0-50 and
		51-100, and requiring gapless coverage would throw on every save of it.
		"""
		for fieldname, label, from_field, to_field, key in _BANDED_TABLES:
			rows = self.get(fieldname) or []
			for row in rows:
				lower = flt(row.get(from_field))
				upper = flt(row.get(to_field))
				if upper and lower > upper:
					frappe.throw(
						_(
							"{0} Row #{1}: From ({2}) cannot be greater than To ({3})."
						).format(_(label), row.idx, lower, upper)
					)

			for group in group_tolerance_rows(rows, key).values():
				_validate_no_overlap(group, label, from_field, to_field)


def _validate_no_overlap(rows, label, from_field, to_field):
	for i, a in enumerate(rows):
		for b in rows[i + 1 :]:
			a_hi = flt(a.get(to_field)) or NO_UPPER_BOUND
			b_hi = flt(b.get(to_field)) or NO_UPPER_BOUND
			if flt(a.get(from_field)) < b_hi and flt(b.get(from_field)) < a_hi:
				frappe.throw(
					_(
						"{0} Rows #{1} and #{2} cover overlapping bands ({3}-{4} and "
						"{5}-{6}). Only one row may apply to a given weight."
					).format(
						_(label),
						a.idx,
						b.idx,
						flt(a.get(from_field)),
						flt(a.get(to_field)) or "∞",
						flt(b.get(from_field)),
						flt(b.get(to_field)) or "∞",
					)
				)
