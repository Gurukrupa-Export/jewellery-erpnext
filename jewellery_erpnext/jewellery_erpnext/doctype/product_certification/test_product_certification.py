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
	update_bom_details,
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
	"""Department to certify a freshly minted serial under, with the serial moved into it.

	``ProductCertification.validate_serial_warehouse_department`` requires an Issue's serial
	to sit in that department's WO (Manufacturing) warehouse, because ``create_stock_entry``
	sources every serial line from exactly that warehouse.

	``create_snc`` leaves the finished serial in the department's **FG** warehouse (Tagging FG),
	which is one transfer short of issuable. Real operation moves it on with a Department IR;
	these tests model just the stock movement, which is all the rule cares about. Certifying
	under the serial's own department -- rather than Product Certification -- keeps sidestepping
	the cross-department routing the Department IR would also do.

	The department rule itself is covered by TestSerialWarehouseDepartment.
	"""
	department = frappe.db.get_value("Warehouse", serial_no.warehouse, "department")
	_move_serial_to_department_wo_warehouse(serial_no, department)
	return department


def _move_serial_to_department_wo_warehouse(serial_no, department):
	"""Material Transfer the serial into ``department``'s WO warehouse, if it is not there."""
	target = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"is_group": 0,
			"department": department,
			"warehouse_type": "Manufacturing",
		},
		"name",
		order_by="name asc",
	)
	source = serial_no.warehouse
	if not target or source == target:
		return

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer"
	se.company = frappe.db.get_value("Warehouse", target, "company")
	se.append(
		"items",
		{
			"item_code": serial_no.item_code,
			"qty": 1,
			"s_warehouse": source,
			"t_warehouse": target,
			"serial_no": serial_no.name,
			"use_serial_batch_fields": True,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()
	serial_no.reload()


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


def _stub_issued_rows(issue_name, rows):
	"""Make ``validate_items`` see ``rows`` as the Issue's Product Details.

	It reads the whole table in one ``frappe.get_all`` and matches in Python, so a Receive
	built against a document that does not exist has to be handed its rows here. Only that
	exact call is intercepted -- it is the one filtering on ``parent`` alone; the receive
	ledger's own read of the same table also passes ``parenttype`` -- and everything else
	goes to the real implementation.
	"""
	orig_get_all = frappe.get_all

	def _inner(doctype, *args, **kwargs):
		filters = kwargs.get("filters")
		if (
			doctype == "Product Details"
			and isinstance(filters, dict)
			and filters == {"parent": issue_name}
		):
			return [frappe._dict(row) for row in rows]
		# Passed straight through: frappe.get_all's first positional is `fields`, not
		# `filters`, so re-assembling the call would silently move the argument.
		return orig_get_all(doctype, *args, **kwargs)

	return patch.object(frappe, "get_all", side_effect=_inner)


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
		# No certification number is set: the `certification` field is gone from Exploded
		# Product Details, and with it validate_items' Diamond Certificate gate. This is the
		# suite's only Diamond Certificate Receive, so this submit IS the regression test
		# that the gate no longer blocks one.
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

		# Validation for serial-no linkage
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

		# This Receive's exploded row carries a PMO and no serial_no, so update_huid takes
		# its PMO branch and appends to the HUID Detail table. That append used to read a
		# `certification` field that no longer exists on Exploded Product Details; assert on
		# the row so the branch is covered rather than merely executed.
		pmo_huids = frappe.db.get_all(
			"HUID Detail",
			filters={"parent": pmo.name, "parenttype": "Parent Manufacturing Order"},
			fields=["huid", "date"],
		)
		self.assertIn("HM-9999", [row.huid for row in pmo_huids])
		self.assertTrue(
			[row.date for row in pmo_huids if row.huid == "HM-9999"][0],
			"update_huid must still stamp the date alongside the HUID",
		)

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

		with (
			patch.object(frappe.db, "get_value", side_effect=side_effect),
			_stub_issued_rows(
				"PC-TEST-001",
				[{"item_code": "TEST-ITEM-001", "tree_no": "TREE-001"}],
			),
		):
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

			# The assay report belongs to the Touch row and is demanded at submit by
			# validate_fire_assy_report.
			main_row.report_no = "FA-RPT-001"
			main_row.report_result = 91.85

			with patch(PURITY_PATH, side_effect=_purity):
				certification.save()

				# 30 × 99.9 / 91.9 = 32.612 at 24KT, so 100 − 60 − 32.612 = 7.388 is lost,
				# and the three rows still sum back to the 100 issued.
				self.assertEqual(pure_row.conversion_quantity, 32.612)
				self.assertEqual(loss_row.gross_weight, 7.388)

				# set_assay_row_types labels the trio in append order.
				self.assertEqual(
					[
						main_row.assay_row_type,
						pure_row.assay_row_type,
						loss_row.assay_row_type,
					],
					["Touch", "Pure", "Loss"],
				)

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

		with (
			patch.object(frappe.db, "get_value", side_effect=side_effect),
			_stub_issued_rows(
				"PC-TEST-001",
				[{"item_code": "TEST-ITEM-001", "tree_no": "TREE-001"}],
			),
		):
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

	def test_duplicate_tree_is_allowed(self):
		# One tree goes for assay in several samples, so the same Tree No may be scanned more
		# than once. set_fire_assy_issue_weight sums those rows onto one exploded main row,
		# which is the intended behaviour; the scan handler protects the weight by leaving a
		# repeat row at 0 rather than re-filling the tree's full weight.
		_check_duplicates(
			[{"tree_no": "TREE-A"}, {"tree_no": "TREE-B"}, {"tree_no": "TREE-A"}]
		)

	def test_duplicate_serial_still_throws_alongside_trees(self):
		# Relaxing trees must not relax the serial guard: one serial is one physical piece.
		with self.assertRaises(ValidationError) as cm:
			_check_duplicates(
				[{"tree_no": "TREE-A"}, {"serial_no": "SN1"}, {"serial_no": "SN1"}]
			)
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("SN1", msg)

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


class TestAssayRowTypes(IntegrationTestCase):
	"""set_assay_row_types is what lets the grid tell a Touch row from a Pure or Loss row.

	depends_on on a child field only ever sees the row and the parent, so the classification
	has to be stamped onto the row; everything here is pure in-memory bookkeeping over the two
	child tables, so an unsaved document is enough.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, service_type, pd_rows, exploded_items, txn_type="Receive"):
		doc = frappe.new_doc("Product Certification")
		doc.type = txn_type
		doc.service_type = service_type
		for row in pd_rows:
			doc.append("product_details", row)
		for item_code, key in exploded_items:
			doc.append(
				"exploded_product_details",
				{
					"item_code": item_code,
					"tree_no": key.get("tree_no"),
					"sample_name": key.get("sample_name"),
				},
			)
		return doc

	def test_fire_assy_labels_touch_pure_loss(self):
		key = {"tree_no": "TREE-A"}
		doc = self._doc(
			"Fire Assy Service",
			[dict(item_code="M22", pure_item="M24", loss_item="ML22", **key)],
			[("M22", key), ("M24", key), ("ML22", key)],
		)
		doc.set_assay_row_types()
		self.assertEqual(
			[r.assay_row_type for r in doc.exploded_product_details],
			["Touch", "Pure", "Loss"],
		)

	def test_xrf_has_no_pure_slot(self):
		key = {"tree_no": "TREE-A"}
		doc = self._doc(
			"XRF Services",
			[dict(item_code="M22", pure_item="M24", loss_item="ML22", **key)],
			[("M22", key), ("ML22", key)],
		)
		doc.set_assay_row_types()
		self.assertEqual(
			[r.assay_row_type for r in doc.exploded_product_details], ["Touch", "Loss"]
		)

	def test_xrf_does_not_label_a_pure_row(self):
		"""get_exploded_table never emits one, so a stray pure row is unclassified, not Pure."""
		key = {"tree_no": "TREE-A"}
		doc = self._doc(
			"XRF Services",
			[dict(item_code="M22", pure_item="M24", loss_item="ML22", **key)],
			[("M22", key), ("M24", key), ("ML22", key)],
		)
		doc.set_assay_row_types()
		self.assertIsNone(doc.exploded_product_details[1].assay_row_type)

	def test_each_sample_group_is_labelled_independently(self):
		a = {"tree_no": "TREE-A", "sample_name": "S1"}
		b = {"tree_no": "TREE-A", "sample_name": "S2"}
		doc = self._doc(
			"Fire Assy Service",
			[
				dict(item_code="M22", pure_item="M24", loss_item="ML22", **a),
				dict(item_code="M22", pure_item="M24", loss_item="ML22", **b),
			],
			[
				("M22", a),
				("M24", a),
				("ML22", a),
				("M22", b),
				("M24", b),
				("ML22", b),
			],
		)
		doc.set_assay_row_types()
		self.assertEqual(
			[r.assay_row_type for r in doc.exploded_product_details],
			["Touch", "Pure", "Loss"] * 2,
		)

	def test_slot_is_consumed_when_main_item_is_also_the_pure_item(self):
		"""A tree of 24KT: the first row is the Touch row, the second the Pure row."""
		key = {"tree_no": "TREE-A"}
		doc = self._doc(
			"Fire Assy Service",
			[dict(item_code="M24", pure_item="M24", loss_item="ML24", **key)],
			[("M24", key), ("M24", key), ("ML24", key)],
		)
		doc.set_assay_row_types()
		self.assertEqual(
			[r.assay_row_type for r in doc.exploded_product_details],
			["Touch", "Pure", "Loss"],
		)

	def test_row_matching_no_slot_stays_blank(self):
		key = {"tree_no": "TREE-A"}
		doc = self._doc(
			"Fire Assy Service",
			[dict(item_code="M22", pure_item="M24", loss_item="ML22", **key)],
			[("M22", key), ("SOMETHING-ELSE", key)],
		)
		doc.set_assay_row_types()
		self.assertEqual(doc.exploded_product_details[0].assay_row_type, "Touch")
		self.assertIsNone(doc.exploded_product_details[1].assay_row_type)

	def test_other_services_are_untouched(self):
		key = {}
		doc = self._doc(
			"Hall Marking Service",
			[dict(item_code="M22", **key)],
			[("M22", key)],
		)
		doc.set_assay_row_types()
		self.assertIsNone(doc.exploded_product_details[0].assay_row_type)


def _check_report(rows, txn_type="Receive", service_type="Fire Assy Service"):
	"""Run validate_fire_assy_report over plain exploded-row dicts."""
	fake_self = SimpleNamespace(
		type=txn_type,
		service_type=service_type,
		exploded_product_details=[
			frappe._dict(idx=i + 1, **row) for i, row in enumerate(rows)
		],
	)
	ProductCertification.validate_fire_assy_report(fake_self)


_FULL_TOUCH = {
	"assay_row_type": "Touch",
	"item_code": "M22",
	"tree_no": "TREE-A",
	"report_no": "RPT-1",
	"report_result": 91.85,
}


class TestFireAssyReport(IntegrationTestCase):
	"""Report No / Report Result are demanded on the Touch row alone.

	The JSON mandatory_depends_on is client-side only, and on a Float it never blocks a save
	at all (is_null(0) is false), so this is the gate that actually holds.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_complete_touch_row_passes(self):
		_check_report([dict(_FULL_TOUCH)])

	def test_missing_report_no_throws_naming_the_tree(self):
		# The row/tree assertions moved here from the deleted
		# test_missing_certification_throws_naming_the_tree: an operator needs to know WHICH
		# sample is short, and report_no is now the only Data field this gate guards.
		with self.assertRaises(ValidationError) as cm:
			_check_report([dict(_FULL_TOUCH, report_no=None)])
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("Report No", msg)
		self.assertIn("Row #1", msg)
		self.assertIn("TREE-A", msg)

	def test_zero_report_result_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_check_report([dict(_FULL_TOUCH, report_result=0)])
		self.assertIn("Report Result", frappe.utils.strip_html(str(cm.exception)))

	def test_sample_name_names_the_row_when_there_is_no_tree(self):
		row = dict(_FULL_TOUCH, tree_no=None, sample_name="S1", report_no=None)
		with self.assertRaises(ValidationError) as cm:
			_check_report([row])
		self.assertIn("S1", frappe.utils.strip_html(str(cm.exception)))

	def test_pure_and_loss_rows_are_never_demanded(self):
		_check_report(
			[
				dict(_FULL_TOUCH),
				{"assay_row_type": "Pure", "item_code": "M24"},
				{"assay_row_type": "Loss", "item_code": "ML22"},
			]
		)

	def test_blank_marker_is_skipped(self):
		"""A legacy row is unclassified, and "no opinion" must not become a new demand."""
		_check_report([{"item_code": "M22", "tree_no": "TREE-A"}])

	def test_no_op_on_xrf_and_on_an_issue(self):
		bare = [{"assay_row_type": "Touch", "item_code": "M22"}]
		_check_report(bare, service_type="XRF Services")
		_check_report(bare, txn_type="Issue")


def _check_tree_or_sample(rows, txn_type="Issue", service_type="Fire Assy Service"):
	fake_self = SimpleNamespace(
		type=txn_type,
		service_type=service_type,
		product_details=[frappe._dict(idx=i + 1, **row) for i, row in enumerate(rows)],
	)
	ProductCertification.validate_tree_or_sample_name(fake_self)


class TestTreeOrSampleName(IntegrationTestCase):
	"""A Fire Assy Issue row must say which sample it is.

	A row carrying neither identity lands in the ("", "", "") bucket, where
	validate_exploded_qty compares it against a grand total instead of its own trio.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_tree_alone_passes(self):
		_check_tree_or_sample([{"tree_no": "TREE-A"}])

	def test_sample_alone_passes(self):
		_check_tree_or_sample([{"sample_name": "S1"}])

	def test_both_pass(self):
		_check_tree_or_sample([{"tree_no": "TREE-A", "sample_name": "S1"}])

	def test_neither_throws_listing_every_offending_row(self):
		with self.assertRaises(ValidationError) as cm:
			_check_tree_or_sample(
				[{"tree_no": "TREE-A"}, {}, {"sample_name": "S1"}, {}]
			)
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn(
			"Enter either Tree Number or Sample Name before submitting the "
			"Fire Assy Certification.",
			msg,
		)
		self.assertIn("2, 4", msg)

	def test_receive_is_not_checked(self):
		"""Legacy Issues with neither identity are already submitted and cannot be edited;
		enforcing here would strand their metal at the supplier."""
		_check_tree_or_sample([{}], txn_type="Receive")

	def test_xrf_is_not_checked(self):
		_check_tree_or_sample([{}], service_type="XRF Services")


def _check_sample_names(rows):
	"""Run the in-document half of validate_unique_sample_name.

	The cross-document half needs saved parents; the collision it guards is the one an
	operator hits first, so it is pinned here on its own.
	"""
	fake_self = SimpleNamespace(
		name="new-pc-1",
		receive_against=None,
		amended_from=None,
		product_details=[frappe._dict(idx=i + 1, **row) for i, row in enumerate(rows)],
	)
	with patch.object(frappe, "get_all", return_value=[]):
		ProductCertification.validate_unique_sample_name(fake_self)


class TestUniqueSampleName(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_distinct_names_pass(self):
		_check_sample_names([{"sample_name": "S1"}, {"sample_name": "S2"}])

	def test_blank_names_are_ignored(self):
		_check_sample_names([{}, {}, {"sample_name": None}])

	def test_duplicate_within_the_document_throws(self):
		with self.assertRaises(ValidationError) as cm:
			_check_sample_names(
				[{"sample_name": "S1"}, {"sample_name": "S2"}, {"sample_name": "S1"}]
			)
		msg = frappe.utils.strip_html(str(cm.exception))
		self.assertIn("Row #3", msg)
		self.assertIn("Row #1", msg)
		self.assertIn("S1", msg)

	def test_duplicate_is_case_insensitive(self):
		"""MariaDB's collation is case-insensitive, so the cross-document lookup is too --
		the in-document rule must not be the looser of the pair."""
		with self.assertRaises(ValidationError):
			_check_sample_names([{"sample_name": "S1"}, {"sample_name": "s1"}])


class TestNormaliseSampleNames(IntegrationTestCase):
	"""A blank Sample Name is stored as NULL, never as "".

	stored_identity reads the column raw while match_identity normalises a blank to None, so
	a cleared cell arriving as "" would stop a Receive row resolving to its Issue row.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _run(self, pd_rows, exploded_rows=()):
		fake_self = SimpleNamespace(
			product_details=[frappe._dict(row) for row in pd_rows],
			exploded_product_details=[frappe._dict(row) for row in exploded_rows],
		)
		ProductCertification._normalise_sample_names(fake_self)
		return fake_self

	def test_blank_becomes_none(self):
		doc = self._run([{"sample_name": ""}, {"sample_name": "   "}, {}])
		self.assertEqual(
			[r.sample_name for r in doc.product_details], [None, None, None]
		)

	def test_surrounding_whitespace_is_stripped(self):
		doc = self._run([{"sample_name": " S1 "}])
		self.assertEqual(doc.product_details[0].sample_name, "S1")

	def test_exploded_rows_are_normalised_too(self):
		doc = self._run([], [{"sample_name": " S1 "}, {"sample_name": ""}])
		self.assertEqual(
			[r.sample_name for r in doc.exploded_product_details], ["S1", None]
		)


class TestSlipKey(IntegrationTestCase):
	"""_slip_key gained a third element; blank must leave every existing document alone."""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_blank_sample_name_appends_an_empty_third_element(self):
		self.assertEqual(
			pc._slip_key(frappe._dict(main_slip="MS-1", tree_no="TREE-A")),
			("MS-1", "TREE-A", ""),
		)

	def test_none_and_empty_string_land_in_one_group(self):
		self.assertEqual(
			pc._slip_key(frappe._dict(tree_no="TREE-A", sample_name=None)),
			pc._slip_key(frappe._dict(main_slip="", tree_no="TREE-A", sample_name="")),
		)

	def test_samples_off_one_tree_are_separate_groups(self):
		self.assertNotEqual(
			pc._slip_key(frappe._dict(tree_no="TREE-A", sample_name="S1")),
			pc._slip_key(frappe._dict(tree_no="TREE-A", sample_name="S2")),
		)


class _FakeStockEntry:
	"""Just enough Stock Entry to capture the rows create_material_receipt_for_certification
	appends, without going near the ledger."""

	def __init__(self):
		self.items = []
		self.flags = frappe._dict()
		self.submitted = False

	def append(self, _table, row):
		self.items.append(frappe._dict(row))
		return self.items[-1]

	def save(self, *args, **kwargs):
		pass

	def submit(self):
		self.submitted = True


class TestFireAssyRepackQty(IntegrationTestCase):
	"""The Repack must consume the main metal at the purity-converted weight and produce the
	pure item at its OWN weight.

	conversion_quantity is written only on the pure row and holds the weight converted to the
	MAIN item's purity. Feeding that single number to both legs booked the 24KT pure item at
	the converted weight, over-receiving fine gold by pure_purity / main_purity on every
	receipt. validate_exploded_qty asserts the document balance in main-purity grams, so it
	balanced and the error never surfaced at submit.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, exploded, product_details=None):
		return frappe._dict(
			name="PC-RECEIVE-1",
			type="Receive",
			service_type="Fire Assy Service",
			company="Test_Company",
			department="Dept",
			supplier="Supp",
			receive_against="PC-ISSUE-1",
			product_details=[
				frappe._dict(idx=i + 1, **row)
				for i, row in enumerate(
					product_details
					if product_details is not None
					else [
						{
							"item_code": "M22",
							"tree_no": "TREE-A",
							"total_weight": 100.0,
							"pure_item": "M24",
							"loss_item": "ML22",
						}
					]
				)
			],
			exploded_product_details=[
				frappe._dict(idx=i + 1, **row) for i, row in enumerate(exploded)
			],
		)

	def _run(self, doc):
		"""Drive the builder with every warehouse / ledger lookup stubbed out."""
		from jewellery_erpnext.jewellery_erpnext import lock_order
		from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events import (
			utils as pc_utils,
		)

		created = []

		def _new_doc(doctype, *args, **kwargs):
			self.assertEqual(doctype, "Stock Entry")
			se = _FakeStockEntry()
			created.append(se)
			return se

		with (
			patch.object(
				pc_utils, "_get_department_rm_warehouse", return_value="RM-WH"
			),
			patch.object(
				pc_utils, "_get_department_scrap_warehouse", return_value="SCRAP-WH"
			),
			patch.object(
				pc_utils, "_get_supplier_certification_warehouse", return_value="SUP-WH"
			),
			patch.object(
				pc_utils, "_get_issue_stock_entry_details", return_value=({}, {})
			),
			patch.object(frappe.db, "get_value", return_value="SE-ISSUE-1"),
			patch.object(frappe, "get_cached_value", return_value=(0, 0, 0)),
			patch.object(frappe, "new_doc", side_effect=_new_doc),
			patch.object(lock_order, "lock_bins"),
			patch.object(lock_order, "preallocate_series_for_docs"),
			patch.object(lock_order, "series_stubs", return_value=()),
		):
			pc_utils.create_material_receipt_for_certification(doc)

		by_type = {se.stock_entry_type: se for se in created}
		return by_type

	def test_pure_item_is_produced_at_its_own_weight(self):
		# Issue 100 g of 22KT, receive 60, recover 30 of 24KT. 30 x 99.9 / 91.9 = 32.612 at
		# 22KT, so 100 - 60 - 32.612 = 7.388 is lost.
		doc = self._doc(
			[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0},
				{
					"item_code": "M24",
					"tree_no": "TREE-A",
					"gross_weight": 30.0,
					"conversion_quantity": 32.612,
				},
				{"item_code": "ML22", "tree_no": "TREE-A", "gross_weight": 7.388},
			]
		)
		entries = self._run(doc)

		receipt = entries["Material Receipt for Certification"]
		self.assertEqual([(r.item_code, r.qty) for r in receipt.items], [("M22", 60.0)])

		repack = entries["Repack"]
		consumed = [
			(r.item_code, r.qty) for r in repack.items if not r.get("is_finished_item")
		]
		produced = [
			(r.item_code, r.qty) for r in repack.items if r.get("is_finished_item")
		]

		# The consume leg is the purity-converted weight -- that much 22KT really is used up.
		self.assertEqual(consumed, [("M22", 32.612), ("M22", 7.388)])
		# The produce leg is each item's own weight. 32.612 here was the bug.
		self.assertEqual(produced, [("M24", 30.0), ("ML22", 7.388)])

		# Total 22KT consumed is exactly what did not come back as 22KT.
		self.assertAlmostEqual(sum(q for _i, q in consumed), 40.0, places=3)

	def test_gross_weight_follows_each_leg(self):
		doc = self._doc(
			[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0},
				{
					"item_code": "M24",
					"tree_no": "TREE-A",
					"gross_weight": 30.0,
					"conversion_quantity": 32.612,
				},
			]
		)
		repack = self._run(doc)["Repack"]
		for row in repack.items:
			self.assertEqual(row.gross_weight, row.qty)

	def test_rows_without_a_pure_conversion_are_unchanged(self):
		"""XRF has no pure row, so both quantities collapse to gross_weight."""
		doc = self._doc(
			[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0},
				{"item_code": "ML22", "tree_no": "TREE-A", "gross_weight": 40.0},
			]
		)
		doc.service_type = "XRF Services"
		entries = self._run(doc)
		self.assertEqual(
			[(r.item_code, r.qty) for r in entries["Repack"].items],
			[("M22", 40.0), ("ML22", 40.0)],
		)

	def test_unresolvable_main_item_throws_instead_of_minting_stock(self):
		"""Mixing a tree row with a no-tree row leaves the ("", "", "") group with no main
		item, and the produce row used to be appended without its consume row."""
		doc = self._doc(
			[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 10.0},
				{"item_code": "ML22", "gross_weight": 5.0},
			],
			product_details=[
				{
					"item_code": "M22",
					"tree_no": "TREE-A",
					"total_weight": 10.0,
					"pure_item": "M24",
					"loss_item": "ML22",
				},
				{
					"item_code": "M18",
					"total_weight": 5.0,
					"pure_item": "M24",
					"loss_item": "ML22",
				},
			],
		)
		with self.assertRaises(ValidationError) as cm:
			self._run(doc)
		self.assertIn("no main item", frappe.utils.strip_html(str(cm.exception)))


_BOM_WEIGHTS = frappe._dict(
	metal_colour="Yellow",
	gross_weight=10.0,
	metal_and_finding_weight=8.0,
	finding_weight_=1.0,
	other_weight=0.5,
	gemstone_weight=0.25,
	diamond_weight=0.25,
	total_diamond_pcs=3,
	total_gemstone_pcs=5,
)


class TestExplodedRowPerProductRow(IntegrationTestCase):
	"""One exploded row per Product Details row, earrings included.

	Earrings used to fan out into two rows carrying half the weight each, which also pushed
	an odd diamond count through cint(x) / 2 into an Int column.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, category, exploded=()):
		doc = frappe.new_doc("Product Certification")
		doc.service_type = "Hall Marking Service"
		doc.type = "Issue"
		doc.append(
			"product_details",
			{
				"item_code": "ITEM-1",
				"serial_no": "SN-1",
				"bom": "BOM-1",
				"category": category,
			},
		)
		for row in exploded:
			doc.append("exploded_product_details", row)
		return doc

	def _explode(self, doc):
		sources = frappe._dict(
			bom={"BOM-1": _BOM_WEIGHTS},
			bom_metal={},
			mwo={},
			mop={},
			latest_mop={},
			pmo={},
			pmo_departments={},
		)
		with patch.object(
			ProductCertification, "_exploded_source_data", return_value=sources
		):
			doc.get_exploded_table()
		return doc.exploded_product_details

	def test_earring_yields_one_row_at_full_weight(self):
		rows = self._explode(self._doc("Earrings"))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].gross_weight, 10.0)
		self.assertEqual(rows[0].gold_weight, 8.0)

	def test_earring_pcs_are_not_halved_into_an_int_column(self):
		rows = self._explode(self._doc("Earrings"))
		self.assertEqual(rows[0].diamond_pcs, 3)
		self.assertEqual(rows[0].stone_pcs, 5)

	def test_non_earring_is_unchanged(self):
		rows = self._explode(self._doc("Ring"))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].gross_weight, 10.0)

	def test_a_legacy_pair_is_left_alone(self):
		"""Submitted documents keep their two rows and stay consistent with the Stock Entry /
		PO / BOM amounts already booked; drafts keep theirs until someone deletes one."""
		legacy = [
			{"item_code": "ITEM-1", "serial_no": "SN-1", "gross_weight": 5.0},
			{"item_code": "ITEM-1", "serial_no": "SN-1", "gross_weight": 5.0},
		]
		doc = self._doc("Earrings", exploded=legacy)
		rows = self._explode(doc)
		self.assertEqual(len(rows), 2)
		self.assertEqual([r.gross_weight for r in rows], [5.0, 5.0])

	def test_re_explode_is_a_no_op(self):
		doc = self._doc("Earrings")
		self._explode(doc)
		rows = self._explode(doc)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].gross_weight, 10.0)


class TestFireAssyExplodedGroups(IntegrationTestCase):
	"""One receive / pure / loss trio per (main_slip, tree_no, sample_name) group.

	A tree legitimately goes for assay as several samples. Before sample_name joined the
	grouping key they shared one trio and their weights were summed onto a single main row.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, pd_rows, exploded=(), service_type="Fire Assy Service"):
		doc = frappe.new_doc("Product Certification")
		doc.type = "Receive"
		doc.service_type = service_type
		doc.company = "Test_Company"
		doc.manufacturer = "Test_Manufacturer"
		for row in pd_rows:
			doc.append("product_details", row)
		for row in exploded:
			doc.append("exploded_product_details", row)
		return doc

	def _explode(self, doc):
		with (
			patch.object(pc, "get_item_loss_item", return_value="ML22"),
			patch.object(frappe.db, "get_value", return_value="M24"),
		):
			doc.get_exploded_table()
		return doc.exploded_product_details

	def test_trio_carries_the_sample_name(self):
		doc = self._doc(
			[{"item_code": "M22", "tree_no": "TREE-A", "sample_name": "S1"}]
		)
		rows = self._explode(doc)
		self.assertEqual([r.item_code for r in rows], ["M22", "M24", "ML22"])
		self.assertEqual([r.sample_name for r in rows], ["S1"] * 3)

	def test_two_samples_off_one_tree_get_their_own_trio(self):
		doc = self._doc(
			[
				{"item_code": "M22", "tree_no": "TREE-A", "sample_name": "S1"},
				{"item_code": "M22", "tree_no": "TREE-A", "sample_name": "S2"},
			]
		)
		rows = self._explode(doc)
		self.assertEqual(len(rows), 6)
		self.assertEqual([r.sample_name for r in rows], ["S1"] * 3 + ["S2"] * 3)

	def test_same_tree_without_sample_names_still_shares_one_trio(self):
		"""The documented pre-existing behaviour, and every document written so far."""
		doc = self._doc(
			[
				{"item_code": "M22", "tree_no": "TREE-A"},
				{"item_code": "M22", "tree_no": "TREE-A"},
			]
		)
		self.assertEqual(len(self._explode(doc)), 3)

	def test_xrf_still_emits_main_and_loss_only(self):
		doc = self._doc(
			[{"item_code": "M22", "tree_no": "TREE-A"}], service_type="XRF Services"
		)
		self.assertEqual([r.item_code for r in self._explode(doc)], ["M22", "ML22"])

	def test_untouched_orphan_group_is_pruned_and_rebuilt(self):
		"""Naming a sample moves the group's key; the old, empty trio is dropped."""
		doc = self._doc(
			[{"item_code": "M22", "tree_no": "TREE-A", "sample_name": "S1"}],
			exploded=[
				{"item_code": "M22", "tree_no": "TREE-A"},
				{"item_code": "M24", "tree_no": "TREE-A"},
				{"item_code": "ML22", "tree_no": "TREE-A"},
			],
		)
		rows = self._explode(doc)
		self.assertEqual(len(rows), 3)
		self.assertEqual([r.sample_name for r in rows], ["S1"] * 3)

	def test_orphan_carrying_weight_throws_rather_than_deleting_it(self):
		"""Silently dropping entered weights would reset the grid to zeros and leave
		validate_exploded_qty rejecting the submit with no explanation."""
		doc = self._doc(
			[{"item_code": "M22", "tree_no": "TREE-A", "sample_name": "S1"}],
			exploded=[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0},
				{"item_code": "M24", "tree_no": "TREE-A"},
				{"item_code": "ML22", "tree_no": "TREE-A"},
			],
		)
		with self.assertRaises(ValidationError) as cm:
			self._explode(doc)
		self.assertIn(
			"no longer in Product Details", frappe.utils.strip_html(str(cm.exception))
		)

	def test_a_live_group_keeps_its_entered_weights(self):
		doc = self._doc(
			[{"item_code": "M22", "tree_no": "TREE-A", "sample_name": "S1"}],
			exploded=[
				{
					"item_code": "M22",
					"tree_no": "TREE-A",
					"sample_name": "S1",
					"gross_weight": 60.0,
				},
				{"item_code": "M24", "tree_no": "TREE-A", "sample_name": "S1"},
				{"item_code": "ML22", "tree_no": "TREE-A", "sample_name": "S1"},
			],
		)
		rows = self._explode(doc)
		self.assertEqual(len(rows), 3)
		self.assertEqual(rows[0].gross_weight, 60.0)

	def test_legacy_document_with_no_sample_names_is_untouched(self):
		doc = self._doc(
			[{"item_code": "M22", "tree_no": "TREE-A"}],
			exploded=[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0},
				{"item_code": "M24", "tree_no": "TREE-A", "gross_weight": 30.0},
				{"item_code": "ML22", "tree_no": "TREE-A", "gross_weight": 7.388},
			],
		)
		rows = self._explode(doc)
		self.assertEqual([r.gross_weight for r in rows], [60.0, 30.0, 7.388])


class TestPerSampleLossWeight(IntegrationTestCase):
	"""Each sample's loss is computed against its own issued weight, not the tree's total."""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def test_each_sample_gets_its_own_conversion_and_loss(self):
		doc = frappe.new_doc("Product Certification")
		doc.type = "Receive"
		doc.service_type = "Fire Assy Service"
		for sample, issued in (("S1", 100.0), ("S2", 50.0)):
			doc.append(
				"product_details",
				{
					"item_code": "TEST-ITEM-001",
					"tree_no": "TREE-A",
					"sample_name": sample,
					"total_weight": issued,
					"pure_item": "PURE-ITEM-001",
					"loss_item": "LOSS-ITEM-001",
				},
			)
		for sample, main_wt, pure_wt in (("S1", 60.0, 30.0), ("S2", 30.0, 15.0)):
			for item_code, weight in (
				("TEST-ITEM-001", main_wt),
				("PURE-ITEM-001", pure_wt),
				("LOSS-ITEM-001", 0.0),
			):
				doc.append(
					"exploded_product_details",
					{
						"item_code": item_code,
						"tree_no": "TREE-A",
						"sample_name": sample,
						"gross_weight": weight,
					},
				)

		with patch(PURITY_PATH, side_effect=_purity):
			doc.calculate_fire_assy_loss_weight()

		rows = doc.exploded_product_details
		# S1: 30 x 99.9 / 91.9 = 32.612 at 22KT, so 100 - 60 - 32.612 = 7.388 is lost.
		self.assertEqual(rows[1].conversion_quantity, 32.612)
		self.assertEqual(rows[2].gross_weight, 7.388)
		# S2: half of everything, and independent of S1.
		self.assertEqual(rows[4].conversion_quantity, 16.306)
		self.assertEqual(rows[5].gross_weight, 3.694)


class TestEarringAmountSplit(IntegrationTestCase):
	"""Total Amount is split per PIECE, not per row.

	Hall Marking and Fire Assy are billed per piece and an earring pair is two pieces sitting
	on ONE exploded row, so that row takes two shares: 150 across an Earrings row and one
	other row is 100 / 50, not 75 / 75.

	The row is never split in two. TestExplodedRowPerProductRow owns the row count and it
	stays one exploded row per Product Details row -- only the share changes.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _doc(self, service_type, total_amount, categories, doc_type="Receive"):
		doc = frappe.new_doc("Product Certification")
		doc.type = doc_type
		doc.service_type = service_type
		doc.total_amount = total_amount
		for index, category in enumerate(categories, start=1):
			doc.append(
				"exploded_product_details",
				{
					"item_code": "TEST-ITEM-001",
					"serial_no": f"TEST-SERIAL-{index:03d}",
					"category": category,
					"gross_weight": 1.0,
				},
			)
		return doc

	def _amounts(self, doc):
		doc.distribute_amount()
		return [flt(row.amount, 2) for row in doc.exploded_product_details]

	def test_hall_marking_earring_takes_two_of_three_shares(self):
		doc = self._doc("Hall Marking Service", 150, ["Earrings", "Ring"])
		self.assertEqual(self._amounts(doc), [100.0, 50.0])

	def test_fire_assy_weights_the_helper_the_same_way(self):
		"""Helper-level only, and inert in production: get_exploded_table appends Fire Assy
		rows with no category at all (they carry the METAL item, never a finished Earrings
		item), so this shape does not occur. It pins that Fire Assy shares the Hall Marking
		rule rather than any billing behaviour -- see
		test_fire_assy_rows_have_no_category_so_the_split_stays_flat for the real shape."""
		doc = self._doc("Fire Assy Service", 150, ["Earrings", "Ring"])
		self.assertEqual(self._amounts(doc), [100.0, 50.0])

	def test_fire_assy_rows_have_no_category_so_the_split_stays_flat(self):
		"""The metal / pure / loss rows get_exploded_table really appends carry no category,
		so every unit is 1 and the split is flat -- the earring weighting cannot reach them."""
		doc = self._doc("Fire Assy Service", 150, [None, None, None])
		self.assertEqual(self._amounts(doc), [50.0, 50.0, 50.0])

	def test_shares_sum_to_the_entered_total(self):
		"""update_bom_details sums these straight onto the BOM, so a total that does not
		divide evenly must still reconcile to what the operator entered."""
		# 9 units into 100 divides to 11.111..., so the rounded shares cannot sum to 100
		# on their own -- a flat `total / units` per row leaves the rows at 99.99.
		doc = self._doc("Hall Marking Service", 100, ["Earrings"] + ["Ring"] * 7)
		doc.distribute_amount()
		amounts = [row.amount for row in doc.exploded_product_details]

		precision = doc.exploded_product_details[0].precision("amount")
		self.assertEqual(flt(sum(amounts), precision), 100.0)
		# The residual lands on the largest share -- the earring row -- so it is the one
		# row that is not exactly twice a single-unit share.
		self.assertEqual(amounts[1:], [amounts[1]] * 7)
		self.assertGreater(amounts[0], amounts[1])
		self.assertAlmostEqual(amounts[0], 2 * amounts[1], places=1)

	def test_the_earring_row_is_not_split_in_two(self):
		doc = self._doc("Hall Marking Service", 150, ["Earrings", "Ring"])
		doc.distribute_amount()
		self.assertEqual(len(doc.exploded_product_details), 2)

	def test_two_earrings_share_equally(self):
		"""Four units between them, so each pair still carries half the total."""
		doc = self._doc("Hall Marking Service", 200, ["Earrings", "Earrings"])
		self.assertEqual(self._amounts(doc), [100.0, 100.0])

	def test_no_earring_is_the_flat_split_it_replaces(self):
		doc = self._doc("Hall Marking Service", 150, ["Ring", "Bangle"])
		self.assertEqual(self._amounts(doc), [75.0, 75.0])

	def test_a_blank_category_is_one_unit(self):
		"""A Fire Assy pure / loss row is appended with no category of its own."""
		doc = self._doc("Fire Assy Service", 150, ["Earrings", None])
		self.assertEqual(self._amounts(doc), [100.0, 50.0])

	def test_diamond_certificate_keeps_the_flat_split(self):
		"""certification_amount is already priced off diamond_weight, which carries both
		stones of a pair -- weighting the row on top would count the pair twice."""
		doc = self._doc("Diamond Certificate service", 150, ["Earrings", "Ring"])
		self.assertEqual(self._amounts(doc), [75.0, 75.0])

	def test_xrf_keeps_the_flat_split(self):
		doc = self._doc("XRF Services", 150, ["Earrings", "Ring"])
		self.assertEqual(self._amounts(doc), [75.0, 75.0])

	def test_an_issue_carries_no_amount(self):
		doc = self._doc(
			"Hall Marking Service", 150, ["Earrings", "Ring"], doc_type="Issue"
		)
		self.assertEqual(self._amounts(doc), [0.0, 0.0])
		self.assertEqual(doc.total_amount, 0)


class TestEarringAmountSplitThroughExplode(IntegrationTestCase):
	"""The split, driven through the path that really populates `category`.

	TestEarringAmountSplit hand-sets `category` on an exploded row. In production nothing
	does that: on the Hall Marking branch `get_exploded_table` copies it down from the
	Product Details row, and the row's own `fetch_from: item_code.item_category` is applied
	by `_validate_links()` -- which frappe runs BEFORE `validate()`, where the exploded rows
	are built, so a row created on that save has no fetched value until the next one.

	So `get_exploded_table` copying it down is the only thing making the weighting work on a
	first save. This class drives that, and would catch a regression in it that the
	hand-set tests cannot see.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _explode_and_split(self, categories, total_amount=150):
		doc = frappe.new_doc("Product Certification")
		doc.service_type = "Hall Marking Service"
		doc.type = "Receive"
		doc.total_amount = total_amount
		for index, category in enumerate(categories, start=1):
			doc.append(
				"product_details",
				{
					"item_code": "ITEM-1",
					"serial_no": f"SN-{index}",
					"bom": "BOM-1",
					"category": category,
				},
			)
		sources = frappe._dict(
			bom={"BOM-1": _BOM_WEIGHTS},
			bom_metal={},
			mwo={},
			mop={},
			latest_mop={},
			pmo={},
			pmo_departments={},
		)
		with patch.object(
			ProductCertification, "_exploded_source_data", return_value=sources
		):
			doc.get_exploded_table()
		doc.distribute_amount()
		return doc.exploded_product_details

	def test_category_reaches_the_exploded_row_from_product_details(self):
		rows = self._explode_and_split(["Earrings", "Ring"])
		self.assertEqual([row.category for row in rows], ["Earrings", "Ring"])

	def test_the_earring_row_takes_two_of_three_shares(self):
		rows = self._explode_and_split(["Earrings", "Ring"])
		self.assertEqual([flt(row.amount, 2) for row in rows], [100.0, 50.0])

	def test_no_earring_is_the_flat_split(self):
		rows = self._explode_and_split(["Ring", "Bangle"])
		self.assertEqual([flt(row.amount, 2) for row in rows], [75.0, 75.0])


class TestBomHallmarkingAmountUnit(IntegrationTestCase):
	"""What `update_bom_details` actually writes onto BOM.hallmarking_amount.

	`TestEarringAmountSplit` stops at `distribute_amount()`, so nothing observed the value
	that leaves the document. This pins it, because the whole point of the per-piece split
	is the number that lands on the BOM.

	The unit it carries is a WHOLE-BOM line total: an Earrings BOM is the pair, so its
	hallmarking_amount covers both pieces. That is what every site pricing a BOM expects
	(`purchase_order.update_rate` and the sales_invoice line rates sum it alongside
	`making_charge` and `gold_bom_amount`), and it is why the e-invoice hallmarking line --
	which reports amount against a PIECE count -- has to count an Earrings BOM as two.
	"""

	@classmethod
	def setUpClass(cls):
		_skip_generated_test_records()
		super().setUpClass()

	def _written(self, rows, service_type="Hall Marking Service"):
		"""Run update_bom_details and capture the BOM writes instead of hitting the DB."""
		doc = frappe.new_doc("Product Certification")
		doc.service_type = service_type
		doc.type = "Receive"
		doc.total_amount = 150
		for index, (bom, category) in enumerate(rows, start=1):
			doc.append(
				"exploded_product_details",
				{
					"item_code": "TEST-ITEM-001",
					"serial_no": f"TEST-SERIAL-{index:03d}",
					"bom": bom,
					"category": category,
					"gross_weight": 1.0,
				},
			)
		doc.distribute_amount()

		written = {}

		def _set_value(doctype, name, field, value):
			written[(doctype, name, field)] = value

		with patch.object(frappe.db, "set_value", _set_value):
			update_bom_details(doc)
		return written

	def test_an_earring_bom_carries_the_pair_total(self):
		written = self._written([("BOM-EAR", "Earrings"), ("BOM-RING", "Ring")])
		self.assertEqual(written[("BOM", "BOM-EAR", "hallmarking_amount")], 100.0)
		self.assertEqual(written[("BOM", "BOM-RING", "hallmarking_amount")], 50.0)

	def test_the_writes_sum_to_the_entered_total(self):
		written = self._written([("BOM-EAR", "Earrings"), ("BOM-RING", "Ring")])
		self.assertEqual(flt(sum(written.values()), 2), 150.0)

	def test_rows_sharing_a_bom_are_summed_onto_it(self):
		written = self._written([("BOM-EAR", "Earrings"), ("BOM-EAR", "Ring")])
		self.assertEqual(written[("BOM", "BOM-EAR", "hallmarking_amount")], 150.0)

	def test_diamond_certificate_writes_the_other_field_and_stays_flat(self):
		written = self._written(
			[("BOM-EAR", "Earrings"), ("BOM-RING", "Ring")],
			service_type="Diamond Certificate service",
		)
		self.assertEqual(written[("BOM", "BOM-EAR", "certification_amount")], 75.0)
		self.assertEqual(written[("BOM", "BOM-RING", "certification_amount")], 75.0)

	def test_an_issue_writes_nothing(self):
		doc = frappe.new_doc("Product Certification")
		doc.service_type = "Hall Marking Service"
		doc.type = "Issue"
		doc.append(
			"exploded_product_details",
			{"item_code": "TEST-ITEM-001", "bom": "BOM-EAR", "category": "Earrings"},
		)
		with patch.object(frappe.db, "set_value") as set_value:
			update_bom_details(doc)
		set_value.assert_not_called()
