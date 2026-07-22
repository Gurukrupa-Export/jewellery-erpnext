# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now, today

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	get_details_to_append,
	get_pending_ppo_quotation,
	get_repair_pending_ppo_quotation,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_sales_order import (
	create_quotation,
)


class TestManufacturingPlan(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

	def test_manufacturing_plan(self):
		create_manufacturing_quotation(self)
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
				man_plan.manufacturing_plan_table[0].quotation, pmo.quotation
			)

	def test_manufacturing_plan_subcontracting(self):
		create_manufacturing_quotation(self)
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

	def test_manufacturing_plan_repair(self):
		repair_quotation = create_repair_quotation(self)

		# order_type == "Repair" is the sole repair marker, set on the Quotation header...
		self.assertEqual(
			frappe.db.get_value("Quotation", repair_quotation, "order_type"), "Repair"
		)
		# ...and every Quotation item must carry the repair BOM the fetch aliases to serial_id_bom.
		quotation_serial_boms = frappe.get_all(
			"Quotation Item",
			filters={"parent": repair_quotation},
			pluck="custom_serial_id_bom",
		)
		self.assertTrue(quotation_serial_boms and all(quotation_serial_boms))

		# The repair picker (filtered on order_type == "Repair") must surface the Quotation...
		repair_list = get_repair_pending_ppo_quotation(
			"Quotation",
			None,
			"name",
			0,
			20,
			{"company": "Test_Company"},
		)
		self.assertIn(repair_quotation, [row.name for row in repair_list])

		# ...and the normal (non-repair) picker must NOT (symmetric exclusion).
		normal_list = get_pending_ppo_quotation(
			"Quotation",
			None,
			"name",
			0,
			20,
			{"company": "Test_Company"},
		)
		self.assertNotIn(repair_quotation, [row.name for row in normal_list])

		# Repair-mode mapping must append rows and resolve each BOM from serial_id_bom.
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Repair"
		man_plan = get_details_to_append(json.dumps([repair_quotation]), doc)
		self.assertTrue(man_plan.manufacturing_plan_table)
		for row in man_plan.manufacturing_plan_table:
			self.assertTrue(row.serial_id_bom)
			self.assertTrue(row.bom)

	def tearDown(self):
		return super().tearDown()


def manufacturing_plan_creation(doc):
	quotation_list = get_pending_ppo_quotation(
		"Quotation",
		None,
		"name",
		0,
		1,
		{"company": "Test_Company"},
	)
	man_plan = get_details_to_append(json.dumps([quotation_list[0].name]), doc)
	return man_plan


def create_manufacturing_quotation(self):
	"""Submit a Quotation the Manufacturing Plan can plan directly from.

	The Sales Order step was removed from the manufacturing flow, so the plan now fetches its
	rows straight off the Quotation (``get_items_for_production`` queries Quotation Item).
	"""
	create_quotation(self)
	return frappe.get_value("Quotation", {"workflow_state": "Submitted"}, "name")


def create_repair_quotation(self):
	"""Build a header-``order_type`` Repair Quotation so the Get Repair Quotation picker has data.

	The repair is marked purely at the header via ``order_type = "Repair"`` (the sole repair
	marker) and each Quotation item gets a ``custom_serial_id_bom`` -- which the plan fetch
	aliases to the row's ``serial_id_bom`` and uses to resolve the repair BOM.
	"""
	quotation = create_manufacturing_quotation(self)

	# Mark the (submitted) quotation as a repair and give each item a repair BOM.
	# (Quotation Item has no ``bom`` column, so source the repair BOM from the Item's master BOM.)
	frappe.db.set_value("Quotation", quotation, "order_type", "Repair")
	for qi in frappe.get_all(
		"Quotation Item", filters={"parent": quotation}, fields=["name", "item_code"]
	):
		bom = frappe.db.get_value("Item", qi.item_code, "master_bom")
		frappe.db.set_value("Quotation Item", qi.name, "custom_serial_id_bom", bom)

	return quotation
