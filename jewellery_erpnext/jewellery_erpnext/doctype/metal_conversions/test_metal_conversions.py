# Copyright (c) 2024, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestMetalConversions(FrappeTestCase):
	def test_scan_source_item_action(self):
		"""Item-code-only scan: the scanned metal item lands in the active mode with no
		qty/batch fetched, and the scan input is cleared. Non-metal / unknown codes are
		rejected."""
		metal_item = frappe.db.get_value(
			"Item", {"variant_of": ["in", ["M", "F"]]}, "name"
		)
		if not metal_item:
			self.skipTest("no metal (M/F) item available in the test database")

		# Multiple mode -> appends a grid row carrying only the item_code.
		mc = frappe.new_doc("Metal Conversions")
		mc.multiple_metal_converter = 1
		mc.scan_source_item = metal_item
		mc.scan_source_item_action(barcode=metal_item)
		self.assertEqual(len(mc.mc_source_table), 1)
		self.assertEqual(mc.mc_source_table[0].item_code, metal_item)
		self.assertFalse(mc.mc_source_table[0].qty, "qty must NOT be fetched on scan")
		self.assertFalse(mc.scan_source_item, "scan input should be cleared")

		# Single mode -> sets Source Item only (qty left for the operator).
		mc2 = frappe.new_doc("Metal Conversions")
		mc2.multiple_metal_converter = 0
		mc2.scan_source_item = metal_item
		mc2.scan_source_item_action(barcode=metal_item)
		self.assertEqual(mc2.source_item, metal_item)
		self.assertFalse(mc2.source_qty, "qty must NOT be fetched on scan")
		self.assertFalse(mc2.scan_source_item, "scan input should be cleared")

		# Unknown barcode is rejected.
		mc3 = frappe.new_doc("Metal Conversions")
		self.assertRaises(
			frappe.ValidationError,
			mc3.scan_source_item_action,
			barcode="__NO_SUCH_ITEM__",
		)
