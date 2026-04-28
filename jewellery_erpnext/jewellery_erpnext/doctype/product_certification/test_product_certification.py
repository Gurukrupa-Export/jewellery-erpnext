# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe import ValidationError
from frappe.tests.utils import FrappeTestCase


class TestProductCertification(FrappeTestCase):
	def setUp(self):
		super().setUp()

	def _product_certification_creation(self):
		serial_no = serial_no_creation()
		certification_issue = frappe.new_doc("Product Certification")
		certification_issue.service_type = "Hall Marking Service"
		certification_issue.department = "Product Certification - GEPL"
		certification_issue.supplier = "Test_Supplier"
		certification_issue.append(
			"product_details",
			{
				"serial_no": serial_no.name,
				"item_code": serial_no.item_code,
				"bom": frappe.get_value("Item", serial_no.item_code, "master_bom"),
			},
		)
		certification_issue.save()
		certification_issue.exploded_product_details[0].gross_weight = 1
		certification_issue.save()
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Receipt"
		se.to_warehouse = "Product Certification WO - GEPL"
		se.append(
			"items",
			{
				"item_code": certification_issue.product_details[0].item_code,
				"qty": 1,
				"serial_no": serial_no.name,
			},
		)
		se.save()
		se.submit()

		certification_issue.submit()

		se = frappe.get_doc(
			"Stock Entry",
			frappe.get_value(
				"Stock Entry",
				filters={"product_certification": certification_issue.name},
			),
		)
		self.assertEqual(certification_issue.name, se.product_certification)
		self.assertEqual(
			certification_issue.product_details[0].serial_no,
			se.items[0].reference_docname,
		)
		self.assertEqual(
			certification_issue.product_details[0].item_code, se.items[0].item_code
		)

		po = frappe.get_doc(
			"Purchase Order",
			frappe.get_value(
				"Purchase Order",
				filters={"product_certification": certification_issue.name},
			),
		)
		self.assertEqual(po.product_certification, certification_issue.name)
		self.assertEqual(po.supplier, certification_issue.supplier)

		certification_receive = frappe.new_doc("Product Certification")
		certification_receive.type = "Receive"
		certification_receive.service_type = "Hall Marking Service"
		certification_receive.receive_against = certification_issue.name
		certification_receive.department = "Product Certification - GEPL"
		certification_receive.supplier = "Test_Supplier"
		certification_receive.append(
			"product_details",
			{
				"serial_no": serial_no.name,
				"item_code": serial_no.item_code,
				"bom": frappe.get_value("Item", serial_no.item_code, "master_bom"),
			},
		)
		certification_receive.total_amount = 450
		certification_receive.save()
		certification_receive.exploded_product_details[0].gross_weight = 1
		certification_receive.submit()

		se = frappe.get_doc(
			"Stock Entry",
			frappe.get_value(
				"Stock Entry",
				filters={"product_certification": certification_receive.name},
			),
		)
		self.assertEqual(certification_receive.name, se.product_certification)
		self.assertEqual(
			certification_receive.product_details[0].serial_no,
			se.items[0].reference_docname,
		)
		self.assertEqual(
			certification_receive.product_details[0].item_code, se.items[0].item_code
		)

	def _validate_warehouse_for_department_not_exists(self):
		certification = frappe.new_doc("Product Certification")
		certification.service_type = "Hall Marking Service"
		certification.department = "Test Department - GEPL"
		certification.supplier = "Test_Supplier"
		certification.company = "_Test Indian Registered Company"

		with self.assertRaises(ValidationError) as context:
			certification.validate()

		self.assertIn(
			"Please set warehouse for selected Department", str(context.exception)
		)

		certification = frappe.new_doc("Product Certification")
		certification.service_type = "Hall Marking Service"
		certification.supplier = "Test_Supplier"
		certification.company = "_Test Indian Registered Company"

		with self.assertRaises(ValidationError) as context:
			certification.validate()

		self.assertIn(
			"Please set warehouse for selected supplier", str(context.exception)
		)

	def _validate_items_receive_type_item_not_found(self):
		certification = frappe.new_doc("Product Certification")
		certification.type = "Receive"
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - GEPL"
		certification.supplier = "Test_Supplier"
		certification.receive_against = "PC-TEST-001"

		certification.append(
			"product_details",
			{
				"serial_no": "TEST-SERIAL-001",
				"item_code": "TEST-ITEM-001",
				"bom": "BOM-TEST-001",
			},
		)

		with self.assertRaises(ValidationError) as context:
			certification.validate()

		self.assertIn("item not found in", str(context.exception))

	def _update_bom_throws_error_when_no_serial_or_mwo(self):
		certification = frappe.new_doc("Product Certification")
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - GEPL"
		certification.supplier = "Test_Supplier"

		certification.append(
			"product_details",
			{
				"item_code": frappe.get_value("Item", filters={"is_design_code": 1}),
			},
		)

		with self.assertRaises(ValidationError) as context:
			certification.validate()

		self.assertIn(
			"Either select serial no or manufacturing work order",
			str(context.exception),
		)

	def _distribute_amount_across_exploded_details(self):
		certification = frappe.new_doc("Product Certification")
		certification.type = "Receive"
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - GEPL"
		certification.supplier = "Test_Supplier"
		certification.company = "_Test Indian Registered Company"
		certification.total_amount = 1000

		certification.append(
			"product_details",
			{
				"serial_no": "TEST-SERIAL-001",
				"item_code": frappe.get_value("Item", filters={"is_design_code": 1}),
				"bom": "BOM-TEST-001",
				"category": "Ring",
				"sub_category": "Gold Ring",
				"total_weight": 10.0,
			},
		)

		certification.append(
			"exploded_product_details",
			{
				"item_code": frappe.get_value("Item", filters={"is_design_code": 1}),
				"serial_no": "TEST-SERIAL-001",
				"bom": "BOM-TEST-001",
				"gross_weight": 10.0,
			},
		)

		certification.append(
			"exploded_product_details",
			{
				"item_code": frappe.get_value(
					"Item", filters={"is_design_code": 1, "master_bom": ["is", "set"]}
				),
				"serial_no": "TEST-SERIAL-002",
				"bom": "BOM-TEST-002",
				"gross_weight": 5.0,
			},
		)

		certification.distribute_amount()

		expected_amount = 1000 / 2
		self.assertEqual(
			certification.exploded_product_details[0].amount, expected_amount
		)
		self.assertEqual(
			certification.exploded_product_details[1].amount, expected_amount
		)

	def test_distribute_amount_multiple_orders(self):
		doc = frappe.new_doc("Product Certification")
		doc.type = "Receive"
		doc.total_amount = 900

		doc.product_details = [
			frappe._dict(
				{
					"parent_manufacturing_order": "PMO-A",
					"manufacturing_work_order": None,
					"serial_no": "S1",
					"qty": 10,
					"total_weight": 2,
				}
			),
			frappe._dict(
				{
					"parent_manufacturing_order": "PMO-B",
					"manufacturing_work_order": None,
					"serial_no": "S2",
					"qty": 20,
					"total_weight": 5,
				}
			),
		]

		doc.exploded_product_details = [
			frappe._dict(
				{
					"parent_manufacturing_order": "PMO-A",
					"manufacturing_work_order": None,
					"serial_no": "S1",
				}
			),
			frappe._dict(
				{
					"parent_manufacturing_order": "PMO-B",
					"manufacturing_work_order": None,
					"serial_no": "S2",
				}
			),
		]

		doc.distribute_amount()

		self.assertNotEqual(
			doc.exploded_product_details[0].gross_weight,
			None,
			"PMO-A row should get amount",
		)

		self.assertNotEqual(
			doc.exploded_product_details[1].gross_weight,
			None,
			"PMO-B row should get amount",
		)

	def tearDown(self):
		return super().tearDown()


def serial_no_creation():
	item_dtl = frappe.get_value(
		"Item",
		filters={"is_design_code": 1, "master_bom": ["is", "set"]},
		fieldname=["name", "master_bom"],
		as_dict=1,
	)
	serial_no = frappe.new_doc("Serial No")
	serial_no.item_code = item_dtl.name
	serial_no.serial_no = "TEST01I1234"
	serial_no.save()
	return serial_no
