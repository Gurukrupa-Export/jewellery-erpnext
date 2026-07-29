# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, now, today

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	get_details_to_append,
	get_pending_ppo_sales_order,
	get_repair_pending_ppo_sales_order,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_sales_order import (
	create_quotation,
	make_sales_order,
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
		create_sales_order(self)
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.company = "Test_Company"
		man_plan.branch = self.branch
		if man_plan.setting_type:
			man_plan.setting_type = "Close"
		man_plan.is_subcontracting = 0
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
		# is_subcontracting is a Check: a "Yes"/"No" string would cint() to 0 and silently
		# save the header as NOT subcontracting, leaving this test green while exercising
		# nothing but the per-row row.subcontracting flag set below.
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

		# The header flag must survive the round-trip as 1, not be coerced to 0.
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Plan", man_plan.name, "is_subcontracting"
			),
			1,
		)

		# on_submit -> create_subcontracting_order -> make_subcontracting_order builds one
		# Purchase Order per supplier, stamped with this plan.
		po_name = frappe.db.get_value(
			"Purchase Order", {"manufacturing_plan": man_plan.name}, "name"
		)
		self.assertIsNotNone(po_name, "no subcontracting Purchase Order was created")
		po = frappe.get_doc("Purchase Order", po_name)
		self.assertEqual(po.supplier, man_plan.supplier)
		self.assertEqual(po.purchase_type, man_plan.purchase_type)
		self.assertEqual(
			sorted(row.item_code for row in po.items),
			sorted(row.item_code for row in man_plan.manufacturing_plan_table),
		)

	def test_manufacturing_plan_repair(self):
		repair_so = create_repair_sales_order(self)

		# order_type == "Repair" must have propagated Quotation -> Sales Order...
		self.assertEqual(
			frappe.db.get_value("Sales Order", repair_so, "order_type"), "Repair"
		)
		# ...and the before_validate bridge must have populated serial_id_bom on every item.
		so_serial_boms = frappe.get_all(
			"Sales Order Item", filters={"parent": repair_so}, pluck="serial_id_bom"
		)
		self.assertTrue(so_serial_boms and all(so_serial_boms))

		# The repair picker (now filtered on order_type == "Repair") must surface the SO...
		repair_list = get_repair_pending_ppo_sales_order(
			"Sales Order",
			None,
			"name",
			0,
			20,
			{"company": "Test_Company"},
		)
		self.assertIn(repair_so, [row.name for row in repair_list])

		# ...and the normal (non-repair) picker must NOT (symmetric exclusion).
		normal_list = get_pending_ppo_sales_order(
			"Sales Order",
			None,
			"name",
			0,
			20,
			{"company": "Test_Company"},
		)
		self.assertNotIn(repair_so, [row.name for row in normal_list])

		# Repair-mode mapping must append rows and resolve each BOM from serial_id_bom.
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Repair"
		man_plan = get_details_to_append(json.dumps([repair_so]), doc)
		self.assertTrue(man_plan.manufacturing_plan_table)
		for row in man_plan.manufacturing_plan_table:
			self.assertTrue(row.serial_id_bom)
			self.assertTrue(row.bom)

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


def create_repair_sales_order(self):
	"""Build a header-``order_type`` Repair sales order so the Get Repair Order picker has data.

	The repair is marked purely at the header via ``order_type = "Repair"`` (the sole repair
	marker). We stamp the submitted source Quotation with ``order_type = "Repair"`` and give each
	Quotation item a ``custom_serial_id_bom``; the Sales Order then inherits ``order_type`` via
	``get_mapped_doc`` (same-named field) and its ``serial_id_bom`` via the before_validate bridge
	(``set_repair_serial_bom``). This exercises the full new propagation path end to end.
	"""
	create_quotation(self)
	quotation = frappe.get_value("Quotation", {"workflow_state": "Submitted"}, "name")

	# Mark the (submitted) quotation as a repair and give each item a repair BOM to bridge over.
	# (Quotation Item has no ``bom`` column, so source the repair BOM from the Item's master BOM.)
	frappe.db.set_value("Quotation", quotation, "order_type", "Repair")
	for qi in frappe.get_all(
		"Quotation Item", filters={"parent": quotation}, fields=["name", "item_code"]
	):
		bom = frappe.db.get_value("Item", qi.item_code, "master_bom")
		frappe.db.set_value("Quotation Item", qi.name, "custom_serial_id_bom", bom)

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
	return sales_order.name
