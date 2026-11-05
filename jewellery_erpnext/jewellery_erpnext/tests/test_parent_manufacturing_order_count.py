"""
Unit tests for the Parent Manufacturing Order Count on Department IR and Employee IR.
Run with: bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_parent_manufacturing_order_count
"""
import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

EIR_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"


def _department_ir(*pmos):
	return frappe._dict(
		department_ir_operation=[
			frappe._dict(parent_manufacturing_order=pmo) for pmo in pmos
		]
	)


def _employee_ir(*work_orders):
	return frappe._dict(
		employee_ir_operations=[
			frappe._dict(manufacturing_work_order=mwo) for mwo in work_orders
		]
	)


def _fake_get_all(pmo_by_mwo):
	def get_all(doctype, filters, pluck):
		return [pmo_by_mwo[mwo] for mwo in filters["name"][1] if mwo in pmo_by_mwo]

	return get_all


class TestDepartmentIRParentManufacturingOrderCount(unittest.TestCase):
	def _count(self, doc):
		DepartmentIR.set_parent_manufacturing_order_count(doc)
		return doc.parent_manufacturing_order_count

	def test_distinct_pmos(self):
		self.assertEqual(
			self._count(_department_ir("PMO-001", "PMO-002", "PMO-003")), 3
		)

	def test_duplicate_pmos_counted_once(self):
		self.assertEqual(
			self._count(_department_ir("PMO-001", "PMO-001", "PMO-002", "PMO-003")), 3
		)

	def test_blank_pmo_ignored(self):
		self.assertEqual(self._count(_department_ir("PMO-001", None, "")), 1)

	def test_empty_table(self):
		self.assertEqual(self._count(_department_ir()), 0)

	def test_recount_after_row_removed(self):
		doc = _department_ir("PMO-001", "PMO-002")
		self.assertEqual(self._count(doc), 2)
		doc.department_ir_operation.pop()
		self.assertEqual(self._count(doc), 1)


class TestEmployeeIRParentManufacturingOrderCount(unittest.TestCase):
	PMO_BY_MWO = {
		"MWO-1": "PMO-002",
		"MWO-2": "PMO-002",
		"MWO-3": "PMO-003",
		"MWO-4": None,
	}

	def _count(self, doc):
		with patch(
			f"{EIR_MOD}.frappe.get_all", side_effect=_fake_get_all(self.PMO_BY_MWO)
		):
			EmployeeIR.set_parent_manufacturing_order_count(doc)
		return doc.parent_manufacturing_order_count

	def test_work_orders_sharing_a_pmo_counted_once(self):
		self.assertEqual(self._count(_employee_ir("MWO-1", "MWO-2", "MWO-3")), 2)

	def test_repeated_work_order_counted_once(self):
		self.assertEqual(self._count(_employee_ir("MWO-1", "MWO-1")), 1)

	def test_work_order_without_pmo_ignored(self):
		self.assertEqual(self._count(_employee_ir("MWO-3", "MWO-4", None)), 1)

	def test_empty_table_skips_query(self):
		with patch(f"{EIR_MOD}.frappe.get_all") as get_all:
			doc = _employee_ir()
			EmployeeIR.set_parent_manufacturing_order_count(doc)
		get_all.assert_not_called()
		self.assertEqual(doc.parent_manufacturing_order_count, 0)


class TestCountsAreIndependent(unittest.TestCase):
	def test_department_and_employee_counts_do_not_mix(self):
		dir_doc = _department_ir("PMO-001", "PMO-002", "PMO-003")
		eir_doc = _employee_ir("MWO-1", "MWO-2", "MWO-3")
		DepartmentIR.set_parent_manufacturing_order_count(dir_doc)
		with patch(
			f"{EIR_MOD}.frappe.get_all",
			side_effect=_fake_get_all(
				TestEmployeeIRParentManufacturingOrderCount.PMO_BY_MWO
			),
		):
			EmployeeIR.set_parent_manufacturing_order_count(eir_doc)
		self.assertEqual(dir_doc.parent_manufacturing_order_count, 3)
		self.assertEqual(eir_doc.parent_manufacturing_order_count, 2)
