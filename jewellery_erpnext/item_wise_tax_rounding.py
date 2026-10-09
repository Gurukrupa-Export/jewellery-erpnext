"""Item-wise tax rounding tolerance that scales with the number of item rows.

ERPNext's ``calculate_taxes_and_totals.adjust_rounding_in_item_wise_tax_details`` checks that
each tax row's per-item breakup adds up to the row's tax amount. Every item's share is rounded
on its own, so the breakup drifts from the total as rows are added -- on a 1,464-item Sales
Order to Delivery Note it was 0.6 per tax row (53 items: 0.04, 1,000 items: 0.43). ERPNext only
absorbs a difference of up to a flat 0.5 and throws "Item Wise Tax Details do not match with
Taxes and Charges" above it, whatever the item count (its own code carries a "TODO: fix
rounding difference issues").

The method below is ERPNext's, with one change: the allowed difference per tax row is the
larger of ERPNext's flat limit and the most rounding the row's item lines can add (half of the
smallest currency unit per line, 0.005 at 2 decimals). The difference is still put on the last
line exactly as ERPNext does, so tax totals are unchanged; a mismatch larger than rounding
explains still raises the same error.
"""

import frappe
from frappe import _
from frappe.utils import flt


def adjust_rounding_in_item_wise_tax_details(self):
	from erpnext.controllers.taxes_and_totals import ignore_item_wise_tax_details

	if ignore_item_wise_tax_details(self.doc):
		return

	if not self.doc.get("_item_wise_tax_details"):
		return

	invalid_rows = []

	# reset temporary attributes
	for tax in self.doc.taxes:
		tax._total_tax_breakup = 0
		tax._last_row_idx = None
		tax._breakup_row_count = 0

	for idx, d in enumerate(self.doc._item_wise_tax_details):
		tax = d.get("tax")
		if not tax or (tax.get("charge_type") == "Actual" and d.rate == 0):
			continue

		tax._total_tax_breakup += d.amount or 0
		tax._last_row_idx = idx
		tax._breakup_row_count += 1

	# Apply rounding difference to the last row
	for tax in self.doc.taxes:
		last_idx = tax._last_row_idx
		if last_idx is None:
			continue

		multiplier = -1 if tax.get("add_deduct_tax") == "Deduct" else 1
		expected_amount = tax.base_tax_amount_after_discount_amount * multiplier
		actual_breakup = tax._total_tax_breakup
		diff = flt(expected_amount - actual_breakup, 5)

		if abs(diff) <= _allowed_rounding_difference(tax):
			detail_row = self.doc._item_wise_tax_details[last_idx]
			detail_row["amount"] = flt(detail_row["amount"] + diff, 5)

		else:
			invalid_rows.append(f"Row {tax.idx} (Difference: {diff})")

	if self.doc.flags.ignore_validate:
		return

	if invalid_rows:
		message = (
			_(
				"Item Wise Tax Details do not match with Taxes and Charges at the following rows:"
			)
			+ "<br>"
			+ "<br>".join(invalid_rows)
		)

		frappe.throw(_(message))


def _allowed_rounding_difference(tax):
	"""ERPNext's flat limit, widened to the rounding the tax row's item lines can add up to."""
	precision = tax.precision("tax_amount")
	# ERPNext: allow up to 1 for zero-precision currencies (e.g. JPY, KRW), else 0.5.
	erpnext_limit = 1 if precision == 0 else 0.5
	# Each line's share can be off by at most half of the smallest unit (0.005 at 2 decimals).
	per_line = 0.5 * 10 ** -(precision or 0)
	return max(erpnext_limit, tax._breakup_row_count * per_line)


def apply():
	"""Install the method on ERPNext's tax calculator. Safe to call more than once."""
	from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals

	if not hasattr(
		calculate_taxes_and_totals, "adjust_rounding_in_item_wise_tax_details"
	):
		# ERPNext no longer has this check; nothing to adjust.
		return
	calculate_taxes_and_totals.adjust_rounding_in_item_wise_tax_details = (
		adjust_rounding_in_item_wise_tax_details
	)
