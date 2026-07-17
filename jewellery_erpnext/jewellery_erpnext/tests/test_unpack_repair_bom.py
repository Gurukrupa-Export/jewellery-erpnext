"""
Pure-logic unit tests for the repair-unpack BOM resolver
(ManufacturingWorkOrder._resolve_repair_order_bom).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.tests.test_unpack_repair_bom

Note: frappe.db is a LocalProxy, so a default patch() mints async-mock children.
We inject an explicit MagicMock via patch(target, obj).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"


def _resolve(pmo_value, ro_new_bom):
	"""Call _resolve_repair_order_bom with frappe.db.get_value mocked.

	pmo_value : what get_value returns for the PMO row -- (order_form_type, order_form_id) or None.
	ro_new_bom: what get_value returns for Repair Order.new_bom (the real repair-components BOM).
	"""

	def gv(dt, name, fieldname):
		if dt == "Parent Manufacturing Order":
			return pmo_value
		if dt == "Repair Order":
			return ro_new_bom
		return None

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace(manufacturing_order="PMO-1")
	with patch(f"{MOD}.frappe.db", mock_db):
		return ManufacturingWorkOrder._resolve_repair_order_bom(fake_self)


class TestResolveRepairOrderBom(IntegrationTestCase):
	def test_returns_new_bom_for_repair_order(self):
		bom, pmo = _resolve(("Repair Order", "RO-1"), "BOM-X")
		self.assertEqual(bom, "BOM-X")
		self.assertEqual(pmo, "PMO-1")

	def test_throws_when_order_form_is_not_repair_order(self):
		# order_form_type is "Order", not "Repair Order".
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Order", "ORD-1"), "BOM-X")

	def test_throws_when_no_order_form_id(self):
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Repair Order", None), "BOM-X")

	def test_throws_when_pmo_missing(self):
		# get_value returns None (PMO row not found) -> (None, None) -> not a Repair Order.
		with self.assertRaises(frappe.ValidationError):
			_resolve(None, "BOM-X")

	def test_throws_when_repair_order_has_no_new_bom(self):
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Repair Order", "RO-1"), None)


def _check_dept(sn_dept, mwo_dept):
	"""Run _assert_serial_in_department with Warehouse.department mocked to sn_dept."""
	mock_db = MagicMock()
	mock_db.get_value.return_value = sn_dept
	fake_self = SimpleNamespace(department=mwo_dept, serial_no="SN1")
	with patch(f"{MOD}.frappe.db", mock_db):
		ManufacturingWorkOrder._assert_serial_in_department(fake_self, "WH-1")


class TestAssertSerialInDepartment(IntegrationTestCase):
	def test_matching_department_ok(self):
		_check_dept("Dept A", "Dept A")  # no throw

	def test_mismatched_department_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_dept("Dept A", "Dept B")

	def test_empty_string_and_none_are_equal(self):
		# "" (warehouse) and None (MWO) normalize together -> allowed.
		_check_dept("", None)
		_check_dept(None, None)

	def test_serial_wh_without_department_but_mwo_has_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_dept(None, "Dept A")


def _check_bookable(item_tracking):
	"""Run _assert_components_bookable with Item (has_serial_no, serial_no_series) mocked.

	item_tracking: dict item_code -> (has_serial_no, serial_no_series).
	"""

	def gv(dt, name, fieldname):
		return item_tracking.get(name)

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace()
	with patch(f"{MOD}.frappe.db", mock_db):
		ManufacturingWorkOrder._assert_components_bookable(
			fake_self, list(item_tracking.keys()), "BOM-X"
		)


class TestAssertComponentsBookable(IntegrationTestCase):
	def test_batch_and_seriesed_serial_items_ok(self):
		# batch item (0, None) and serial item WITH a series -> allowed.
		_check_bookable({"BATCH-ITEM": (0, None), "SER-ITEM": (1, "SER-.###")})

	def test_serial_item_without_series_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_bookable({"M-METAL": (0, None), "STONE-DV": (1, None)})

	def test_empty_bom_ok(self):
		_check_bookable({})
