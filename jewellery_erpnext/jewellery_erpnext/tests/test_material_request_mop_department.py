# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Guard: "Transfer to MOP" requires the material to already sit in the selected
Manufacturing Operation's department.

The check lives in ``material_request.before_update_after_submit`` and compares the
department of the warehouse the material actually sits in against
``Manufacturing Operation.department``. That is normally the Request Items' warehouse, but
a completed Transfer to Department has moved the material on to
``custom_destination_warehouse`` -- see ``_current_material_warehouse``.

It deliberately does NOT key on ``Material Request.custom_department`` -- that field
is a write-once stamp of the *source* (bagging) department and never equals the
operation's department.

On the classic path it applies only to an operation that has been walked into a department
by a Department IR (``previous_mop`` set). The MWO's first operation is a gathering point
in the default department and is exempt.

That exemption does NOT survive a Transfer to Department: once the operator has staged the
material into a named department every operation is checked, gathering point included, and
the message tells them to change the operation rather than move the material again.
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
	transfer_se=None,
	destination_warehouse=None,
):
	"""A Material Request as before_update_after_submit reads it.

	``transfer_se`` / ``destination_warehouse`` describe a request that has already been
	through Transfer to Department: the material has moved on, so the guard must read the
	destination rather than the Request Items' (now stale) warehouse.
	"""
	return SimpleNamespace(
		name="MR-001",
		workflow_state=workflow_state,
		custom_manufacturing_operation=mop,
		custom_department=custom_department,
		custom_department_transfer_se=transfer_se,
		custom_destination_warehouse=destination_warehouse,
		items=[SimpleNamespace(warehouse=warehouse)] if warehouse is not None else [],
	)


def _mop_row(department, status="Not Started", previous_mop="MOP-PREV-001"):
	"""A Manufacturing Operation row as before_update_after_submit reads it.

	``previous_mop`` defaults to set, i.e. an operation a Department IR has already
	walked into ``department`` -- the case the guard applies to.
	"""
	return frappe._dict(status=status, department=department, previous_mop=previous_mop)


class TestTransferToMopDepartmentGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, mop_row, warehouse_dept=None):
		"""Run before_update_after_submit with both Stock Entry makers stubbed.

		``warehouse_dept`` is either one department for every warehouse, or a
		``{warehouse: department}`` map when a test needs to prove *which* warehouse the
		guard consulted.

		Returns (department_maker_mock, plain_maker_mock) so callers can assert that
		nothing was created on the throwing paths.
		"""

		def _gv(doctype, name, fieldname=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return mop_row
			if doctype == "Warehouse":
				if isinstance(warehouse_dept, dict):
					return warehouse_dept.get(name)
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
		row = _mop_row("Pre Polish - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Diamond Setting - GEPL", msg)  # where the material is
		self.assertIn(_MOP, msg)  # the operation
		self.assertIn("Pre Polish - GEPL", msg)  # the operation's department

	def test_mismatch_creates_no_stock_entry(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Pre Polish - GEPL")
		_msg, dept_se, plain_se = self._run_expecting_throw(
			doc, row, "Diamond Setting - GEPL"
		)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_mismatch_blocked_on_custom_department_branch(self):
		"""Regression: custom_department used to bypass the check entirely."""
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Sub Contracting - GEPL")
		_msg, dept_se, _p = self._run_expecting_throw(
			doc, row, "Diamond Setting - GEPL"
		)
		dept_se.assert_not_called()

	def test_match_with_custom_department_calls_department_maker(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Diamond Setting - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_called_once_with(doc, mop=_MOP)
		plain_se.assert_not_called()

	def test_match_without_custom_department_calls_plain_maker(self):
		doc = _mr(custom_department=None)
		row = _mop_row("Diamond Setting - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		plain_se.assert_called_once_with(doc, mop=_MOP)
		dept_se.assert_not_called()

	def test_mop_department_unset_throws(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row(None)
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("(not set)", msg)
		self.assertIn("Diamond Setting - GEPL", msg)

	def test_warehouse_department_unset_throws(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Diamond Setting - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, None)
		self.assertIn("(not set)", msg)
		self.assertIn("Diamond Setting - GEPL", msg)

	def test_both_departments_unset_is_treated_as_a_match(self):
		"""None == None -- no mismatch to report, so the existing flow proceeds."""
		doc = _mr(custom_department=None)
		row = _mop_row(None)
		_d, plain_se = self._run(doc, row, None)
		plain_se.assert_called_once_with(doc, mop=_MOP)

	def test_missing_items_throws_warehouse_message(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL", warehouse=None)
		row = _mop_row("Diamond Setting - GEPL")
		msg, dept_se, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Warehouse is missing", msg)
		dept_se.assert_not_called()

	# --- the never-moved gathering-point exemption -----------------------

	def test_never_moved_mop_is_exempt_from_the_department_check(self):
		"""The MWO's first operation is minted in the default department and
		gathers material staged across several departments, so a mismatch there is
		normal. This is the shape create_pmo builds in the DB-backed fixtures."""
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Manufacturing Plan & Management - GEPL", previous_mop=None)
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_called_once_with(doc, mop=_MOP)
		plain_se.assert_not_called()

	def test_never_moved_mop_exempt_on_plain_branch_too(self):
		doc = _mr(custom_department=None)
		row = _mop_row("Manufacturing Plan & Management - GEPL", previous_mop=None)
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		plain_se.assert_called_once_with(doc, mop=_MOP)
		dept_se.assert_not_called()

	def test_never_moved_mop_skips_the_warehouse_lookup_entirely(self):
		"""No items at all is fine on the exempt path -- the guard never looks."""
		doc = _mr(custom_department="Diamond Bagging - GEPL", warehouse=None)
		row = _mop_row("Manufacturing Plan & Management - GEPL", previous_mop=None)
		dept_se, _p = self._run(doc, row, None)
		dept_se.assert_called_once_with(doc, mop=_MOP)

	def test_blank_previous_mop_is_treated_as_never_moved(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Pre Polish - GEPL", previous_mop="")
		dept_se, _p = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_called_once_with(doc, mop=_MOP)

	def test_finished_check_still_applies_to_a_never_moved_mop(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row(
			"Manufacturing Plan & Management - GEPL",
			status="Finished",
			previous_mop=None,
		)
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Finished", msg)

	# --- after a Transfer to Department, the material has moved -----------

	def test_uses_destination_warehouse_once_a_department_transfer_happened(self):
		"""The Request Items' warehouse is stale from that point on.

		Same warehouse map either way, so only the choice of warehouse can decide the
		outcome: WH-Bagging is in the wrong department, WH-Dest is in the operation's.
		"""
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		row = _mop_row("Pre Polish - GEPL")
		warehouses = {
			"WH-Bagging": "Diamond Bagging - GEPL",
			"WH-Dest": "Pre Polish - GEPL",
		}

		dept_se, plain_se = self._run(doc, row, warehouses)

		dept_se.assert_called_once_with(doc, mop=_MOP)
		plain_se.assert_not_called()

	def test_destination_in_another_department_throws_naming_it(self):
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		row = _mop_row("Pre Polish - GEPL")
		warehouses = {
			"WH-Bagging": "Pre Polish - GEPL",  # would have PASSED on the old reading
			"WH-Dest": "Diamond Setting - GEPL",
		}

		msg, dept_se, plain_se = self._run_expecting_throw(doc, row, warehouses)

		self.assertIn("Diamond Setting - GEPL", msg)  # where the material actually is
		self.assertIn("Pre Polish - GEPL", msg)  # the operation's department
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_missing_destination_warehouse_throws(self):
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse=None,
		)
		row = _mop_row("Pre Polish - GEPL")
		msg, dept_se, _p = self._run_expecting_throw(doc, row, "Pre Polish - GEPL")

		self.assertIn("Warehouse is missing", msg)
		dept_se.assert_not_called()

	def test_mismatch_tells_the_operator_to_change_the_operation(self):
		"""The remedy is the opposite of the classic path's.

		The material was just deliberately placed in a department, so it is the operation
		that has to change -- not the material that has to move again.
		"""
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		row = _mop_row("Pre Polish - GEPL")
		warehouses = {
			"WH-Bagging": "Pre Polish - GEPL",
			"WH-Dest": "Diamond Setting - GEPL",
		}

		msg, _d, _p = self._run_expecting_throw(doc, row, warehouses)

		self.assertIn("Select a Manufacturing Operation in Diamond Setting - GEPL", msg)
		self.assertIn("WH-Dest", msg)
		self.assertNotIn("Transfer the material", msg)

	# --- the gathering-point exemption does not survive a transfer -------

	def test_never_moved_mop_is_validated_after_a_department_transfer(self):
		"""Exempt on the classic path, NOT once the operator has staged the material.

		This is the case that used to pass with no department check at all.
		"""
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		row = _mop_row("Manufacturing Plan & Management - GEPL", previous_mop=None)
		warehouses = {
			"WH-Bagging": "Diamond Bagging - GEPL",
			"WH-Dest": "Diamond Setting - GEPL",
		}

		msg, dept_se, plain_se = self._run_expecting_throw(doc, row, warehouses)

		self.assertIn("Diamond Setting - GEPL", msg)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_never_moved_mop_in_the_destination_department_still_passes(self):
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		row = _mop_row("Diamond Setting - GEPL", previous_mop=None)
		warehouses = {
			"WH-Bagging": "Diamond Bagging - GEPL",
			"WH-Dest": "Diamond Setting - GEPL",
		}

		dept_se, _p = self._run(doc, row, warehouses)

		dept_se.assert_called_once_with(doc, mop=_MOP)

	def test_request_item_warehouse_still_used_without_a_transfer(self):
		"""Regression: nothing changes for a request that never took the department route."""
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			destination_warehouse="WH-Dest",  # set, but no transfer SE -- must be ignored
		)
		row = _mop_row("Diamond Setting - GEPL")
		warehouses = {
			"WH-Bagging": "Diamond Setting - GEPL",
			"WH-Dest": "Pre Polish - GEPL",
		}

		dept_se, _p = self._run(doc, row, warehouses)

		dept_se.assert_called_once_with(doc, mop=_MOP)

	# --- pre-existing guards keep priority -------------------------------

	def test_finished_mop_takes_priority(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Pre Polish - GEPL", status="Finished")
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("Finished", msg)

	def test_mop_not_found_takes_priority(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, None, "Diamond Setting - GEPL")
		self.assertIn("not found", msg)

	def test_no_mop_selected_throws(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL", mop=None)
		row = _mop_row("Diamond Setting - GEPL")
		msg, _d, _p = self._run_expecting_throw(doc, row, "Diamond Setting - GEPL")
		self.assertIn("select a Manufacturing Operation", msg)

	def test_other_workflow_state_is_noop(self):
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			workflow_state="Material Transferred",
		)
		row = _mop_row("Pre Polish - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def tearDown(self):
		return super().tearDown()
