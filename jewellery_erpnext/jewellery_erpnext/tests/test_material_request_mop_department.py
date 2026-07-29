# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Guard: "Transfer to MOP" requires the material to already sit in the selected
Manufacturing Operation's department.

The check lives in ``material_request.before_update_after_submit`` and compares the
Request Items' warehouse department against ``Manufacturing Operation.department``.
It deliberately does NOT key on ``Material Request.custom_department`` -- that field
is a write-once stamp of the *source* (bagging) department and never equals the
operation's department.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod

_MR = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"

_MOP = "MOP-001"


def _mr(
	custom_department=None,
	mop=_MOP,
	warehouse="WH-Setting",
	workflow_state="Material Transferred to MOP",
):
	return SimpleNamespace(
		name="MR-001",
		workflow_state=workflow_state,
		custom_manufacturing_operation=mop,
		custom_department=custom_department,
		items=[SimpleNamespace(warehouse=warehouse)] if warehouse is not None else [],
	)


class TestTransferToMopDepartmentGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, mop_row, warehouse_dept=None):
		"""Run before_update_after_submit with both Stock Entry makers stubbed.

		Returns (department_maker_mock, plain_maker_mock) so callers can assert that
		nothing was created on the throwing paths.
		"""

		def _gv(doctype, name, fieldname=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return mop_row
			if doctype == "Warehouse":
				return warehouse_dept
			return None

		with patch(f"{_MR}.frappe.db.get_value", side_effect=_gv), patch.object(
			mr_mod, "make_department_mop_stock_entry"
		) as dept_se, patch.object(mr_mod, "make_mop_stock_entry") as plain_se:
			try:
				mr_mod.before_update_after_submit(doc, None)
			finally:
				self._dept_se = dept_se
				self._plain_se = plain_se
		return dept_se, plain_se

	def _run_expecting_throw(self, doc, mop_row, warehouse_dept=None):
		"""Same as _run but returns (message, dept_mock, plain_mock) after a throw."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(doc, mop_row, warehouse_dept)
		return str(ctx.exception), self._dept_se, self._plain_se

	# --- the new guard ---------------------------------------------------

	def test_mismatch_throws_and_names_all_three(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Not Started", department="Pre Polish - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Diamond Setting - GEPL", msg)  # where the material is
		self.assertIn(_MOP, msg)  # the operation
		self.assertIn("Pre Polish - GEPL", msg)  # the operation's department

	def test_mismatch_creates_no_stock_entry(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Not Started", department="Pre Polish - GEPL")
		_msg, dept_se, plain_se = self._run_expecting_throw(
			doc, row, "Diamond Setting - GEPL"
		)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_mismatch_blocked_on_custom_department_branch(self):
		"""Regression: custom_department used to bypass the check entirely."""
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Not Started", department="Sub Contracting - GEPL")
		_msg, dept_se, _p = self._run_expecting_throw(
			doc, row, "Diamond Setting - GEPL"
		)
		dept_se.assert_not_called()

	def test_match_with_custom_department_calls_department_maker(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Not Started", department="Diamond Setting - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_called_once_with(doc, mop=_MOP)
		plain_se.assert_not_called()

	def test_match_without_custom_department_calls_plain_maker(self):
		doc = _mr(custom_department=None)
		row = frappe._dict(status="Not Started", department="Diamond Setting - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		plain_se.assert_called_once_with(doc, mop=_MOP)
		dept_se.assert_not_called()

	def test_mop_department_unset_throws(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Not Started", department=None)
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("(not set)", msg)
		self.assertIn("Diamond Setting - GEPL", msg)

	def test_warehouse_department_unset_throws(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Not Started", department="Diamond Setting - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, None)
		self.assertIn("(not set)", msg)
		self.assertIn("Diamond Setting - GEPL", msg)

	def test_both_departments_unset_is_treated_as_a_match(self):
		"""None == None -- no mismatch to report, so the existing flow proceeds."""
		doc = _mr(custom_department=None)
		row = frappe._dict(status="Not Started", department=None)
		_d, plain_se = self._run(doc, row, None)
		plain_se.assert_called_once_with(doc, mop=_MOP)

	def test_missing_items_throws_warehouse_message(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL", warehouse=None)
		row = frappe._dict(status="Not Started", department="Diamond Setting - GEPL")
		msg, dept_se, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Warehouse is missing", msg)
		dept_se.assert_not_called()

	# --- pre-existing guards keep priority -------------------------------

	def test_finished_mop_takes_priority(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = frappe._dict(status="Finished", department="Pre Polish - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Finished", msg)

	def test_mop_not_found_takes_priority(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, None, "Diamond Setting - GEPL")
		self.assertIn("not found", msg)

	def test_no_mop_selected_throws(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL", mop=None)
		row = frappe._dict(status="Not Started", department="Diamond Setting - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("select a Manufacturing Operation", msg)

	def test_other_workflow_state_is_noop(self):
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			workflow_state="Material Transferred",
		)
		row = frappe._dict(status="Not Started", department="Pre Polish - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def tearDown(self):
		return super().tearDown()
