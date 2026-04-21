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


class TestDepartmentIR(FrappeTestCase):
	def setUp(self):
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
