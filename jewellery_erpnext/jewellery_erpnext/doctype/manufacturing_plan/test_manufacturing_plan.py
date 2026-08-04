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
	is_subcontracting_selected,
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

	def test_is_subcontracting_selected(self):
		"""``is_subcontracting`` is a Select, so only an explicit yes may take the
		subcontracting branch -- the string "No" is truthy and must not. "1"/"0" are the
		legacy values left over from when the field was a Check."""
		for value in ("Yes", "yes", " YES ", "1"):
			self.assertTrue(is_subcontracting_selected(value), msg=value)
		for value in ("No", "no", "0", "", None):
			self.assertFalse(is_subcontracting_selected(value), msg=value)

	def test_manufacturing_plan(self):
		create_sales_order(self)
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		# Set BEFORE fetching the items -- get_items_for_production() reads this field, so
		# setting it afterwards leaves the branch under test unexercised.
		doc.is_subcontracting = "No"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.company = "Test_Company"
		man_plan.branch = self.branch
		if man_plan.setting_type:
			man_plan.setting_type = "Close"

		# "No" must leave the full pending qty on the manufacturing side.
		self.assertTrue(man_plan.manufacturing_plan_table)
		for row in man_plan.manufacturing_plan_table:
			self.assertTrue(row.pending_qty)
			self.assertEqual(row.manufacturing_order_qty, row.pending_qty)
			self.assertFalse(row.subcontracting)
			self.assertFalse(row.subcontracting_qty)

		man_plan.save()
		expected_qty = sum(
			row.manufacturing_order_qty for row in man_plan.manufacturing_plan_table
		)
		self.assertTrue(expected_qty)
		self.assertEqual(man_plan.total_planned_qty, expected_qty)
		man_plan.submit()
		pmo_list = frappe.get_all(
			"Parent Manufacturing Order",
			filters={"manufacturing_plan": man_plan.name},
			pluck="name",
		)
		# One Parent Manufacturing Order per unit -- a zero qty would silently create none.
		self.assertEqual(len(pmo_list), expected_qty)
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

		# ...and the consumed qty must be posted back onto the Sales Order Item.
		for row in man_plan.manufacturing_plan_table:
			self.assertEqual(
				frappe.db.get_value(
					"Sales Order Item", row.docname, "manufacturing_order_qty"
				),
				row.manufacturing_order_qty,
			)

	def test_manufacturing_plan_subcontracting(self):
		create_sales_order(self)
		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Manufacturing"
		# Every subcontracting input must be set BEFORE the fetch -- get_items_for_production()
		# copies them onto each row.
		doc.is_subcontracting = "Yes"
		doc.supplier = "Test_Supplier"
		doc.estimated_date = add_to_date(now(), days=-4)
		doc.purchase_type = "FG Purchase"
		man_plan = manufacturing_plan_creation(doc)
		man_plan.branch = self.branch
		man_plan.company = "Test_Company"
		if man_plan.setting_type:
			man_plan.setting_type = "Close"

		# The fetch itself must flag the rows. Asserting this before any manual fix-up is the
		# point: hand-setting row.subcontracting here is what used to hide the defect.
		self.assertTrue(man_plan.manufacturing_plan_table)
		for row in man_plan.manufacturing_plan_table:
			self.assertEqual(row.subcontracting, 1)
			self.assertEqual(row.subcontracting_qty, row.pending_qty)
			self.assertEqual(row.manufacturing_order_qty, 0)
			self.assertEqual(row.supplier, "Test_Supplier")
			self.assertEqual(row.purchase_type, "FG Purchase")
			row.estimated_delivery_date = row.estimated_delivery_date or today()

		man_plan.save()
		expected_qty = sum(
			row.subcontracting_qty for row in man_plan.manufacturing_plan_table
		)
		self.assertTrue(expected_qty)
		self.assertEqual(man_plan.total_planned_qty, expected_qty)
		man_plan.submit()

		# Subcontracting routes through a Purchase Order, not a Parent Manufacturing Order.
		self.assertTrue(
			frappe.db.exists("Purchase Order", {"manufacturing_plan": man_plan.name})
		)
		self.assertFalse(
			frappe.db.exists(
				"Parent Manufacturing Order", {"manufacturing_plan": man_plan.name}
			)
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
