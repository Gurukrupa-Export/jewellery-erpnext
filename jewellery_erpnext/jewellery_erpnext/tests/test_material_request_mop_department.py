# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Guard: the selected Manufacturing Operation must sit in the department the material is
actually in.

The rule is one function, ``material_request.validate_mop_department``, with two callers --
both inside ``before_update_after_submit``:

* the save-time gate at the top of that hook, which runs on a plain Update in
  ``Material Transferred`` / ``Material Transferred to Department``;
* the "Transfer to MOP" dispatch further down, which passes the department it has already
  read alongside ``status``.

Not ``before_validate``, where an "on save" check would normally go: frappe runs that only
for ``_action`` "save"/"submit", never "update_after_submit", and every request carrying an
operation is already submitted.

"Actually in" is ``_current_material_warehouse``'s call -- normally the Request Items'
warehouse, ``custom_destination_warehouse`` once a Transfer to Department has moved the
material on. That second branch is the "check the destination department" half of the rule.

It deliberately does NOT key on ``Material Request.custom_department`` -- that field is a
write-once stamp of the *source* (bagging) department and never equals the operation's.

There is no exemption. The MWO's first operation is minted in the default department and
used to be waved through unchecked as a "gathering point", keyed on its missing
``previous_mop``; that is the hole wrong-department operations went through and it is gone.

``Material Transferred to MOP`` is outside the save-time gate's state whitelist: by then the
Stock Entry has moved the material out of the warehouse being compared, and the field is
read-only there, so re-asserting the rule would leave the document unsaveable.
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
	material_request_type="Manufacture",
	custom_operation_type="Transfer to MOP",
):
	"""A Material Request as before_update_after_submit reads it.

	``transfer_se`` / ``destination_warehouse`` describe a request that has already been
	through Transfer to Department: the material has moved on, so the guard must read the
	destination rather than the Request Items' (now stale) warehouse.

	``material_request_type`` / ``custom_operation_type`` carry the defaults the save-time
	gate keys on. They are defaults rather than per-test arguments because a document
	missing them would silently skip that gate, and every test here would pass for the
	wrong reason.
	"""
	return SimpleNamespace(
		name="MR-001",
		workflow_state=workflow_state,
		material_request_type=material_request_type,
		custom_operation_type=custom_operation_type,
		custom_manufacturing_operation=mop,
		custom_department=custom_department,
		custom_department_transfer_se=transfer_se,
		custom_destination_warehouse=destination_warehouse,
		items=[SimpleNamespace(warehouse=warehouse)] if warehouse is not None else [],
	)


def _mop_row(department, status="Not Started"):
	"""A Manufacturing Operation row as the Transfer to MOP dispatch reads it."""
	return frappe._dict(status=status, department=department)


def _get_value_stub(mop_row, warehouse_dept):
	"""``frappe.db.get_value`` for both call shapes the rule uses.

	The dispatch asks for a list of Manufacturing Operation fields ``as_dict``; the
	save-time path asks for the single ``department`` string. ``warehouse_dept`` is either
	one department for every warehouse, or a ``{warehouse: department}`` map when a test
	needs to prove *which* warehouse was consulted.
	"""

	def _gv(doctype, name, fieldname=None, **kwargs):
		if doctype == "Manufacturing Operation":
			if isinstance(fieldname, str):
				return mop_row.get(fieldname) if mop_row else None
			return mop_row
		if doctype == "Warehouse":
			if isinstance(warehouse_dept, dict):
				return warehouse_dept.get(name)
			return warehouse_dept
		return None

	return _gv


class TestTransferToMopDepartmentGuard(IntegrationTestCase):
	"""The transition path: the save that applies "Transfer to MOP"."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, mop_row, warehouse_dept=None):
		"""Run before_update_after_submit with both Stock Entry makers stubbed.

		Returns (department_maker_mock, plain_maker_mock) so callers can assert that
		nothing was created on the throwing paths.
		"""
		stub = _get_value_stub(mop_row, warehouse_dept)
		with patch(f"{_MR}.frappe.db.get_value", side_effect=stub), patch.object(
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

	# --- the guard -------------------------------------------------------

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

	# --- the gathering-point exemption is gone ---------------------------

	def test_gathering_point_mop_is_no_longer_exempt(self):
		"""The MWO's first operation used to be waved through unchecked.

		It is minted in Manufacturing Setting's default_department and was exempted as a
		gathering point for material staged across several departments, keyed on its
		missing ``previous_mop``. That is the hole this closes, so the same shape now
		throws like any other mismatch.
		"""
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Manufacturing Plan & Management - GEPL")
		msg, dept_se, plain_se = self._run_expecting_throw(
			doc, row, "Diamond Setting - GEPL"
		)
		self.assertIn("Diamond Setting - GEPL", msg)
		self.assertIn("Manufacturing Plan & Management - GEPL", msg)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_finished_check_takes_priority_over_the_department_check(self):
		doc = _mr(custom_department="Diamond Bagging - GEPL")
		row = _mop_row("Manufacturing Plan & Management - GEPL", status="Finished")
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

	def test_default_department_mop_is_validated_after_a_department_transfer(self):
		"""This is the case that used to pass with no department check at all."""
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		row = _mop_row("Manufacturing Plan & Management - GEPL")
		warehouses = {
			"WH-Bagging": "Diamond Bagging - GEPL",
			"WH-Dest": "Diamond Setting - GEPL",
		}

		msg, dept_se, plain_se = self._run_expecting_throw(doc, row, warehouses)

		self.assertIn("Diamond Setting - GEPL", msg)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

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

	def test_unrelated_workflow_state_is_a_noop(self):
		"""A state outside both the dispatch and the save-time whitelist does nothing."""
		doc = _mr(
			custom_department="Diamond Bagging - GEPL",
			workflow_state="Material Reserved",
		)
		row = _mop_row("Pre Polish - GEPL")
		dept_se, plain_se = self._run(doc, row, "Diamond Setting - GEPL")
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestMopDepartmentCheckOnSave(IntegrationTestCase):
	"""The save-time gate: a plain Update, with no workflow transition in play.

	Every document here carries a ``get_doc_before_save`` returning the *same*
	``workflow_state``, so ``_workflow_action_just_applied`` is False and the dispatch below
	it returns early. Anything that throws can therefore only be the new gate.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _saved(self, **kwargs):
		doc = _mr(**kwargs)
		doc.get_doc_before_save = lambda: frappe._dict(
			workflow_state=doc.workflow_state
		)
		return doc

	def _run(self, doc, mop_department, warehouse_dept=None):
		stub = _get_value_stub(_mop_row(mop_department), warehouse_dept)
		with patch(f"{_MR}.frappe.db.get_value", side_effect=stub), patch.object(
			mr_mod, "make_department_mop_stock_entry"
		) as dept_se, patch.object(mr_mod, "make_mop_stock_entry") as plain_se:
			try:
				mr_mod.before_update_after_submit(doc, None)
			finally:
				self._dept_se = dept_se
				self._plain_se = plain_se
		return dept_se, plain_se

	def _run_expecting_throw(self, doc, mop_department, warehouse_dept=None):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(doc, mop_department, warehouse_dept)
		return str(ctx.exception), self._dept_se, self._plain_se

	def test_plain_update_in_material_transferred_throws_on_a_mismatch(self):
		doc = self._saved(workflow_state="Material Transferred")
		msg, dept_se, plain_se = self._run_expecting_throw(
			doc, "Pre Polish - GEPL", "Diamond Setting - GEPL"
		)
		self.assertIn("Diamond Setting - GEPL", msg)
		self.assertIn("Pre Polish - GEPL", msg)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_plain_update_in_material_transferred_passes_when_departments_agree(self):
		doc = self._saved(workflow_state="Material Transferred")
		dept_se, plain_se = self._run(
			doc, "Diamond Setting - GEPL", "Diamond Setting - GEPL"
		)
		# The gate validates; the dispatch below is still skipped, so no Stock Entry.
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_plain_update_uses_the_destination_warehouse_in_the_department_state(self):
		doc = self._saved(
			workflow_state="Material Transferred to Department",
			warehouse="WH-Bagging",
			transfer_se="SE-DEPT-1",
			destination_warehouse="WH-Dest",
		)
		warehouses = {
			"WH-Bagging": "Pre Polish - GEPL",  # would have passed on the stale reading
			"WH-Dest": "Diamond Setting - GEPL",
		}
		msg, _d, _p = self._run_expecting_throw(doc, "Pre Polish - GEPL", warehouses)
		self.assertIn("Select a Manufacturing Operation in Diamond Setting - GEPL", msg)
		self.assertIn("WH-Dest", msg)

	def test_the_check_runs_above_the_transition_gate(self):
		"""The whole point of the gate's position.

		``_workflow_action_just_applied`` is False here, so if the check sat below it this
		save would pass silently -- which is the bug being fixed.
		"""
		doc = self._saved(workflow_state="Material Transferred")
		self.assertFalse(mr_mod._workflow_action_just_applied(doc))
		self._run_expecting_throw(doc, "Pre Polish - GEPL", "Diamond Setting - GEPL")

	def test_reserved_state_is_not_checked(self):
		"""Pre-transfer states are outside the whitelist: the material is not placed yet."""
		doc = self._saved(workflow_state="Material Reserved")
		self._run(doc, "Pre Polish - GEPL", "Diamond Setting - GEPL")

	def test_draft_state_is_not_checked(self):
		"""Models the split-MR flow, which copies a request into Draft with an operation
		already stamped from the Manufacturing Work Order."""
		doc = self._saved(workflow_state="Draft")
		self._run(doc, "Pre Polish - GEPL", "Diamond Setting - GEPL")

	def test_non_manufacture_request_is_not_checked(self):
		doc = self._saved(
			workflow_state="Material Transferred",
			material_request_type="Material Transfer",
		)
		self._run(doc, "Pre Polish - GEPL", "Diamond Setting - GEPL")

	def test_department_route_is_not_checked_in_material_transferred(self):
		"""The route hides custom_manufacturing_operation but keeps its value.

		Checking that stale value would block the Transfer to Department action -- the very
		remedy the classic message recommends.
		"""
		doc = self._saved(
			workflow_state="Material Transferred",
			custom_operation_type="Transfer to Department",
		)
		self._run(doc, "Pre Polish - GEPL", "Diamond Setting - GEPL")

	def test_department_route_transition_save_is_not_checked(self):
		"""The save that lands in the department state, before any Stock Entry exists."""
		doc = self._saved(
			workflow_state="Material Transferred to Department",
			custom_operation_type="Transfer to Department",
			warehouse="WH-Bagging",
			transfer_se=None,
		)
		self._run(doc, "Pre Polish - GEPL", {"WH-Bagging": "Diamond Setting - GEPL"})

	def test_mop_state_is_not_rechecked_on_a_plain_update(self):
		"""By then the material has left the warehouse being compared, and the field is
		read-only -- so the operator would have no way past the check."""
		doc = self._saved(workflow_state="Material Transferred to MOP")
		dept_se, plain_se = self._run(
			doc, "Pre Polish - GEPL", "Diamond Setting - GEPL"
		)
		dept_se.assert_not_called()
		plain_se.assert_not_called()

	def test_no_operation_selected_is_not_checked_on_save(self):
		doc = self._saved(workflow_state="Material Transferred", mop=None)
		self._run(doc, "Pre Polish - GEPL", "Diamond Setting - GEPL")

	def test_save_path_uses_the_transition_message(self):
		"""One function, one wording -- the operator sees the same text either way."""
		doc = self._saved(workflow_state="Material Transferred")
		msg, _d, _p = self._run_expecting_throw(
			doc, "Pre Polish - GEPL", "Diamond Setting - GEPL"
		)
		self.assertIn("Transfer the material to", msg)

	def tearDown(self):
		return super().tearDown()


class TestValidateMopDepartment(IntegrationTestCase):
	"""The shared function on its own, including the _UNREAD sentinel's reason to exist."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_supplied_department_is_not_re_read(self):
		doc = _mr(workflow_state="Material Transferred")
		with patch(
			f"{_MR}.frappe.db.get_value",
			side_effect=_get_value_stub(None, "Diamond Setting - GEPL"),
		) as gv:
			mr_mod.validate_mop_department(doc, "Diamond Setting - GEPL")

		self.assertEqual(
			[call.args[0] for call in gv.call_args_list],
			["Warehouse"],
			"the caller had already read the department; it must not be read again",
		)

	def test_department_is_read_when_not_supplied(self):
		doc = _mr(workflow_state="Material Transferred")
		with patch(
			f"{_MR}.frappe.db.get_value",
			side_effect=_get_value_stub(
				_mop_row("Diamond Setting - GEPL"), "Diamond Setting - GEPL"
			),
		) as gv:
			mr_mod.validate_mop_department(doc)

		self.assertEqual(
			[call.args[0] for call in gv.call_args_list],
			["Manufacturing Operation", "Warehouse"],
		)

	def test_explicit_none_department_is_not_treated_as_unread(self):
		"""The bug a plain ``mop_department=None`` default would cause: a genuinely blank
		department would be re-read instead of reported."""
		doc = _mr(workflow_state="Material Transferred")
		with patch(
			f"{_MR}.frappe.db.get_value",
			side_effect=_get_value_stub(
				_mop_row("Diamond Setting - GEPL"), "Diamond Setting - GEPL"
			),
		), self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.validate_mop_department(doc, None)

		self.assertIn("(not set)", str(ctx.exception))
		self.assertIn("Diamond Setting - GEPL", str(ctx.exception))

	def tearDown(self):
		return super().tearDown()
