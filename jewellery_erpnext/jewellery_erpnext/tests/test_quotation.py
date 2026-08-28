from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase
from gke_customization.gke_order_forms.doctype.order.order import make_quotation_batch
from gke_customization.gke_order_forms.doctype.order_form.test_order_form import (
	make_order_form,
)

from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events import (
	remote_po,
)
from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.remote_po import (
	fetch_remote_ref_customer,
)
from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.utils import (
	validate_po,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import quotation as quotation_module
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import (
	create_tracking_bom_directly,
	generate_bom,
	get_gold_rate,
	update_status,
	validate_gold_rate_with_gst,
)

QUOTATION_UTILS = (
	"jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.utils"
)
REMOTE_PO = (
	"jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.remote_po"
)


class TestQuotation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
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
		row = frappe._dict()
		row.metal_type = "Gold"
		row.order_form_id = None
		dummy = SimpleNamespace(items=[row], gold_rate_with_gst=None)
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

	def _quotation_with_po_rows(self, ref_customer=None, po_nos=("PUR-ORD-TEST-1",)):
		"""Minimal stand-in for a Quotation whose item rows point at a Purchase Order."""
		items = [
			SimpleNamespace(
				idx=idx,
				po_no=po_no,
				qty=1,
				custom_hallmarking_amount=0,
				custom_customer_gold=None,
				custom_customer_diamond=None,
				custom_customer_stone=None,
				custom_customer_good=None,
				custom_customer_finding=None,
			)
			for idx, po_no in enumerate(po_nos, start=1)
		]
		return SimpleNamespace(
			name="QTN-TEST-1",
			company="Test Company",
			party_name="Test Customer",
			ref_customer=ref_customer,
			items=items,
			custom_customer_gold="No",
			custom_customer_diamond="No",
			custom_customer_stone="No",
			custom_customer_good="No",
			custom_customer_finding="No",
		)

	def _patched_po_lookup(self, po_value):
		def get_value(doctype, name=None, fieldname=None, **kwargs):
			return po_value if doctype == "Purchase Order" else None

		return patch(f"{QUOTATION_UTILS}.frappe.db.get_value", side_effect=get_value)

	def _patched_customer_exists(self, exists=True):
		return patch(f"{QUOTATION_UTILS}.frappe.db.exists", return_value=exists)

	def test_validate_po_sets_ref_customer_from_purchase_order(self):
		doc = self._quotation_with_po_rows(
			ref_customer=None, po_nos=("PUR-ORD-TEST-1", "PUR-ORD-TEST-1")
		)
		po_value = frappe._dict(custom_quotation=None, ref_customer="CUST-A")

		with (
			self._patched_po_lookup(po_value),
			self._patched_customer_exists(),
			patch(f"{QUOTATION_UTILS}.frappe.db.set_value") as set_value,
		):
			validate_po(doc)

		self.assertEqual(doc.ref_customer, "CUST-A")
		# both rows share one PO, so it is linked back exactly once
		set_value.assert_called_once_with(
			"Purchase Order", "PUR-ORD-TEST-1", "custom_quotation", "QTN-TEST-1"
		)

	def test_validate_po_does_not_overwrite_existing_ref_customer(self):
		doc = self._quotation_with_po_rows(ref_customer="CUST-EXISTING")
		po_value = frappe._dict(custom_quotation="QTN-OTHER", ref_customer="CUST-A")

		with (
			self._patched_po_lookup(po_value),
			patch(f"{QUOTATION_UTILS}.frappe.db.set_value") as set_value,
		):
			validate_po(doc)

		self.assertEqual(doc.ref_customer, "CUST-EXISTING")
		set_value.assert_not_called()

	def test_validate_po_ignores_po_no_that_is_not_a_purchase_order(self):
		# order-sourced quotations carry a free-text customer PO in po_no
		doc = self._quotation_with_po_rows(
			ref_customer=None, po_nos=("JO_11228198_STXO12409",)
		)

		with (
			self._patched_po_lookup(None),
			patch(f"{QUOTATION_UTILS}.frappe.db.set_value"),
			patch(f"{QUOTATION_UTILS}.fetch_remote_ref_customer") as fetch_remote,
		):
			validate_po(doc)

		self.assertIsNone(doc.ref_customer)
		# free text resolves to no Purchase Order anywhere, so it must not reach across sites
		fetch_remote.assert_not_called()

	def test_validate_po_asks_the_owning_site_when_the_mirror_has_no_ref_customer(self):
		# the Purchase Order row is here, but the mirror landed without the field
		doc = self._quotation_with_po_rows(
			ref_customer=None, po_nos=("PUR-ORD-TEST-1", "PUR-ORD-TEST-1")
		)
		po_value = frappe._dict(custom_quotation=None, ref_customer=None)

		with (
			self._patched_po_lookup(po_value),
			self._patched_customer_exists(),
			patch(f"{QUOTATION_UTILS}.frappe.db.set_value"),
			patch(
				f"{QUOTATION_UTILS}.fetch_remote_ref_customer",
				return_value="CUST-REMOTE",
			) as fetch_remote,
		):
			validate_po(doc)

		self.assertEqual(doc.ref_customer, "CUST-REMOTE")
		# both rows share one PO, so the owning site is asked exactly once
		fetch_remote.assert_called_once_with("PUR-ORD-TEST-1")

	def test_validate_po_prefers_the_local_ref_customer_over_the_owning_site(self):
		doc = self._quotation_with_po_rows(ref_customer=None)
		po_value = frappe._dict(custom_quotation=None, ref_customer="CUST-A")

		with (
			self._patched_po_lookup(po_value),
			self._patched_customer_exists(),
			patch(f"{QUOTATION_UTILS}.frappe.db.set_value"),
			patch(f"{QUOTATION_UTILS}.fetch_remote_ref_customer") as fetch_remote,
		):
			validate_po(doc)

		self.assertEqual(doc.ref_customer, "CUST-A")
		fetch_remote.assert_not_called()

	def test_validate_po_ignores_a_remote_customer_that_is_not_on_this_site(self):
		# ref_customer is a Link: assigning an unresolvable Customer would turn a blank field
		# into a hard throw on save, so the value is dropped instead
		doc = self._quotation_with_po_rows(ref_customer=None)
		po_value = frappe._dict(custom_quotation=None, ref_customer=None)

		with (
			self._patched_po_lookup(po_value),
			self._patched_customer_exists(False),
			patch(f"{QUOTATION_UTILS}.frappe.db.set_value"),
			patch(
				f"{QUOTATION_UTILS}.fetch_remote_ref_customer",
				return_value="CUST-GHOST",
			),
		):
			validate_po(doc)

		self.assertIsNone(doc.ref_customer)

	def tearDown(self):
		return super().tearDown()


class TestRemotePoRefCustomer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		setattr(frappe.local, remote_po.CACHE_KEY, {})

	def _patched_settings(self, site_field="from_site_1", site="https://gk.example.com"):
		values = {"api_key": "key", "api_secret": "secret"}
		if site_field:
			values[site_field] = site
		return patch(
			f"{REMOTE_PO}.frappe.db.get_single_value",
			side_effect=lambda doctype, field: values.get(field),
		)

	def _patched_cache(self, breaker_open=False):
		"""Keep the breaker out of real Redis so cases cannot leak into each other."""
		cache = MagicMock()
		cache.get_value.return_value = 1 if breaker_open else None
		return patch(f"{REMOTE_PO}.frappe.cache", return_value=cache), cache

	def test_returns_none_and_stays_offline_when_no_from_site_is_set(self):
		# the owning site itself points neither field anywhere -- that is the off switch
		patched_cache, _ = self._patched_cache()

		with (
			patched_cache,
			self._patched_settings(site_field=None),
			patch(f"{REMOTE_PO}.requests") as requests_mod,
		):
			self.assertIsNone(fetch_remote_ref_customer("PUR-ORD-TEST-1"))

		requests_mod.post.assert_not_called()

	def test_returns_the_ref_customer_from_the_owning_site(self):
		response = SimpleNamespace(
			raise_for_status=lambda: None, json=lambda: {"message": "CUST-REMOTE"}
		)
		patched_cache, _ = self._patched_cache()

		with (
			patched_cache,
			self._patched_settings(),
			patch(f"{REMOTE_PO}.requests.post", return_value=response) as post,
		):
			self.assertEqual(fetch_remote_ref_customer("PUR-ORD-TEST-1"), "CUST-REMOTE")

		# the pricing-section field is the one the other pull paths use, so it is asked first
		self.assertTrue(post.call_args.args[0].startswith("https://gk.example.com/api/method/"))

	def test_falls_back_to_the_push_pair_site_when_the_pricing_site_is_unset(self):
		response = SimpleNamespace(
			raise_for_status=lambda: None, json=lambda: {"message": "CUST-REMOTE"}
		)
		patched_cache, _ = self._patched_cache()

		with (
			patched_cache,
			self._patched_settings(site_field="from_site", site="https://gk-alt.example.com"),
			patch(f"{REMOTE_PO}.requests.post", return_value=response) as post,
		):
			self.assertEqual(fetch_remote_ref_customer("PUR-ORD-TEST-1"), "CUST-REMOTE")

		# neither way of configuring the Single may leave the lookup permanently dead
		self.assertTrue(
			post.call_args.args[0].startswith("https://gk-alt.example.com/api/method/")
		)

	def test_asks_the_owning_site_once_per_purchase_order(self):
		response = SimpleNamespace(
			raise_for_status=lambda: None, json=lambda: {"message": None}
		)
		patched_cache, _ = self._patched_cache()

		with (
			patched_cache,
			self._patched_settings(),
			patch(f"{REMOTE_PO}.requests.post", return_value=response) as post,
		):
			fetch_remote_ref_customer("PUR-ORD-TEST-1")
			fetch_remote_ref_customer("PUR-ORD-TEST-1")

		# a miss is memoized too, so a second row on the same PO costs nothing
		post.assert_called_once()

	def test_an_open_breaker_stays_offline(self):
		patched_cache, _ = self._patched_cache(breaker_open=True)

		with (
			patched_cache,
			patch(f"{REMOTE_PO}.frappe.db.get_single_value") as get_single_value,
			patch(f"{REMOTE_PO}.requests") as requests_mod,
		):
			self.assertIsNone(fetch_remote_ref_customer("PUR-ORD-TEST-1"))

		requests_mod.post.assert_not_called()
		# the breaker is checked ahead of the settings read, so an outage costs nothing at all
		get_single_value.assert_not_called()

	def test_a_failing_request_is_swallowed_and_trips_the_breaker(self):
		# best-effort by design: this runs inside a Quotation save and must never raise into one
		patched_cache, cache = self._patched_cache()

		with (
			patched_cache,
			self._patched_settings(),
			patch(
				f"{REMOTE_PO}.requests.post", side_effect=OSError("connection refused")
			),
			patch(f"{REMOTE_PO}.frappe.log_error") as log_error,
		):
			self.assertIsNone(fetch_remote_ref_customer("PUR-ORD-TEST-1"))

		log_error.assert_called_once()
		# one failure buys silence for the whole window, so an outage is not paid per save
		cache.set_value.assert_called_once_with(
			remote_po.BREAKER_KEY, 1, expires_in_sec=remote_po.BREAKER_TTL
		)

	def test_an_unreadable_breaker_does_not_disable_the_lookup(self):
		# fail closed: a cache that cannot be read must cost latency, not the feature
		response = SimpleNamespace(
			raise_for_status=lambda: None, json=lambda: {"message": "CUST-REMOTE"}
		)
		patched_cache, cache = self._patched_cache()
		cache.get_value.side_effect = RuntimeError("OOM command not allowed")

		with (
			patched_cache,
			self._patched_settings(),
			patch(f"{REMOTE_PO}.requests.post", return_value=response),
		):
			self.assertEqual(fetch_remote_ref_customer("PUR-ORD-TEST-1"), "CUST-REMOTE")

	def test_a_failing_breaker_write_and_log_still_return_none(self):
		# the handler's own cleanup must not raise into the Quotation save it was protecting
		patched_cache, cache = self._patched_cache()
		cache.set_value.side_effect = RuntimeError("cache write failed")

		with (
			patched_cache,
			self._patched_settings(),
			patch(
				f"{REMOTE_PO}.requests.post", side_effect=OSError("connection refused")
			),
			patch(
				f"{REMOTE_PO}.frappe.log_error", side_effect=RuntimeError("log failed")
			),
		):
			self.assertIsNone(fetch_remote_ref_customer("PUR-ORD-TEST-1"))

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
