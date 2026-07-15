# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for doc_events/warehouse_stock_entry.py — button-driven Issue / Receive
Material on an employee MSL warehouse.

Pure-logic: DB / Stock Entry persistence are patched. Covers the MSL-warehouse
validation guards, the Issue corridor (Dept RM -> this MSL WH via a
``Material Transfer (MAIN SLIP)``), and the Receive auto-difference model
(``loss = pending - returned``, difference booked to Dept Scrap via a
``Process Loss`` Repack).
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import (
	warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	warehouse_stock_entry as wse,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	warehouse_tracking as wt,
)


def _det_flt(value, precision=None, rounding_method=None):
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


class _FakeSE:
	"""Minimal stand-in for a Stock Entry document."""

	_counter = 0

	def __init__(self):
		self.items = []
		self.flags = SimpleNamespace()
		self.name = None
		self.inserted = False
		self.submitted = False

	def append(self, field, row):
		self.items.append(row)
		return row

	def insert(self):
		self.inserted = True
		_FakeSE._counter += 1
		self.name = f"SE-{_FakeSE._counter:04d}"

	def submit(self):
		self.submitted = True


_CTX = frappe._dict(
	msl_wh="MSL-WH",
	employee="EMP",
	department="DEPT",
	company="CO",
	manufacturer="MFG",
)


# ---------------------------------------------------------------------------
# _validate_msl_warehouse
# ---------------------------------------------------------------------------
class TestValidateMslWarehouse(IntegrationTestCase):
	@staticmethod
	def _gv_factory(wh_row, dept="DEPT", mfg="MFG"):
		def _gv(doctype, name, fieldname=None, **kwargs):
			if doctype == "Warehouse":
				return frappe._dict(wh_row) if wh_row else None
			if doctype == "Employee":
				return dept
			if doctype == "Department":
				return mfg
			return None

		return _gv

	def test_missing_warehouse(self):
		with self.assertRaises(frappe.ValidationError):
			wse._validate_msl_warehouse(None)

	def test_not_found(self):
		with patch("frappe.db.get_value", side_effect=self._gv_factory(None)):
			with self.assertRaises(frappe.ValidationError):
				wse._validate_msl_warehouse("WH")

	def test_no_employee(self):
		row = {
			"name": "WH",
			"employee": None,
			"warehouse_type": "Raw Material",
			"disabled": 0,
			"company": "CO",
		}
		with patch("frappe.db.get_value", side_effect=self._gv_factory(row)):
			with self.assertRaises(frappe.ValidationError):
				wse._validate_msl_warehouse("WH")

	def test_not_raw_material(self):
		row = {
			"name": "WH",
			"employee": "EMP",
			"warehouse_type": "Manufacturing",
			"disabled": 0,
			"company": "CO",
		}
		with patch("frappe.db.get_value", side_effect=self._gv_factory(row)):
			with self.assertRaises(frappe.ValidationError):
				wse._validate_msl_warehouse("WH")

	def test_disabled(self):
		row = {
			"name": "WH",
			"employee": "EMP",
			"warehouse_type": "Raw Material",
			"disabled": 1,
			"company": "CO",
		}
		with patch("frappe.db.get_value", side_effect=self._gv_factory(row)):
			with self.assertRaises(frappe.ValidationError):
				wse._validate_msl_warehouse("WH")

	def test_employee_no_department(self):
		row = {
			"name": "WH",
			"employee": "EMP",
			"warehouse_type": "Raw Material",
			"disabled": 0,
			"company": "CO",
		}
		with patch("frappe.db.get_value", side_effect=self._gv_factory(row, dept=None)):
			with self.assertRaises(frappe.ValidationError):
				wse._validate_msl_warehouse("WH")

	def test_happy_path(self):
		row = {
			"name": "WH",
			"employee": "EMP",
			"warehouse_type": "Raw Material",
			"disabled": 0,
			"company": "CO",
		}
		with patch(
			"frappe.db.get_value",
			side_effect=self._gv_factory(row, dept="DEPT", mfg="MFG"),
		):
			ctx = wse._validate_msl_warehouse("WH")
		self.assertEqual(ctx.msl_wh, "WH")
		self.assertEqual(ctx.employee, "EMP")
		self.assertEqual(ctx.department, "DEPT")
		self.assertEqual(ctx.company, "CO")
		self.assertEqual(ctx.manufacturer, "MFG")


# ---------------------------------------------------------------------------
# Shared setup for operation tests
# ---------------------------------------------------------------------------
class _OpTestBase(IntegrationTestCase):
	def setUp(self):
		self._patches = [
			patch.object(wse, "flt", _det_flt),
			patch("frappe.get_precision", return_value=3),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_cached_value", return_value="CO"),
			patch.object(wse, "_validate_msl_warehouse", return_value=_CTX),
			patch.object(wse, "_apply_fifo_batches_to_stock_entry"),
			patch.object(wse, "preallocate_series_for_docs"),
			patch.object(wse, "lock_bins"),
			patch.object(wse, "recalculate_msl_tracking"),
			patch.object(wse, "_get_department_rm_warehouse", return_value="DEPT-RM"),
			patch.object(wse, "get_scrap_warehouse", return_value="DEPT-SCRAP"),
			patch.object(wse, "_resolve_loss_item", return_value="LOSS-ITEM"),
		]
		for p in self._patches:
			p.start()
		self._made = []

		def _make(dt):
			se = _FakeSE()
			self._made.append(se)
			return se

		self._newdoc = patch("frappe.new_doc", side_effect=_make)
		self._newdoc.start()

	def tearDown(self):
		self._newdoc.stop()
		for p in self._patches:
			p.stop()


# ---------------------------------------------------------------------------
# issue_material
# ---------------------------------------------------------------------------
class TestIssueMaterial(_OpTestBase):
	def test_zero_qty_throws(self):
		with self.assertRaises(frappe.ValidationError):
			wse.issue_material("MSL-WH", "M-G", 0)

	def test_missing_item_throws(self):
		with self.assertRaises(frappe.ValidationError):
			wse.issue_material("MSL-WH", "", 5)

	def test_source_equals_msl_throws(self):
		# Explicit source_warehouse equal to the MSL warehouse must be rejected.
		with self.assertRaises(frappe.ValidationError):
			wse.issue_material("MSL-WH", "M-G", 5, source_warehouse="MSL-WH")

	def test_issue_corridor(self):
		name = wse.issue_material("MSL-WH", "M-G", 5)
		self.assertEqual(len(self._made), 1)
		se = self._made[0]
		self.assertEqual(se.stock_entry_type, wse.MATERIAL_TRANSFER_MAIN_SLIP)
		self.assertEqual(se.purpose, "Material Transfer")
		self.assertEqual(se.auto_created, 1)
		self.assertTrue(se.submitted)
		self.assertEqual(len(se.items), 1)
		row = se.items[0]
		self.assertEqual(row["item_code"], "M-G")
		self.assertEqual(row["qty"], 5)
		self.assertEqual(row["s_warehouse"], "DEPT-RM")  # dept RM auto-resolved
		self.assertEqual(row["t_warehouse"], "MSL-WH")  # target = this MSL warehouse
		self.assertEqual(name, se.name)
		wse.recalculate_msl_tracking.assert_called_once_with("MSL-WH")

	def test_issue_with_explicit_source(self):
		wse.issue_material("MSL-WH", "M-G", 5, source_warehouse="OTHER-RM")
		row = self._made[0].items[0]
		self.assertEqual(row["s_warehouse"], "OTHER-RM")


# ---------------------------------------------------------------------------
# receive_material (auto-difference)
# ---------------------------------------------------------------------------
class TestReceiveMaterial(_OpTestBase):
	def _pending(self, mapping):
		return patch.object(
			wse,
			"get_warehouse_item_tracking",
			return_value=[
				{"item_code": k, "pending_qty": v} for k, v in mapping.items()
			],
		)

	def test_partial_return_books_difference_as_loss(self):
		# pending 10, return 6 -> loss 4. Two SEs (transfer + Process Loss Repack).
		with self._pending({"M-G": 10.0}):
			names = wse.receive_material(
				"MSL-WH", [{"item_code": "M-G", "return_qty": 6}]
			)
		self.assertEqual(len(self._made), 2)
		se_recv, se_loss = self._made
		self.assertEqual(se_recv.stock_entry_type, wse.MATERIAL_TRANSFER_MAIN_SLIP)
		self.assertEqual(se_recv.purpose, "Material Transfer")
		self.assertEqual(se_loss.stock_entry_type, wse.PROCESS_LOSS)
		self.assertEqual(se_loss.purpose, "Repack")
		# received leg: 6 from MSL -> Dept RM
		self.assertEqual(se_recv.items[0]["qty"], 6)
		self.assertEqual(se_recv.items[0]["s_warehouse"], "MSL-WH")
		self.assertEqual(se_recv.items[0]["t_warehouse"], "DEPT-RM")
		# loss leg: consume 4 @ MSL + produce loss variant @ Scrap
		self.assertEqual(len(se_loss.items), 2)
		self.assertEqual(se_loss.items[0]["item_code"], "M-G")
		self.assertEqual(se_loss.items[0]["qty"], 4)
		self.assertEqual(se_loss.items[0]["s_warehouse"], "MSL-WH")
		self.assertEqual(se_loss.items[1]["item_code"], "LOSS-ITEM")
		self.assertEqual(se_loss.items[1]["t_warehouse"], "DEPT-SCRAP")
		self.assertEqual(len(names), 2)
		# Transfer submitted before the loss (FIFO against post-transfer stock).
		self.assertTrue(se_recv.submitted and se_loss.submitted)

	def test_full_return_no_loss(self):
		with self._pending({"M-G": 10.0}):
			names = wse.receive_material(
				"MSL-WH", [{"item_code": "M-G", "return_qty": 10}]
			)
		self.assertEqual(len(self._made), 1)
		self.assertEqual(
			self._made[0].stock_entry_type, wse.MATERIAL_TRANSFER_MAIN_SLIP
		)
		self.assertEqual(len(names), 1)

	def test_zero_return_scraps_all(self):
		with self._pending({"M-G": 10.0}):
			names = wse.receive_material(
				"MSL-WH", [{"item_code": "M-G", "return_qty": 0}]
			)
		self.assertEqual(len(self._made), 1)
		se_loss = self._made[0]
		self.assertEqual(se_loss.stock_entry_type, wse.PROCESS_LOSS)
		self.assertEqual(se_loss.items[0]["qty"], 10)
		self.assertEqual(len(names), 1)

	def test_return_over_pending_throws(self):
		with self._pending({"M-G": 10.0}):
			with self.assertRaises(frappe.ValidationError):
				wse.receive_material("MSL-WH", [{"item_code": "M-G", "return_qty": 11}])

	def test_negative_return_throws(self):
		with self._pending({"M-G": 10.0}):
			with self.assertRaises(frappe.ValidationError):
				wse.receive_material("MSL-WH", [{"item_code": "M-G", "return_qty": -1}])

	def test_item_without_pending_throws(self):
		with self._pending({"M-G": 10.0}):
			with self.assertRaises(frappe.ValidationError):
				wse.receive_material("MSL-WH", [{"item_code": "X", "return_qty": 1}])

	def test_multi_item_grid(self):
		# M-G: return 3 of 5 -> loss 2 ; M-Y: return 4 of 4 -> loss 0.
		with self._pending({"M-G": 5.0, "M-Y": 4.0}):
			names = wse.receive_material(
				"MSL-WH",
				[
					{"item_code": "M-G", "return_qty": 3},
					{"item_code": "M-Y", "return_qty": 4},
				],
			)
		self.assertEqual(len(self._made), 2)
		se_recv, se_loss = self._made
		# Both items returned; only M-G has a loss row pair.
		recv_items = {r["item_code"]: r["qty"] for r in se_recv.items}
		self.assertEqual(recv_items, {"M-G": 3, "M-Y": 4})
		loss_metal = [r for r in se_loss.items if r["item_code"] == "M-G"]
		self.assertEqual(loss_metal[0]["qty"], 2)
		self.assertEqual(len(names), 2)

	def test_empty_rows_throws(self):
		with self._pending({"M-G": 10.0}):
			with self.assertRaises(frappe.ValidationError):
				wse.receive_material("MSL-WH", [])

	def test_string_rows_parsed(self):
		import json

		with self._pending({"M-G": 10.0}):
			names = wse.receive_material(
				"MSL-WH", json.dumps([{"item_code": "M-G", "return_qty": 10}])
			)
		self.assertEqual(len(names), 1)


# ---------------------------------------------------------------------------
# get_receivable_items
# ---------------------------------------------------------------------------
class TestGetReceivableItems(IntegrationTestCase):
	def test_filters_non_positive_pending(self):
		rows = [
			{"item_code": "M-G", "pending_qty": 5.0},
			{"item_code": "M-Y", "pending_qty": 0.0},
			{"item_code": "M-Z", "pending_qty": -1.0},
		]
		with (
			patch.object(wse, "flt", _det_flt),
			patch("frappe.get_precision", return_value=3),
			patch.object(wse, "_validate_msl_warehouse", return_value=_CTX),
			patch.object(wse, "get_warehouse_item_tracking", return_value=rows),
		):
			out = wse.get_receivable_items("MSL-WH")
		self.assertEqual(out, [{"item_code": "M-G", "pending_qty": 5.0}])


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


def _det_flt(value, precision=None, rounding_method=None):
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


class _FakeWH:
	def __init__(self):
		self.rows = []
		self.flags = SimpleNamespace()
		self.saved = False

	def set(self, field, val):
		self.rows = list(val)

	def append(self, field, row):
		self.rows.append(row)

	def save(self, ignore_permissions=False):
		self.saved = True


class TestPendingComputation(IntegrationTestCase):
	def setUp(self):
		self._p = patch.object(wt, "flt", _det_flt)
		self._p.start()

	def tearDown(self):
		self._p.stop()

	def test_pending_is_issue_minus_receive_minus_loss(self):
		raw = [
			{
				"warehouse": "WH",
				"employee": "E",
				"employee_name": "n",
				"department": "D",
				"item_code": "M-G",
				"issue_qty": 10.0,
				"receive_qty": 3.0,
				"loss_qty": 1.0,
			}
		]
		with patch("frappe.db.escape", side_effect=lambda v: "'%s'" % v), patch(
			"frappe.db.sql", return_value=raw
		):
			out = wt.get_warehouse_item_tracking({"warehouse": "WH"})
		self.assertEqual(out[0]["pending_qty"], 6.0)
		self.assertEqual(out[0]["issue_qty"], 10.0)
		self.assertEqual(out[0]["loss_qty"], 1.0)


class TestRecalculateScoping(IntegrationTestCase):
	def test_empty_arg(self):
		self.assertEqual(wt.recalculate_msl_tracking(None), 0)

	def test_skips_non_employee(self):
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(employee=None, warehouse_type="Raw Material"),
		):
			self.assertEqual(wt.recalculate_msl_tracking("WH"), 0)

	def test_skips_wip_warehouse(self):
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(
				employee="EMP", warehouse_type="Manufacturing"
			),
		), patch.object(wt, "get_warehouse_item_tracking") as g, patch(
			"frappe.get_doc"
		) as gd:
			self.assertEqual(wt.recalculate_msl_tracking("WH"), 0)
			g.assert_not_called()
			gd.assert_not_called()

	def test_writes_rm_warehouse(self):
		fake = _FakeWH()
		rows = [
			{
				"item_code": "M-G",
				"issue_qty": 10.0,
				"receive_qty": 3.0,
				"loss_qty": 1.0,
				"pending_qty": 6.0,
			}
		]
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(employee="EMP", warehouse_type="Raw Material"),
		), patch.object(wt, "get_warehouse_item_tracking", return_value=rows), patch(
			"frappe.get_doc", return_value=fake
		):
			n = wt.recalculate_msl_tracking("WH")
		self.assertEqual(n, 1)
		self.assertEqual(len(fake.rows), 1)
		self.assertEqual(fake.rows[0]["item_code"], "M-G")
		self.assertEqual(fake.rows[0]["pending_qty"], 6.0)
		self.assertTrue(fake.flags.ignore_msl_guards)
		self.assertTrue(fake.saved)
