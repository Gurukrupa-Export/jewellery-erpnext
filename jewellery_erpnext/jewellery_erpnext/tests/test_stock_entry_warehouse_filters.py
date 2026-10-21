# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The Stock Entry warehouse picker for "Material Transfer (DEPARTMENT)".

That type carries ``add_to_transit = 1``, and ERPNext v16.36.0 rejects a sending leg whose
target is not a Transit warehouse (``StockEntry.validate_transit_warehouses``). The picker
must not offer what the server will refuse; the End Transit receipt leg, which lands the
stock in the receiving department, must not be offered Transit instead.

DB-free, per the app's test idiom: ``setUpClass`` is neutralised and the one Warehouse read
is answered by a keyed stub that lets every other ``get_value`` through.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	filters as se_filters,
)

_DEPARTMENT_TYPE = "Material Transfer (DEPARTMENT)"
_RM = "Casting RM - KGJPL"

Warehouse = frappe.qb.DocType("Warehouse")


class TestDepartmentWarehouseFilters(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _conditions(self, raw_department=_RM, **filters):
		"""Render get_filters_cond for a DEPARTMENT entry; returns (conditions, reads)."""
		real_get_value = frappe.db.get_value
		reads = []

		def _get_value(doctype, filters=None, *args, **kwargs):
			if doctype == "Warehouse" and isinstance(filters, dict):
				reads.append(filters)
				return raw_department
			return real_get_value(doctype, filters, *args, **kwargs)

		with patch.object(se_filters.frappe.db, "get_value", side_effect=_get_value):
			conditions = se_filters.get_filters_cond(
				{"stock_entry_type": _DEPARTMENT_TYPE, "company": "KGJPL", **filters}
			)
		return [str(c) for c in conditions], reads

	def test_sending_target_offers_transit_only(self):
		conditions, reads = self._conditions(
			department="Casting - KGJPL",
			field="t_warehouse",
			receive_leg=0,
			add_to_transit=1,
		)
		self.assertEqual(conditions, [str(Warehouse.warehouse_type == "Transit")])
		self.assertEqual(reads, [])

	def test_sending_target_offers_transit_only_without_a_department(self):
		"""Used to apply no condition at all when the user had no Employee department."""
		conditions, _ = self._conditions(
			department=None, field="t_warehouse", receive_leg=0, add_to_transit=1
		)
		self.assertEqual(conditions, [str(Warehouse.warehouse_type == "Transit")])

	def test_sending_target_not_in_transit_keeps_the_old_rule(self):
		"""An amended one-shot move is held at add_to_transit = 0 on the server; offering
		it Transit only would leave its stock there with no receipt leg."""
		conditions, _ = self._conditions(
			department="Casting - KGJPL",
			field="t_warehouse",
			receive_leg=0,
			add_to_transit=0,
		)
		self.assertEqual(
			conditions,
			[str((Warehouse.warehouse_type == "Transit") | (Warehouse.name == _RM))],
		)

	def test_receipt_target_offers_the_department_non_transit_warehouses(self):
		"""End Transit lands the stock in the receiving department -- RM, Manufacturing,
		FG or, after a Transfer to Department, its Reserve warehouse -- never in Transit."""
		conditions, reads = self._conditions(
			department="Casting - KGJPL", field="t_warehouse", receive_leg=1
		)
		self.assertEqual(
			conditions,
			[
				str(
					(Warehouse.department == "Casting - KGJPL")
					& (Warehouse.warehouse_type != "Transit")
				)
			],
		)
		self.assertEqual(reads, [])

	def test_receipt_target_without_a_department_offers_non_transit(self):
		conditions, _ = self._conditions(
			department=None, field="t_warehouse", receive_leg=1
		)
		self.assertEqual(conditions, [str(Warehouse.warehouse_type != "Transit")])

	def test_source_keeps_transit_or_department_raw_material(self):
		expected = [
			str((Warehouse.warehouse_type == "Transit") | (Warehouse.name == _RM))
		]
		for receive_leg in (0, 1):
			conditions, _ = self._conditions(
				department="Casting - KGJPL",
				field="s_warehouse",
				receive_leg=receive_leg,
			)
			self.assertEqual(conditions, expected)

	def test_caller_without_field_keeps_the_old_rule(self):
		conditions, _ = self._conditions(department="Casting - KGJPL")
		self.assertEqual(
			conditions,
			[str((Warehouse.warehouse_type == "Transit") | (Warehouse.name == _RM))],
		)
