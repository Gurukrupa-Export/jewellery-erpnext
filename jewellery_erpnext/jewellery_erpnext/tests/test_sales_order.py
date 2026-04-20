from unittest.mock import patch

import frappe
from erpnext.selling.doctype.quotation.quotation import make_sales_order
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days
from gke_customization.gke_order_forms.doctype.order.order import make_quotation_batch

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import (
	create_tracking_bom_directly,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_quotation import (
	create_order,
	customer_payment_terms,
)


class TestSalesOrder(FrappeTestCase):
	def setUp(self):
		create_test_data()
		self.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		self.department = frappe.get_value(
			"Department",
			{"department_name": "Test_Department", "company": "Test_Company"},
			"name",
		)
		self.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)
		customer_payment_terms()

		order_criteria = frappe.get_single("Order Criteria")
		if not order_criteria.order:
			order_criteria.append(
				"order",
				{
					"sketch_submission_time": "06:00:00",
					"cad_appoval_timefrom_ibm_team": "06:00:00",
					"cad_approval_day": 6,
					"cad_submission_time": "10:45:00",
				},
			)

		if not order_criteria.department_shift:
			order_criteria.append(
				"department_shift",
				{
					"branch": frappe.get_value(
						"Branch", {"branch_name": "Test Branch"}
					),
					"department": frappe.get_value(
						"Department", {"department_name": "Test_Department"}
					),
				},
			)
		order_criteria.save()

		return super().setUp()

	def test_sales_order(self):
		create_quotation(self)
		quotation = frappe.get_value(
			"Quotation", {"workflow_state": "Submitted"}, "name"
		)
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
		for row in sales_order.items:
			self.assertEqual(
				frappe.get_value(
					"Tracking Bom", row.custom_tracking_bom, "reference_doctype"
				),
				"Sales Order",
			)
			self.assertEqual(
				sales_order.name,
				frappe.get_value(
					"Tracking Bom", row.custom_tracking_bom, "reference_docname"
				),
			)

	def test_validate_sales_type_mandatory(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import (
			sales_order as so_events,
		)

		class Dummy:
			pass

		d = Dummy()
		d.items = []
		d.sales_type = None
		with self.assertRaises(frappe.ValidationError):
			so_events.validate_sales_type(d)

	def test_validate_serial_number_reuse_throws(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import (
			sales_order as so_events,
		)

		so = type("SO", (), {})()
		so.items = [type("I", (), {"serial_no": "SRL-TEST-001"})()]
		so.skip_serial_validation = False

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.frappe.db.sql"
		) as sql:
			sql.return_value = [frappe._dict(parent="SO-0001")]
			with self.assertRaises(frappe.ValidationError):
				so_events.validate_serial_number(so)

	def test_validate_snc_sets_reserved_on_save_and_active_on_cancel(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import (
			sales_order as so_events,
		)

		so = type("SO", (), {})()
		so.items = [
			type("I", (), {"serial_no": "SRL-A", "bom": "BOM-A"})(),
			type("I", (), {"serial_no": "SRL-B", "bom": "BOM-B"})(),
		]

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.frappe.db.set_value"
		) as setv:
			so.docstatus = 0
			so_events.validate_snc(so)
			setv.assert_any_call("Serial No", "SRL-A", "status", "Reserved")
			setv.assert_any_call("Serial No", "SRL-B", "status", "Reserved")

			so.docstatus = 2
			so_events.validate_snc(so)
			setv.assert_any_call("Serial No", "SRL-A", "status", "Active")
			setv.assert_any_call("Serial No", "SRL-B", "status", "Active")

	def test_validate_quotation_item_copies_from_quotation_when_empty(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import (
			sales_order as so_events,
		)

		class Parent:
			def __init__(self):
				self.custom_invoice_item = []
				self.items = [type("I", (), {"prevdoc_docname": "QTN-1"})()]

			def append(self, table, row):
				self.custom_invoice_item.append(row)

		p = Parent()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.frappe.get_all"
		) as ga:
			ga.return_value = [
				frappe._dict(
					item_code="X", item_name="X", uom="Nos", qty=2, rate=5, amount=10
				)
			]
			so_events.validate_quotation_item(p)
			self.assertEqual(len(p.custom_invoice_item), 1)
			self.assertEqual(p.custom_invoice_item[0]["item_code"], "X")

	def test_validate_items_aggregates_components_into_custom_invoice_items(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import (
			sales_order as so_events,
		)

		class FakeBOM:
			def __init__(self):
				self.metal_detail = [
					frappe._dict(
						metal_type="Gold",
						metal_touch="18K",
						stock_uom="Gram",
						quantity=1,
						rate=100,
						se_rate=100,
						making_rate=10,
					)
				]
				self.diamond_detail = [
					frappe._dict(
						diamond_type="Natural",
						stock_uom="Carat",
						quantity=0.5,
						total_diamond_rate=200,
						se_rate=200,
					)
				]
				self.finding_detail = [
					frappe._dict(
						metal_type="Gold",
						metal_touch="18K",
						stock_uom="Gram",
						quantity=0.2,
						rate=50,
						se_rate=50,
						making_rate=5,
					)
				]
				self.gemstone_detail = [
					frappe._dict(
						stock_uom="Carat",
						quantity=0.3,
						total_gemstone_rate=300,
						se_rate=300,
					)
				]

		class Parent:
			def __init__(self):
				self.company = "Any Co"
				self.customer = "Any Customer"
				self.sales_type = "Finished Goods"
				self.delivery_date = frappe.utils.nowdate()
				self.items = [type("I", (), {"bom": "BOM-1", "qty": 2})()]
				self.custom_invoice_item = []

			def set(self, field, val):
				if field == "custom_invoice_item":
					self.custom_invoice_item = val

			def append(self, field, row):
				self.custom_invoice_item.append(row)

		p = Parent()

		class CPTDoc:
			def __init__(self):
				self.customer_payment_details = [
					type("R", (), {"item_type": "Studded Natural Diamond Jewellery"})()
				]

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.frappe.get_doc"
		) as get_doc, patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.frappe.db.get_value"
		) as gv:

			def get_doc_side_effect(doctype, name=None, filters=None):
				if doctype == "Customer Payment Terms":
					return CPTDoc()
				if doctype == "BOM":
					return FakeBOM()
				if doctype == "E Invoice Item":
					return type(
						"EItem",
						(),
						{
							"is_for_metal": 1,
							"is_for_diamond": 1,
							"is_for_making": 1,
							"is_for_finding": 1,
							"is_for_finding_making": 1,
							"is_for_gemstone": 1,
							"diamond_type": "Natural",
							"metal_type": "Gold",
							"metal_purity": "18K",
							"uom": "Gram",
							"sales_type": [
								type(
									"S",
									(),
									{"sales_type": "Finished Goods", "tax_rate": 10},
								)()
							],
						},
					)
				return None

			def gv_side_effect(doctype, name, fieldname=None):
				if doctype == "Customer":
					return None
				return None

			get_doc.side_effect = get_doc_side_effect
			gv.side_effect = gv_side_effect

			so_events.validate_items(p)
			self.assertTrue(len(p.custom_invoice_item) > 0)

	def tearDown(self):
		return super().tearDown()


def create_quotation(self):
	create_order(self)
	order = frappe.db.get_value(
		"Order",
		{
			"customer_code": "Test_Customer_External",
			"item": ["is", "set"],
			"workflow_state": "Approved",
			"docstatus": 1,
		},
		"name",
		order_by="creation desc",
	)
	quotation = make_quotation_batch([order])
	quotation.branch = self.branch
	quotation.custom_sales_type = "Finished Goods"
	quotation.gold_rate_with_gst = 15000
	quotation.custom_customer_gold = "No"
	quotation.custom_customer_diamond = "No"
	quotation.custom_customer_stone = "No"
	quotation.custom_customer_good = "No"
	quotation.custom_customer_finding = "No"
	quotation.diamond_quality = "EF-VVS"
	quotation.items[0].diamond_quality = "EF-VVS"
	quotation.selling_price_list = "Standard Selling"
	quotation.price_list_currency = "INR"
	quotation.plc_conversion_rate = 1
	quotation.save()

	apply_workflow(quotation, "Create BOM")
	create_tracking_bom_directly(quotation)

	apply_workflow(quotation, "Submit")
