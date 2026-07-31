# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.product_certification import (
	product_certification as pc,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	create_po,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification import (
	ProductCertification,
	create_product_certification_receive,
	get_stock_item_against_mwo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.test_serial_number_creator import (
	create_snc,
)

PURITY_PATH = "jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.get_purity_percentage"

# Purities for the synthetic Fire Assy items — the real ones come from the Metal Purity
# attribute value, which test items do not carry.
_TEST_PURITY = {"TEST-ITEM-001": 91.9, "PURE-ITEM-001": 99.9}

# Must match the "name" in product_certification.json — that is what the test runner
# derives cls.doctype from, and the key _skip_generated_test_records seeds.
_DOCTYPE = "Product Certification"


def _purity(item_code):
	return _TEST_PURITY.get(item_code)


def _serial_department(serial_no):
	"""Department of the warehouse a freshly minted serial currently sits in.

	``ProductCertification.validate_serial_warehouse_department`` blocks an Issue whose
	Product Details serial is not in a warehouse of the document's department. The serial
	``create_snc`` mints lands wherever the manufacturing flow left it, not in a Product
	Certification warehouse, so tests that issue it must name that same department --
	moving the stock into Product Certification first is a Department IR flow these tests
	do not model. The department rule itself is covered by TestSerialWarehouseDepartment.
	"""
	return frappe.db.get_value("Warehouse", serial_no.warehouse, "department")


def _skip_generated_test_records():
	"""Mark this doctype's auto-generated test records as already present.

	IntegrationTestCase.setUpClass walks Product Certification's link graph to build
	fixtures, and that graph reaches Company — whose erpnext test module bootstraps the
	whole master-data set at import time and blows up in CI. Classes that build their
	documents by hand need none of it, so seed the cache the generator checks
	(``make_test_records`` uses the same idiom to avoid repeat work) and let
	``super().setUpClass()`` still do its real job: site init, connection handles and the
	class-level rollback that keeps inserted documents out of the next test.

	The doctype is named literally rather than read off ``cls.doctype``: that attribute is
	assigned by UnitTestCase.setUpClass, so it does not exist yet when this runs — it has
	to, since the generation being skipped happens later in that same super() call.
	"""
	frappe.local.test_objects.setdefault(_DOCTYPE, [])


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
		certification_issue.department = _serial_department(serial_no)
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
		certification_issue.department = _serial_department(serial_no)
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

			# Only the received metal and the recovered pure are entered — the loss row
			# and the pure row's purity-converted quantity are derived on save.
			main_row, pure_row, loss_row = certification.exploded_product_details
			main_row.gross_weight = 60.0
			pure_row.gross_weight = 30.0
			loss_row.gross_weight = 0.0

			with patch(PURITY_PATH, side_effect=_purity):
				certification.save()

				# 30 × 99.9 / 91.9 = 32.612 at 24KT, so 100 − 60 − 32.612 = 7.388 is lost,
				# and the three rows still sum back to the 100 issued.
				self.assertEqual(pure_row.conversion_quantity, 32.612)
				self.assertEqual(loss_row.gross_weight, 7.388)

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


class TestFireAssyLossWeight(IntegrationTestCase):
	"""The reported "loss is not calculated in the receive entry" bug.

	Kept free of the heavy create_test_data() fixture: calculate_fire_assy_loss_weight is
	pure arithmetic over the two child tables, so an unsaved document is enough to pin
	the behaviour that production data proved wrong (GE-PFA-26-00082 booked a hand-typed
	0.05 where the purity-converted answer is 0.041).
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, service_type, issue_weight, rows, main_slip=None, tree_no=None):
		doc = frappe.new_doc("Product Certification")
		doc.type = "Receive"
		doc.service_type = service_type
		doc.append(
			"product_details",
			{
				"item_code": "TEST-ITEM-001",
				"main_slip": main_slip,
				"tree_no": tree_no,
				"total_weight": issue_weight,
				"pure_item": "PURE-ITEM-001",
				"loss_item": "LOSS-ITEM-001",
			},
		)
		for item_code, gross_weight in rows:
			doc.append(
				"exploded_product_details",
				{
					"item_code": item_code,
					"main_slip": main_slip,
					"tree_no": tree_no,
					"gross_weight": gross_weight,
				},
			)
		return doc

	def _calculate(self, doc):
		with patch(PURITY_PATH, side_effect=_purity):
			doc.calculate_fire_assy_loss_weight()
		return doc.exploded_product_details

	def test_loss_calculated_without_main_slip(self):
		"""The bug: keying on main_slip alone made the routine return early.

		Most Fire Assy documents carry no main slip at all, so the loss row was left at
		whatever the operator typed.
		"""
		doc = self._doc(
			"Fire Assy Service",
			2.0,
			[("TEST-ITEM-001", 1.85), ("PURE-ITEM-001", 0.1), ("LOSS-ITEM-001", 0.0)],
		)
		_main, pure, loss = self._calculate(doc)

		self.assertEqual(pure.conversion_quantity, 0.109)
		self.assertEqual(loss.gross_weight, 0.041)

	def test_loss_calculated_with_main_slip_unchanged(self):
		doc = self._doc(
			"Fire Assy Service",
			1.0,
			[("TEST-ITEM-001", 0.9), ("PURE-ITEM-001", 0.02), ("LOSS-ITEM-001", 0.0)],
			main_slip="SLIP-001",
			tree_no="TREE-001",
		)
		_main, pure, loss = self._calculate(doc)

		self.assertEqual(pure.conversion_quantity, 0.022)
		self.assertEqual(loss.gross_weight, 0.078)

	def test_loss_calculated_for_xrf_without_pure_row(self):
		"""XRF was excluded outright by the old service_type guard, and it has no pure
		row — so `if not pure_weight: continue` would have skipped it regardless."""
		doc = self._doc(
			"XRF Services",
			7.0,
			[("TEST-ITEM-001", 6.5), ("LOSS-ITEM-001", 0.0)],
		)
		_main, loss = self._calculate(doc)

		self.assertEqual(loss.gross_weight, 0.5)

	def test_loss_clamped_at_zero_on_gain(self):
		doc = self._doc(
			"Fire Assy Service",
			1.0,
			[("TEST-ITEM-001", 1.0), ("PURE-ITEM-001", 0.5), ("LOSS-ITEM-001", 0.0)],
		)
		_main, _pure, loss = self._calculate(doc)

		self.assertEqual(loss.gross_weight, 0.0)

	def test_nothing_entered_yet_does_not_book_the_whole_issue_as_loss(self):
		"""The exploded rows are created on the first save, before any weight is typed.

		Booking the issue as loss there would balance validate_exploded_qty and let an
		all-loss document through.
		"""
		doc = self._doc(
			"Fire Assy Service",
			30.0,
			[("TEST-ITEM-001", 0.0), ("PURE-ITEM-001", 0.0), ("LOSS-ITEM-001", 0.0)],
		)
		rows = self._calculate(doc)

		self.assertEqual([r.gross_weight for r in rows], [0.0, 0.0, 0.0])

	def test_missing_purity_throws_instead_of_silently_zeroing(self):
		doc = self._doc(
			"Fire Assy Service",
			2.0,
			[("TEST-ITEM-001", 1.85), ("PURE-ITEM-001", 0.1), ("LOSS-ITEM-001", 0.0)],
		)
		with patch(PURITY_PATH, return_value=None):
			with self.assertRaises(frappe.ValidationError):
				doc.calculate_fire_assy_loss_weight()

	def test_distribute_amount_does_not_overwrite_the_computed_loss(self):
		"""distribute_amount used to back-fill zero-weight exploded rows with an
		un-purity-converted remainder, which fought the loss calculation."""
		doc = self._doc(
			"Fire Assy Service",
			2.0,
			[("TEST-ITEM-001", 1.85), ("PURE-ITEM-001", 0.1), ("LOSS-ITEM-001", 0.0)],
		)
		doc.total_amount = 90.0
		self._calculate(doc)
		doc.distribute_amount()

		main, pure, loss = doc.exploded_product_details
		self.assertEqual(loss.gross_weight, 0.041)
		self.assertEqual(main.gross_weight, 1.85)
		self.assertEqual(pure.gross_weight, 0.1)
		self.assertEqual([r.amount for r in doc.exploded_product_details], [30.0] * 3)

	def test_distribute_amount_keys_on_each_rows_own_order(self):
		"""The back-fill used to reuse the `common_order` left over from the loop above,
		so it only ever looked up the LAST Product Details row's order."""
		doc = frappe.new_doc("Product Certification")
		doc.type = "Receive"
		doc.service_type = "Hall Marking Service"
		doc.total_amount = 100.0
		for pmo, weight in (("PMO-A", 2.0), ("PMO-B", 5.0)):
			doc.append(
				"product_details",
				{"parent_manufacturing_order": pmo, "total_weight": weight},
			)
		for pmo in ("PMO-A", "PMO-B"):
			doc.append(
				"exploded_product_details",
				{
					"item_code": "TEST-ITEM-001",
					"parent_manufacturing_order": pmo,
					"gross_weight": 0,
				},
			)

		doc.distribute_amount()

		self.assertEqual(doc.exploded_product_details[0].gross_weight, 2.0)
		self.assertEqual(doc.exploded_product_details[1].gross_weight, 5.0)


class TestFireAssyIssueWeight(IntegrationTestCase):
	"""The Issue-side counterpart of TestFireAssyLossWeight.

	Fire Assy / XRF Issues stopped populating the main exploded row's gross_weight after PR #926
	removed the generic distribute_amount back-fill for these service types, so submit threw
	"No item found for Repack". set_fire_assy_issue_weight restores it: the operator-typed
	product_details.total_weight lands on the main exploded row, per (main_slip, tree_no) group,
	while pure/loss rows stay 0 (they are skipped when the Stock Entry is built).

	Pure arithmetic over the two child tables, like the loss calc — an unsaved document is enough.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, service_type, product_rows, exploded_rows, txn_type="Issue"):
		"""product_rows: (item_code, total_weight, main_slip, tree_no) tuples.
		exploded_rows: (item_code, gross_weight, main_slip, tree_no) tuples."""
		doc = frappe.new_doc("Product Certification")
		doc.type = txn_type
		doc.service_type = service_type
		for item_code, total_weight, main_slip, tree_no in product_rows:
			doc.append(
				"product_details",
				{
					"item_code": item_code,
					"total_weight": total_weight,
					"main_slip": main_slip,
					"tree_no": tree_no,
				},
			)
		for item_code, gross_weight, main_slip, tree_no in exploded_rows:
			doc.append(
				"exploded_product_details",
				{
					"item_code": item_code,
					"gross_weight": gross_weight,
					"main_slip": main_slip,
					"tree_no": tree_no,
				},
			)
		return doc

	def test_fire_assy_issue_sets_main_row_weight(self):
		doc = self._doc(
			"Fire Assy Service",
			[("TEST-ITEM-001", 2.0, None, None)],
			[
				("TEST-ITEM-001", 0.0, None, None),
				("PURE-ITEM-001", 0.0, None, None),
				("LOSS-ITEM-001", 0.0, None, None),
			],
		)
		doc.set_fire_assy_issue_weight()

		main, pure, loss = doc.exploded_product_details
		self.assertEqual(main.gross_weight, 2.0)
		self.assertEqual(pure.gross_weight, 0.0)
		self.assertEqual(loss.gross_weight, 0.0)

	def test_xrf_issue_sets_main_row_weight(self):
		"""XRF has no pure row — only main + loss."""
		doc = self._doc(
			"XRF Services",
			[("TEST-ITEM-001", 7.0, None, None)],
			[("TEST-ITEM-001", 0.0, None, None), ("LOSS-ITEM-001", 0.0, None, None)],
		)
		doc.set_fire_assy_issue_weight()

		main, loss = doc.exploded_product_details
		self.assertEqual(main.gross_weight, 7.0)
		self.assertEqual(loss.gross_weight, 0.0)

	def test_multi_tree_each_group_gets_its_own_weight(self):
		doc = self._doc(
			"Fire Assy Service",
			[
				("TEST-ITEM-001", 2.0, "SLIP-A", "TREE-A"),
				("TEST-ITEM-001", 5.0, "SLIP-B", "TREE-B"),
			],
			[
				("TEST-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
				("PURE-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
				("LOSS-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
				("TEST-ITEM-001", 0.0, "SLIP-B", "TREE-B"),
				("PURE-ITEM-001", 0.0, "SLIP-B", "TREE-B"),
				("LOSS-ITEM-001", 0.0, "SLIP-B", "TREE-B"),
			],
		)
		doc.set_fire_assy_issue_weight()

		weights = [r.gross_weight for r in doc.exploded_product_details]
		self.assertEqual(weights, [2.0, 0.0, 0.0, 5.0, 0.0, 0.0])

	def test_same_slip_rows_are_summed_onto_one_main_row(self):
		"""Two lines for one tree issue their combined weight — no double count.

		The grid no longer produces this shape (validate_duplicate_product_rows rejects a
		second row for the same tree), but the summing still has to hold for documents
		created before that guard existed.
		"""
		doc = self._doc(
			"Fire Assy Service",
			[
				("TEST-ITEM-001", 1.5, "SLIP-A", "TREE-A"),
				("TEST-ITEM-001", 0.5, "SLIP-A", "TREE-A"),
			],
			[
				("TEST-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
				("PURE-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
				("LOSS-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
			],
		)
		doc.set_fire_assy_issue_weight()

		main, pure, loss = doc.exploded_product_details
		self.assertEqual(main.gross_weight, 2.0)
		self.assertEqual(pure.gross_weight, 0.0)
		self.assertEqual(loss.gross_weight, 0.0)

	def test_overwrite_keeps_main_in_sync_with_corrected_total_weight(self):
		"""Un-guarded overwrite: idempotent on re-save, and a corrected total_weight propagates."""
		doc = self._doc(
			"Fire Assy Service",
			[("TEST-ITEM-001", 2.0, None, None)],
			[
				("TEST-ITEM-001", 0.0, None, None),
				("PURE-ITEM-001", 0.0, None, None),
				("LOSS-ITEM-001", 0.0, None, None),
			],
		)
		doc.set_fire_assy_issue_weight()
		doc.set_fire_assy_issue_weight()
		self.assertEqual(doc.exploded_product_details[0].gross_weight, 2.0)

		doc.product_details[0].total_weight = 3.0
		doc.set_fire_assy_issue_weight()
		self.assertEqual(doc.exploded_product_details[0].gross_weight, 3.0)

	def test_receive_is_left_untouched(self):
		"""The type guard: a Receive's operator-entered main/pure weights are never overwritten."""
		doc = self._doc(
			"Fire Assy Service",
			[("TEST-ITEM-001", 2.0, None, None)],
			[
				("TEST-ITEM-001", 1.85, None, None),
				("PURE-ITEM-001", 0.1, None, None),
				("LOSS-ITEM-001", 0.0, None, None),
			],
			txn_type="Receive",
		)
		doc.set_fire_assy_issue_weight()

		main, pure, loss = doc.exploded_product_details
		self.assertEqual(main.gross_weight, 1.85)
		self.assertEqual(pure.gross_weight, 0.1)
		self.assertEqual(loss.gross_weight, 0.0)

	def test_duplicate_main_row_is_not_double_weighted(self):
		"""A duplicate main row for one group must be weighted only once.

		get_exploded_table no longer emits one (its existing_data guard is updated in-loop and
		keyed through _slip_key), but rows can still arrive duplicated from an older document or
		a hand edit. The next() first-match must weight only the first row, so create_stock_entry
		— which skips gross_weight<=0 rows — never double-issues."""
		doc = self._doc(
			"Fire Assy Service",
			[("TEST-ITEM-001", 2.0, "SLIP-A", "TREE-A")],
			[
				("TEST-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
				("TEST-ITEM-001", 0.0, "SLIP-A", "TREE-A"),  # duplicate main row
				("LOSS-ITEM-001", 0.0, "SLIP-A", "TREE-A"),
			],
		)
		doc.set_fire_assy_issue_weight()

		first, duplicate, loss = doc.exploded_product_details
		self.assertEqual(first.gross_weight, 2.0)
		self.assertEqual(duplicate.gross_weight, 0.0)
		self.assertEqual(loss.gross_weight, 0.0)

	def test_validate_wires_in_the_issue_weight_step(self):
		"""Guards the validate() wiring, not just the method in isolation.

		The other tests call set_fire_assy_issue_weight() directly, so deleting its call from
		validate() — the exact shape of the original "No item found for Repack" bug — would
		leave them all green. This drives validate() end to end (get_exploded_table stubbed,
		since it needs a manufacturer / Manufacturing Setting; every other validate step
		early-returns for an Issue) and asserts the main row came out weighted.
		"""
		doc = self._doc(
			"Fire Assy Service",
			[("TEST-ITEM-001", 2.0, None, None)],
			[
				("TEST-ITEM-001", 0.0, None, None),
				("PURE-ITEM-001", 0.0, None, None),
				("LOSS-ITEM-001", 0.0, None, None),
			],
		)
		with patch.object(type(doc), "get_exploded_table", lambda self: None):
			doc.validate()

		self.assertEqual(doc.exploded_product_details[0].gross_weight, 2.0)


class TestPartialReceipt(IntegrationTestCase):
	"""Receive status rollup, over-receipt cap and the pre-filled "Create Receiving".

	Service type is left blank so submit does not reach into the stock machinery — the
	ledger under test is service-type agnostic, and the stock side is covered by
	TestHallmarkingStockEntryPcs.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def setUp(self):
		# The ledger under test is pure arithmetic over the two child tables — it does
		# not care whether the company or the item codes exist, so the documents stay
		# synthetic and this class needs no create_test_data() fixture.
		#
		# The service Purchase Order is patched out for the same reason the stock entry is:
		# these Issues carry no department, service type or supplier, so both the
		# submit-time guard (before_submit -> validate_po_configuration) and the creator
		# (on_submit -> create_po, which re-validates) would reject every one of them. PO
		# configuration is master data, not part of the receive ledger — TestProductCertification
		# covers it against the real fixtures. Both names are patched in this module's
		# namespace because product_certification.py imports them at module level.
		for target in (
			"frappe.model.document.Document._validate_links",
			"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.create_stock_entry",
			"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.validate_po_configuration",
			"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.create_po",
			"frappe.enqueue",
		):
			patcher = patch(target)
			self.addCleanup(patcher.stop)
			patcher.start()

	def _issue(self, *weights):
		doc = frappe.new_doc("Product Certification")
		doc.type = "Issue"
		doc.company = "Test_Company"
		for index, weight in enumerate(weights, start=1):
			doc.append(
				"product_details",
				{"item_code": f"PARTIAL-ITEM-{index:03d}", "total_weight": weight},
			)
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def _receive(self, issue, rows):
		"""``rows`` is ``[(issue_row_index, weight), ...]`` — a subset of the issue."""
		doc = frappe.new_doc("Product Certification")
		doc.type = "Receive"
		doc.company = "Test_Company"
		doc.receive_against = issue.name
		for index, weight in rows:
			source = issue.product_details[index]
			doc.append(
				"product_details",
				{
					"item_code": source.item_code,
					"total_weight": weight,
					"issue_row": source.name,
				},
			)
		doc.insert(ignore_permissions=True)
		return doc

	def _status(self, issue):
		return frappe.db.get_value(
			"Product Certification", issue.name, "receive_status"
		)

	def _ledger(self, issue):
		return [
			(flt(r.received_weight), flt(r.pending_weight))
			for r in frappe.get_all(
				"Product Details",
				filters={
					"parent": issue.name,
					"parenttype": "Product Certification",
				},
				fields=["received_weight", "pending_weight"],
				order_by="idx asc",
			)
		]

	def test_issue_starts_not_received(self):
		issue = self._issue(2.0, 5.0)

		self.assertEqual(self._status(issue), "Not Received")
		self.assertEqual(self._ledger(issue), [(0.0, 2.0), (0.0, 5.0)])

	def test_partial_then_full_receipt(self):
		issue = self._issue(2.0, 5.0)

		self._receive(issue, [(0, 2.0)]).submit()
		self.assertEqual(self._status(issue), "Partially Received")
		self.assertEqual(self._ledger(issue), [(2.0, 0.0), (0.0, 5.0)])

		second = self._receive(issue, [(1, 5.0)])
		second.submit()
		self.assertEqual(self._status(issue), "Fully Received")
		self.assertEqual(self._ledger(issue), [(2.0, 0.0), (5.0, 0.0)])

		# Cancelling the last receipt must put the Issue back, not leave it closed.
		second.cancel()
		self.assertEqual(self._status(issue), "Partially Received")
		self.assertEqual(self._ledger(issue), [(2.0, 0.0), (0.0, 5.0)])

	def test_weight_level_partial_on_a_single_row(self):
		issue = self._issue(30.0)

		self._receive(issue, [(0, 12.0)]).submit()
		self.assertEqual(self._status(issue), "Partially Received")
		self.assertEqual(self._ledger(issue), [(12.0, 18.0)])

		self._receive(issue, [(0, 18.0)]).submit()
		self.assertEqual(self._status(issue), "Fully Received")
		self.assertEqual(self._ledger(issue), [(30.0, 0.0)])

	def test_float_dust_still_reads_as_fully_received(self):
		"""3 × 10 against 30 lands pending on ~1e-15, not exactly 0."""
		issue = self._issue(30.0)
		for _ in range(3):
			self._receive(issue, [(0, 10.0)]).submit()

		self.assertEqual(self._status(issue), "Fully Received")

	def test_over_receipt_is_blocked(self):
		issue = self._issue(2.0)
		self._receive(issue, [(0, 1.5)]).submit()

		with self.assertRaises(frappe.ValidationError):
			self._receive(issue, [(0, 1.0)])

	def test_over_receipt_capped_on_the_sum_of_repeated_rows(self):
		issue = self._issue(2.0)

		with self.assertRaises(frappe.ValidationError):
			self._receive(issue, [(0, 1.5), (0, 1.0)])

	def test_amending_a_receipt_does_not_count_itself(self):
		issue = self._issue(2.0)
		receive = self._receive(issue, [(0, 2.0)])
		receive.submit()

		# Re-validating the same document must not see its own weight as consumed.
		receive.reload()
		receive.validate()

	def test_create_receiving_prefills_only_the_pending_rows(self):
		issue = self._issue(2.0, 5.0)
		self._receive(issue, [(0, 2.0)]).submit()

		target = create_product_certification_receive(issue.name)

		self.assertEqual(target.type, "Receive")
		self.assertEqual(target.receive_against, issue.name)
		self.assertEqual(len(target.product_details), 1)
		self.assertEqual(target.product_details[0].total_weight, 5.0)
		self.assertEqual(
			target.product_details[0].issue_row, issue.product_details[1].name
		)
		# The Issue's exploded rows must not ride along — get_exploded_table rebuilds
		# the table from the rows that actually land on this receipt.
		self.assertEqual(len(target.exploded_product_details), 0)

	def test_create_receiving_prefills_the_outstanding_weight(self):
		issue = self._issue(30.0)
		self._receive(issue, [(0, 12.0)]).submit()

		target = create_product_certification_receive(issue.name)

		self.assertEqual(target.product_details[0].total_weight, 18.0)

	def test_create_receiving_refuses_a_closed_issue(self):
		issue = self._issue(2.0)
		self._receive(issue, [(0, 2.0)]).submit()

		with self.assertRaises(frappe.ValidationError):
			create_product_certification_receive(issue.name)


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

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
				return_value=balance_rows,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.product_certification."
				"product_certification.resolve_and_validate",
				return_value="Test - WH",
			),
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
		):
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


PC_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification"


def _stub_get_all(serial_wh):
	"""Stand in for the Serial No -> warehouse lookup validate_serial_warehouse_department makes.

	Returns list-of-lists because the method calls get_all with as_list=True and
	feeds the result straight into dict().
	"""

	def _inner(doctype, filters=None, fields=None, as_list=False, **kwargs):
		wanted = set(filters["name"][1])
		if doctype == "Serial No":
			return [[sn, wh] for sn, wh in serial_wh.items() if sn in wanted]
		raise AssertionError(f"unexpected get_all for {doctype}")

	return _inner


def _check_serial_dept(
	serials, department, serial_wh, doc_type="Issue", expected_wh=None
):
	"""Run validate_serial_warehouse_department against an in-memory document.

	DB-free: only frappe.get_all and the WO-warehouse resolver are stubbed, so
	frappe.throw / _ / frappe.bold still behave normally and the assertions exercise the
	real message construction.
	"""
	fake_self = SimpleNamespace(
		type=doc_type,
		department=department,
		product_details=[
			SimpleNamespace(idx=i + 1, serial_no=sn) for i, sn in enumerate(serials)
		],
	)
	with (
		patch(f"{PC_MOD}.frappe.get_all", _stub_get_all(serial_wh)),
		patch(
			f"{PC_MOD}._department_wo_warehouse",
			lambda dept, throw=True: expected_wh or PC_WO,
		),
	):
		ProductCertification.validate_serial_warehouse_department(fake_self)


PC_DEPT = "Product Certification - T"
PC_WO = "Product Certification WO - T"
PC_TRANSIT = "Product Certification Transit - T"
OTHER_WH = "Tagging FG - T"
OTHER_DEPT = "Tagging - T"
SUPPLIER_WH = "Hallmarking Centre WIP WH - T"


class TestSerialWarehouseDepartment(IntegrationTestCase):
	"""On an Issue, a Product Details serial must sit in the document Department's WO
	warehouse -- see ProductCertification.validate_serial_warehouse_department.

	The check is against that one warehouse by name because create_stock_entry sources every
	serial line from it; anything parked elsewhere would issue out of a warehouse that does
	not hold the piece.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_serial_in_department_wo_warehouse_ok(self):
		_check_serial_dept(["SN1"], PC_DEPT, {"SN1": PC_WO})

	def test_serial_in_department_transit_warehouse_throws(self):
		# Transit belongs to the department but is not the WO warehouse: a serial still in
		# Transit has not finished arriving, and issuing it would source stock that is not
		# there. It must be moved in first.
		with self.assertRaises(ValidationError) as cm:
			_check_serial_dept(["SN1"], PC_DEPT, {"SN1": PC_TRANSIT})
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn(PC_TRANSIT, msg)
		self.assertIn(PC_WO, msg)

	def test_serial_in_another_department_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_check_serial_dept(["SN1"], PC_DEPT, {"SN1": OTHER_WH})
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("SN1", msg)
		self.assertIn(OTHER_WH, msg)
		self.assertIn(PC_WO, msg)

	def test_serial_in_supplier_warehouse_throws(self):
		# A subcontractor warehouse carries no department at all -- it must not slip through.
		with self.assertRaises(ValidationError):
			_check_serial_dept(["SN1"], PC_DEPT, {"SN1": SUPPLIER_WH})

	def test_serial_not_in_stock_throws(self):
		# ERPNext clears Serial No.warehouse on every outward movement.
		with self.assertRaises(ValidationError) as cm:
			_check_serial_dept(["SN1"], PC_DEPT, {"SN1": None})
		self.assertIn("not in stock", frappe.utils.strip_html(str(cm.exception)))

	def test_receive_is_not_validated(self):
		# A Receive legitimately carries serials still in the supplier's WIP warehouse.
		_check_serial_dept(["SN1"], PC_DEPT, {"SN1": SUPPLIER_WH}, doc_type="Receive")

	def test_blank_department_skips_check(self):
		_check_serial_dept(["SN1"], None, {"SN1": OTHER_WH})

	def test_rows_without_serial_are_ignored(self):
		# MWO/PMO-only rows carry no serial and must pass untouched.
		_check_serial_dept([None, ""], PC_DEPT, {})

	def test_offending_row_index_is_reported(self):
		with self.assertRaises(ValidationError) as cm:
			_check_serial_dept(["SN1", "SN2"], PC_DEPT, {"SN1": PC_WO, "SN2": OTHER_WH})
		self.assertIn("Row #2", frappe.utils.strip_html(str(cm.exception)))

	def test_mixed_rows_all_in_wo_warehouse_ok(self):
		_check_serial_dept(["SN1", None, "SN2"], PC_DEPT, {"SN1": PC_WO, "SN2": PC_WO})


_PC = "jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification"
_P = "jewellery_erpnext.patches.restore_over_consumed_pc_reservations"

SO = "SAL-ORD-2026-00036"
METAL = "M-G-22KT-91.9-Y"
PMO = "PMO-KGJPL-PE01656-001-0012"
OWN_MWO = "MWO-KGJPL-PE01656-001-12-91.9-Y-01"
SIBLING_MWO = "MWO-KGJPL-NE02477-001-30-91.9-Y-01"  # same SO, different PMO


def _run_certification(sre_cols=("manufacturing_work_order",)):
	"""Drive the real ``get_stock_item_against_mwo`` and return its get_all calls.

	Only the SRE-selection block matters here; the Stock Entry row building downstream is
	fed a single benign MOP balance row and its side effects land on a throwaway fake doc.
	"""
	db = MagicMock()
	db.get_table_columns.return_value = list(sre_cols)
	db.get_value.side_effect = lambda dt, *a, **kw: {
		"Manufacturing Work Order": PMO,
		"Parent Manufacturing Order": SO,
	}.get(dt)
	db.get_all.return_value = []

	balance_row = {
		"item_code": METAL,
		"qty_after_transaction_batch_based": 3.4,
		"batch_no": "BATCH-A",
	}

	se_doc = SimpleNamespace(items=[], append=lambda *a, **kw: None)
	doc = SimpleNamespace(type="Issue", name="CRT-1", company="C")
	row = SimpleNamespace(
		idx=1, manufacturing_work_order=OWN_MWO, parent_manufacturing_order=PMO
	)

	with (
		patch("frappe.db", db),
		patch("frappe.get_all", return_value=[OWN_MWO]),
		patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log."
			"get_current_mop_balance_rows",
			return_value=[balance_row],
		),
		patch("frappe.msgprint"),
		patch("frappe.clear_document_cache"),
		patch("frappe.get_doc"),
		patch("frappe.log_error"),
	):
		try:
			pc.get_stock_item_against_mwo(se_doc, doc, row, "S-WH", "T-WH")
		except Exception:
			# Downstream SE-row building is out of scope; the SRE queries have already run.
			pass

	return [
		call.kwargs.get("filters", call.args[1] if len(call.args) > 1 else None)
		for call in db.get_all.call_args_list
		if call.args and call.args[0] == "Stock Reservation Entry"
	]


class TestProductCertificationSreScope(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_sre_list_2_excludes_reservations_tagged_to_another_mwo(self):
		"""The regression itself: PMO-A's certification must not reach PMO-B's reservation."""
		filters = _run_certification()
		self.assertEqual(len(filters), 2, f"expected both SRE queries, got {filters}")

		so_scoped = [f for f in filters if f.get("voucher_no") == SO]
		self.assertEqual(len(so_scoped), 1)
		allowed = so_scoped[0].get("manufacturing_work_order")

		self.assertIsNotNone(
			allowed, "sre_list_2 must be scoped by manufacturing_work_order"
		)
		self.assertEqual(allowed, ["in", ["", None]])
		self.assertNotIn(SIBLING_MWO, allowed[1])

	def test_sre_list_1_still_covers_the_certified_pmos_own_mwos(self):
		filters = _run_certification()
		mwo_scoped = [f for f in filters if f.get("voucher_no") is None]
		self.assertEqual(len(mwo_scoped), 1)
		self.assertEqual(mwo_scoped[0]["manufacturing_work_order"], ["in", [OWN_MWO]])

	def test_filter_is_omitted_when_the_custom_column_is_absent(self):
		"""Sites without the custom column keep the old behaviour rather than crashing."""
		filters = _run_certification(sre_cols=())
		for f in filters:
			self.assertNotIn("manufacturing_work_order", f)


class TestRestorePatchSelection(IntegrationTestCase):
	"""The repair patch must only take back reservations outside the certified scope."""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_skips_sres_whose_mwo_was_legitimately_certified(self):
		from jewellery_erpnext.patches import restore_over_consumed_pc_reservations as p

		candidates = [
			MagicMock(manufacturing_work_order="MWO-CERTIFIED"),
			MagicMock(manufacturing_work_order="MWO-VICTIM"),
		]
		db = MagicMock()
		db.has_column.return_value = True
		with (
			patch("frappe.db", db),
			patch(f"{_P}._certified_mwos", return_value={"MWO-CERTIFIED"}),
			patch(f"{_P}._candidate_sres", return_value=candidates),
			patch(f"{_P}._blocked_reason", return_value=None),
			patch(f"{_P}._report") as report,
		):
			p.execute(dry_run=True)

		restored, skipped, dry_run = report.call_args[0]
		self.assertEqual([s.manufacturing_work_order for s in restored], ["MWO-VICTIM"])
		self.assertEqual(skipped, [])
		self.assertTrue(dry_run)

	def test_dry_run_never_writes(self):
		from jewellery_erpnext.patches import restore_over_consumed_pc_reservations as p

		db = MagicMock()
		db.has_column.return_value = True
		with (
			patch("frappe.db", db),
			patch(f"{_P}._certified_mwos", return_value=set()),
			patch(
				f"{_P}._candidate_sres",
				return_value=[MagicMock(manufacturing_work_order="M")],
			),
			patch(f"{_P}._blocked_reason", return_value=None),
			patch(f"{_P}._restore") as restore,
			patch(f"{_P}._refresh_bins") as refresh,
			patch(f"{_P}._report"),
		):
			p.execute(dry_run=True)

		restore.assert_not_called()
		refresh.assert_not_called()

	def test_availability_is_consumed_cumulatively_across_restores(self):
		"""95 reservations of 3.4 g each fit one-at-a-time but not in aggregate."""
		from jewellery_erpnext.patches import restore_over_consumed_pc_reservations as p

		def _sre(name, qty):
			return SimpleNamespace(
				name=name,
				item_code=METAL,
				warehouse="Waxing WO",
				reserved_qty=qty,
				reservation_based_on="Qty",
			)

		claimed_wh, claimed_batch = {}, {}
		with patch(
			"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry."
			"get_available_qty_to_reserve",
			return_value=5.0,
		):
			first = p._blocked_reason(_sre("A", 3.0), claimed_wh, claimed_batch)
			second = p._blocked_reason(_sre("B", 3.0), claimed_wh, claimed_batch)

		self.assertIsNone(first)
		self.assertIsNotNone(second, "second restore must see the first one's claim")
		self.assertIn("already claimed", second)


def _tree(**kw):
	"""A Tree Number row as frappe.db.get_value(..., as_dict=1) returns it."""
	base = {
		"name": "T-TREE-001",
		"metal_type": None,
		"metal_touch": None,
		"metal_purity": None,
		"metal_colour": None,
	}
	base.update(kw)
	return frappe._dict(base)


CURRENT_TREE = _tree(
	metal_type="Gold", metal_touch="22KT", metal_purity="91.9", metal_colour="Yellow"
)
LEGACY_TREE = _tree()  # minted by Main Slip.before_insert: company and nothing else


def _ledger(item_code="M-G-22KT-91.9-Y", issue=0.0, receive=0.0, loss=0.0):
	"""One Tree Material Detail row as frappe.get_all returns it."""
	return frappe._dict(
		item_code=item_code, issue_qty=issue, receive_qty=receive, loss_qty=loss
	)


def _resolve_tree(tree=None, slips=None, attr_item="M-G-22KT-91.9-Y", ledger=None):
	"""Drive get_item_from_tree_no with the DB stubbed out.

	``tree`` is what Tree Number resolves to (None = missing), ``slips`` what the legacy
	Main Slip fallback finds, ``attr_item`` what get_item_from_attribute returns and
	``ledger`` the tree's own material_details rows (see ``_ledger``).
	"""

	def _get_value(doctype, filters, fields=None, **kwargs):
		if doctype == "Tree Number":
			return tree
		raise AssertionError(f"unexpected get_value for {doctype}")

	def _get_all(doctype, **kwargs):
		if doctype == "Tree Material Detail":
			return ledger or []
		if doctype == "Main Slip":
			return slips or []
		raise AssertionError(f"unexpected get_all for {doctype}")

	with (
		patch(f"{PC_MOD}.frappe.db.get_value", _get_value),
		patch(f"{PC_MOD}.frappe.get_all", _get_all),
		patch(
			"jewellery_erpnext.utils.get_item_from_attribute",
			lambda *a, **k: attr_item,
		),
	):
		return ProductCertification.get_item_from_tree_no(
			SimpleNamespace(), "T-TREE-001"
		)


class TestGetItemFromTreeNo(IntegrationTestCase):
	"""Tree No scanning resolves against the Tree Number, not a submitted Main Slip.

	Trees minted since the casting rework carry their own metal attributes and have no Main
	Slip at all, so the old ``{"tree_number": ..., "docstatus": 1}`` lookup could only ever
	throw "No submitted Main Slip found".
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_current_tree_resolves_from_its_own_attributes(self):
		out = _resolve_tree(tree=CURRENT_TREE)
		self.assertEqual(out["item_code"], "M-G-22KT-91.9-Y")
		self.assertEqual(out["main_slip"], "")

	def test_current_tree_falls_back_to_its_material_ledger(self):
		# No matching Item variant for the attributes: the metal actually issued onto the
		# tree is a better answer than a blank row.
		out = _resolve_tree(
			tree=CURRENT_TREE, attr_item=None, ledger=[_ledger(issue=10.0)]
		)
		self.assertEqual(out["item_code"], "M-G-22KT-91.9-Y")

	def test_weight_comes_from_the_tree_ledger(self):
		# The reported bug: every scanned tree landed at 0. KGJPL-TR-26-00297 has
		# issue 10.0 / receive 9.9 / loss 0.1 and must resolve to the 9.9 drawn off it.
		out = _resolve_tree(
			tree=CURRENT_TREE, ledger=[_ledger(issue=10.0, receive=9.9, loss=0.1)]
		)
		self.assertEqual(out["total_weight"], 9.9)

	def test_weight_falls_back_to_issue_before_casting(self):
		# Funded but not yet cast: receive is still 0, so the metal put ON the tree is the
		# best available answer.
		out = _resolve_tree(tree=CURRENT_TREE, ledger=[_ledger(issue=6.0)])
		self.assertEqual(out["total_weight"], 6.0)

	def test_weight_is_zero_when_the_tree_has_no_ledger(self):
		# A never-funded tree. Left for the operator; validate_fire_assy_weight refuses the
		# submit while it still reads 0.
		out = _resolve_tree(tree=CURRENT_TREE, ledger=[])
		self.assertEqual(out["total_weight"], 0.0)

	def test_legacy_tree_resolves_through_a_draft_main_slip(self):
		# The old code required docstatus == 1; every other consumer of Main Slip works
		# against draft ("In Use") slips.
		slip = frappe._dict(
			name="WXK-G-22KT-91.9-Y-00199",
			metal_type="Gold",
			metal_touch="22KT",
			metal_purity="91.9",
			metal_colour="Yellow",
		)
		out = _resolve_tree(tree=LEGACY_TREE, slips=[slip])
		self.assertEqual(out["main_slip"], "WXK-G-22KT-91.9-Y-00199")
		self.assertEqual(out["item_code"], "M-G-22KT-91.9-Y")
		# Legacy trees carry no ledger at all, so there is no weight to hand back.
		self.assertEqual(out["total_weight"], 0.0)

	def test_missing_tree_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_resolve_tree(tree=None)
		self.assertIn("does not exist", frappe.utils.strip_html(str(cm.exception)))

	def test_tree_without_metal_and_without_slip_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_resolve_tree(tree=LEGACY_TREE, slips=[])
		self.assertIn(
			"no metal details", frappe.utils.strip_html(str(cm.exception)).lower()
		)

	def test_unresolvable_item_throws_instead_of_returning_blank(self):
		# get_item_from_attribute returns None rather than throwing; the row used to be
		# appended with a blank Design ID.
		with self.assertRaises(ValidationError) as cm:
			_resolve_tree(tree=CURRENT_TREE, attr_item=None, ledger=[])
		self.assertIn("No metal Item", frappe.utils.strip_html(str(cm.exception)))


def _check_duplicates(rows):
	"""Run validate_duplicate_product_rows over plain row dicts."""
	fake_self = SimpleNamespace(
		product_details=[frappe._dict(idx=i + 1, **row) for i, row in enumerate(rows)]
	)
	ProductCertification.validate_duplicate_product_rows(fake_self)


class TestDuplicateProductRows(IntegrationTestCase):
	"""A repeat scan must not append a second row -- it silently doubled the issued weight."""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_duplicate_serial_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_check_duplicates([{"serial_no": "SN1"}, {"serial_no": "SN1"}])
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("Row #2", msg)
		self.assertIn("Row #1", msg)
		self.assertIn("SN1", msg)

	def test_duplicate_mwo_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_check_duplicates(
				[
					{"manufacturing_work_order": "MWO-A"},
					{"manufacturing_work_order": "MWO-B"},
					{"manufacturing_work_order": "MWO-A"},
				]
			)
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("Row #3", msg)
		self.assertIn("MWO-A", msg)

	def test_distinct_rows_pass(self):
		_check_duplicates(
			[
				{"serial_no": "SN1"},
				{"serial_no": "SN2"},
				{"manufacturing_work_order": "MWO-A"},
			]
		)

	def test_duplicate_tree_throws(self):
		# set_fire_assy_issue_weight does sum same-tree rows onto one exploded main row, so a
		# duplicate never double-issued -- but now that a scan auto-fills the tree's weight,
		# two rows would sum to twice the metal.
		with self.assertRaises(ValidationError) as cm:
			_check_duplicates(
				[{"tree_no": "TREE-A"}, {"tree_no": "TREE-B"}, {"tree_no": "TREE-A"}]
			)
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("Row #3", msg)
		self.assertIn("Row #1", msg)
		self.assertIn("TREE-A", msg)

	def test_distinct_tree_rows_pass(self):
		_check_duplicates([{"tree_no": "TREE-A"}, {"tree_no": "TREE-B"}])

	def test_empty_rows_are_ignored(self):
		_check_duplicates([{}, {}])


def _check_fa_weight(rows, txn_type="Issue", service_type="Fire Assy Service"):
	"""Run validate_fire_assy_weight over plain row dicts."""
	fake_self = SimpleNamespace(
		type=txn_type,
		service_type=service_type,
		product_details=[frappe._dict(idx=i + 1, **row) for i, row in enumerate(rows)],
	)
	ProductCertification.validate_fire_assy_weight(fake_self)


class TestFireAssyWeightRequired(IntegrationTestCase):
	"""A Fire Assy / XRF Issue row must carry the weight actually being sent.

	At 0 the submit used to die inside create_stock_entry with "No item found for Repack",
	which names neither the row nor the tree.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_zero_weight_tree_row_throws_naming_the_tree(self):
		with self.assertRaises(ValidationError) as cm:
			_check_fa_weight([{"tree_no": "TREE-A", "total_weight": 0}])
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("Row #1", msg)
		self.assertIn("TREE-A", msg)
		self.assertIn("Total Weight", msg)

	def test_offending_row_index_is_reported(self):
		with self.assertRaises(ValidationError) as cm:
			_check_fa_weight(
				[
					{"tree_no": "TREE-A", "total_weight": 9.9},
					{"tree_no": "TREE-B", "total_weight": 0},
				]
			)
		self.assertIn("Row #2", frappe.utils.strip_html(str(cm.exception)))

	def test_non_zero_weight_passes(self):
		_check_fa_weight(
			[
				{"tree_no": "TREE-A", "total_weight": 9.9},
				{"tree_no": "TREE-B", "total_weight": 0.25},
			]
		)

	def test_xrf_is_checked_too(self):
		with self.assertRaises(ValidationError):
			_check_fa_weight(
				[{"tree_no": "TREE-A", "total_weight": 0}],
				service_type="XRF Services",
			)

	def test_receive_is_skipped(self):
		# A Receive's weights are the operator-entered recovery figures on the exploded rows.
		_check_fa_weight([{"tree_no": "TREE-A", "total_weight": 0}], txn_type="Receive")

	def test_other_service_types_are_skipped(self):
		# Hall Marking rows are serial-driven; total_weight is informational there.
		_check_fa_weight(
			[{"serial_no": "SN1", "total_weight": 0}],
			service_type="Hall Marking Service",
		)
