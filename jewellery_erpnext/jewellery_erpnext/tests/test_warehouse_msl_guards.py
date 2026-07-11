# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Employee MSL Warehouse guards in doc_events/warehouse.py.

Pure-logic tests: DB / session / role lookups are patched so the department-scope
creation guard (req #4), the no-re-enable guard (req #5) and the department-wise
permission query (req #6) can be exercised in isolation.
"""

from types import SimpleNamespace
from unittest.mock import patch

from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import warehouse


def _wh(**fields):
	"""A stand-in Warehouse document."""
	defaults = {
		"name": "WH-EMP-0001",
		"employee": "EMP-0001",
		"department": None,
		"disabled": 0,
		"_is_new": False,
		"_before": None,
	}
	defaults.update(fields)
	d = SimpleNamespace(**defaults)
	d.is_new = lambda: d._is_new
	d.get_doc_before_save = lambda: d._before
	return d


class TestIsPrivileged(IntegrationTestCase):
	def test_administrator(self):
		self.assertTrue(warehouse._is_privileged("Administrator"))

	def test_system_manager(self):
		with patch.object(
			warehouse.frappe, "get_roles", return_value=["System Manager", "Employee"]
		):
			self.assertTrue(warehouse._is_privileged("u@x"))

	def test_stock_manager(self):
		with patch.object(
			warehouse.frappe, "get_roles", return_value=["Stock Manager"]
		):
			self.assertTrue(warehouse._is_privileged("u@x"))

	def test_plain_user(self):
		with patch.object(
			warehouse.frappe,
			"get_roles",
			return_value=["Employee", "Manufacturing User"],
		):
			self.assertFalse(warehouse._is_privileged("u@x"))


class TestGetUserDepartment(IntegrationTestCase):
	def test_lookup(self):
		with patch.object(
			warehouse.frappe.db, "get_value", return_value="Casting - GK"
		) as m:
			self.assertEqual(warehouse.get_user_department("u@x"), "Casting - GK")
			m.assert_called_once_with("Employee", {"user_id": "u@x"}, "department")

	def test_defaults_to_session_user(self):
		with patch.object(
			warehouse.frappe, "session", SimpleNamespace(user="sess@x")
		), patch.object(warehouse.frappe.db, "get_value", return_value=None) as m:
			warehouse.get_user_department()
			m.assert_called_once_with("Employee", {"user_id": "sess@x"}, "department")


class TestValidateMslDepartmentScope(IntegrationTestCase):
	def _run(self, doc, user_dept, wh_emp_dept, privileged=False):
		with patch.object(
			warehouse.frappe, "session", SimpleNamespace(user="u@x")
		), patch.object(
			warehouse, "_is_privileged", return_value=privileged
		), patch.object(
			warehouse, "get_user_department", return_value=user_dept
		), patch.object(warehouse.frappe.db, "get_value", return_value=wh_emp_dept):
			warehouse.validate_msl_department_scope(doc)

	def test_non_employee_warehouse_noop(self):
		# Mismatched depts but employee unset -> not an MSL warehouse -> no throw.
		self._run(_wh(employee=None), "A", "B")

	def test_privileged_bypass(self):
		self._run(_wh(), "A", "B", privileged=True)

	def test_user_without_department_throws(self):
		with self.assertRaises(ValidationError):
			self._run(_wh(), None, "B")

	def test_department_mismatch_throws(self):
		with self.assertRaises(ValidationError):
			self._run(_wh(), "A", "B")

	def test_department_match_passes(self):
		self._run(_wh(), "Casting - GK", "Casting - GK")


class TestGuardMslReenable(IntegrationTestCase):
	def _run(self, doc, privileged=False):
		with patch.object(
			warehouse.frappe, "session", SimpleNamespace(user="u@x")
		), patch.object(warehouse, "_is_privileged", return_value=privileged):
			warehouse.guard_msl_reenable(doc)

	def test_new_doc_noop(self):
		self._run(_wh(_is_new=True, disabled=0, _before=_wh(disabled=1)))

	def test_non_employee_noop(self):
		self._run(_wh(employee=None, disabled=0, _before=_wh(disabled=1)))

	def test_still_disabled_noop(self):
		self._run(_wh(disabled=1, _before=_wh(disabled=1)))

	def test_privileged_bypass(self):
		self._run(_wh(disabled=0, _before=_wh(disabled=1)), privileged=True)

	def test_reenable_throws(self):
		with self.assertRaises(ValidationError):
			self._run(_wh(disabled=0, _before=_wh(disabled=1)))

	def test_was_already_enabled_noop(self):
		self._run(_wh(disabled=0, _before=_wh(disabled=0)))


class TestPermissionQueryConditions(IntegrationTestCase):
	def test_privileged_no_restriction(self):
		with patch.object(warehouse, "_is_privileged", return_value=True):
			self.assertEqual(warehouse.get_permission_query_conditions("admin"), "")

	def test_no_department_hides_employee_warehouses(self):
		with patch.object(
			warehouse, "_is_privileged", return_value=False
		), patch.object(warehouse, "get_user_department", return_value=None):
			cond = warehouse.get_permission_query_conditions("u@x")
		self.assertEqual(cond, "(`tabWarehouse`.employee IS NULL)")

	def test_department_scopes_employee_warehouses(self):
		with patch.object(
			warehouse, "_is_privileged", return_value=False
		), patch.object(
			warehouse, "get_user_department", return_value="Casting - GK"
		), patch.object(
			warehouse.frappe.db, "escape", side_effect=lambda v: "'%s'" % v
		):
			cond = warehouse.get_permission_query_conditions("u@x")
		self.assertIn("`tabWarehouse`.employee IS NULL", cond)
		self.assertIn(
			"SELECT name FROM `tabEmployee` WHERE department = 'Casting - GK'", cond
		)

	def test_defaults_to_session_user(self):
		with patch.object(
			warehouse.frappe, "session", SimpleNamespace(user="sess@x")
		), patch.object(warehouse, "_is_privileged", return_value=True) as m:
			warehouse.get_permission_query_conditions()
			m.assert_called_once_with("sess@x")
