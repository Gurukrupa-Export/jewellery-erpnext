# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, now, today

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	get_details_to_append,
	get_pending_ppo_sales_order,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_sales_order import (
	create_quotation,
	make_sales_order,
)


class TestManufacturingPlan(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		create_test_data()
		cls.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

	def test_manufacturing_plan(self):
		create_sales_order(self)
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.company = "Test_Company"
		man_plan.branch = self.branch
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
		create_sales_order(self)
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.branch = self.branch
		man_plan.company = "Test_Company"
		if man_plan.setting_type:
			man_plan.setting_type = "Close"
		man_plan.is_subcontracting = 1
		man_plan.supplier = "Test_Supplier"
		man_plan.estimated_date = add_to_date(now(), days=-4)
		man_plan.purchase_type = "FG Purchase"
		for row in man_plan.manufacturing_plan_table:
			row.estimated_delivery_date = row.estimated_delivery_date or today()
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
		{"company": "Test_Company"},
	)
	man_plan = get_details_to_append(json.dumps([so_list[0].name]), doc)
	return man_plan


def create_sales_order(self):
	create_quotation(self)
	quotation = frappe.get_value("Quotation", {"workflow_state": "Submitted"}, "name")
	sales_order = make_sales_order(quotation)
	sales_order.sales_type = "Finished Goods"
	sales_order.delivery_date = add_days(sales_order.transaction_date, 3)
	sales_order.custom_diamond_quality = "EF-VVS"
	for row in sales_order.items:
		row.bom_rate = 150000
		row.gold_bom_rate = 120000
		row.diamond_bom_rate = 25000
		row.making_charges = 5000
		row.rate = 150000
		if not row.warehouse:
			row.warehouse = self.warehouse
	sales_order.save()
	sales_order.submit()
