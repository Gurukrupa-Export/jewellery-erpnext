# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for doc_events/warehouse_tracking.py (req #7 / #8).

Pure-logic: DB calls are patched. Covers the Pending = Issue - Receive - Loss
computation and the ``recalculate_msl_tracking`` scoping (only employee Raw
Material / MSL warehouses are materialized; WIP/Manufacturing warehouses are
skipped and left to the report).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import warehouse_tracking as wt


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
