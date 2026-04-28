# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	create_manufacturing_work_order,
)


class TestManufacturingWorkOrder(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_manufacturing_work_order(self):
		now = now_datetime()
		start = add_to_date(now, minutes=-5)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		create_manufacturing_work_order(pmo)
		mwo_list = frappe.get_all(
			"Manufacturing Work Order",
			filters={
				"manufacturing_order": pmo.name,
				"creation": ["between", [start, now]],
			},
			fields=["name", "department"],
		)
		self.mwo_list = mwo_list

		for mwo_name in mwo_list:
			if mwo_name.department != "Serial Number - GEPL":
				mwo = frappe.get_doc("Manufacturing Work Order", mwo_name.name)
				mwo.submit()

				mo = frappe.get_last_doc("Manufacturing Operation")
				self.assertEqual(mwo.name, mo.manufacturing_work_order)
				self.assertEqual(mwo.manufacturing_operation, mo.name)
				self.assertEqual(mwo.manufacturing_order, mo.manufacturing_order)
				self.assertEqual(mwo.manufacturing_plan, mo.manufacturing_plan)
				self.assertEqual(mwo.item_code, mo.item_code)
				self.assertEqual(mwo.master_bom, mo.design_id_bom)
				self.assertEqual(mwo.metal_type, mo.metal_type)
				self.assertEqual(mwo.metal_touch, mo.metal_touch)
				self.assertEqual(mwo.metal_colour, mo.metal_colour)
				self.assertEqual(mwo.metal_purity, mo.metal_purity)

			else:
				mwo = frappe.get_doc("Manufacturing Work Order", mwo_name.name)
				with self.assertRaises(frappe.ValidationError) as context:
					mwo.submit()

				self.assertIn(
					"All the pending manufacturing work orders should be in Tagging - GEPL.",
					str(context.exception),
				)

	def test_on_cancel_sets_status_cancelled(self):
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		create_manufacturing_work_order(pmo)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - GEPL"]]},
		)
		if mwo.docstatus == 0:
			mwo.submit()
		mwo.cancel()
		mwo.reload()
		self.assertEqual(mwo.status, "Cancelled")

	def test_validate_other_work_orders_blocks_if_pending(self):
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		create_manufacturing_work_order(pmo)
		mwo = frappe.get_last_doc("Manufacturing Work Order")

		mwo.for_fg = 1
		mwo.flags.ignore_validate = True

		create_manufacturing_work_order(pmo)
		pending = frappe.get_last_doc("Manufacturing Work Order")
		pending.department = "Manufacturing Plan & Management - GEPL"
		pending.save()
		with self.assertRaises(frappe.ValidationError):
			mwo.validate_other_work_orders()
