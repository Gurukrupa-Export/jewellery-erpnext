from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.doc_events import (
	utils as si_utils,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import sales_invoice as si_events

SI = "jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice"
UTILS = (
	"jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.doc_events.utils"
)
KG_COMPANY = "KG GK Jewellers Private Limited"


class DummySalesInvoice:
	"""Minimal Sales Invoice stub covering the surface the hooks read/write."""

	DEFAULTS = {
		"name": "SI-1",
		"company": "Test Company",
		"customer": "CUST-1",
		"customer_address": "ADDR-1",
		"company_address": "ADDR-2",
		"posting_date": "2026-08-07",
		"doctype": "Sales Invoice",
		"quotation_to": None,
		"party_name": None,
		"is_return": 0,
		"sales_type": "Outright",
		"gold_rate": 0,
		"gold_rate_with_gst": 0,
		"item_same_as_above": 0,
		"payment_terms_template": None,
		"grand_total": 0,
		"currency": "INR",
		"items": [],
		"invoice_item": [],
		"taxes": [],
		"payment_schedule": [],
		"total": 0,
		"is_customer_metal": False,
		"is_customer_diamond": False,
		# Real Link/Data fields present on every Sales Invoice: an early return in
		# set_gst_details leaves them as-is, so assertIsNone (not hasattr) below.
		"tax_category": None,
		"taxes_and_charges": None,
		# Real field read by update_income_account.
		"is_opening": "No",
	}

	def __init__(self, **kwargs):
		for field, value in self.DEFAULTS.items():
			setattr(self, field, value.copy() if isinstance(value, list) else value)
		for field, value in kwargs.items():
			setattr(self, field, value)

	def get(self, field, default=None):
		return getattr(self, field, default)

	def set(self, field, value):
		setattr(self, field, value)

	def append(self, field, doc=None):
		if not isinstance(getattr(self, field, None), list):
			setattr(self, field, [])
		if isinstance(doc, dict):
			doc = frappe._dict(doc)
		getattr(self, field).append(doc)
		return doc

	def calculate_taxes_and_totals(self):
		pass

	def insert(self, **kwargs):
		pass


class StubItemRow:
	"""Plain-object item row (NOT frappe._dict) so the item_same_as_above clone sees a
	populated __dict__ — a frappe._dict row would silently no-op the clone."""

	DEFAULTS = {
		"custom_diamond_pcs": 0,
		"custom_gemstone_pcs": 0,
		"custom_other_weight": 0,
		"custom_metal_weight": 0,
		"custom_finding_weight": 0,
		"custom_diamond_weight": 0,
		"custom_gemstone_weight": 0,
		"custom_gross_weight": 0,
		"qty": 1,
		"rate": 0,
		"amount": 0,
		"base_amount": 0,
		"bom": None,
		"idx": 1,
	}

	def __init__(self, **kwargs):
		for field, value in self.DEFAULTS.items():
			setattr(self, field, value)
		for field, value in kwargs.items():
			setattr(self, field, value)

	def get(self, key, default=None):
		return getattr(self, key, default)


def _bom(**overrides):
	fields = dict(
		customer="CUST-1",
		total_diamond_pcs=2,
		total_gemstone_pcs=3,
		total_other_weight=1.5,
		total_metal_weight=4.5,
		finding_weight=0.5,
		total_diamond_weight_in_gms=0.4,
		total_gemstone_weight_in_gms=0.2,
		gross_weight=6.0,
		metal_detail=[],
		finding_detail=[],
		total_bom_amount=100,
		making_charge=10,
		certification_amount=5,
		custom_duty_amount=0,
		hallmarking_amount=0,
		freight_amount=0,
		sale_amount=0,
		total_diamond_amount=50,
		metal_touch="18K",
		gold_bom_amount=0,
		other_bom_amount=0,
		finding_bom_amount=0,
		gemstone_bom_amount=0,
	)
	fields.update(overrides)
	return SimpleNamespace(**fields)


def _qb_chain_mock(run_rows):
	"""Mock for frappe.qb.DocType().from_(...).select().distinct().where().run()."""
	mock_qb = MagicMock()
	chain = MagicMock()
	chain.select.return_value = chain
	chain.distinct.return_value = chain
	chain.where.return_value = chain
	chain.run.return_value = run_rows
	mock_qb.from_.return_value = chain
	return mock_qb


def _sub_category_si(*categories):
	"""Sales Invoice whose items each carry the given custom_item_sub_category."""
	return DummySalesInvoice(
		items=[StubItemRow(custom_item_sub_category=cat) for cat in categories]
	)


def _allowed_item_types_get_all(existing_item_types, matching_parents):
	"""frappe.get_all side-effect for get_allowed_item_types: the batched E Invoice
	Item existence check, then the batched Sales Type Multiselect match."""

	def _get_all(doctype, **kwargs):
		if doctype == "E Invoice Item":
			return list(existing_item_types)
		if doctype == "Sales Type Multiselect":
			return list(matching_parents)
		return []

	return _get_all


def _run_with_patches(si, method, *patchers):
	"""Enter *patchers, call the doc-event hook method(si, None), and return a
	namespace of the mocks keyed by their last path segment (e.g. mocks.get_value,
	mocks.update_income_account). Hook signatures are (self, method)."""
	with ExitStack() as stack:
		mocks = SimpleNamespace()
		for patcher in patchers:
			setattr(
				mocks, patcher.attribute.split(".")[-1], stack.enter_context(patcher)
			)
		method(si, None)
	return mocks


def _std_before_validate_patches(get_value=None):
	"""The three end-of-function helper patches plus db.get_value where the non-return
	path needs it. get_value=None skips the patch; get_value={} patches it bare; a
	dict of kwargs (e.g. {"return_value": "Internal"}) configures it."""
	patches = [
		patch(f"{SI}.update_income_account"),
		patch(f"{SI}.update_si_data"),
		patch(f"{SI}.update_payment_terms"),
	]
	if get_value is not None:
		patches.append(patch(f"{SI}.frappe.db.get_value", **get_value))
	return patches


def _std_validate_patches(*, get_value=None, include_income_account=True, extra=()):
	"""Patches shared by validate tests: get_doc (returning _bom()), the GST/SI
	helpers, plus optionally db.get_value / update_income_account / extra patchers."""
	patches = [
		patch(f"{SI}.frappe.get_doc", return_value=_bom()),
		patch(f"{SI}.set_gst_details"),
		patch(f"{SI}.update_si_data", return_value={}),
		patch(f"{SI}.update_payment_terms"),
	]
	if include_income_account:
		patches.append(patch(f"{SI}.update_income_account"))
	if get_value is not None:
		patches.append(patch(f"{SI}.frappe.db.get_value", **get_value))
	patches.extend(extra)
	return patches


def _gst_charge(account_head, rate):
	"""One Sales Taxes and Charges row for the given Output GST account."""
	return frappe._dict(
		charge_type="On Net Total",
		account_head=account_head,
		description=account_head.split(" - ")[0],
		rate=rate,
		cost_center="Main",
	)


def _address_get_all(
	customer_state="GJ", company_state="GJ", template_rows=(), charge_rows=()
):
	"""frappe.get_all side-effect for set_gst_details: the batched Address state
	lookup, plus optionally the two GST tables from fixed lists."""

	def _get_all(doctype, **kwargs):
		if doctype == "Address":
			return [
				frappe._dict(name="ADDR-1", gst_state_number=customer_state),
				frappe._dict(name="ADDR-2", gst_state_number=company_state),
			]
		if doctype == "Item Tax Template Detail":
			return list(template_rows)
		if doctype == "Sales Taxes and Charges":
			return list(charge_rows)
		return []

	return _get_all


class _SIBase(IntegrationTestCase):
	"""Shared stub-test base: no seeded data; IntegrationTestCase rollback is a no-op
	safety net."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		# set_gst_details rounds with frappe.utils.flt, and flt(value, precision) ->
		# rounded() -> frappe.get_system_settings("rounding_method"), which loads System
		# Settings through frappe.db the first time a process needs it and caches it on
		# frappe.local. The GST tests install the get_value mock before that read ever
		# happens on CI's freshly-created test_site, so the lookup cannot resolve and
		# flt swallows the failure, returning 0.0 -- the amount assertions then read
		# 0.0, not 30.0/15.0. Resolve it against the real DB first so the value is
		# cached; the mock then only has to stand in for the lookups the tests actually
		# want to suppress. Same failure 0b4e638d fixed in the refining suite.
		frappe.get_system_settings("rounding_method")


class TestSoGoldRateChanged(_SIBase):
	@patch(f"{SI}.frappe.db.get_value")
	def test_no_sales_order_returns_true(self, mock_get_value):
		self.assertTrue(si_events._so_gold_rate_changed(1000, None))
		mock_get_value.assert_not_called()

	@patch(f"{SI}.frappe.db.get_value")
	def test_missing_so_gold_rate_returns_true(self, mock_get_value):
		mock_get_value.return_value = None
		self.assertTrue(si_events._so_gold_rate_changed(1000, "SO-1"))
		mock_get_value.assert_called_once_with("Sales Order", "SO-1", "gold_rate")

	@patch(f"{SI}.frappe.db.get_value")
	def test_equal_rates_returns_false(self, mock_get_value):
		mock_get_value.return_value = 100.0005
		self.assertFalse(si_events._so_gold_rate_changed(100.0, "SO-1"))

	@patch(f"{SI}.frappe.db.get_value")
	def test_different_rates_returns_true(self, mock_get_value):
		mock_get_value.return_value = 101.0
		self.assertTrue(si_events._so_gold_rate_changed(100.0, "SO-1"))


class TestUpdateIncomeAccount(_SIBase):
	def test_opening_invoice_skips_lookup(self):
		si = DummySalesInvoice(is_opening="Yes", items=[StubItemRow()])
		with patch(f"{SI}.frappe.db.get_value") as mock_get_value:
			si_events.update_income_account(si)
		mock_get_value.assert_not_called()

	def test_sets_income_account_from_sales_type(self):
		si = DummySalesInvoice(
			company="Test Company", sales_type="Outright", items=[StubItemRow()]
		)
		with patch(f"{SI}.frappe.db.get_value", return_value="ACC-1") as mock_get_value:
			si_events.update_income_account(si)
		mock_get_value.assert_called_once_with(
			"Account",
			{"company": "Test Company", "custom_sales_type": "Outright"},
			"name",
		)
		self.assertEqual(si.items[0].income_account, "ACC-1")

	def test_no_matching_account_leaves_items_untouched(self):
		si = DummySalesInvoice(company="Other", items=[StubItemRow()])
		with patch(f"{SI}.frappe.db.get_value", return_value=None):
			si_events.update_income_account(si)
		self.assertIsNone(getattr(si.items[0], "income_account", None))


class TestSalesInvoiceBeforeValidate(_SIBase):
	def test_is_return_sign_flips_invoice_item_rows(self):
		si = DummySalesInvoice(
			is_return=1,
			invoice_item=[
				StubItemRow(qty=2, amount=100, base_amount=100),
				StubItemRow(qty=-3, amount=-50, base_amount=-50),
			],
		)
		mocks = _run_with_patches(
			si, si_events.before_validate, *_std_before_validate_patches()
		)
		self.assertEqual(si.invoice_item[0].qty, -2.0)
		self.assertEqual(si.invoice_item[0].amount, -100.0)
		self.assertEqual(si.invoice_item[0].base_amount, -100.0)
		self.assertEqual(si.invoice_item[1].qty, -3.0)
		self.assertEqual(si.invoice_item[1].amount, -50.0)
		mocks.update_income_account.assert_not_called()
		mocks.update_si_data.assert_not_called()
		mocks.update_payment_terms.assert_not_called()

	def test_is_return_empty_invoice_item_no_error(self):
		si = DummySalesInvoice(is_return=1, invoice_item=None)
		_run_with_patches(
			si, si_events.before_validate, *_std_before_validate_patches()
		)
		self.assertEqual(si.invoice_item, None)

	def test_hybrid_purges_subcontracting_charges_when_real_items_exist(self):
		si = DummySalesInvoice(
			company=KG_COMPANY,
			sales_type="Hybrid",
			items=[
				StubItemRow(item_code="Gold Ring"),
				StubItemRow(item_code="Subcontracting Charges"),
			],
		)
		_run_with_patches(
			si, si_events.before_validate, *_std_before_validate_patches(get_value={})
		)
		self.assertEqual(len(si.items), 1)
		self.assertEqual(si.items[0].item_code, "Gold Ring")

	def test_hybrid_keeps_all_rows_when_only_subcontracting_charges(self):
		si = DummySalesInvoice(
			company=KG_COMPANY,
			sales_type="Hybrid",
			items=[
				StubItemRow(item_code="Subcontracting Charges"),
				StubItemRow(item_code="Subcontracting Charges"),
			],
		)
		_run_with_patches(
			si, si_events.before_validate, *_std_before_validate_patches(get_value={})
		)
		self.assertEqual(len(si.items), 2)

	def test_gold_rate_with_gst_follows_sales_type_and_rate(self):
		# Computed as round(gold_rate * 1.03, 3) for Outright, skipped when the
		# rate is zero or the sales type is Certification.
		cases = [
			("Outright", 1000.0, round(1000 * 1.03, 3)),
			("Outright", 0, 0),
			("Certification", 1000.0, 0),
		]
		for sales_type, gold_rate, expected in cases:
			with self.subTest(sales_type=sales_type, gold_rate=gold_rate):
				si = DummySalesInvoice(
					company=KG_COMPANY, sales_type=sales_type, gold_rate=gold_rate
				)
				_run_with_patches(
					si,
					si_events.before_validate,
					*_std_before_validate_patches(get_value={}),
				)
				self.assertEqual(si.gold_rate_with_gst, expected)

	def test_item_same_as_above_clones_items_into_invoice_item(self):
		si = DummySalesInvoice(
			company=KG_COMPANY,
			item_same_as_above=1,
			items=[StubItemRow(item_code="RING-1", qty=2, rate=100)],
			invoice_item=[frappe._dict(item_code="OLD")],
		)
		_run_with_patches(
			si, si_events.before_validate, *_std_before_validate_patches(get_value={})
		)
		self.assertEqual(len(si.invoice_item), 1)
		self.assertEqual(si.invoice_item[0]["item_code"], "RING-1")
		self.assertIsNone(si.invoice_item[0]["name"])

	def test_item_same_as_above_skipped_when_false(self):
		si = DummySalesInvoice(
			company=KG_COMPANY, items=[StubItemRow(item_code="RING-1")]
		)
		_run_with_patches(
			si, si_events.before_validate, *_std_before_validate_patches(get_value={})
		)
		self.assertEqual(si.invoice_item, [])

	def test_internal_customer_group_skips_bom_reprice(self):
		si = DummySalesInvoice(
			company="Other",
			sales_type="Outright",
			gold_rate=1000.0,
			items=[StubItemRow(item_code="RING-1", bom="BOM-1")],
		)
		mocks = _run_with_patches(
			si,
			si_events.before_validate,
			*_std_before_validate_patches(
				get_value={"return_value": {"customer_group": "Internal"}}
			),
			patch(f"{SI}.frappe.get_doc"),
		)
		self.assertEqual(si.gold_rate_with_gst, round(1000 * 1.03, 3))
		mocks.get_doc.assert_not_called()
		mocks.update_income_account.assert_called_once_with(si)


class TestSalesInvoiceValidate(_SIBase):
	def _assert_bom_header_sums(self, si):
		self.assertEqual(si.custom_diamond_pcs, 2)
		self.assertEqual(si.custom_gemstone_pcs, 3)
		self.assertEqual(si.custom_other_weight, 1.5)
		self.assertEqual(si.custom_metal_weight, 4.5)
		self.assertEqual(si.custom_finding_weight, 0.5)
		self.assertEqual(si.custom_diamond_weight, 0.4)
		self.assertEqual(si.custom_gemstone_weight, 0.2)
		self.assertEqual(si.custom_gross_weight, 6.0)

	def test_is_return_copies_bom_aggregates_and_sums_headers(self):
		si = DummySalesInvoice(
			is_return=1, items=[StubItemRow(bom="BOM-1"), StubItemRow()]
		)
		si.calculate_taxes_and_totals = MagicMock()
		mocks = _run_with_patches(
			si, si_events.validate, *_std_validate_patches(include_income_account=False)
		)
		mocks.get_doc.assert_called_once_with("BOM", "BOM-1")
		self._assert_bom_header_sums(si)
		mocks.set_gst_details.assert_called_once_with(si)
		mocks.update_si_data.assert_called_once_with(si)
		mocks.update_payment_terms.assert_called_once_with(si, {})
		si.calculate_taxes_and_totals.assert_called_once()

	def test_non_return_header_sums_and_calls_gst_and_taxes(self):
		si = DummySalesInvoice(
			company=KG_COMPANY,
			is_return=0,
			items=[StubItemRow(bom="BOM-1"), StubItemRow()],
		)
		si.calculate_taxes_and_totals = MagicMock()
		mocks = _run_with_patches(
			si,
			si_events.validate,
			*_std_validate_patches(get_value={"return_value": None}),
		)
		mocks.get_doc.assert_called_once_with("BOM", "BOM-1")
		self._assert_bom_header_sums(si)
		mocks.update_income_account.assert_called_once_with(si)
		mocks.set_gst_details.assert_called_once_with(si)
		si.calculate_taxes_and_totals.assert_called_once()

	def test_non_return_external_customer_recomputes_total(self):
		si = DummySalesInvoice(
			company="Other",
			is_return=0,
			items=[StubItemRow(item_code="RING-1", bom="BOM-1", amount=999)],
		)
		si.calculate_taxes_and_totals = MagicMock()
		mocks = _run_with_patches(
			si,
			si_events.validate,
			*_std_validate_patches(
				get_value={
					"return_value": {
						"customer_group": "External",
						"custom_precision_variable": 2,
					}
				},
				extra=[patch(f"{SI}.update_making_charges")],
			),
		)
		self.assertEqual(si.items[0].rate, 115.0)
		self.assertEqual(si.items[0].amount, 115.0)
		self.assertEqual(si.total, 115.0)
		mocks.set_gst_details.assert_called_once_with(si)
		si.calculate_taxes_and_totals.assert_called_once()

	def test_internal_customer_group_skips_total_recompute(self):
		si = DummySalesInvoice(
			company="Other",
			is_return=0,
			total=999,
			items=[StubItemRow(item_code="RING-1", bom="BOM-1", rate=999, amount=999)],
		)
		si.calculate_taxes_and_totals = MagicMock()
		mocks = _run_with_patches(
			si,
			si_events.validate,
			*_std_validate_patches(
				get_value={
					"return_value": {
						"customer_group": "Internal",
						"custom_precision_variable": 2,
					}
				}
			),
		)
		self.assertEqual(si.items[0].rate, 999)
		self.assertEqual(si.items[0].amount, 999)
		self.assertEqual(si.total, 999)
		mocks.get_doc.assert_called_once_with("BOM", "BOM-1")


class TestOnSubmit(_SIBase):
	def test_sadguru_diamond_returns_early(self):
		si = DummySalesInvoice(company="Sadguru Diamond")
		with patch(f"{SI}.frappe.get_doc") as mock_get_doc:
			si_events.on_submit(si, None)
		mock_get_doc.assert_not_called()

	def test_return_hybrid_with_sc_row_returns_early(self):
		si = DummySalesInvoice(
			is_return=1,
			sales_type="Hybrid",
			items=[StubItemRow(item_code="Subcontracting Charges", bom="BOM-1")],
		)
		with patch(f"{SI}.frappe.get_doc") as mock_get_doc:
			si_events.on_submit(si, None)
		mock_get_doc.assert_not_called()

	def test_return_hybrid_accumulates_making_charge_and_creates_sc_invoice(self):
		si = DummySalesInvoice(
			is_return=1,
			sales_type="Hybrid",
			company="Other",
			items=[
				StubItemRow(item_code="RING-1", bom="BOM-1"),
				StubItemRow(
					item_code="PEND-1",
					bom="BOM-2",
					sales_order="SO-1",
					serial_no="SN-2",
					delivery_note=None,
				),
			],
		)
		new_si = DummySalesInvoice(name="NEW-SI")
		with (
			patch(
				f"{SI}.frappe.get_doc",
				side_effect=lambda dt, name: {
					"BOM-1": _bom(making_charge=10.5),
					"BOM-2": _bom(making_charge=20.25),
				}[name],
			),
			patch(f"{SI}.frappe.copy_doc", return_value=new_si) as mock_copy_doc,
			patch(
				f"{SI}.frappe.db.get_value",
				return_value=("Labour Item", "HSN-1", "GMS"),
			),
			patch(f"{SI}.frappe.db.set_value") as mock_set_value,
			patch(f"{SI}.set_gst_details"),
		):
			si_events.on_submit(si, None)
		self.assertEqual(new_si.sales_invoice, si.name)
		self.assertEqual(new_si.items[0].item_code, "Subcontracting Charges")
		self.assertEqual(new_si.items[0].qty, -1)
		self.assertEqual(new_si.items[0].rate, 30.75)
		self.assertEqual(new_si.items[0].amount, 30.75)
		self.assertEqual(new_si.items[0].custom_is_subcontracting_charge_row, 1)
		self.assertEqual(new_si.invoice_item[0]["item_code"], "Labour Item")
		self.assertEqual(new_si.invoice_item[0]["amount"], 30.75)
		self.assertEqual(new_si.invoice_item[0]["qty"], -1)
		self.assertEqual(new_si.total, 30.75)
		mock_copy_doc.assert_called_once_with(si)
		mock_set_value.assert_called_once_with(
			"Serial No",
			"SN-2",
			{"status": "Active", "warehouse": "Product Allocation FG - KGJPL"},
		)

	def test_return_hybrid_looks_up_dn_detail_when_delivery_note_set(self):
		si = DummySalesInvoice(
			is_return=1,
			sales_type="Hybrid",
			company="Other",
			items=[
				StubItemRow(
					item_code="RING-1",
					bom="BOM-1",
					sales_order="SO-1",
					delivery_note="DN-1",
					serial_no="SN-1",
				)
			],
		)
		new_si = DummySalesInvoice(name="NEW-SI")
		with (
			patch(f"{SI}.frappe.get_doc", return_value=_bom(making_charge=5)),
			patch(f"{SI}.frappe.copy_doc", return_value=new_si),
			patch(
				f"{SI}.frappe.db.get_value",
				side_effect=["DNI-1", ("Labour Item", "HSN-1", "GMS")],
			) as mock_get_value,
			patch(f"{SI}.frappe.db.set_value") as mock_set_value,
			patch(f"{SI}.set_gst_details"),
		):
			si_events.on_submit(si, None)
		self.assertEqual(new_si.items[0].dn_detail, "DNI-1")
		mock_get_value.assert_any_call(
			"Delivery Note Item",
			{
				"parent": "DN-1",
				"item_code": "Subcontracting Charges",
				"against_sales_order": "SO-1",
			},
			"name",
		)
		mock_set_value.assert_called_once_with(
			"Serial No",
			"SN-1",
			{"status": "Active", "warehouse": "Product Allocation FG - KGJPL"},
		)

	def test_return_hybrid_logs_and_throws_on_insert_failure(self):
		si = DummySalesInvoice(
			is_return=1,
			sales_type="Hybrid",
			company="Other",
			items=[
				StubItemRow(
					item_code="RING-1",
					bom="BOM-1",
					sales_order="SO-1",
					delivery_note=None,
					serial_no="SN-1",
				)
			],
		)
		new_si = DummySalesInvoice(name="NEW-SI")
		new_si.insert = MagicMock(side_effect=Exception("boom"))
		with (
			patch(f"{SI}.frappe.get_doc", return_value=_bom(making_charge=5)),
			patch(f"{SI}.frappe.copy_doc", return_value=new_si),
			patch(
				f"{SI}.frappe.db.get_value",
				return_value=("Labour Item", "HSN-1", "GMS"),
			),
			patch(f"{SI}.frappe.db.set_value") as mock_set_value,
			patch(f"{SI}.frappe.log_error") as mock_log_error,
			patch(f"{SI}.frappe.get_traceback", return_value="trace"),
			patch(f"{SI}.set_gst_details"),
		):
			with self.assertRaises(frappe.ValidationError):
				si_events.on_submit(si, None)
		mock_log_error.assert_called_once_with(
			title="New SI Creation Failed", message="trace"
		)
		mock_set_value.assert_not_called()

	def test_certification_sales_type_is_noop(self):
		si = DummySalesInvoice(sales_type="Certification")
		with patch(f"{SI}.frappe.get_doc") as mock_get_doc:
			si_events.on_submit(si, None)
		mock_get_doc.assert_not_called()


class TestValidateItemCategoryForCustomer(_SIBase):
	def test_mix_returns_immediately(self):
		si = DummySalesInvoice()
		with patch(
			f"{UTILS}.frappe.db.get_value", return_value="Mix"
		) as mock_get_value:
			si_utils.validate_item_category_for_customer(si)
		mock_get_value.assert_called_once_with(
			"Customer", "CUST-1", "custom_allowed_item_category_for_invoice"
		)

	def test_none_allowed_category_defaults_to_permissive(self):
		si = _sub_category_si("Ring")
		with patch(f"{UTILS}.frappe.db.get_value", return_value=None):
			si_utils.validate_item_category_for_customer(si)

	def test_quotation_resolves_party_name(self):
		si = DummySalesInvoice(
			doctype="Quotation", quotation_to="Customer", party_name="PARTY-1"
		)
		with patch(
			f"{UTILS}.frappe.db.get_value", return_value="Mix"
		) as mock_get_value:
			si_utils.validate_item_category_for_customer(si)
		mock_get_value.assert_called_once_with(
			"Customer", "PARTY-1", "custom_allowed_item_category_for_invoice"
		)

	def test_unique_single_category_ok(self):
		si = _sub_category_si("Ring")
		with patch(f"{UTILS}.frappe.db.get_value", return_value="Unique"):
			si_utils.validate_item_category_for_customer(si)

	def test_unique_multiple_categories_throws(self):
		si = _sub_category_si("Ring", "Pendant")
		with patch(f"{UTILS}.frappe.db.get_value", return_value="Unique"):
			with self.assertRaises(frappe.ValidationError):
				si_utils.validate_item_category_for_customer(si)

	def test_separate_selection_multiple_selected_throws(self):
		si = _sub_category_si("Cat A", "Cat B")
		qb = _qb_chain_mock(["Cat A", "Cat B"])
		with (
			patch(f"{UTILS}.frappe.db.get_value", return_value="Separate Selection"),
			patch(f"{UTILS}.frappe.qb", qb),
		):
			with self.assertRaises(frappe.ValidationError):
				si_utils.validate_item_category_for_customer(si)
		qb.DocType.assert_called_once_with("Item Category Multiselect")

	def test_separate_selection_mixed_selected_and_unselected_throws(self):
		si = _sub_category_si("Cat A", "Cat B")
		qb = _qb_chain_mock(["Cat A", "Cat B", "Cat C"])
		with (
			patch(f"{UTILS}.frappe.db.get_value", return_value="Separate Selection"),
			patch(f"{UTILS}.frappe.qb", qb),
		):
			with self.assertRaises(frappe.ValidationError):
				si_utils.validate_item_category_for_customer(si)

	def test_separate_selection_single_selected_ok(self):
		si = _sub_category_si("Cat A")
		qb = _qb_chain_mock(["Cat A", "Cat B"])
		with (
			patch(f"{UTILS}.frappe.db.get_value", return_value="Separate Selection"),
			patch(f"{UTILS}.frappe.qb", qb),
		):
			si_utils.validate_item_category_for_customer(si)


class TestCreateBranchPo(_SIBase):
	def test_non_branch_returns_without_db(self):
		si = DummySalesInvoice(sales_type="Outright")
		with patch(f"{UTILS}.frappe.db.get_value") as mock_get_value:
			si_utils.create_branch_po(si)
		mock_get_value.assert_not_called()

	def test_missing_branch_or_supplier_throws(self):
		# Either a customer with no Branch, or a Branch with no supplier, throws.
		for get_value in ({"return_value": None}, {"side_effect": ["BR-1", None]}):
			with self.subTest(get_value=get_value):
				si = DummySalesInvoice(sales_type="Branch")
				with patch(f"{UTILS}.frappe.db.get_value", **get_value):
					with self.assertRaises(frappe.ValidationError):
						si_utils.create_branch_po(si)

	def test_creates_po_and_msgprints(self):
		si = DummySalesInvoice(sales_type="Branch")
		with (
			patch(
				f"{UTILS}.frappe.db.get_value", side_effect=["BR-1", "SUP-1"]
			) as mock_get_value,
			patch(f"{UTILS}.create_po", return_value="PO-1") as mock_create_po,
			patch(f"{UTILS}.frappe.msgprint") as mock_msgprint,
		):
			si_utils.create_branch_po(si)
		mock_get_value.assert_any_call("Branch", {"custom_customer": "CUST-1"})
		mock_get_value.assert_any_call("Branch", "BR-1", "custom_supplier")
		mock_create_po.assert_called_once_with(si, "BR-1", "SUP-1")
		mock_msgprint.assert_called_once_with("PO-1 generated as Branch PO")


class TestCreatePo(_SIBase):
	def test_creates_draft_po_with_copied_items(self):
		po_doc = MagicMock()
		po_doc.save.return_value = None
		po_doc.name = "PO-1"
		si = DummySalesInvoice(
			company="Test Company",
			posting_date="2026-08-07",
			items=[StubItemRow(item_code="ITEM-1", qty=2, rate=100)],
		)
		with (
			patch(f"{UTILS}.frappe.new_doc", return_value=po_doc) as mock_new_doc,
			patch(
				f"{UTILS}.frappe.db.get_value", return_value="WH-1"
			) as mock_get_value,
		):
			result = si_utils.create_po(si, "BR-1", "SUP-1")
		self.assertEqual(result, "PO-1")
		mock_new_doc.assert_called_once_with("Purchase Order")
		self.assertEqual(po_doc.supplier, "SUP-1")
		self.assertEqual(po_doc.company, "Test Company")
		self.assertEqual(po_doc.branch, "BR-1")
		self.assertEqual(po_doc.purchase_type, "Branch Purchase")
		self.assertEqual(po_doc.schedule_date, date(2026, 8, 8))
		mock_get_value.assert_called_once_with(
			"Company", "Test Company", "custom_default_purchase_warehouse"
		)
		po_doc.append.assert_called_once_with(
			"items", {"item_code": "ITEM-1", "qty": 2, "rate": 100, "warehouse": "WH-1"}
		)
		po_doc.save.assert_called_once_with()


class TestGetAllowedItemTypes(_SIBase):
	def test_no_customer_payment_terms_returns_empty(self):
		with patch(f"{SI}.frappe.db.get_value", return_value=None) as mock_get_value:
			result = si_events.get_allowed_item_types("CUST-1", "Outright")
		self.assertEqual(result, set())
		mock_get_value.assert_called_once_with(
			"Customer Payment Terms", {"customer": "CUST-1"}
		)

	def test_no_matching_sales_type_returns_empty(self):
		cpt = frappe._dict(customer_payment_details=[frappe._dict(item_type="METAL")])
		with (
			patch(f"{SI}.frappe.db.get_value", return_value="CPT-1"),
			patch(f"{SI}.frappe.get_doc", return_value=cpt),
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_allowed_item_types_get_all(
					existing_item_types={"METAL"}, matching_parents=[]
				),
			),
		):
			result = si_events.get_allowed_item_types("CUST-1", "Outright")
		self.assertEqual(result, set())

	def test_matching_sales_type_adds_item_type(self):
		cpt = frappe._dict(
			customer_payment_details=[
				frappe._dict(item_type="METAL"),
				frappe._dict(item_type="STONE"),
			]
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value="CPT-1"),
			patch(f"{SI}.frappe.get_doc", return_value=cpt),
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_allowed_item_types_get_all(
					existing_item_types={"METAL", "STONE"},
					matching_parents=["METAL", "STONE"],
				),
			),
		):
			result = si_events.get_allowed_item_types("CUST-1", "Outright")
		self.assertEqual(result, {"METAL", "STONE"})

	def test_mixed_matching_and_non_matching_item_types(self):
		cpt = frappe._dict(
			customer_payment_details=[
				frappe._dict(item_type="METAL"),
				frappe._dict(item_type="STONE"),
				frappe._dict(item_type="DIAMOND"),
			]
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value="CPT-1"),
			patch(f"{SI}.frappe.get_doc", return_value=cpt),
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_allowed_item_types_get_all(
					existing_item_types={"METAL", "STONE", "DIAMOND"},
					matching_parents=["METAL", "STONE"],
				),
			),
		):
			result = si_events.get_allowed_item_types("CUST-1", "Outright")
		self.assertEqual(result, {"METAL", "STONE"})


class TestSetGstDetails(_SIBase):
	def test_returns_early_for_unsupported_sales_type(self):
		si = DummySalesInvoice(sales_type="Certification")
		with patch(f"{SI}.frappe.db.get_value") as mock_get_value:
			si_events.set_gst_details(si)
		mock_get_value.assert_not_called()

	def test_returns_early_if_state_missing(self):
		si = DummySalesInvoice(
			sales_type="Outright", customer_address="ADDR-1", company_address="ADDR-2"
		)
		with patch(
			f"{SI}.frappe.get_all",
			return_value=[frappe._dict(name="ADDR-1", gst_state_number="GJ")],
		):
			si_events.set_gst_details(si)
		self.assertIsNone(si.tax_category)

	def test_sets_tax_category_from_states(self):
		# Same state as the company -> In-State, otherwise Out-State.
		cases = [("GJ", "GJ", "In-State"), ("MH", "GJ", "Out-State")]
		for customer_state, company_state, expected in cases:
			with self.subTest(
				customer_state=customer_state, company_state=company_state
			):
				si = DummySalesInvoice(
					sales_type="Outright",
					company="Other",
					customer_address="ADDR-1",
					company_address="ADDR-2",
				)
				with patch(
					f"{SI}.frappe.get_all",
					side_effect=_address_get_all(customer_state, company_state),
				):
					si_events.set_gst_details(si)
				self.assertEqual(si.tax_category, expected)

	def test_hybrid_chooses_template_key_from_items(self):
		# Only Subcontracting Charges -> 'Outwork' key (GST 5%); any real item ->
		# 'Outright' key (GST 3%). Both on KG GK Jewellers Private Limited.
		cases = [
			("Subcontracting Charges", "GST 5% - KGJPL"),
			("Gold Ring", "GST 3% - KGJPL"),
		]
		for item_code, expected_template in cases:
			with self.subTest(item_code=item_code):
				si = DummySalesInvoice(
					sales_type="Hybrid",
					company=KG_COMPANY,
					items=[StubItemRow(item_code=item_code)],
				)
				with (
					patch(f"{SI}.frappe.db.get_value", return_value="TAX_TMPL"),
					patch(
						f"{SI}.frappe.get_all",
						side_effect=_address_get_all("GJ", "GJ"),
					),
				):
					si_events.set_gst_details(si)
				self.assertEqual(si.taxes_and_charges, "TAX_TMPL")
				self.assertEqual(si.items[0].item_tax_template, expected_template)

	def test_out_state_populates_igst_rates(self):
		si = DummySalesInvoice(
			sales_type="Outright",
			company="Gurukrupa Export Private Limited",
			total=1000.0,
			items=[StubItemRow(item_code="ITEM-1", amount=1000.0)],
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value="STC-TMPL"),
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_address_get_all(
					"MH",
					"GJ",
					template_rows=[
						frappe._dict(tax_type="IGST Output - GEPL", tax_rate=3.0)
					],
					charge_rows=[_gst_charge("IGST Output - GEPL", 3.0)],
				),
			),
		):
			si_events.set_gst_details(si)
		self.assertEqual(si.tax_category, "Out-State")
		self.assertEqual(len(si.taxes), 1)
		self.assertEqual(si.items[0].igst_rate, 3.0)
		self.assertEqual(si.items[0].igst_amount, 30.0)
		self.assertEqual(si.items[0].cgst_rate, 0.0)
		self.assertEqual(si.items[0].sgst_rate, 0.0)

	def test_returns_early_if_item_tax_template_missing(self):
		si = DummySalesInvoice(sales_type="Outright", company="Unknown Company")
		with (
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_address_get_all("GJ", "GJ"),
			) as mock_get_all,
			patch(f"{SI}.frappe.db.get_value") as mock_get_value,
		):
			si_events.set_gst_details(si)
		mock_get_all.assert_called_once()
		mock_get_value.assert_not_called()
		self.assertIsNone(si.taxes_and_charges)

	def test_logs_error_and_returns_if_taxes_and_charges_template_missing(self):
		si = DummySalesInvoice(
			sales_type="Outright", company="Gurukrupa Export Private Limited"
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value=None),
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_address_get_all("GJ", "GJ"),
			),
			patch(f"{SI}.frappe.log_error") as mock_log_error,
		):
			si_events.set_gst_details(si)
		mock_log_error.assert_called_once()
		self.assertIsNone(si.taxes_and_charges)

	def test_populates_taxes_and_item_gst_details(self):
		si = DummySalesInvoice(
			sales_type="Outright",
			company="Gurukrupa Export Private Limited",
			total=1000.0,
			items=[StubItemRow(item_code="ITEM-1", amount=1000.0)],
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value="STC-TMPL"),
			patch(
				f"{SI}.frappe.get_all",
				side_effect=_address_get_all(
					"GJ",
					"GJ",
					template_rows=[
						frappe._dict(tax_type="CGST Output - GEPL", tax_rate=1.5),
						frappe._dict(tax_type="SGST Output - GEPL", tax_rate=1.5),
					],
					charge_rows=[
						_gst_charge("CGST Output - GEPL", 1.5),
						_gst_charge("SGST Output - GEPL", 1.5),
					],
				),
			),
		):
			si_events.set_gst_details(si)

		self.assertEqual(len(si.taxes), 2)
		self.assertEqual(si.taxes[0].get("account_head"), "CGST Output - GEPL")
		self.assertEqual(si.items[0].gst_treatment, "Taxable")
		self.assertEqual(si.items[0].cgst_rate, 1.5)
		self.assertEqual(si.items[0].cgst_amount, 15.0)


class TestGetMetalPurity(_SIBase):
	"""Regression for A1: the raw SQL this replaced ran under MariaDB's
	utf8mb4_*_ci PAD SPACE collation, so it matched case-insensitively and
	ignored trailing spaces. Plain Python `==` does not."""

	def test_case_and_trailing_space_insensitive_match(self):
		rows = [frappe._dict(metal_type="Gold", metal_touch="18KT ", metal_purity=75.0)]
		self.assertEqual(si_events._get_metal_purity(rows, "gold", "18kt"), 75.0)

	def test_no_match_raises_index_error(self):
		rows = [frappe._dict(metal_type="Gold", metal_touch="18KT", metal_purity=75.0)]
		with self.assertRaises(IndexError):
			si_events._get_metal_purity(rows, "Gold", "22KT")


class TestBeforeValidateMetalCriteriaOrdering(_SIBase):
	"""Regression for A2: Metal Criteria declares sort_field=modified/DESC in its
	doctype meta, so an unguarded frappe.get_all would silently promote whichever
	duplicate row was edited most recently. order_by=None must disable that."""

	def test_metal_criteria_fetch_disables_meta_sort(self):
		si = DummySalesInvoice(
			company="Other",
			gold_rate=1000.0,
			items=[StubItemRow(item_code="RING-1", bom="BOM-1", idx=1)],
		)
		bom_doc = _bom(save=MagicMock(), total_wastage_amount=0)
		get_all_mock = MagicMock(return_value=[])
		_run_with_patches(
			si,
			si_events.before_validate,
			*_std_before_validate_patches(
				get_value={
					"return_value": {
						"customer_group": "External",
						"custom_precision_variable": 2,
					}
				}
			),
			patch(f"{SI}.frappe.get_doc", return_value=bom_doc),
			patch(f"{SI}.frappe.db.get_single_value", return_value=3),
			patch(f"{SI}.frappe.get_all", get_all_mock),
			patch(f"{SI}.update_making_charges"),
		)
		get_all_mock.assert_called_once_with(
			"Metal Criteria",
			filters={"parent": "CUST-1"},
			fields=["metal_type", "metal_touch", "metal_purity"],
			order_by=None,
		)


class TestValidateBomCacheKeyedByBom(_SIBase):
	"""Regression for B1: bom_cache must key off row.bom so rows sharing a BOM
	(across both the header-sum loop and the reprice loop) issue a single
	frappe.get_doc('BOM', ...) call instead of one per distinct row.idx."""

	def test_two_rows_sharing_a_bom_issue_a_single_get_doc_call(self):
		si = DummySalesInvoice(
			company="Other",
			items=[
				StubItemRow(item_code="RING-1", bom="BOM-1", idx=1),
				StubItemRow(item_code="RING-2", bom="BOM-1", idx=2),
			],
		)
		si.calculate_taxes_and_totals = MagicMock()
		mocks = _run_with_patches(
			si,
			si_events.validate,
			*_std_validate_patches(
				get_value={
					"return_value": {
						"customer_group": "External",
						"custom_precision_variable": 2,
					}
				},
				extra=[patch(f"{SI}.update_making_charges")],
			),
		)
		mocks.get_doc.assert_called_once_with("BOM", "BOM-1")


class TestValidateLoop2ReusesCustomerGroup(_SIBase):
	"""Regression for B3: the reprice loop must reuse validate()'s already-fetched
	customer_group instead of re-querying Customer.customer_group per BOM row —
	bom_doc.customer is forced to self.customer by update_bom_details() (run via
	update_si_data() earlier in validate()), so the value is provably identical."""

	def test_no_per_row_customer_group_query(self):
		si = DummySalesInvoice(
			company="Other",
			items=[StubItemRow(item_code="RING-1", bom="BOM-1", idx=1)],
		)
		si.calculate_taxes_and_totals = MagicMock()
		item_details = {"item_subcategory": "Ring", "setting_type": "Type"}

		def get_value_side_effect(doctype, name=None, fieldname=None, **kwargs):
			if doctype == "Item":
				return item_details
			if doctype == "Customer" and fieldname == "customer_group":
				raise AssertionError(
					"per-row Customer.customer_group query should be eliminated"
				)
			if doctype == "Customer" and isinstance(fieldname, list):
				return {"customer_group": "External", "custom_precision_variable": 2}
			return None

		_run_with_patches(
			si,
			si_events.validate,
			*_std_validate_patches(
				get_value={"side_effect": get_value_side_effect},
				extra=[patch(f"{SI}.update_making_charges")],
			),
		)


class TestUpdateSiDataEinvoiceItemsLazyFetch(_SIBase):
	"""Regression for B2: the E Invoice Item prefetch in update_si_data() must be
	lazy — invoices with no BOM rows (or fully item_same_as_above) must not issue
	the query, and invoices with multiple BOM rows must issue it exactly once."""

	def test_no_bom_rows_skips_einvoice_item_query(self):
		si = DummySalesInvoice(items=[StubItemRow(bom=None)])
		with (
			patch(f"{SI}.frappe.db.get_value", return_value=None),
			patch(f"{SI}.get_allowed_item_types", return_value=set()),
			patch(f"{SI}.frappe.get_all") as get_all_mock,
		):
			si_events.update_si_data(si)
		get_all_mock.assert_not_called()

	def test_item_same_as_above_skips_einvoice_item_query(self):
		si = DummySalesInvoice(
			item_same_as_above=1,
			items=[StubItemRow(item_code="RING-1", bom="BOM-1")],
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value=None),
			patch(f"{SI}.get_allowed_item_types", return_value=set()),
			patch(f"{SI}.frappe.get_all") as get_all_mock,
		):
			si_events.update_si_data(si)
		get_all_mock.assert_not_called()

	def test_multiple_bom_rows_fetch_einvoice_items_once_with_creation_desc(self):
		bom_doc = SimpleNamespace(
			currency="INR", hallmarking_amount=0, certification_amount=0
		)
		si = DummySalesInvoice(
			currency="INR",
			sales_type="Certification",
			items=[
				StubItemRow(item_code="RING-1", bom="BOM-1"),
				StubItemRow(item_code="RING-2", bom="BOM-1"),
			],
		)
		with (
			patch(f"{SI}.frappe.db.get_value", return_value=None),
			patch(f"{SI}.get_allowed_item_types", return_value=set()),
			patch(f"{SI}.frappe.get_doc", return_value=bom_doc),
			patch(f"{SI}.frappe.get_all", return_value=[]) as get_all_mock,
			patch(f"{SI}.update_bom_details"),
			patch(f"{SI}.update_einvoice_items"),
		):
			si_events.update_si_data(si)
		get_all_mock.assert_called_once()
		self.assertEqual(get_all_mock.call_args.args[0], "E Invoice Item")
		self.assertEqual(get_all_mock.call_args.kwargs["order_by"], "creation desc")


class TestUpdateBomDetailsCustomerGroupDedup(_SIBase):
	"""Regression for B4: update_bom_details() must not re-fetch
	Customer.customer_group at its tail — it already fetched the same
	bom_doc.customer (== self.customer, set at the top of this call) into
	making_charge_customer_group."""

	def _bom_doc(self):
		return SimpleNamespace(
			customer=None,
			company="Other",
			metal_detail=[],
			finding_detail=[],
			diamond_detail=[],
			gemstone_detail=[],
			certification_amount=0,
			validate=MagicMock(),
			save=MagicMock(),
			name="BOM-1",
		)

	def test_customer_group_fetched_once_not_twice(self):
		si = DummySalesInvoice(
			customer="CUST-1",
			company="Other",
			sales_type="Outright",
			is_return=0,
			gold_rate_with_gst=1000.0,
		)
		row = StubItemRow(item_code="RING-1", sales_order=None)
		bom_doc = self._bom_doc()
		item_details = {"item_subcategory": "Ring", "setting_type": "Type"}
		customer_group_calls = []

		def get_value_side_effect(doctype, name=None, fieldname=None, **kwargs):
			if doctype == "Item":
				return item_details
			if doctype == "Customer" and fieldname == "customer_group":
				customer_group_calls.append(name)
				return "External"
			return None

		with (
			patch(f"{SI}.frappe.db.get_value", side_effect=get_value_side_effect),
			patch(f"{SI}.update_making_charges"),
			patch(f"{SI}.update_totals"),
		):
			si_events.update_bom_details(
				si,
				row,
				bom_doc,
				is_branch_customer=False,
				invoice_data={},
				einvoice_items=[],
				einvoice_index={},
				precision=2,
			)
		self.assertEqual(len(customer_group_calls), 1)
		bom_doc.save.assert_called_once()

	def test_fallback_einvoice_fetch_uses_creation_desc(self):
		"""A3 (second call site): update_bom_details()'s own fallback fetch, used by
		callers that don't prefetch einvoice_items/einvoice_index."""
		si = DummySalesInvoice(
			customer="CUST-1",
			company="Other",
			sales_type="Outright",
			is_return=0,
			gold_rate_with_gst=1000.0,
		)
		row = StubItemRow(item_code="RING-1", sales_order=None)
		bom_doc = self._bom_doc()
		item_details = {"item_subcategory": "Ring", "setting_type": "Type"}

		def get_value_side_effect(doctype, name=None, fieldname=None, **kwargs):
			if doctype == "Item":
				return item_details
			if doctype == "Customer":
				return "External"
			return None

		with (
			patch(f"{SI}.frappe.db.get_value", side_effect=get_value_side_effect),
			patch(f"{SI}.frappe.get_all", return_value=[]) as get_all_mock,
			patch(f"{SI}.update_making_charges"),
			patch(f"{SI}.update_totals"),
		):
			si_events.update_bom_details(
				si,
				row,
				bom_doc,
				is_branch_customer=False,
				invoice_data={},
				precision=2,
			)
		get_all_mock.assert_called_once()
		self.assertEqual(get_all_mock.call_args.kwargs["order_by"], "creation desc")
