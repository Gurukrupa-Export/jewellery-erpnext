"""A repair's two BOMs must survive the order chain.

``custom_serial_id_bom`` is the serial's own BOM (unpacked); ``custom_new_bom`` is the BOM the
repair is manufactured on, and it becomes the row's Copy BOM.
"""

from types import SimpleNamespace

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import seed_repair_copy_bom


class TestSeedRepairCopyBom(UnitTestCase):
	def _quotation(self, **row):
		# Not frappe._dict: its .items is the dict method.
		return SimpleNamespace(items=[frappe._dict(row)])

	def test_repair_row_takes_new_bom_as_copy_bom(self):
		quotation = self._quotation(
			order_form_type="Repair Order",
			custom_new_bom="BOM-NEW-1",
			custom_serial_id_bom="BOM-SERIAL-1",
		)
		seed_repair_copy_bom(quotation)
		self.assertEqual(quotation.items[0].copy_bom, "BOM-NEW-1")
		self.assertEqual(quotation.items[0].custom_serial_id_bom, "BOM-SERIAL-1")

	def test_an_existing_copy_bom_is_kept(self):
		quotation = self._quotation(
			order_form_type="Repair Order", custom_new_bom="BOM-NEW-1", copy_bom="BOM-HAND"
		)
		seed_repair_copy_bom(quotation)
		self.assertEqual(quotation.items[0].copy_bom, "BOM-HAND")

	def test_repair_without_new_bom_is_left_to_the_item_lookup(self):
		quotation = self._quotation(order_form_type="Repair Order")
		seed_repair_copy_bom(quotation)
		self.assertIsNone(quotation.items[0].copy_bom)

	def test_non_repair_rows_are_untouched(self):
		quotation = self._quotation(order_form_type="Order", custom_new_bom="BOM-NEW-1")
		seed_repair_copy_bom(quotation)
		self.assertIsNone(quotation.items[0].copy_bom)
