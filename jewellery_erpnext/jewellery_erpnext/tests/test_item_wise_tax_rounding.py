from types import SimpleNamespace

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext.item_wise_tax_rounding import (
	_allowed_rounding_difference,
	adjust_rounding_in_item_wise_tax_details,
)


class _Tax(frappe._dict):
	"""A tax row with 2-decimal currency fields, as on the GEPL sites."""

	def precision(self, fieldname):
		return 2


def _calculator(tax_rows, details, conversion_rate):
	doc = frappe._dict(
		taxes=tax_rows,
		_item_wise_tax_details=details,
		conversion_rate=conversion_rate,
		flags=frappe._dict(),
		meta=SimpleNamespace(get_field=lambda fieldname: True),
	)
	return SimpleNamespace(doc=doc)


def _quotation_case(conversion_rate):
	"""GE-QTN-SO-26-04427: two lines at 0.125% with "Round Row-wise Tax" on.

	The tax row adds the per-line rounded amounts (10.74 + 13.02 = 23.76); the breakup
	rounds only its running total (10.744 + 13.022 = 23.766 -> 23.77). In USD at 93.48
	that one cent is 2221.08 vs 2222.02 INR.
	"""
	tax = _Tax(
		idx=1, charge_type="On Net Total", base_tax_amount_after_discount_amount=0
	)
	tax.base_tax_amount_after_discount_amount = round(23.76 * conversion_rate, 2)
	first = round(round(10.74425, 2) * conversion_rate, 2)
	second = round(round(10.74425 + 13.0219375, 2) * conversion_rate, 2) - first
	details = [
		frappe._dict(tax=tax, rate=0.125, amount=first),
		frappe._dict(tax=tax, rate=0.125, amount=second),
	]
	return tax, details


class TestItemWiseTaxRounding(UnitTestCase):
	def test_one_cent_in_usd_is_absorbed_onto_the_last_line(self):
		tax, details = _quotation_case(93.48)
		adjust_rounding_in_item_wise_tax_details(_calculator([tax], details, 93.48))
		self.assertAlmostEqual(sum(d.amount for d in details), 2221.08, places=2)
		# The tax row itself is untouched.
		self.assertEqual(tax.base_tax_amount_after_discount_amount, 2221.08)

	def test_inr_document_keeps_the_flat_limit(self):
		tax = _Tax(_breakup_row_count=2)
		self.assertEqual(_allowed_rounding_difference(tax, 1), 0.5)

	def test_allowance_scales_with_the_conversion_rate(self):
		tax = _Tax(_breakup_row_count=2)
		# (2 lines + 1) * 0.005 USD * 93.48 + 0.01 INR
		self.assertAlmostEqual(
			_allowed_rounding_difference(tax, 93.48), 1.4122, places=4
		)

	def test_a_real_mismatch_still_throws(self):
		tax, details = _quotation_case(93.48)
		details[-1].amount -= 50
		with self.assertRaises(frappe.ValidationError):
			adjust_rounding_in_item_wise_tax_details(_calculator([tax], details, 93.48))
