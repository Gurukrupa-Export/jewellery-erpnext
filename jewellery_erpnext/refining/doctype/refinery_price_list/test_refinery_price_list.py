# Copyright (c) 2026, Nirali and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.refining.doctype.refinery_price_list.refinery_price_list import (
	compute_refining_amount,
	get_refinery_rate,
)

DUST_ITEM = "_Test Vacuum Bag Dust"
PROC_POLISH = "Polishing/Vacuum"
PROC_FLOOR = "Floor/Vacuum Cleaning"


def _ensure_process(name):
	if not frappe.db.exists("Refining Process", name):
		frappe.get_doc({"doctype": "Refining Process", "process_name": name}).insert(
			ignore_permissions=True
		)


def _ensure_dust_item():
	if not frappe.db.exists("Item", DUST_ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": DUST_ITEM,
				"item_name": DUST_ITEM,
				"item_group": frappe.db.get_value(
					"Item Group", {"is_group": 0}, "name"
				),
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"stock_uom": "Gram",
			}
		).insert(ignore_permissions=True)


class TestRefineryPriceList(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_process(PROC_POLISH)
		_ensure_process(PROC_FLOOR)
		_ensure_dust_item()
		frappe.db.delete("Refinery Price List", {"dust_item": DUST_ITEM})
		# (process, from_g, to_g, charge_type, rate, weight_basis)
		rows = [
			(PROC_POLISH, 0, 1000, "Per Kg", 1800, "Gross Weight"),
			(PROC_POLISH, 1000, 0, "Per Kg", 1800, "After Burning Weight"),
			(PROC_FLOOR, 0, 0, "Per Kg", 2200, "Gross Weight"),
		]
		for process, from_g, to_g, charge_type, rate, basis in rows:
			frappe.get_doc(
				{
					"doctype": "Refinery Price List",
					"refining_process": process,
					"dust_item": DUST_ITEM,
					"from_weight": from_g,
					"to_weight": to_g,
					"charge_type": charge_type,
					"rate": rate,
					"weight_basis": basis,
				}
			).insert(ignore_permissions=True)

	def test_band_within_range(self):
		row = get_refinery_rate(DUST_ITEM, 500, PROC_POLISH)
		self.assertIsNotNone(row)
		self.assertEqual(row.rate, 1800)
		self.assertEqual(row.weight_basis, "Gross Weight")

	def test_above_band_uses_to_weight_zero(self):
		# to_weight = 0 encodes "no upper bound" (the "Above 1 kg" row)
		row = get_refinery_rate(DUST_ITEM, 5000, PROC_POLISH)
		self.assertIsNotNone(row)
		self.assertEqual(row.weight_basis, "After Burning Weight")

	def test_process_disambiguation(self):
		# Same dust item, different process → different rate
		self.assertEqual(get_refinery_rate(DUST_ITEM, 500, PROC_POLISH).rate, 1800)
		self.assertEqual(get_refinery_rate(DUST_ITEM, 500, PROC_FLOOR).rate, 2200)

	def test_lower_boundary_inclusive(self):
		self.assertEqual(get_refinery_rate(DUST_ITEM, 0, PROC_POLISH).rate, 1800)

	def test_no_match_returns_none(self):
		# item exists but no process match
		self.assertIsNone(get_refinery_rate(DUST_ITEM, 500, "No Such Process"))
		# unknown item
		self.assertIsNone(get_refinery_rate("_No Such Item _x", 500))

	def test_compute_amount_by_charge_type(self):
		self.assertEqual(compute_refining_amount("Flat Charge", 800, 40), 800)
		self.assertEqual(compute_refining_amount("Per Gram", 18, 60), 1080)  # 18 × 60
		self.assertEqual(
			compute_refining_amount("Per Kg", 1800, 500), 900
		)  # 1800 × 0.5 kg

	def test_from_gt_to_is_rejected(self):
		doc = frappe.get_doc(
			{
				"doctype": "Refinery Price List",
				"refining_process": PROC_POLISH,
				"dust_item": DUST_ITEM,
				"from_weight": 100,
				"to_weight": 10,
				"charge_type": "Flat Charge",
				"rate": 1,
				"weight_basis": "Gross Weight",
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert)
