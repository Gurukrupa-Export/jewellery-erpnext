# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the loss-qty gram normalization helper.

Pure-function tests; no DB, no Frappe site required.
"""

from frappe.tests import IntegrationTestCase


class TestGetLossQtyInGrams(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			get_loss_qty_in_grams,
		)

		self.norm = get_loss_qty_in_grams

	def test_diamond_carat_to_gram(self):
		# 1 carat = 0.2 g
		self.assertAlmostEqual(self.norm("D-G-18KT-75.4-Y", 1.0), 0.200, places=3)

	def test_gemstone_carat_to_gram(self):
		# 2 carat = 0.4 g
		self.assertAlmostEqual(self.norm("G-G-18KT-75.4-Y", 2.0), 0.400, places=3)

	def test_metal_unchanged(self):
		self.assertAlmostEqual(self.norm("M-G-22KT-91.9-Y", 1.0), 1.000, places=3)

	def test_finding_unchanged(self):
		self.assertAlmostEqual(self.norm("F-G-18KT-75.4-Y", 1.0), 1.000, places=3)

	def test_other_unchanged(self):
		self.assertAlmostEqual(self.norm("O-MISC-001", 1.0), 1.000, places=3)

	def test_zero_qty(self):
		self.assertEqual(self.norm("D-G-18KT-75.4-Y", 0), 0.0)
		self.assertEqual(self.norm("M-G-22KT-91.9-Y", 0), 0.0)

	def test_none_item_code_returns_qty_unchanged(self):
		# Defensive path for callers that hand in a missing item_code.
		self.assertAlmostEqual(self.norm(None, 1.5), 1.500, places=3)
		self.assertAlmostEqual(self.norm("", 1.5), 1.500, places=3)

	def test_precision_3_rounding(self):
		# 5 carat -> 1 g exactly; ensure we round to 3 dp not extend precision.
		self.assertEqual(self.norm("D-X", 5.0), 1.000)
		# 0.333... carat -> 0.0666... g -> rounded to 0.067
		self.assertAlmostEqual(self.norm("D-X", 1.0 / 3), 0.067, places=3)

	def test_dg_prefix_decided_by_first_char_only(self):
		# Items with prefix Dx-... or Gx-... still convert (single-char rule).
		self.assertAlmostEqual(self.norm("D2-FOO", 5.0), 1.000, places=3)
		# An item code starting with another letter does NOT convert even if
		# 'D' or 'G' appears later.
		self.assertAlmostEqual(self.norm("MD-FOO", 5.0), 5.000, places=3)

	def test_negative_qty_preserved(self):
		# Gain rows (negative loss) should normalize the sign too.
		self.assertAlmostEqual(self.norm("D-X", -1.0), -0.200, places=3)

	def test_mixed_basket_sum_matches_expected(self):
		# Smoke test for the pattern callers will use: sum a heterogeneous
		# loss basket and verify the gram-normalized total.
		basket = [
			("M-G-22KT-91.9-Y", 0.300),
			("F-G-18KT-75.4-Y", 0.200),
			("D-G-18KT-75.4-Y", 1.000),  # 1 ct -> 0.2 g
			("G-G-18KT-75.4-Y", 0.500),  # 0.5 ct -> 0.1 g
		]
		total = sum(self.norm(code, qty) for code, qty in basket)
		self.assertAlmostEqual(total, 0.800, places=3)
