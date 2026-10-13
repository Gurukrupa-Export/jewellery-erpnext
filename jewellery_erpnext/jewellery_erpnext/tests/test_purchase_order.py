from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import purchase_order as po_events


class DummyPO:
	def __init__(self, **kwargs):
		self.name = kwargs.get("name", "PO-1")
		self.purchase_type = kwargs.get("purchase_type", "Finished Goods")
		self.company = kwargs.get("company", "Gurukrupa Export Private Limited")
		self.supplier_address = kwargs.get("supplier_address", "Addr1")
		self.company_address = kwargs.get("company_address", "Addr2")
		self.billing_address = kwargs.get("billing_address", "Addr2")
		self.total = kwargs.get("total", 1000.0)
		if "items" in kwargs:
			self.items = kwargs["items"]
		if "taxes" in kwargs:
			self.taxes = kwargs["taxes"]
		self.custom_customer_po = kwargs.get("custom_customer_po", "CUST-PO")
		self.transaction_date = kwargs.get("transaction_date", "2026-08-01")
		self.ref_customer = kwargs.get("ref_customer", "Customer X")
		self._is_new = kwargs.get("is_new", False)
		self.calculate_taxes_and_totals = MagicMock()
		self.set_total_in_words = MagicMock()

		for k, v in kwargs.items():
			if k != "is_new":
				setattr(self, k, v)

	def append(self, key, val):
		if not hasattr(self, key):
			setattr(self, key, [])
		getattr(self, key).append(val)

	def get(self, k, d=None):
		return getattr(self, k, d)

	def is_new(self):
		return self._is_new


class DummyQuotation:
	def __init__(self, **kwargs):
		self.company = kwargs.get("company", "Test Company")
		self.currency = kwargs.get("currency", "INR")
		self.transaction_date = kwargs.get("transaction_date", "2026-08-01")
		self.items = kwargs.get("items", [])
		for k, v in kwargs.items():
			setattr(self, k, v)

	def update(self, d):
		pass

	def run_method(self, method_name):
		pass

	def set(self, k, v):
		setattr(self, k, v)

	def append(self, key, val):
		getattr(self, key).append(val)


class TestPurchaseOrderEvents(IntegrationTestCase):
	def test_validate(self):
		po = DummyPO(
			purchase_type="FG Purchase",
			supplier_address="Address1",
			company_address="Address2",
			items=[
				frappe._dict(
					item_code="Item1",
					manufacturing_bom="BOM1",
					metal_amount=100,
					item_tax_rate="{}",
					taxable_value=100,
				)
			],
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.update_rate"
		) as mock_ur, patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.set_gst_details"
		) as mock_sgd:
			po_events.validate(po, "validate")
			mock_ur.assert_called_once_with(po)
			mock_sgd.assert_called_once_with(po)
			po.calculate_taxes_and_totals.assert_called_once()

	def test_validate_refreshes_in_words_after_totals(self):
		"""The amount in words is refreshed after the totals this hook recomputes.

		ERPNext fills in_words in its own validate, before doc_events run. update_rate and
		set_gst_details change the totals afterwards, so in_words must be recomputed after
		calculate_taxes_and_totals -- once, and never before it.
		"""
		po = DummyPO(purchase_type="FG Purchase")
		manager = MagicMock()
		manager.attach_mock(po.calculate_taxes_and_totals, "calculate_taxes_and_totals")
		manager.attach_mock(po.set_total_in_words, "set_total_in_words")

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.update_rate"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.set_gst_details"
		):
			po_events.validate(po, "validate")

		self.assertEqual(
			[name for name, _args, _kwargs in manager.mock_calls],
			["calculate_taxes_and_totals", "set_total_in_words"],
		)
		po.calculate_taxes_and_totals.assert_called_once_with()
		po.set_total_in_words.assert_called_once_with()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_in_state(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(
					item_code="Item1", item_tax_rate="{}", taxable_value=1000.0
				)
			],
			taxes=[],
		)

		def get_value_side_effect(doctype, filters, fieldname=None, **kwargs):
			if doctype == "Address":
				return "24"
			if doctype == "Purchase Taxes and Charges Template":
				return "In State GST"
			return None

		mock_get_value.side_effect = get_value_side_effect

		def get_all_side_effect(doctype, filters, **kwargs):
			if doctype == "Item Tax Template Detail":
				return [
					frappe._dict(tax_type="Output CGST", tax_rate=1.5),
					frappe._dict(tax_type="Output SGST", tax_rate=1.5),
				]
			if doctype == "Purchase Taxes and Charges":
				return [
					frappe._dict(
						charge_type="Actual",
						account_head="CGST",
						description="CGST",
						rate=1.5,
						cost_center="Main",
					),
					frappe._dict(
						charge_type="Actual",
						account_head="SGST",
						description="SGST",
						rate=1.5,
						cost_center="Main",
					),
				]
			return []

		mock_get_all.side_effect = get_all_side_effect

		po_events.set_gst_details(po)
		self.assertEqual(po.tax_category, "In-State")
		self.assertEqual(po.taxes_and_charges, "In State GST")
		self.assertEqual(len(po.taxes), 2)
		self.assertEqual(po.items[0].cgst_rate, 1.5)
		self.assertEqual(po.items[0].sgst_rate, 1.5)
		self.assertEqual(po.items[0].igst_rate, 0.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_update_rate(self, mock_get_value):
		po = DummyPO(
			purchase_type="FG Purchase",
			items=[frappe._dict(manufacturing_bom="BOM-1", metal_amount=1000)],
		)
		mock_get_value.return_value = frappe._dict(
			making_fg_purchase=100,
			finding_bom_amount=50,
			diamond_fg_purchase=200,
			gemstone_fg_purchase=10,
			certification_amount=5,
			freight_amount=15,
			hallmarking_amount=20,
			custom_duty_amount=0,
		)
		po_events.update_rate(po)
		item = po.items[0]
		self.assertEqual(item.making_amount, 100)
		self.assertEqual(item.rate, 1000 + 100 + 50 + 200 + 10 + 5 + 15 + 20 + 0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_single_value"
	)
	def test_make_subcontracting_order(self, mock_get_single_value, mock_new_doc):
		mock_get_single_value.return_value = "Subcontracting Service"
		doc = frappe._dict(
			name="MPLAN-1",
			company="Test Company",
			manufacturing_plan_table=[
				frappe._dict(
					supplier="Supplier A",
					customer="Customer X",
					purchase_type="FG Purchase",
					customer_po="PO-1",
					item_code="Item 1",
					subcontracting_qty=2,
					manufacturing_bom="BOM-1",
					diamond_quality="VVS",
					child_po="CPO-1",
					name="MP-Det-1",
				),
				frappe._dict(
					supplier="Supplier B",
					customer="Customer Y",
					purchase_type="Subcontracting",
					customer_po="PO-2",
					item_code="Item 2",
					subcontracting_qty=3,
					estimated_delivery_date="2026-08-10",
					child_po="CPO-2",
					name="MP-Det-2",
				),
			],
		)
		mock_po = MagicMock()
		mock_new_doc.return_value = mock_po
		po_events.make_subcontracting_order(doc)
		self.assertEqual(mock_new_doc.call_count, 2)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_exchange_rate"
	)
	def test_make_quotation(
		self,
		mock_get_exchange_rate,
		mock_get_value,
		mock_get_cached,
		mock_new_doc,
		mock_get_doc,
	):
		po_doc = DummyPO(
			company="Test Company",
			items=[
				frappe._dict(
					idx=1,
					name="ItemDet-1",
					branch="B1",
					project="P1",
					item_code="Item1",
					qty=1,
					diamond_quality="VVS",
					rate=500,
					custom_copy_bom="BOM-PO-1",
				)
			],
		)
		quotation_doc = DummyQuotation()
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else (dt if not isinstance(dt, str) else None)
		)
		mock_new_doc.return_value = quotation_doc
		mock_get_cached.return_value = "INR"
		mock_get_value.return_value = "Cust1"
		mock_get_exchange_rate.return_value = 1
		with patch(
			"erpnext.controllers.accounts_controller.get_default_taxes_and_charges"
		) as mock_get_taxes, patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.exists",
			return_value=True,
		):
			mock_get_taxes.return_value = {"taxes": []}
			res = po_events.make_quotation("PO-1", None)
			self.assertEqual(res.po_no, "CUST-PO")
			self.assertEqual(len(res.items), 1)
			self.assertEqual(res.ref_customer, "Customer X")
			# The Purchase Order's Copy BOM is the origin of the whole manufacturing chain.
			self.assertEqual(res.items[0]["copy_bom"], "BOM-PO-1")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	def test_make_quotation_without_copy_bom_throws(self, mock_new_doc, mock_get_doc):
		"""A Purchase Order row with no Copy BOM must never reach a Quotation.

		Copy BOM is the origin of the manufacturing chain, so the failure belongs on the
		button the user pressed, not at the far end of the enqueued BOM job.
		"""
		po_doc = DummyPO(
			company="Test Company",
			items=[
				frappe._dict(
					idx=1, name="ItemDet-1", item_code="Item1", qty=1, rate=500
				)
			],
		)
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else (dt if not isinstance(dt, str) else None)
		)
		mock_new_doc.return_value = DummyQuotation()

		with self.assertRaises(frappe.ValidationError) as ctx:
			po_events.make_quotation("PO-1", None)

		self.assertIn("Copy BOM", str(ctx.exception))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.exists",
		return_value=False,
	)
	def test_make_quotation_unknown_copy_bom_throws(
		self, mock_exists, mock_new_doc, mock_get_doc
	):
		"""A mirrored Purchase Order can name a BOM that was never replicated to this site.

		gke_customization only pushes a BOM when setting_type == "Nova Glow" and
		bom_type == "Template", so this is a real state, not a hypothetical one. It must read
		as a Copy BOM problem rather than a raw DoesNotExistError.
		"""
		po_doc = DummyPO(
			company="Test Company",
			items=[
				frappe._dict(
					idx=1,
					name="ItemDet-1",
					item_code="Item1",
					qty=1,
					rate=500,
					custom_copy_bom="BOM-NOT-HERE",
				)
			],
		)
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else (dt if not isinstance(dt, str) else None)
		)
		mock_new_doc.return_value = DummyQuotation()

		with self.assertRaises(frappe.ValidationError) as ctx:
			po_events.make_quotation("PO-1", None)

		self.assertIn("BOM-NOT-HERE", str(ctx.exception))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.exists",
		return_value=True,
	)
	def test_validate_copy_bom_reports_every_bad_row(self, mock_exists):
		"""One round trip to fix a Purchase Order, not one per bad row."""
		mock_exists.side_effect = lambda dt, name: name != "BOM-NOT-HERE"
		po_doc = DummyPO(
			items=[
				frappe._dict(idx=1, item_code="Item1", custom_copy_bom="BOM-OK"),
				frappe._dict(idx=2, item_code="Item2"),
				frappe._dict(idx=3, item_code="Item3", custom_copy_bom="BOM-NOT-HERE"),
			]
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			po_events.validate_copy_bom(po_doc)

		message = str(ctx.exception)
		self.assertIn("Row #2", message)
		self.assertIn("Row #3", message)
		self.assertIn("BOM-NOT-HERE", message)
		self.assertNotIn("BOM-OK", message)

	def test_set_gst_details_invalid_purchase_type(self):
		po = DummyPO(purchase_type="Invalid Type")
		po_events.set_gst_details(po)
		self.assertNotIn("tax_category", po.__dict__)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_set_gst_details_missing_state(self, mock_get_value):
		po = DummyPO()
		mock_get_value.return_value = None
		po_events.set_gst_details(po)
		self.assertNotIn("tax_category", po.__dict__)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_out_state(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(
					item_code="Item1", item_tax_rate="{}", taxable_value=1000.0
				)
			]
		)

		def get_value_side_effect(*args, **kwargs):
			if args[0] == "Address":
				return "24" if args[1] == "Addr1" else "25"
			if args[0] == "Purchase Taxes and Charges Template":
				return "Out State GST"
			return None

		mock_get_value.side_effect = get_value_side_effect

		def get_all_side_effect(*args, **kwargs):
			if args[0] == "Item Tax Template Detail":
				return [frappe._dict(tax_type="Output IGST", tax_rate=3.0)]
			if args[0] == "Purchase Taxes and Charges":
				return [
					frappe._dict(
						charge_type="Actual",
						account_head="IGST",
						description="IGST",
						rate=3.0,
						cost_center="Main",
					)
				]
			return []

		mock_get_all.side_effect = get_all_side_effect

		po_events.set_gst_details(po)
		self.assertEqual(po.tax_category, "Out-State")
		self.assertEqual(po.items[0].igst_rate, 3.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_set_gst_details_no_tax_template(self, mock_get_value):
		po = DummyPO(company="Unknown Company")
		mock_get_value.return_value = "24"
		po_events.set_gst_details(po)
		self.assertNotIn("taxes_and_charges", po.__dict__)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.log_error"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_set_gst_details_no_taxes_and_charges(self, mock_get_value, mock_log_error):
		po = DummyPO()
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else None
		)
		po_events.set_gst_details(po)
		mock_log_error.assert_called_once()
		self.assertNotIn("taxes", po.__dict__)

	def test_update_rate_not_fg_purchase(self):
		po = DummyPO(purchase_type="Subcontracting")
		po_events.update_rate(po)
		self.assertTrue(True)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_update_rate_no_bom(self, mock_get_value):
		po = DummyPO(
			purchase_type="FG Purchase", items=[frappe._dict(manufacturing_bom=None)]
		)
		po_events.update_rate(po)
		mock_get_value.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_exchange_rate"
	)
	def test_make_quotation_different_currency(
		self,
		mock_get_exchange_rate,
		mock_get_value,
		mock_get_cached,
		mock_new_doc,
		mock_get_doc,
	):
		po_doc = DummyPO(company="Test Company", items=[])
		quotation_doc = DummyQuotation(currency="USD")
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else (dt if not isinstance(dt, str) else None)
		)
		mock_new_doc.return_value = quotation_doc
		mock_get_cached.return_value = "INR"
		mock_get_exchange_rate.return_value = 80.0
		with patch(
			"erpnext.controllers.accounts_controller.get_default_taxes_and_charges"
		) as mock_get_taxes:
			mock_get_taxes.return_value = {"taxes": [{"account_head": "Tax 1"}]}
			res = po_events.make_quotation("PO-1", None)
			self.assertEqual(res.conversion_rate, 80.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_make_quotation_with_target_doc_string(
		self, mock_get_value, mock_get_cached, mock_get_doc
	):
		po_doc = DummyPO(company="Test Company", items=[])
		quotation_doc = DummyQuotation()
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else quotation_doc
		)
		mock_get_cached.return_value = "INR"
		with patch(
			"erpnext.controllers.accounts_controller.get_default_taxes_and_charges"
		) as mock_get_taxes:
			mock_get_taxes.return_value = {}
			res = po_events.make_quotation("PO-1", '{"company": "Test Company"}')
			self.assertEqual(res.po_no, "CUST-PO")

	def test_on_cancel(self):
		po_events.on_cancel(frappe._dict())
		self.assertTrue(True)

	def test_update_rate_is_new(self):
		po = DummyPO(purchase_type="FG Purchase", is_new=True)
		po_events.update_rate(po)
		self.assertTrue(True)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_skip_rcm_and_input(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(
					item_code="Item1", item_tax_rate="{}", taxable_value=1000.0
				)
			]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "In State GST"
		)
		mock_get_all.side_effect = (
			lambda *args, **kw: [
				frappe._dict(tax_type="Output CGST - RCM", tax_rate=1.5),
				frappe._dict(tax_type="Input CGST", tax_rate=1.5),
				frappe._dict(tax_type="Output CGST", tax_rate=5.0),
			]
			if args[0] == "Item Tax Template Detail"
			else []
		)
		po_events.set_gst_details(po)
		self.assertEqual(po.items[0].cgst_rate, 5.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_empty_item_code(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(item_code=None, item_tax_rate="{}", taxable_value=1000.0)
			]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "In State GST"
		)
		mock_get_all.return_value = []
		po_events.set_gst_details(po)
		self.assertEqual(po.items[0].get("cgst_rate", None), None)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_no_items(self, mock_get_all, mock_get_value):
		po = DummyPO(items=[], taxes=[])
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "In State GST"
		)
		mock_get_all.return_value = []
		po_events.set_gst_details(po)
		self.assertEqual(po.get("taxes", []), [])

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_missing_item_tax_rate(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(
					item_code="Item1", item_tax_rate=None, taxable_value=1000.0
				)
			]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "In State GST"
		)
		mock_get_all.return_value = []
		po_events.set_gst_details(po)
		self.assertEqual(po.items[0].cgst_rate, 0.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_update_rate_multiple_items_same_bom(self, mock_get_value):
		po = DummyPO(
			purchase_type="FG Purchase",
			items=[
				frappe._dict(manufacturing_bom="BOM-1", metal_amount=1000),
				frappe._dict(manufacturing_bom="BOM-1", metal_amount=2000),
			],
		)
		mock_get_value.return_value = frappe._dict(
			making_fg_purchase=100,
			finding_bom_amount=50,
			diamond_fg_purchase=200,
			gemstone_fg_purchase=10,
			certification_amount=5,
			freight_amount=15,
			hallmarking_amount=20,
			custom_duty_amount=0,
		)
		po_events.update_rate(po)
		mock_get_value.assert_called_once()
		self.assertEqual(po.items[0].rate, 1000 + 400)
		self.assertEqual(po.items[1].rate, 2000 + 400)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	def test_make_subcontracting_order_empty_plan_table(self, mock_new_doc):
		doc = frappe._dict(
			name="MPLAN-2", company="Test Company", manufacturing_plan_table=[]
		)
		po_events.make_subcontracting_order(doc)
		mock_new_doc.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_single_value"
	)
	def test_make_subcontracting_order_fg_purchase_schedule_date_fallback(
		self, mock_get_single_value, mock_new_doc
	):
		mock_get_single_value.return_value = "Subcontracting Service"
		doc = frappe._dict(
			name="MPLAN-3",
			company="Test Company",
			manufacturing_plan_table=[
				frappe._dict(
					supplier="Supplier A",
					customer="Customer X",
					purchase_type="FG Purchase",
					customer_po="PO-1",
					item_code="Item 1",
					subcontracting_qty=2,
					manufacturing_bom="BOM-1",
					diamond_quality="VVS",
					child_po="CPO-1",
					name="MP-Det-1",
				)
			],
		)
		mock_po = MagicMock()
		mock_po.transaction_date = "2026-08-01"
		mock_new_doc.return_value = mock_po
		po_events.make_subcontracting_order(doc)
		self.assertEqual(mock_po.schedule_date, "2026-08-01")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_exchange_rate"
	)
	def test_make_quotation_missing_item_fields(
		self,
		mock_get_exchange_rate,
		mock_get_value,
		mock_get_cached,
		mock_new_doc,
		mock_get_doc,
	):
		po_doc = DummyPO(
			items=[
				frappe._dict(
					idx=1,
					name="ItemDet-1",
					item_code="Item1",
					qty=1,
					rate=500,
					custom_copy_bom="BOM-PO-1",
				)
			]
		)
		quotation_doc = DummyQuotation()
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else (dt if not isinstance(dt, str) else None)
		)
		mock_new_doc.return_value = quotation_doc
		mock_get_cached.return_value = "INR"
		mock_get_exchange_rate.return_value = 1
		with patch(
			"erpnext.controllers.accounts_controller.get_default_taxes_and_charges"
		) as mock_get_taxes, patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.exists",
			return_value=True,
		):
			mock_get_taxes.return_value = {"taxes": []}
			res = po_events.make_quotation("PO-2", None)
			self.assertEqual(res.items[0].get("branch"), None)
			self.assertEqual(res.items[0]["item_code"], "Item1")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_invalid_json_tax_rate(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(
					item_code="Item1",
					item_tax_rate="INVALID JSON",
					taxable_value=1000.0,
				)
			]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "In State GST"
		)
		mock_get_all.return_value = [
			frappe._dict(
				charge_type="Actual",
				account_head="CGST",
				description="CGST",
				rate=1.5,
				cost_center="Main",
			)
		]
		try:
			po_events.set_gst_details(po)
		except Exception:
			pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_set_gst_details_missing_customer_state(self, mock_get_value):
		po = DummyPO(supplier_address="Addr1", company_address="Addr2")
		mock_get_value.side_effect = (
			lambda *args, **kw: None if args[1] == "Addr1" else "24"
		)
		po_events.set_gst_details(po)
		self.assertNotIn("tax_category", po.__dict__)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	def test_set_gst_details_missing_company_state(self, mock_get_value):
		po = DummyPO(supplier_address="Addr1", company_address="Addr2")
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[1] == "Addr1" else None
		)
		po_events.set_gst_details(po)
		self.assertNotIn("tax_category", po.__dict__)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_subcontracting_template(
		self, mock_get_all, mock_get_value
	):
		po = DummyPO(
			purchase_type="Subcontracting",
			items=[
				frappe._dict(item_code="I1", taxable_value=100.0, item_tax_rate="{}")
			],
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "Temp"
		)
		mock_get_all.return_value = []
		po_events.set_gst_details(po)
		self.assertEqual(po.tax_category, "In-State")
		self.assertEqual(po.taxes_and_charges, "Temp")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_branch_purchase_template(
		self, mock_get_all, mock_get_value
	):
		po = DummyPO(
			purchase_type="Branch Purchase",
			company="KG GK Jewellers Private Limited",
			items=[
				frappe._dict(item_code="I1", taxable_value=100.0, item_tax_rate="{}")
			],
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "Temp"
		)
		mock_get_all.return_value = []
		po_events.set_gst_details(po)
		self.assertEqual(po.taxes_and_charges, "Temp")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_other_tax_type(self, mock_get_all, mock_get_value):
		po = DummyPO(
			items=[
				frappe._dict(item_code="I1", item_tax_rate="{}", taxable_value=100.0)
			]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "Temp"
		)
		mock_get_all.side_effect = (
			lambda *args, **kw: [frappe._dict(tax_type="Output CESS", tax_rate=10.0)]
			if args[0] == "Item Tax Template Detail"
			else []
		)
		po_events.set_gst_details(po)
		self.assertEqual(po.items[0].cgst_rate, 0.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_item_tax_rate_override(self, mock_get_all, mock_get_value):
		# Rate now comes from "Item Tax Template Detail" (the item's real
		# Item Tax Template), not from item.item_tax_rate -- that field is
		# computed by core ERPNext's update_item_tax_map(), which silently
		# backfills any account head the template doesn't define with the
		# stale Purchase Taxes and Charges Template rate, so it can't be
		# trusted as the source of truth. See sync_tax_row_rate_with_item()
		# in doc_events/purchase_invoice.py for the same fix.
		po = DummyPO(
			items=[frappe._dict(item_code="I1", taxable_value=1000.0)], taxes=[]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "Temp"
		)

		def get_all_side_effect(doctype, filters=None, **kw):
			if doctype == "Purchase Taxes and Charges":
				return [
					frappe._dict(
						charge_type="Actual",
						account_head="Input CGST_ACC",
						description="CGST",
						rate=9.0,
						cost_center="Main",
						add_deduct_tax="Add",
					)
				]
			if doctype == "Item Tax Template Detail":
				return [frappe._dict(tax_type="Input CGST_ACC", tax_rate=2.5)]
			return []

		mock_get_all.side_effect = get_all_side_effect
		po_events.set_gst_details(po)
		self.assertEqual(po.taxes[0]["rate"], 2.5)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_all"
	)
	def test_set_gst_details_multiple_tax_rows_total_override(
		self, mock_get_all, mock_get_value
	):
		po = DummyPO(
			items=[
				frappe._dict(item_code="I1", item_tax_rate="{}", taxable_value=1000.0)
			]
		)
		mock_get_value.side_effect = (
			lambda *args, **kw: "24" if args[0] == "Address" else "Temp"
		)
		mock_get_all.side_effect = (
			lambda *args, **kw: [
				frappe._dict(
					charge_type="Actual",
					account_head="CGST",
					description="CGST",
					rate=5.0,
					cost_center="Main",
				),
				frappe._dict(
					charge_type="Actual",
					account_head="SGST",
					description="SGST",
					rate=10.0,
					cost_center="Main",
				),
			]
			if args[0] == "Purchase Taxes and Charges"
			else []
		)
		po_events.set_gst_details(po)
		self.assertEqual(len(po.taxes), 2)
		self.assertEqual(po.taxes[0]["rate"], 5.0)
		self.assertEqual(po.taxes[1]["rate"], 10.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_single_value"
	)
	def test_make_subcontracting_order_other_purchase_type(
		self, mock_get_single_value, mock_new_doc
	):
		mock_get_single_value.return_value = "Subcontracting Service"
		doc = frappe._dict(
			name="MPLAN-5",
			company="Test",
			manufacturing_plan_table=[
				frappe._dict(
					supplier="Supp A",
					purchase_type="Unknown Type",
					estimated_delivery_date="2026-08-10",
					item_code="FG1",
					subcontracting_qty=1,
				)
			],
		)
		mock_po = MagicMock()
		mock_new_doc.return_value = mock_po
		po_events.make_subcontracting_order(doc)
		self.assertEqual(mock_po.is_subcontracted, 1)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_exchange_rate"
	)
	def test_make_quotation_empty_items(
		self,
		mock_get_exchange_rate,
		mock_get_value,
		mock_get_cached,
		mock_new_doc,
		mock_get_doc,
	):
		po_doc = DummyPO(
			name="PO-3", custom_customer_po="CUST-3", company="Test", items=[]
		)
		mock_get_doc.side_effect = (
			lambda dt, name=None, **kwargs: po_doc
			if dt == "Purchase Order"
			else (dt if not isinstance(dt, str) else None)
		)
		mock_new_doc.return_value = DummyQuotation()
		mock_get_cached.return_value = "INR"
		with patch(
			"erpnext.controllers.accounts_controller.get_default_taxes_and_charges"
		) as mock_get_taxes:
			mock_get_taxes.return_value = {}
			res = po_events.make_quotation("PO-3", None)
			self.assertEqual(len(res.items), 0)


class TestMakeSubcontractingOrderEnrichments(IntegrationTestCase):
	"""What make_subcontracting_order stamps on the Purchase Orders a Manufacturing Plan raises.

	Three header/row enrichments, each with its own source:

	* the four order dimensions, copied from the plan header -- but only when the plan was
	  drawn from Sales Orders, since an MWO-only plan never has its header refreshed;
	* custom_copy_bom on FG Purchase rows, read from the Sales Order Item each row was
	  planned from, in a single query that covers FG Purchase rows only;
	* gold_rate_with_gst (and gold_rate, converted as purchase_order.js does), stamped only
	  when every source Sales Order with a rate agrees.

	DB-free per the suite convention: setUpClass is a deliberate no-op, the plan is a
	frappe._dict and every Purchase Order is a MagicMock.
	"""

	MODULE = "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order"

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		# flt(x, 3) rounds through System Settings; read it before frappe.get_all is faked.
		frappe.get_system_settings("rounding_method")

		self.sales_order_rates = []
		self.sales_order_item_rows = []

		def get_all(doctype, *args, **kwargs):
			if doctype == "Sales Order":
				return [
					frappe._dict(gold_rate_with_gst=rate)
					for rate in self.sales_order_rates
				]
			if doctype == "Sales Order Item":
				return [frappe._dict(row) for row in self.sales_order_item_rows]
			return []

		for target, kwargs in (
			("frappe.new_doc", {}),
			("frappe.db.get_single_value", {"return_value": "Subcontracting Service"}),
			("frappe.get_all", {"side_effect": get_all}),
		):
			patcher = patch(f"{self.MODULE}.{target}", **kwargs)
			setattr(self, "mock_" + target.rsplit(".", 1)[-1], patcher.start())
			self.addCleanup(patcher.stop)

	def _new_po(self):
		po = MagicMock()
		po.gold_rate_with_gst = None
		po.gold_rate = None
		return po

	def _calls_for(self, doctype):
		return [c for c in self.mock_get_all.call_args_list if c.args[0] == doctype]

	@staticmethod
	def _row(**kwargs):
		row = {
			"supplier": "_Test Supplier A",
			"customer": "_Test Customer",
			"purchase_type": "FG Purchase",
			"customer_po": "_TEST-CPO",
			"item_code": "_TEST-ITEM-1",
			"subcontracting_qty": 1,
			"manufacturing_bom": "_TEST-BOM-1",
			"diamond_quality": "_TEST-DQ",
			"child_po": "_TEST-CHILD-PO",
			"estimated_delivery_date": "2026-08-10",
			"name": "_TEST-MPT-1",
		}
		row.update(kwargs)
		return frappe._dict(row)

	@staticmethod
	def _plan(rows, **header):
		return frappe._dict(
			name="_TEST-MPLAN",
			company="_Test Company",
			manufacturing_plan_table=rows,
			**header,
		)

	def test_stamps_order_dimensions_from_plan_header(self):
		po = self._new_po()
		self.mock_new_doc.return_value = po
		plan = self._plan(
			[self._row(sales_order="_TEST-SO-1")],
			order_type="_TEST-ORDER-TYPE",
			sales_type="_TEST-SALES-TYPE",
			flow_type="_TEST-FLOW-TYPE",
			design_type="_TEST-DESIGN-TYPE",
		)

		po_events.make_subcontracting_order(plan)

		self.assertEqual(po.set.call_count, 4)
		self.assertEqual(
			{c.args[0]: c.args[1] for c in po.set.call_args_list},
			{
				"custom_order_type": "_TEST-ORDER-TYPE",
				"custom_sales_type": "_TEST-SALES-TYPE",
				"custom_flow_type": "_TEST-FLOW-TYPE",
				"custom_design_type": "_TEST-DESIGN-TYPE",
			},
		)

	def test_no_order_dimensions_without_sales_order_rows(self):
		"""An MWO-only plan keeps whatever its header held; that must not reach the PO."""
		po = self._new_po()
		self.mock_new_doc.return_value = po
		plan = self._plan(
			[self._row(mwo="_TEST-MWO-1")],
			order_type="_TEST-STALE",
			sales_type="_TEST-STALE",
			flow_type="_TEST-STALE",
			design_type="_TEST-STALE",
		)

		po_events.make_subcontracting_order(plan)

		stamped = {c.args[0] for c in po.set.call_args_list}
		self.assertFalse(stamped & set(po_events.PO_DIMENSION_FIELDS.values()))
		po.save.assert_called_once()

	def test_fg_rows_carry_copy_bom_from_sales_order_item(self):
		fg_po, service_po = self._new_po(), self._new_po()
		self.mock_new_doc.side_effect = [fg_po, service_po]
		self.sales_order_item_rows = [
			{"name": "_TEST-SOI-1", "copy_bom": "_TEST-BOM-ORIGIN-1"}
		]
		plan = self._plan(
			[
				self._row(sales_order="_TEST-SO-1", docname="_TEST-SOI-1"),
				self._row(
					supplier="_Test Supplier B",
					purchase_type="Subcontracting",
					sales_order="_TEST-SO-1",
					docname="_TEST-SOI-2",
					name="_TEST-MPT-2",
				),
			]
		)

		po_events.make_subcontracting_order(plan)

		soi_calls = self._calls_for("Sales Order Item")
		self.assertEqual(len(soi_calls), 1)
		self.assertEqual(
			soi_calls[0].kwargs["filters"], {"name": ["in", ["_TEST-SOI-1"]]}
		)

		fg_payload = fg_po.append.call_args.args[1]
		self.assertEqual(fg_payload["custom_copy_bom"], "_TEST-BOM-ORIGIN-1")
		service_payload = service_po.append.call_args.args[1]
		self.assertNotIn("custom_copy_bom", service_payload)

	def test_rows_without_docname_get_no_copy_bom_and_no_query(self):
		po = self._new_po()
		self.mock_new_doc.return_value = po

		po_events.make_subcontracting_order(self._plan([self._row()]))

		self.assertIsNone(po.append.call_args.args[1]["custom_copy_bom"])
		self.assertEqual(self._calls_for("Sales Order Item"), [])

	def test_common_gold_rate_stamped_before_items(self):
		po = self._new_po()
		self.mock_new_doc.return_value = po
		seen_at_first_append = []
		po.append.side_effect = lambda *args: seen_at_first_append.append(
			(po.gold_rate_with_gst, po.gold_rate)
		)
		self.sales_order_rates = [1030.0, 1030.0]
		plan = self._plan(
			[
				self._row(sales_order="_TEST-SO-1"),
				self._row(sales_order="_TEST-SO-2", name="_TEST-MPT-2"),
			]
		)

		po_events.make_subcontracting_order(plan)

		self.assertEqual(po.gold_rate_with_gst, 1030.0)
		self.assertEqual(po.gold_rate, 1000.0)
		self.assertEqual(seen_at_first_append[0], (1030.0, 1000.0))
		self.assertEqual(len(self._calls_for("Sales Order")), 1)

	def test_gold_rate_left_blank_when_sources_disagree_or_zero(self):
		cases = (
			("disagree", [1000.0, 2000.0], None, None),
			("zero ignored", [0, 1000.0], 1000.0, 970.874),
			("all zero", [0, 0], None, None),
		)
		for label, rates, expected_with_gst, expected_rate in cases:
			with self.subTest(label):
				po = self._new_po()
				self.mock_new_doc.return_value = po
				self.sales_order_rates = rates
				plan = self._plan(
					[
						self._row(sales_order="_TEST-SO-1"),
						self._row(sales_order="_TEST-SO-2", name="_TEST-MPT-2"),
					]
				)

				po_events.make_subcontracting_order(plan)

				self.assertEqual(po.gold_rate_with_gst, expected_with_gst)
				self.assertEqual(po.gold_rate, expected_rate)

	def test_source_gold_rate_without_sales_order_rows_skips_query(self):
		self.assertIsNone(po_events._source_gold_rate(self._plan([self._row()])))
		self.mock_get_all.assert_not_called()
