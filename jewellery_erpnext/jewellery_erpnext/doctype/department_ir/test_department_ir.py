# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
)


class FakeDepartmentIR(FrappeDict):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		if "department_ir_operation" not in self:
			self.department_ir_operation = []

	def append(self, key, value):
		self[key].append(FrappeDict(value))
		
	def on_submit_issue_new(self, cancel=False):
		DepartmentIR.on_submit_issue_new(self, cancel)
		
	def on_submit_receive(self, cancel=False):
		DepartmentIR.on_submit_receive(self, cancel)
		
	def validate_receive_lineage(self):
		DepartmentIR.validate_receive_lineage(self)

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
		self.assertEqual(status, "WIP")

		started_time = frappe.db.get_value(
			"Manufacturing Operation", mop.name, "started_time"
		)
		self.assertIsNotNone(started_time, "started_time should be set")

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
		pass

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_datetime", return_value="2026-01-01 12:00:00")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_doc")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_operation_for_next_dept")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_mop_log_for_department_ir")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.add_time_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.set_value")
	def test_on_submit_issue_creates_mop_log_and_transitions(
		self, mock_set_val, mock_add_time, mock_create_mop, mock_create_op, mock_get_doc, mock_get_val, mock_datetime
	):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-ISS-001",
			type="Issue",
			current_department="Dept A",
			next_department="Dept B",
		)
		
		doc.append("department_ir_operation", {
			"manufacturing_operation": "MOP-CURRENT",
			"manufacturing_work_order": "MWO-1",
		})
		
		# Mocking warehouse fetch
		def get_val_side_effect(dt, filters=None, fieldname=None, as_dict=False):
			if fieldname == "default_in_transit_warehouse": return "Transit-WH"
			return "Dept-WH"
			
		mock_get_val.side_effect = get_val_side_effect
		
		# Mock new operation
		mock_create_op.return_value = FrappeDict(name="MOP-NEW")

		doc.on_submit_issue_new(cancel=False)

		# Verify MOP transition
		mock_set_val.assert_any_call("Manufacturing Operation", "MOP-CURRENT", "status", "Finished")
		mock_create_op.assert_called_once_with("DIR-ISS-001", "MWO-1", "MOP-CURRENT", "Dept B")
		
		# Verify MOP Log Generation
		self.assertTrue(mock_create_mop.called)
		args, kwargs = mock_create_mop.call_args
		self.assertEqual(args[0].name, "DIR-ISS-001")
		self.assertEqual(args[2], "Transit-WH") # In transit is source for issue log
		self.assertEqual(args[4], "MOP-NEW")

	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_datetime", return_value="2026-01-01 12:00:00")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value", return_value="Test WH")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_value", return_value="Test WH")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_doc")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_mop_log_for_department_ir")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.set_value")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.add_time_log")
	def test_on_submit_receive_marks_received_and_logs(
		self, mock_add_time, mock_set_val, mock_create_mop, mock_get_doc, mock_gv1, mock_gv2, mock_datetime
	):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-REC-001",
			type="Receive",
			current_department="Dept B",
			receive_against="DIR-ISS-001"
		)
		
		doc.append("department_ir_operation", {
			"manufacturing_operation": "MOP-NEW",
			"manufacturing_work_order": "MWO-1",
		})

		doc.on_submit_receive(cancel=False)

		# Verify status updates
		args, kwargs = mock_set_val.call_args_list[0]
		self.assertEqual(args[0], "Manufacturing Operation")
		self.assertEqual(args[1], "MOP-NEW")
		self.assertEqual(args[2]["department_receive_id"], "DIR-REC-001")
		self.assertEqual(args[2]["department_ir_status"], "Received")
		
		# Verify MOP Log Generation
		mock_create_mop.assert_called_once()
		
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value")
	def test_validate_receive_lineage_blocks_invalid_parents(self, mock_get_value):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-REC-001",
			type="Receive",
			receive_against="DIR-ISS-BAD"
		)
		
		doc.append("department_ir_operation", {
			"manufacturing_operation": "MOP-ORPHAN"
		})
		
		# Simulate a receive_against document that is in Draft (0) not Submitted (1)
		mock_get_value.return_value = FrappeDict(docstatus=0, type="Issue")
		
		with self.assertRaises(frappe.ValidationError) as context:
			doc.validate_receive_lineage()
			
		self.assertIn("must be a submitted Department IR", str(context.exception))
