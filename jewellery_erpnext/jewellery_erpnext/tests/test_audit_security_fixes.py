# Copyright (c) 2026, Nirali and contributors
# See license.txt
#
# Security fixes from the Lens audit: the Metal Criteria purity lookup on Sales Invoice
# (was an f-string SQL query indexed with [0]) and the Gemstone Size parser on Gemstone
# Conversion (was eval() of attribute text). DB-free: frappe.db is mocked.

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import sales_invoice
from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion import (
	gemstone_conversion,
)


class TestCustomerMetalPurity(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@classmethod
	def tearDownClass(cls):
		pass

	def test_lookup_is_parameterised_and_scoped_to_customer(self):
		customer = "O'Brien Jewellers"
		with patch.object(frappe.db, "get_value", return_value=91.8) as get_value:
			purity = sales_invoice._get_customer_metal_purity(customer, "Gold", "22KT")

		self.assertEqual(purity, 91.8)
		doctype, filters, field = get_value.call_args.args
		self.assertEqual(doctype, "Metal Criteria")
		self.assertEqual(field, "metal_purity")
		self.assertEqual(
			filters,
			{
				"parenttype": "Customer",
				"parent": customer,
				"metal_type": "Gold",
				"metal_touch": "22KT",
			},
		)

	def test_zero_purity_is_returned_not_treated_as_missing(self):
		with patch.object(frappe.db, "get_value", return_value=0):
			self.assertEqual(
				sales_invoice._get_customer_metal_purity("C", "Gold", "22KT"), 0
			)

	def test_missing_criteria_raises_validation_error(self):
		with patch.object(frappe.db, "get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				sales_invoice._get_customer_metal_purity("C", "Gold", "22KT")


class TestGemstoneSizeProduct(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@classmethod
	def tearDownClass(cls):
		pass

	def test_single_dimension(self):
		self.assertAlmostEqual(
			gemstone_conversion._gemstone_size_product("1.60 MM"), 1.6
		)

	def test_two_dimensions_multiply(self):
		self.assertAlmostEqual(
			gemstone_conversion._gemstone_size_product("10.00*6.50 MM"), 65.0
		)

	def test_matches_previous_ordering(self):
		bigger = gemstone_conversion._gemstone_size_product("10.00*8.00 MM")
		smaller = gemstone_conversion._gemstone_size_product("10.00*7.50 MM")
		self.assertLess(smaller, bigger)

	def test_non_numeric_text_is_rejected_not_executed(self):
		for size in ("__import__('os').system('id') MM", "2+3 MM", "", None):
			with self.assertRaises(frappe.ValidationError):
				gemstone_conversion._gemstone_size_product(size)
