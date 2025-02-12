import unittest

import frappe
from frappe import throw
from frappe.model.document import Document
from frappe.utils import flt


class TestBOMDoctyp(unittest.TestCase):
	def setup(self):
		self.bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"name": "BOM-RI00297-001-001",
				"bom_type": "Template",
				"item": "RI00297-001",
				"company": "Gurukrupa Export Private Limited",
				"quantity": 1,
				"items": [{"item_code": "STO-ITEM-2022-00016", "qty": 1, "rate": 555}],
			}
		)
		self.bom.insert()

	def test_set_bom_items(self):
		bom_list = frappe.db.get_all("BOM", {"bom_type": "Sales Order"}, "name")
		for i in bom_list:
			bom = frappe.get_doc(
				{
					"doctype": "BOM",
					"name": i.get("name"),
				}
			)

			bom_items = {}
			bom_items.update({row.item_variant: row.quantity for row in bom.metal_detail if row.quantity})
			bom_items.update({row.item_variant: row.quantity for row in bom.diamond_detail if row.quantity})
			bom_items.update(
				{row.item_variant: row.quantity for row in bom.gemstone_detail if row.quantity}
			)
			bom_items.update({row.item_variant: row.quantity for row in bom.finding_detail if row.quantity})
			for row in bom.items:
				self.assertEqual(
					row.qty, bom_items.get(row.item_code), "Quantity should match the expected value"
				)

	def test_calculate_diamond_qty(self):
		bom_list = frappe.db.get_all("BOM")
		for i in bom_list:
			bom = frappe.get_doc(
				{
					"doctype": "BOM",
					"name": i.get("name"),
				}
			)
			for row in bom.diamond_detail + bom.gemstone_detail:
				self.assertEqual(
					row.qty, flt(flt(row.quantity) / 5, 3), "Quantity should match the expected value"
				)

	def test_calculate_total(self):
		bom_list = frappe.db.get_all("BOM")
		for i in bom_list:
			bom = frappe.get_doc(
				{
					"doctype": "BOM",
					"name": i.get("name"),
				}
			)
			if bom.metal_detail:
				self.assertEqual(bom.total_metal_weight, sum(row.quantity for row in bom.metal_detail))
			if bom.diamond_detail:
				self.assertEqual(bom.diamond_weight, sum(row.quantity for row in bom.diamond_detail))
			if bom.diamond_detail:
				self.assertEqual(
					bom.total_diamond_weight_in_gms, sum(row.weight_in_gms for row in bom.diamond_detail)
				)
			if bom.diamond_detail:
				self.assertEqual(bom.total_gemstone_weight, sum(row.quantity for row in bom.gemstone_detail))
			if bom.diamond_detail:
				self.assertEqual(
					bom.total_gemstone_weight_in_gms, sum(row.weight_in_gms for row in bom.gemstone_detail)
				)
			if bom.finding_detail:
				self.assertEqual(bom.finding_weight, sum(row.quantity for row in bom.finding_detail))
			if bom.diamond_detail:
				self.assertEqual(bom.total_diamond_pcs, sum(flt(row.pcs) for row in bom.diamond_detail))
			if bom.gemstone_detail:
				self.assertEqual(bom.total_gemstone_pcs, sum(flt(row.pcs) for row in bom.gemstone_detail))
			if bom.other_detail:
				self.assertEqual(bom.total_other_weight, sum(flt(row.quantity) for row in bom.other_detail))

			self.assertEqual(
				flt(bom.metal_and_finding_weight), (flt(bom.metal_weight) + flt(bom.finding_weight))
			)

			if bom.diamond_weight:
				self.assertEqual(
					flt(bom.gold_to_diamond_ratio), (flt(bom.metal_and_finding_weight) / flt(bom.diamond_weight))
				)

			if bom.total_diamond_pcs:
				self.assertEqual(
					flt(bom.diamond_ratio), (flt(bom.diamond_weight) / flt(bom.total_diamond_pcs))
				)

			self.assertEqual(
				flt(bom.gross_weight),
				(
					flt(bom.metal_and_finding_weight)
					+ flt(bom.total_diamond_weight_in_gms)
					+ flt(bom.total_gemstone_weight_in_gms)
					+ flt(bom.total_other_weight)
				),
			)

			if bom.metal_detail:
				self.assertEqual(
					flt(bom.custom_total_pure_weight),
					(sum(row.quantity * (flt(row.metal_purity) / 100) for row in bom.metal_detail)),
				)

			if bom.finding_detail:
				self.assertEqual(
					flt(bom.custom_total_pure_finding_weight),
					(sum(row.quantity * (flt(row.metal_purity) / 100) for row in bom.finding_detail)),
				)

	def test_product_ratio(self):
		gold_wt = 0.0
		bom_list = frappe.db.get_all("BOM")
		for i in bom_list:
			bom = frappe.get_doc(
				{
					"doctype": "BOM",
					"name": i.get("name"),
				}
			)
			for gold in bom.metal_detail:
				if gold.metal_type == "Gold":
					gold_wt += gold.quantity
				self.assertEqual(flt(bom.metal_to_diamond_ratio_excl_of_finding), flt(gold_wt))

	def test_bom_creation(self):
		self.bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"name": "BOM-RI00297-001-001",
				"bom_type": "Template",
				"item": "RI00297-001",
				"company": "Gurukrupa Export Private Limited",
				"quantity": 1,
				"items": [{"item_code": "STO-ITEM-2022-00016", "qty": 1, "rate": 555}],
			}
		)

		bom = frappe.get_doc("BOM", self.bom)

		self.assertIsNotNone(bom, "BOM should be created")
		# self.assertEqual(bom.item, "RI00297-001", "Item code should match")
		self.assertEqual(bom.quantity, 1, "Quantity should match the expected value")

		# Verify the components of the BOM
		self.assertGreater(len(bom.items), 0, "BOM should have at least one item component")
		self.assertEqual(
			bom.items[0].item_code, "STO-ITEM-2022-00016", "Item code in BOM item should match"
		)
		self.assertEqual(bom.items[0].qty, 1, "Item quantity in BOM should match")
		self.assertEqual(bom.items[0].rate, 555, "Item rate in BOM should match")
