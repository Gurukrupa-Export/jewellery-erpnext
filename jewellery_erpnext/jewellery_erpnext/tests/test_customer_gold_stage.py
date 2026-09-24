# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""F28 -- a custody event's stage comes from the warehouse type, not from whether it has a department.

On kg-gk 13 KGJPL Raw Material warehouses carry a department and were labelled WIP, and 165
Manufacturing warehouses carry none and were labelled RM. Pure-logic: the Warehouse read is stubbed.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf

WAREHOUSES = {
	"Diamond Bagging RM - KGJPL": ("Raw Material", "Diamond Bagging - KGJPL"),
	"Diamond Setting RSV - KGJPL": ("Reserve", "Diamond Setting - KGJPL"),
	"Casting WO - KGJPL": ("Manufacturing", None),
	"Tagging WO - KGJPL": ("Manufacturing", "Tagging - KGJPL"),
	"In Transit - KGJPL": ("Transit", None),
	"FG Store - KGJPL": ("Finished Goods", "Tagging - KGJPL"),
	"Scrap - KGJPL": ("Scrap", None),
	"Untyped Floor - KGJPL": (None, "Casting - KGJPL"),
	"Untyped Store - KGJPL": (None, None),
}


def _get_cached_value(doctype, name, fields, as_dict=False):
	if name not in WAREHOUSES:
		return None
	warehouse_type, department = WAREHOUSES[name]
	return frappe._dict(warehouse_type=warehouse_type, department=department)


@patch(f"{cgf.__name__}.frappe.get_cached_value", side_effect=_get_cached_value)
class TestWarehouseStage(unittest.TestCase):
	def test_a_raw_material_warehouse_with_a_department_is_rm(self, _mock):
		self.assertEqual(
			cgf.warehouse_stage("Diamond Bagging RM - KGJPL"), cgf.STAGE_RM
		)

	def test_a_reserve_warehouse_is_rm(self, _mock):
		self.assertEqual(
			cgf.warehouse_stage("Diamond Setting RSV - KGJPL"), cgf.STAGE_RM
		)

	def test_a_manufacturing_warehouse_without_a_department_is_wip(self, _mock):
		self.assertEqual(cgf.warehouse_stage("Casting WO - KGJPL"), cgf.STAGE_WIP)
		self.assertEqual(cgf.warehouse_stage("Tagging WO - KGJPL"), cgf.STAGE_WIP)

	def test_transit_finished_goods_and_scrap(self, _mock):
		self.assertEqual(cgf.warehouse_stage("In Transit - KGJPL"), cgf.STAGE_TRANSIT)
		self.assertEqual(cgf.warehouse_stage("FG Store - KGJPL"), cgf.STAGE_FG)
		self.assertEqual(
			cgf.warehouse_stage("Scrap - KGJPL"), cgf.STAGE_RECOVERABLE_SCRAP
		)

	def test_an_untyped_warehouse_keeps_the_old_rule(self, _mock):
		self.assertEqual(cgf.warehouse_stage("Untyped Floor - KGJPL"), cgf.STAGE_WIP)
		self.assertEqual(cgf.warehouse_stage("Untyped Store - KGJPL"), cgf.STAGE_RM)

	def test_no_warehouse_or_unknown_warehouse_is_no_stage(self, _mock):
		self.assertIsNone(cgf.warehouse_stage(None))
		self.assertIsNone(cgf.warehouse_stage("No Such Warehouse"))
