# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe import ValidationError
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification import (
	get_stock_item_against_mwo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.test_serial_number_creator import (
	create_snc,
)


class TestProductCertification(FrappeTestCase):
	def setUp(self):
		create_test_data()
		self.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		super().setUp()

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

	def tearDown(self):
		return super().tearDown()


class TestHallmarkingStockEntryPcs(FrappeTestCase):
	"""Unit coverage for pcs propagation in get_stock_item_against_mwo.

	Kept separate from TestProductCertification so it does not depend on the
	heavy create_test_data() fixture: the MWO/MOP/warehouse machinery is mocked
	and only the pcs-assignment logic is exercised.
	"""

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
