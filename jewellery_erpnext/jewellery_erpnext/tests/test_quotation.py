from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase
from gke_customization.gke_order_forms.doctype.order.order import make_quotation_batch
from gke_customization.gke_order_forms.doctype.order_form.test_order_form import (
	make_order_form,
)

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.jewellery_erpnext.doc_events import quotation as quotation_module
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import (
	create_tracking_bom_directly,
	generate_bom,
	get_gold_rate,
	update_status,
	validate_gold_rate_with_gst,
)


class TestQuotation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		create_test_data()
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

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
		quotation.selling_price_list = "Standard Selling"
		quotation.price_list_currency = "INR"
		quotation.plc_conversion_rate = 1
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
		quotation.selling_price_list = "Standard Selling"
		quotation.price_list_currency = "INR"
		quotation.plc_conversion_rate = 1
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

	# def test_get_gold_rate_returns_value_and_warns_when_missing(self):
	# 	def gv_side_effect(doctype, filters, fieldname=None, order_by=None):
	# 		if doctype == "Customer":
	# 			return "India"
	# 		if doctype == "Gold Price List":
	# 			return 5000
	# 		return None

	# 	with patch(
	# 		"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.db.get_value",
	# 		side_effect=gv_side_effect,
	# 	), patch(
	# 		"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.msgprint"
	# 	) as mp:
	# 		rate = get_gold_rate(party_name="Any Customer", currency="INR")
	# 		self.assertEqual(rate, 5000)
	# 		mp.assert_not_called()

	# 	def gv_side_effect_missing(doctype, filters, fieldname=None, order_by=None):
	# 		if doctype == "Customer":
	# 			return "India"
	# 		if doctype == "Gold Price List":
	# 			return None
	# 		return None

	# 	with patch(
	# 		"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.db.get_value",
	# 		side_effect=gv_side_effect_missing,
	# 	), patch(
	# 		"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.msgprint"
	# 	) as mp:
	# 		rate = get_gold_rate(party_name="Any Customer", currency="INR")
	# 		self.assertIsNone(rate)
	# 		mp.assert_called()

	def test_generate_bom_enqueues_job(self):
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
		quotation.items[0].diamond_quality = "EF-VVS"
		quotation.selling_price_list = "Standard Selling"
		quotation.price_list_currency = "INR"
		quotation.plc_conversion_rate = 1
		quotation.save()

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.quotation.frappe.enqueue"
		) as enqueue_mock:
			generate_bom(quotation.name)
			enqueue_mock.assert_called()
			enq_args, enq_kwargs = enqueue_mock.call_args
			self.assertIs(enq_args[0], quotation_module.create_bom_scientifically)
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


def create_order(self):
	order_form = make_order_form(
		department="Order Management - T",
		branch=self.branch,
		order_type="Sales",
		design_by="Our Design",
		design_type="New Design",
	)
	order = frappe.get_doc(
		"Order",
		frappe.get_value("Order", {"cad_order_form": order_form.name, "docstatus": 0}),
	)

	order.append(
		"designer_assignment",
		{
			"designer": frappe.db.exists(
				"Employee", {"employee_name": "Test Designer Employee"}
			)
		},
	)
	order.save()
	apply_workflow(order, "Assigned")

	timesheets = frappe.get_all(
		"Timesheet", filters={"order": order.name, "docstatus": 0}
	)

	for ts in timesheets:
		timesheet = frappe.get_doc("Timesheet", ts.name)
		apply_workflow(timesheet, "Start Designing")
		apply_workflow(timesheet, "Send to QC")
		apply_workflow(timesheet, "Update Design")
		apply_workflow(timesheet, "Start Designing")
		apply_workflow(timesheet, "Send to QC")
		if timesheet.custom_required_customer_approval:
			apply_workflow(timesheet, "Send For Approval")
		apply_workflow(timesheet, "Approve")

	order.reload()
	order.capganthan = "None"
	order.rhodium_ = "None"
	order.save()
	apply_workflow(order, "Update")
	order.reload()

	order.append(
		"bom_assignment",
		{
			"designer": frappe.db.exists(
				"Employee", {"employee_name": "Test Designer Employee"}
			)
		},
	)
	order.save()

	apply_workflow(order, "Create")

	frappe.db.set_value("Item", order.item, "master_bom", order.new_bom)
	order.reload()
	order.save()

	apply_workflow(order, "Send to QC")

	# order.cad_file = "https://www.chidambaramcovering.in/image/cache/catalog/Mogappu%20Chain/mchn510-gold-plated-jewellery-mugappu-design-without-stone-5-425x500.jpg.webp"
	# order.cad_image = "https://www.chidambaramcovering.in/image/cache/catalog/Mogappu%20Chain/mchn510-gold-plated-jewellery-mugappu-design-without-stone-5-425x500.jpg.webp"

	bom = frappe.get_doc("BOM", order.new_bom)
	bom.append(
		"metal_detail",
		{
			"metal_type": "Gold",
			"metal_touch": "22KT",
			"metal_purity": "91.6",
			"metal_colour": "Yellow",
			"quantity": 1.3,
			"stock_uom": "Gram",
		},
	)
	bom.append(
		"diamond_detail",
		{
			"diamond_type": "Natural",
			"stone_shape": "Round",
			"diamond_sieve_size": "+9-9.5",
			"diamond_grade": "7",
			"pcs": 1,
			"quantity": 1,
		},
	)
	bom.save()

	item = frappe.get_doc("Item", order.item)
	item.append(
		"item_defaults",
		{"company": "Test_Company", "default_warehouse": "Product Allocation FG - T"},
	)
	item.has_serial_no = 1
	item.flags.ignore_validate = True
	item.save()

	apply_workflow(order, "Approve")
