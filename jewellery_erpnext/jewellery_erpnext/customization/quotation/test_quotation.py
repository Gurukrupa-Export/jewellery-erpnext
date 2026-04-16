from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase
from gke_customization.gke_order_forms.doctype.order.order import make_quotation_batch

from jewellery_erpnext.jewellery_erpnext.doc_events import quotation as quotation_module
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import (
	create_tracking_bom_directly,
	generate_bom,
	get_gold_rate,
	update_status,
	validate_gold_rate_with_gst,
)


class TestQuotation(FrappeTestCase):
	def setUp(self):
		self.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		customer_payment_terms()
		return super().setUp()

	def test_quotation(self):
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
		self.assertEqual(
			quotation.items[0].qty,
			frappe.get_value("Order", quotation.items[0].order_form_id, "qty"),
		)
		quotation.save()

		apply_workflow(quotation, "Create BOM")
		create_tracking_bom_directly(quotation)

		self.assertTrue(
			frappe.db.exists("Tracking Bom", {"reference_docname": quotation.name})
		)
		apply_workflow(quotation, "Submit")

	def test_update_status_toggles_between_closed_and_open(self):
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
		quotation.diamond_quality = "EF-VVS"
		quotation.items[0].diamond_quality = "EF-VVS"
		quotation.save()

		update_status(quotation.name)
		self.assertEqual(
			frappe.db.get_value("Quotation", quotation.name, "status"), "Closed"
		)

		update_status(quotation.name)
		self.assertEqual(
			frappe.db.get_value("Quotation", quotation.name, "status"), "Open"
		)

	def test_validate_gold_rate_with_gst_raises_when_missing(self):
		dummy = SimpleNamespace(items=[], gold_rate_with_gst=None)
		with self.assertRaises(frappe.ValidationError):
			validate_gold_rate_with_gst(dummy)

	def test_validate_gold_rate_with_gst_raises_when_qty_exceeds_order(self):
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
		order_qty = frappe.db.get_value("Order", order, "qty")

		item = SimpleNamespace(order_form_id=order, qty=order_qty + 1, idx=1)
		dummy = SimpleNamespace(items=[item], gold_rate_with_gst=15000)
		with self.assertRaises(frappe.ValidationError):
			validate_gold_rate_with_gst(dummy)

	def test_get_gold_rate_returns_value_and_warns_when_missing(self):
		def gv_side_effect(doctype, filters, fieldname=None, order_by=None):
			if doctype == "Customer":
				return "India"
			if doctype == "Gold Price List":
				return 5000
			return None

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.db.get_value",
			side_effect=gv_side_effect,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.msgprint"
		) as mp:
			rate = get_gold_rate(party_name="Any Customer", currency="INR")
			self.assertEqual(rate, 5000)
			mp.assert_not_called()

		def gv_side_effect_missing(doctype, filters, fieldname=None, order_by=None):
			if doctype == "Customer":
				return "India"
			if doctype == "Gold Price List":
				return None
			return None

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.db.get_value",
			side_effect=gv_side_effect_missing,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.msgprint"
		) as mp:
			rate = get_gold_rate(party_name="Any Customer", currency="INR")
			self.assertIsNone(rate)
			mp.assert_called()

	def test_generate_bom_enqueues_job(self):
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
		quotation.items[0].diamond_quality = "EF-VVS"
		quotation.save()

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.enqueue"
		) as enqueue_mock:
			generate_bom(quotation.name)
			enqueue_mock.assert_called()
			enq_args, enq_kwargs = enqueue_mock.call_args
			self.assertIs(enq_args[0], quotation_module.create_bom_sceintifically)
			self.assertIn("self", enq_kwargs)
			self.assertEqual(enq_kwargs.get("queue"), "long")
			self.assertEqual(enq_kwargs.get("timeout"), 10000)

	def test_validate_rate_enforces_tolerance(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.throw"
		) as thr:
			parent = SimpleNamespace(company="GK")
			doc_ok = {"rate": 100, "actual_rate": 105}
			quotation_module.validate_rate(parent, 10, doc_ok, "Metal")

			doc_bad = {
				"rate": 90,
				"actual_rate": 100,
			}
			quotation_module.validate_rate(parent, 5, doc_bad, "Metal")
			thr.assert_called()

	def test_get_gold_rate_returns_none_when_no_party(self):
		self.assertIsNone(get_gold_rate(None, "INR"))

	def tearDown(self):
		return super().tearDown()


def customer_payment_terms():
	if not frappe.db.exists("Customer Payment Terms", "Test_Customer_External"):
		customer_payment_term = frappe.new_doc("Customer Payment Terms")
		customer_payment_term.customer = "Test_Customer_External"
		row = [
			{"item_type": "18KT Gold Jewellery Making Charges", "payment_term": 2},
			{"item_type": "18KT Gold Chain Making Charges", "payment_term": 2},
			{"item_type": "Studded 18KT Gold Chain Jewellery", "payment_term": 15},
			{"item_type": "Studded 18KT Gold Jewellery", "payment_term": 15},
			{"item_type": "Studded Natural Diamond Jewellery", "payment_term": 30},
			{"item_type": "Studded Gemstone Jewellery", "payment_term": 45},
			{"item_type": "Studded 22KT Gold Chain Jewellery", "payment_term": 15},
			{"item_type": "Studded 22KT Gold Jewellery", "payment_term": 15},
			{"item_type": "22KT Gold Jewellery Making Charges", "payment_term": 2},
			{"item_type": "22KT Gold Chain Making Charges", "payment_term": 2},
		]
		customer_payment_term.append("customer_payment_details", row)
		customer_payment_term.save()
