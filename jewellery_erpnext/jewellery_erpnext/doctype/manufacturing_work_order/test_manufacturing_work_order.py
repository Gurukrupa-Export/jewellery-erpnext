# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.test_parent_manufacturing_order import (
	create_man_plan,
)


class TestManufacturingWorkOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(clas):
		clas.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		clas.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		clas.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

	def test_submit_creates_manufacturing_operation_and_validates_pending_work_orders(
		self,
	):
		create_pmo(self)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		mwo_list = frappe.get_all(
			"Manufacturing Work Order",
			filters={
				"manufacturing_order": pmo.name,
			},
			fields=["name", "department"],
		)

		for mwo_name in mwo_list:
			mwo = frappe.get_doc("Manufacturing Work Order", mwo_name.name)
			if mwo.department != "Serial Number - T":
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
				with self.assertRaises(frappe.ValidationError) as context:
					mwo.submit()

				self.assertIn(
					"Cannot submit. The following linked MWO(s) are not yet in",
					str(context.exception),
				)

	def test_cancel_sets_status_cancelled(self):
		create_pmo(self)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - T"]]},
		)
		if mwo.docstatus == 0:
			mwo.submit()

		mwo.cancel()
		mwo.reload()
		self.assertEqual(mwo.status, "Cancelled")

	def test_transfer_to_mwo_delegates_to_stock_transfer_entry(self):
		create_pmo(self)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - T"]]},
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.create_stock_transfer_entry"
		) as mock_transfer:
			mwo.transfer_to_mwo()
			mock_transfer.assert_called_once_with(mwo)

	def test_create_mfg_entry_delegates_to_create_se_entry(self):
		create_pmo(self)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - T"]]},
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.create_se_entry"
		) as mock_create_se:
			mwo.create_mfg_entry()
			mock_create_se.assert_called_once_with(mwo)


def create_pmo(self):
	create_man_plan(self)
	pmo = frappe.get_last_doc("Parent Manufacturing Order")
	pmo.diamond_department = "Diamond Setting - T"
	pmo.gemstone_department = "Diamond Setting - T"
	pmo.manufacturer = "Shubh"
	pmo.save()
	pmo.submit()
	return pmo
