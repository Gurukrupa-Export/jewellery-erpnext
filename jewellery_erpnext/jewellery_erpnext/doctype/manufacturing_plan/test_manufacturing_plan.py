# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	get_details_to_append,
	get_pending_ppo_sales_order,
)


class TestManufacturingPlan(FrappeTestCase):
	def setUp(self):
		self.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		self.branch = frappe.get_value("Branch", {"branch_name": "Test_Branch"}, "name")
		return super().setUp()

	def test_manufacturing_plan(self):
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.branch = frappe.get_value(
			"Branch", {"branch_name": "Test_Branch"}, "name"
		)
		if man_plan.setting_type:
			man_plan.setting_type = "Close"
		man_plan.save()
		self.assertEqual(
			man_plan.total_planned_qty, len(man_plan.manufacturing_plan_table)
		)
		man_plan.submit()
		pmo_list = frappe.get_all(
			"Parent Manufacturing Order",
			filters={"manufacturing_plan": man_plan.name},
			pluck="name",
		)
		for pm in pmo_list:
			pmo = frappe.get_doc("Parent Manufacturing Order", pm)
			self.assertEqual(man_plan.name, pmo.manufacturing_plan)
			self.assertEqual(
				man_plan.manufacturing_plan_table[0].item_code, pmo.item_code
			)
			self.assertEqual(man_plan.manufacturing_plan_table[0].bom, pmo.master_bom)
			self.assertEqual(
				man_plan.manufacturing_plan_table[0].sales_order, pmo.sales_order
			)

	def test_manufacturing_plan_subcontracting(self):
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.branch = frappe.get_value(
			"Branch", {"branch_name": "Test_Branch"}, "name"
		)
		if man_plan.setting_type:
			man_plan.setting_type = "Close"
		man_plan.is_subcontracting = 1
		man_plan.supplier = "Test_Supplier"
		man_plan.estimated_date = add_to_date(now(), days=-4)
		man_plan.purchase_type = "FG Purchase"
		for row in man_plan.manufacturing_plan_table:
			row.subcontracting = 1
			row.supplier = man_plan.supplier
			row.subcontracting_qty = 1
			row.manufacturing_order_qty -= 1
			row.purchase_type = man_plan.purchase_type

		man_plan.save()
		self.assertEqual(
			man_plan.total_planned_qty, len(man_plan.manufacturing_plan_table)
		)
		man_plan.submit()

	def tearDown(self):
		return super().tearDown()


def manufacturing_plan_creation(doc):
	so_list = get_pending_ppo_sales_order(
		"Sales Order",
		None,
		"name",
		0,
		1,
		{"company": "Gurukrupa Export Private Limited"},
	)
	man_plan = get_details_to_append(json.dumps([so_list[0].name]), doc)
	return man_plan
