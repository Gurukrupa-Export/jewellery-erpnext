from unittest.mock import patch

import frappe
from erpnext.selling.doctype.quotation.quotation import make_sales_order
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days
from gke_customization.gke_order_forms.doctype.order.order import make_quotation_batch

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import (
	create_tracking_bom_directly,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.sales_order import (
	before_submit,
	validate_sales_type,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_quotation import (
	create_order,
)


class TestSalesOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		create_test_data()
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		cls.department = frappe.get_value(
			"Department",
			{"department_name": "Test_Department", "company": "Test_Company"},
			"name",
		)
		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

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
		sales_order.submit()

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

	def test_validate_sales_type_with_valid_sales_type_and_(self):
		so = frappe.new_doc("Sales Order")
		so.customer = "Test_Customer"
		so.sales_type = ""
		so.company = "Test_Company"

		with self.assertRaises(frappe.ValidationError):
			validate_sales_type(so)

		with self.assertRaises(frappe.ValidationError):
			before_submit(so, self)

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
	quotation.reload()

	apply_workflow(quotation, "Submit")
