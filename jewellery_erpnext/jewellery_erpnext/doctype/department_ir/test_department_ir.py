# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	add_time_log_optimize,
	department_receive_query,
	fetch_and_update,
	get_manufacturing_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
)


class TestDepartmentIR(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_department_ir_scan(self):
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo
		)
		for row in dir_issue.department_ir_operation:
			self.assertEqual(row.manufacturing_work_order, mo.manufacturing_work_order)
			self.assertEqual(row.manufacturing_operation, mo.name)
			self.assertEqual(row.parent_manufacturing_order, mo.manufacturing_order)
		mo.reload()

		self.assertEqual("Finished", mo.status)

		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		self.assertIsNotNone(mo_wax.department_issue_id)
		self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

		dir_receive = dir_for_receive(dir_issue)
		for row in dir_receive.department_ir_operation:
			self.assertEqual(row.gross_wt, mo.gross_wt)
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)
			self.assertEqual(row.parent_manufacturing_order, mo_wax.manufacturing_order)
		mo_wax.reload()
		self.assertIsNotNone(mo_wax.department_receive_id)
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

	def test_department_ir_by_manufacturing_operation(self):
		mo = mo_creation()
		dir_issue = frappe.new_doc("Department IR")
		dir_issue.manufacturer = "Shubh"
		dir_issue.current_department = "Manufacturing Plan & Management - GEPL"
		dir_issue.next_department = "Waxing - GEPL"
		dir_issue = get_manufacturing_operations(mo.name, dir_issue)
		dir_issue.save()
		dir_issue.submit()

		for row in dir_issue.department_ir_operation:
			self.assertEqual(row.manufacturing_work_order, mo.manufacturing_work_order)
			self.assertEqual(row.manufacturing_operation, mo.name)
			self.assertEqual(row.parent_manufacturing_order, mo.manufacturing_order)
		mo.reload()

		self.assertEqual("Finished", mo.status)

		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		self.assertIsNotNone(mo_wax.department_issue_id)
		self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

		dir_receive = dir_for_receive(dir_issue)
		for row in dir_receive.department_ir_operation:
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)
			self.assertEqual(row.parent_manufacturing_order, mo_wax.manufacturing_order)
		mo_wax.reload()
		self.assertIsNotNone(mo_wax.department_receive_id)
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

	def test_department_receive_query_no_match_returns_empty(self):
		res = department_receive_query(
			"Department IR",
			"non-existent-xyz",
			"name",
			0,
			20,
			{"current_department": "", "next_department": ""},
		)
		self.assertEqual(res, [])

	def test_add_time_log_optimize_updates_and_inserts_time_log(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.department = "Manufacturing Plan & Management - GEPL"
		mop.insert()

		add_time_log_optimize(
			mop.name, {"status": "WIP", "start_time": frappe.utils.now()}
		)

		status = frappe.db.get_value("Manufacturing Operation", mop.name, "status")
		self.assertIn(status, ["WIP", "WIP"])

		time_logs = frappe.db.get_all(
			"Manufacturing Operation Time Log",
			filters={"parent": mop.name},
			pluck="name",
		)
		self.assertTrue(len(time_logs) >= 1)

	def test_get_manufacturing_operations_does_not_duplicate(self):
		mo = mo_creation()
		dir_issue = frappe.new_doc("Department IR")
		dir_issue.manufacturer = "Shubh"
		dir_issue.current_department = "Manufacturing Plan & Management - GEPL"
		dir_issue.next_department = "Waxing - GEPL"

		dir_issue.append(
			"department_ir_operation",
			{
				"manufacturing_operation": mo.name,
				"manufacturing_work_order": mo.manufacturing_work_order,
			},
		)

		updated = get_manufacturing_operations(mo.name, dir_issue)
		entries = [
			r
			for r in updated.department_ir_operation
			if r.manufacturing_work_order == mo.manufacturing_work_order
		]
		self.assertEqual(len(entries), 1)

	def test_fetch_and_update_returns_false_when_no_stock_entries(self):
		# mo = mo_creation()

		class Row:
			manufacturing_work_order = "NON-EXISTENT-MWO"

		res = fetch_and_update(frappe.new_doc("Department IR"), Row(), "MOP-UNKNOWN")
		self.assertFalse(res)

	def tearDown(self):
		return super().tearDown()


def mo_creation():
	mwo = frappe.get_last_doc("Manufacturing Work Order")
	mo = frappe.new_doc("Manufacturing Operation")
	mo.department = "Manufacturing Plan & Management - GEPL"
	mo.manufacturer = "Shubh"
	mo.manufacturing_work_order = mwo.name
	mo.manufacturing_order = mwo.manufacturing_order
	mo.manufacturing_plan = mwo.manufacturing_plan
	mo.type = "Manufacturing Work Order"
	mo.operation = "Manufacturing Plan & Management"
	mo.item_code = mwo.item_code
	mo.design_id_bom = mwo.master_bom
	mo.metal_type = mwo.metal_type
	mo.metal_touch = mwo.metal_touch
	mo.metal_colour = mwo.metal_colour
	mo.meatal_purity = mwo.metal_purity

	mo.save()

	return mo
