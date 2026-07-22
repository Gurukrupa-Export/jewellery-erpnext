# Copyright (c) 2024, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
	get_scan_metal_item,
)


class TestMetalConversions(FrappeTestCase):
	def test_get_scan_metal_item(self):
		"""The Item-Code scan resolver returns a valid metal (M/F) item as-is, and
		rejects unknown codes and non-metal items."""
		metal_item = frappe.db.get_value(
			"Item", {"variant_of": ["in", ["M", "F"]]}, "name"
		)
		if not metal_item:
			self.skipTest("no metal (M/F) item available in the test database")

		# A metal item resolves to itself (item code only — no qty/batch/purity).
		self.assertEqual(get_scan_metal_item(metal_item), metal_item)

		# Unknown barcode is rejected.
		self.assertRaises(
			frappe.ValidationError, get_scan_metal_item, "__NO_SUCH_ITEM__"
		)

		# A non-metal item (not an M/F variant) is rejected.
		nonmetal = frappe.db.get_value(
			"Item", {"variant_of": ["in", ["", None]]}, "name"
		)
		if nonmetal:
			self.assertRaises(frappe.ValidationError, get_scan_metal_item, nonmetal)
