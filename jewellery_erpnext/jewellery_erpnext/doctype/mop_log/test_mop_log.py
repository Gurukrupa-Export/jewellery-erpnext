# Copyright (c) 2026, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	mo_creation,
	mop_log_creation,
)


class TestMOPLog(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_mop_log_creation(self):
		mwo_list = mo_creation()
		mr_list = frappe.get_all(
			"Material Request",
			filters={
				"manufacturing_order": mwo_list[0].manufacturing_order,
				"docstatus": 0,
			},
			pluck="name",
		)
		for row in mwo_list:
			if row.department == "Manufacturing Plan & Management - GEPL":
				mwo = frappe.get_doc("Manufacturing Work Order", row.name)
				mwo.submit()
				mo_man = frappe.get_last_doc(
					"Manufacturing Operation",
					filters={"manufacturing_work_order": mwo.name},
				)

				if mr_list:
					mop_log_se = mop_log_creation(mr_list[0], mo_man)
					sed = frappe.get_doc("Stock Entry Detail", mop_log_se.row_name)
					self.assertEqual(mop_log_se.voucher_no, sed.parent)
					self.assertEqual(mop_log_se.row_name, sed.name)
					self.assertEqual(mop_log_se.item_code, sed.item_code)
					self.assertEqual(mop_log_se.from_warehouse, sed.s_warehouse)
					self.assertEqual(mop_log_se.to_warehouse, sed.t_warehouse)
					self.assertEqual(mop_log_se.qty_change, sed.qty)
					self.assertEqual(
						mop_log_se.serial_and_batch_bundle, sed.serial_and_batch_bundle
					)
					self.assertEqual(mop_log_se.batch_no, sed.batch_no)
					self.assertEqual(
						mop_log_se.manufacturing_operation, sed.manufacturing_operation
					)

				dir_issue = dir_for_issue(
					"Manufacturing Plan & Management - GEPL", "Tagging - GEPL", mo_man
				)
				mo_man.reload()
				self.assertEqual("Finished", mo_man.status)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value("MOP Log", filters={"voucher_no": dir_issue.name}),
				)
				from_warehouse = frappe.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_issue.current_department,
						"warehouse_type": "Manufacturing",
					},
				)
				to_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_issue.next_department,
						"warehouse_type": "Manufacturing",
					},
					"default_in_transit_warehouse",
				)
				self.assertEqual(mop_log.voucher_no, dir_issue.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, dir_issue.department_ir_operation[0].name
				)

				mo_wax = frappe.get_last_doc("Manufacturing Operation")
				self.assertIsNotNone(mo_wax.department_issue_id)
				self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

				dir_receive = dir_for_receive(dir_issue)
				mo_wax.reload()
				self.assertIsNotNone(mo_wax.department_receive_id)
				self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value(
						"MOP Log", filters={"voucher_no": dir_receive.name}
					),
				)
				to_warehouse = frappe.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_receive.current_department,
						"warehouse_type": "Manufacturing",
					},
				)
				from_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_receive.current_department,
						"warehouse_type": "Manufacturing",
					},
					"default_in_transit_warehouse",
				)

				self.assertEqual(mop_log.voucher_no, dir_receive.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, dir_receive.department_ir_operation[0].name
				)

	def test_mop_log_validate_with_empty_item_code(self):
		mo = create_test_manufacturing_operation()

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = ""
		mop_log.qty_after_transaction = 10.0
		mop_log.pcs_after_transaction = 5
		mop_log.manufacturing_operation = mo.name

		try:
			mop_log.validate()
		except IndexError:
			self.fail("validate() raised IndexError with empty item_code")

	def test_mop_log_validate_with_diamond_prefix(self):
		mo = create_test_manufacturing_operation()

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "D-001"
		mop_log.qty_after_transaction = 10.0
		mop_log.pcs_after_transaction = 20
		mop_log.manufacturing_operation = mo.name

		mop_log.validate()

		updated_mo = frappe.get_doc("Manufacturing Operation", mo.name)
		self.assertEqual(updated_mo.diamond_wt, 10.0)
		self.assertEqual(updated_mo.diamond_wt_in_gram, 10.0 * 0.2)  # 2.0
		self.assertEqual(updated_mo.diamond_pcs, 20)

	def test_mop_log_validate_with_invalid_prefix(self):
		mo = create_test_manufacturing_operation()
		initial_net_wt = mo.net_wt

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = "X-001"
		mop_log.qty_after_transaction = 20.0
		mop_log.pcs_after_transaction = 10
		mop_log.manufacturing_operation = mo.name

		mop_log.validate()

		updated_mo = frappe.get_doc("Manufacturing Operation", mo.name)
		self.assertEqual(updated_mo.net_wt, initial_net_wt)


def create_test_manufacturing_operation():
	mo = frappe.new_doc("Manufacturing Operation")
	mo.net_wt = 10.0
	mo.finding_wt = 5.0
	mo.diamond_wt_in_gram = 2.0
	mo.gemstone_wt_in_gram = 1.5
	mo.other_wt = 0.5
	mo.previous_mop = None
	mo.save()

	return mo
