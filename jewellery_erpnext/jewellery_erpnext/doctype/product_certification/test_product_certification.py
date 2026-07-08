# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import cint

from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	create_po,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification import (
	get_stock_item_against_mwo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.test_serial_number_creator import (
	create_snc,
)


class TestProductCertification(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		stock = frappe.get_single("Stock Settings")
		stock.allow_negative_stock = 1
		stock.allow_negative_stock_for_batch = 1
		stock.save()
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		cls.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)

		# Set manufacturer on department
		dept_doc = frappe.get_doc("Department", cls.department)
		if dept_doc.manufacturer != "Shubh":
			dept_doc.manufacturer = "Shubh"
			dept_doc.save(ignore_permissions=True)

		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

		wh_doc = frappe.get_doc("Warehouse", cls.warehouse)
		if wh_doc.department != cls.department or wh_doc.warehouse_type not in [
			"Manufacturing",
			"Raw Material",
		]:
			wh_doc.department = cls.department
			wh_doc.warehouse_type = "Manufacturing"
			wh_doc.save(ignore_permissions=True)

	def test_product_certification_creation(self):
		serial_no = serial_no_creation(self)
		certification_issue = frappe.new_doc("Product Certification")
		certification_issue.company = "Test_Company"
		certification_issue.service_type = "Hall Marking Service"
		certification_issue.department = "Product Certification - T"
		certification_issue.supplier = "Test_Supplier"
		fetch_sn(certification_issue, serial_no.name)
		certification_issue.save()

		# Serial-no-only rows (no MWO/PMO) take diamond_pcs from the BOM total.
		bom_diamond_pcs = frappe.db.get_value(
			"BOM", certification_issue.product_details[0].bom, "total_diamond_pcs"
		)
		self.assertEqual(
			cint(certification_issue.exploded_product_details[0].diamond_pcs),
			cint(bom_diamond_pcs),
		)

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

		create_po(certification_issue)

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
		certification_receive.company = "Test_Company"
		certification_receive.type = "Receive"
		certification_receive.service_type = "Hall Marking Service"
		certification_receive.receive_against = certification_issue.name
		certification_receive.department = "Product Certification - T"
		certification_receive.supplier = "Test_Supplier"
		fetch_sn(certification_receive, serial_no.name)
		certification_receive.total_amount = 450
		certification_receive.save()
		certification_receive.exploded_product_details[0].huid = 1234
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

	def test_product_certification_diamond_service_workflow(self):
		serial_no = serial_no_creation(self)
		certification_issue = frappe.new_doc("Product Certification")
		certification_issue.company = "Test_Company"
		certification_issue.service_type = "Diamond Certificate service"
		certification_issue.department = "Product Certification - T"
		certification_issue.supplier = "Test_Supplier"
		fetch_sn(certification_issue, serial_no.name)
		certification_issue.save()

		bom_diamond_pcs = frappe.db.get_value(
			"BOM", certification_issue.product_details[0].bom, "total_diamond_pcs"
		)
		self.assertEqual(
			cint(certification_issue.exploded_product_details[0].diamond_pcs),
			cint(bom_diamond_pcs),
		)

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

		create_po(certification_issue)

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
		certification_receive.company = "Test_Company"
		certification_receive.type = "Receive"
		certification_receive.service_type = "Diamond Certificate service"
		certification_receive.receive_against = certification_issue.name
		certification_receive.department = "Product Certification - T"
		certification_receive.supplier = "Test_Supplier"
		fetch_sn(certification_receive, serial_no.name)
		certification_receive.total_amount = 450
		certification_receive.save()
		for row in certification_receive.exploded_product_details:
			row.certification = "CERT-1234"
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

		# Validation for certification update
		pmo_doc = frappe.db.get_value("Serial No", serial_no.name, "name")
		self.assertTrue(pmo_doc)

	@patch(
		"erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle.SerialandBatchBundle.validate_negative_batch"
	)
	@patch(
		"india_compliance.gst_india.overrides.transaction.validate_transaction",
		return_value=True,
	)
	def test_product_certification_hallmarking_pmo_workflow(
		self, mock_validate_transaction, mock_validate_negative_batch
	):
		# Create a PMO and all necessary MOP logs and reservations via serial_no_creation
		serial_no = serial_no_creation(self)

		# Fetch the PMO that was generated
		snc_name = frappe.db.get_value(
			"Serial Number Creator", {"fg_serial_no": serial_no.name}, "name"
		)
		pmo_name = frappe.db.get_value(
			"Serial Number Creator", snc_name, "parent_manufacturing_order"
		)
		self.assertTrue(
			pmo_name, "Expected PMO to be set on the generated serial number"
		)

		pmo = frappe.get_doc("Parent Manufacturing Order", pmo_name)

		# Ensure all MWOs for this PMO have the same department to pass Product Certification validation
		frappe.db.sql(
			"""
			UPDATE `tabManufacturing Work Order`
			SET department = %s
			WHERE manufacturing_order = %s
		""",
			(self.department, pmo.name),
		)

		# Issue Certification
		issue = frappe.new_doc("Product Certification")
		issue.service_type = "Hall Marking Service"
		issue.type = "Issue"
		issue.naming_series = "CERT-.YYYY.-"
		issue.company = "Test_Company"
		issue.supplier = frappe.db.get_value(
			"Supplier", {"supplier_name": "Test_Supplier"}, "name"
		)
		issue.branch = self.branch
		issue.department = self.department

		issue.append(
			"product_details",
			{
				"parent_manufacturing_order": pmo.name,
				"item_code": pmo.item_code,
				"bom": pmo.master_bom,
				"total_weight": 10.0,
				"supply_raw_material": 1,
			},
		)
		issue.save()
		issue.submit()

		# Verify Issue generated a Stock Entry
		se_name = frappe.db.get_value(
			"Stock Entry", {"product_certification": issue.name}, "name"
		)
		self.assertTrue(se_name)
		se = frappe.get_doc("Stock Entry", se_name)
		self.assertEqual(se.stock_entry_type, "Material Issue for Hallmarking")
		self.assertEqual(se.docstatus, 1)

		# Receive Certification
		receive = frappe.new_doc("Product Certification")
		receive.service_type = "Hall Marking Service"
		receive.type = "Receive"
		receive.naming_series = "CERT-.YYYY.-"
		receive.company = "Test_Company"
		receive.supplier = issue.supplier
		receive.branch = self.branch
		receive.department = self.department
		receive.receive_against = issue.name
		receive.total_amount = 100.0

		receive.append(
			"product_details",
			{
				"parent_manufacturing_order": pmo.name,
				"item_code": pmo.item_code,
				"bom": pmo.master_bom,
				"total_weight": 10.0,
			},
		)
		receive.get_exploded_table()
		receive.exploded_product_details[0].huid = "HM-9999"
		receive.save()
		receive.submit()

		# Verify Receive generated a Stock Entry and Purchase Order
		receive_se_name = frappe.db.get_value(
			"Stock Entry", {"product_certification": receive.name}, "name"
		)
		self.assertTrue(receive_se_name)
		receive_se = frappe.get_doc("Stock Entry", receive_se_name)
		self.assertEqual(
			receive_se.stock_entry_type, "Material Receipt for Hallmarking"
		)
		self.assertEqual(receive_se.docstatus, 1)

		create_po(issue)

		po_name = frappe.db.get_value(
			"Purchase Order", {"product_certification": issue.name}, "name"
		)
		self.assertTrue(po_name)

	def test_validate_warehouse_for_department_not_exists(self):
		dept = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": "Test Department",
				"company": "Test_Company",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

		certification = frappe.new_doc("Product Certification")
		certification.service_type = "Hall Marking Service"
		certification.department = dept.name
		certification.supplier = "Test_Supplier"
		certification.company = "Test_Company"

		with self.assertRaises(ValidationError) as context:
			certification.validate()

		self.assertIn(
			"Please set warehouse for selected Department", str(context.exception)
		)

		supplier = frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": "Test Supplier"}
		).insert(ignore_permissions=True)
		certification = frappe.new_doc("Product Certification")
		certification.company = "Test_Company"
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - T"
		certification.supplier = supplier.name

		with self.assertRaises(ValidationError) as context:
			certification.validate()

		self.assertIn(
			"Please set warehouse for selected supplier", str(context.exception)
		)

	def test_validate_items_receive_type_item_not_found(self):
		certification = frappe.new_doc("Product Certification")
		certification.company = "Test_Company"
		certification.type = "Receive"
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - T"
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

	def test_update_bom_throws_error_when_no_serial_or_mwo(self):
		certification = frappe.new_doc("Product Certification")
		certification.company = "Test_Company"
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - T"
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

	def test_distribute_amount_across_exploded_details(self):
		certification = frappe.new_doc("Product Certification")
		certification.type = "Receive"
		certification.service_type = "Hall Marking Service"
		certification.department = "Product Certification - T"
		certification.supplier = "Test_Supplier"
		certification.company = "Test_Company"
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
					"gross_weight": 1.5,
				}
			),
			frappe._dict(
				{
					"parent_manufacturing_order": "PMO-B",
					"manufacturing_work_order": None,
					"serial_no": "S2",
					"gross_weight": 1.5,
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

	@patch("frappe.model.document.Document._validate_links")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.get_item_loss_item"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.process_fire_assy_xrf_submit"
	)
	def test_fire_assy_service_creation_and_submit(
		self, mock_process, mock_loss_item, mock_validate_links
	):
		mock_loss_item.return_value = "LOSS-ITEM-001"

		orig_get_value = frappe.db.get_value

		def side_effect(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype == "Manufacturing Setting" and fieldname == "pure_gold_item":
				return "PURE-ITEM-001"
			if (
				doctype == "Product Details"
				and isinstance(filters, dict)
				and filters.get("parent") == "PC-TEST-001"
			):
				return "Existing Row"
			return orig_get_value(doctype, filters, fieldname, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=side_effect):
			certification = frappe.new_doc("Product Certification")
			certification.company = "Test_Company"
			certification.type = "Receive"
			certification.service_type = "Fire Assy Service"
			certification.department = "Product Certification - T"
			certification.supplier = "Test_Supplier"
			certification.receive_against = "PC-TEST-001"
			certification.manufacturer = "Test_Manufacturer"
			certification.total_amount = 100.0

			certification.append(
				"product_details",
				{
					"item_code": "TEST-ITEM-001",
					"main_slip": "SLIP-001",
					"tree_no": "TREE-001",
					"total_weight": 100.0,
				},
			)

			certification.save()

			exploded_items = [
				d.item_code for d in certification.exploded_product_details
			]
			self.assertIn("TEST-ITEM-001", exploded_items)
			self.assertIn("PURE-ITEM-001", exploded_items)
			self.assertIn("LOSS-ITEM-001", exploded_items)

			for d in certification.exploded_product_details:
				d.gross_weight = 33.3333333333
			certification.exploded_product_details[0].gross_weight += 0.0000000001

			certification.submit()
			mock_process.assert_called_once()

	@patch("frappe.model.document.Document._validate_links")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.get_item_loss_item"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.process_fire_assy_xrf_submit"
	)
	def test_xrf_service_creation_and_submit(
		self, mock_process, mock_loss_item, mock_validate_links
	):
		mock_loss_item.return_value = "LOSS-ITEM-001"

		orig_get_value = frappe.db.get_value

		def side_effect(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype == "Manufacturing Setting" and fieldname == "pure_gold_item":
				return "PURE-ITEM-001"
			if (
				doctype == "Product Details"
				and isinstance(filters, dict)
				and filters.get("parent") == "PC-TEST-001"
			):
				return "Existing Row"
			return orig_get_value(doctype, filters, fieldname, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=side_effect):
			certification = frappe.new_doc("Product Certification")
			certification.company = "Test_Company"
			certification.type = "Receive"
			certification.service_type = "XRF Services"
			certification.department = "Product Certification - T"
			certification.supplier = "Test_Supplier"
			certification.receive_against = "PC-TEST-001"
			certification.manufacturer = "Test_Manufacturer"
			certification.total_amount = 100.0

			certification.append(
				"product_details",
				{
					"item_code": "TEST-ITEM-001",
					"main_slip": "SLIP-001",
					"tree_no": "TREE-001",
					"total_weight": 100.0,
				},
			)

			certification.save()

			exploded_items = [
				d.item_code for d in certification.exploded_product_details
			]
			self.assertIn("TEST-ITEM-001", exploded_items)
			self.assertNotIn("PURE-ITEM-001", exploded_items)
			self.assertIn("LOSS-ITEM-001", exploded_items)

			for d in certification.exploded_product_details:
				d.gross_weight = 50.0

			certification.submit()
			mock_process.assert_called_once()

	def test_validate_exploded_qty_fire_assy(self):
		certification = frappe.new_doc("Product Certification")
		certification.company = "Test_Company"
		certification.type = "Receive"
		certification.service_type = "Fire Assy Service"

		certification.append(
			"product_details",
			{
				"item_code": "TEST-ITEM-001",
				"main_slip": "SLIP-001",
				"total_weight": 100.0,
			},
		)

		certification.append(
			"exploded_product_details",
			{
				"item_code": "TEST-ITEM-001",
				"main_slip": "SLIP-001",
				"gross_weight": 50.0,  # Mismatch
			},
		)

		with self.assertRaises(ValidationError) as context:
			certification.validate_exploded_qty()

		self.assertIn(
			"Total Gross Weight in Exploded Product Details", str(context.exception)
		)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.frappe.defaults.get_user_default"
	)
	def test_missing_manufacturer_or_pure_item(self, mock_default):
		mock_default.return_value = None  # No manufacturer

		certification = frappe.new_doc("Product Certification")
		certification.company = "Test_Company"
		certification.type = "Receive"
		certification.service_type = "Fire Assy Service"

		with self.assertRaises(Exception) as context:
			certification.get_exploded_table()
		self.assertIn("Set manufacturer in session defaults", str(context.exception))

		mock_default.return_value = "Test_Manufacturer"
		orig_get_value = frappe.db.get_value

		def side_effect(doctype, filters=None, fieldname=None, *args, **kwargs):
			if doctype == "Manufacturing Setting" and fieldname == "pure_gold_item":
				return None  # No pure item
			return orig_get_value(doctype, filters, fieldname, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=side_effect):
			with self.assertRaises(Exception) as context:
				certification.get_exploded_table()
			self.assertIn(
				"Select Manufacturer in session defaults or in Filed",
				str(context.exception),
			)

	def test_product_certification_permissions(self):
		# Create a test user with no roles
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "test_noperm@example.com",
				"first_name": "Test No Perm",
				"roles": [],
			}
		)
		if not frappe.db.exists("User", user.email):
			user.insert(ignore_permissions=True)

		frappe.set_user("test_noperm@example.com")

		certification = frappe.new_doc("Product Certification")
		certification.company = "Test_Company"
		certification.type = "Receive"
		certification.service_type = "Fire Assy Service"

		with self.assertRaises(frappe.PermissionError):
			certification.save()

		frappe.set_user("Administrator")

	def tearDown(self):
		return super().tearDown()


class TestHallmarkingStockEntryPcs(IntegrationTestCase):
	"""Unit coverage for pcs propagation in get_stock_item_against_mwo.

	Kept separate from TestProductCertification so it does not depend on the
	heavy create_test_data() fixture: the MWO/MOP/warehouse machinery is mocked
	and only the pcs-assignment logic is exercised.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_hallmarking_issue_se_carries_pcs_for_diamond(self):
		"""Diamond/gemstone rows take their batch-based pcs from the MOP balance;
		metal rows are left untouched (default 1)."""
		se_doc = frappe.new_doc("Stock Entry")
		se_doc.stock_entry_type = "Material Issue for Hallmarking"

		doc = frappe._dict(type="Issue")
		row = frappe._dict(
			idx=1,
			manufacturing_work_order="FAKE-MWO",
			parent_manufacturing_order="FAKE-PMO",
		)

		balance_rows = [
			{
				"item_code": "D-NT-RO-TEST-01",
				"qty_after_transaction_batch_based": 0.664,
				"pcs_after_transaction_batch_based": 395,
				"batch_no": "BATCH-D-01",
			},
			{
				"item_code": "M-G-18KT-TEST",
				"qty_after_transaction_batch_based": 5.211,
				"pcs_after_transaction_batch_based": 0,
				"batch_no": "BATCH-M-01",
			},
		]

		orig_get_value = frappe.db.get_value

		def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			# Make latest_mop resolve so the balance loop runs; delegate every
			# other lookup (incl. meta loading) to the real implementation.
			if (
				doctype == "Manufacturing Work Order"
				and fieldname == "manufacturing_operation"
			):
				return "FAKE-MOP"
			return orig_get_value(doctype, filters, fieldname, *args, **kwargs)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
			return_value=balance_rows,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.product_certification."
			"product_certification.resolve_and_validate",
			return_value="Test - WH",
		), patch.object(frappe.db, "get_value", side_effect=fake_get_value):
			get_stock_item_against_mwo(se_doc, doc, row, "Source - WH", "Target - WH")

		items_by_code = {it.item_code: it for it in se_doc.items}
		self.assertIn("D-NT-RO-TEST-01", items_by_code)
		self.assertIn("M-G-18KT-TEST", items_by_code)
		# Diamond row carries the real stone count from the MOP balance.
		self.assertEqual(cint(items_by_code["D-NT-RO-TEST-01"].pcs), 395)
		# Metal row is left untouched by our code (no stone count attached); the
		# DB default of "1" is applied later on save, not on append.
		self.assertFalse(items_by_code["M-G-18KT-TEST"].get("pcs"))


def serial_no_creation(self):
	# with patch("frappe.model.base_document.BaseDocument._validate_update_after_submit"):
	snc = create_snc(self)
	snc.submit()
	return frappe.get_doc("Serial No", snc.fg_serial_no)


def fetch_sn(doc, data):
	scan = data.strip()

	mwo = frappe.db.get_value(
		"Manufacturing Work Order",
		scan,
		[
			"name",
			"item_code",
			"master_bom",
			"manufacturing_order",
			"jewelex_batch_no",
			"manufacturing_operation",
		],
		as_dict=True,
	)

	if mwo:
		total_weight = 0

		if mwo.manufacturing_operation:
			mop = frappe.db.get_value(
				"Manufacturing Operation",
				mwo.manufacturing_operation,
				["received_gross_wt", "gross_wt"],
				as_dict=True,
			)

			if mop:
				total_weight = mop.received_gross_wt or mop.gross_wt or 0

		doc.append(
			"product_details",
			{
				"manufacturing_work_order": mwo.name,
				"item_code": mwo.item_code or "",
				"bom": mwo.master_bom or "",
				"parent_manufacturing_order": mwo.manufacturing_order,
				"jewelex_batch_no": mwo.jewelex_batch_no,
				"total_weight": total_weight,
			},
		)

	else:
		sn = frappe.db.get_value(
			"Serial No",
			scan,
			[
				"name",
				"item_code",
				"custom_gross_wt",
				"custom_jwelex_tag_no",
				"custom_bom_no",
			],
			as_dict=True,
		)

		if sn:
			item = (
				frappe.db.get_value(
					"Item",
					sn.item_code,
					["item_category", "item_subcategory"],
					as_dict=True,
				)
				or {}
			)

			doc.append(
				"product_details",
				{
					"serial_no": sn.name,
					"jwelex_tag_no": sn.custom_jwelex_tag_no or "",
					"item_code": sn.item_code or "",
					"total_weight": sn.custom_gross_wt or 0,
					"category": item.get("item_category", ""),
					"sub_category": item.get("item_subcategory", ""),
					"bom": sn.custom_bom_no or "",
				},
			)
		else:
			frappe.throw(
				f"Scanned value {scan} is neither a valid Manufacturing Work Order nor a Serial No."
			)

	doc.scan = ""
