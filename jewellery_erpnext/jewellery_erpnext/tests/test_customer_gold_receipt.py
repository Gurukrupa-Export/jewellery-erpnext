# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold receipt eligibility rules.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, Stock Entries are
``frappe._dict`` fakes and every DB read is patched. No document is created.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import (
	customer_gold_fulfilment,
	customer_goods_eligibility,
)
from jewellery_erpnext.customer_subcontracting import (
	customer_gold_receipt as cg_receipt,
)
from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
	validate_customer_gold_batches,
	validate_customer_gold_receipt,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	_PURE_QTY_LEGACY_EXCLUDED_TYPES,
	_pure_qty_excluded_types,
)

MOD = "jewellery_erpnext.customer_subcontracting.customer_gold_receipt"
ELIGIBILITY_MOD = "jewellery_erpnext.customer_subcontracting.customer_goods_eligibility"
#: ``_pure_qty_excluded_types`` imports the two settings helpers INSIDE the function,
#: so they must be patched where they are defined, not on the stock_entry module.
SE_MOD = (
	"jewellery_erpnext.customer_subcontracting.doctype."
	"subcontracting_settings.subcontracting_settings"
)

ITEM = "M-G-24KT-99.9-Y"
SE_TYPE = "Customer Goods Received"
CUSTOMER = "GJCU0009"
OTHER_CUSTOMER = "MHCU0012"
BATCH = "GJCU0009-2F07-M-G-24KT-99.9-Y-01"
COMPANY = "Gurukrupa Export Private Limited"

SETTINGS = frappe._dict(
	customer_goods_stock_entry_type=SE_TYPE,
	customer_24kt_item=ITEM,
	gold_rate_source="Jain Jewels",
	gold_rate_field="live_rate",
	gold_rate_unit="Per 10 Gram",
)

#: Canned result from the rate service. The service itself is covered by
#: test_customer_gold_rate.py; here we only care that the receipt freezes what it returns.
RATE = frappe._dict(
	gold_rate_reference="R-2026-08-15",
	gold_rate_date="2026-08-15",
	requested_date="2026-08-15",
	rate_source="Jain Jewels",
	rate_field="live_rate",
	raw_rate=72000.0,
	rate_unit="Per 10 Gram",
	per_gram_rate=7200.0,
)


def _item_row(idx=1, **overrides):
	row = frappe._dict(
		idx=idx,
		item_code=ITEM,
		qty=10,
		customer=CUSTOMER,
		inventory_type="Customer Goods",
		batch_no=BATCH,
	)
	row.update(overrides)
	return row


def _entry(**overrides):
	# NOTE: frappe._dict subclasses dict, so `doc.items` resolves to dict.items, not this
	# list. Always reach rows through doc.get("items") -- which is what the code under
	# test does too.
	doc = frappe._dict(
		doctype="Stock Entry",
		stock_entry_type=SE_TYPE,
		posting_date="2026-08-15",
		# An explicitly chosen date: without it the receipt posts "now", as ERPNext would.
		set_posting_time=1,
		company=COMPANY,
		_customer=CUSTOMER,
		items=[_item_row()],
	)
	doc.update(overrides)
	return doc


def _batch(**overrides):
	"""A Batch row as ``validate_customer_gold_batches`` now reads it.

	Defaults are the healthy case; each C06 fixture overrides one field. Note
	``custom_company`` defaults to ``None`` on purpose -- ``create_parent_batches`` never
	stamps it, so a freshly minted batch really does look like this, and the validator
	must tolerate it rather than demand a company.
	"""
	row = frappe._dict(
		item=ITEM,
		custom_customer=CUSTOMER,
		custom_inventory_type="Customer Goods",
		custom_company=None,
		disabled=0,
		expiry_date=None,
	)
	row.update(overrides)
	return row


def _db_get_value(doctype, name, fieldname=None, as_dict=False):
	if doctype == "Stock Entry Type":
		return "Material Receipt"
	if doctype == "Batch":
		if name == BATCH:
			return _batch()
		if name == "FOREIGN-BATCH":
			return _batch(custom_customer=OTHER_CUSTOMER)
		if name == "REGULAR-BATCH":
			return _batch(custom_customer=None, custom_inventory_type="Regular Stock")
		# C06 fixtures. Both shapes short-circuited past the ownership guards, which
		# are `if <field> and <field> != expected`. row_ownership records that the
		# second shape -- Customer Goods with a NULL customer -- exists in production.
		if name == "UNSTAMPED-BATCH":
			return _batch(custom_customer=None, custom_inventory_type=None)
		if name == "OWNERLESS-CG-BATCH":
			return _batch(custom_customer=None)
		# C06 second wave: item / company / disabled / expiry.
		if name == "WRONG-ITEM-BATCH":
			return _batch(item="M-G-22KT-91.6-Y")
		if name == "OTHER-COMPANY-BATCH":
			return _batch(custom_company="Some Other Company")
		if name == "SAME-COMPANY-BATCH":
			return _batch(custom_company=COMPANY)
		if name == "DISABLED-BATCH":
			return _batch(disabled=1)
		if name == "EXPIRED-BATCH":
			return _batch(expiry_date="2020-01-01")
		if name == "FUTURE-EXPIRY-BATCH":
			return _batch(expiry_date="2099-01-01")
		return None
	return None


# -- Item masters: the flag, and the booking gates ---------------------------------------------
#: A gold purity that is NOT the rate reference. Flagged on kg-gk, and the one row the retired
#: Settings list held there.
GOLD_22KT = "M-G-22KT-91.75-Y"
#: Stones are received in their own UOM (Carat) at a typed rate. ``variant_of`` decides that,
#: not the code.
DIAMOND = "D-NT-RO-MH12A-+9-9.5"
GEMSTONE = "G-RU-OVAL"
#: Gold stocked in Carat: flagged, but the per-gram gold rate cannot price it.
GOLD_IN_CARAT = "M-G-18KT-75.0-Y"
STONE_WITHOUT_BATCH = "D-NT-RO-LOOSE"
#: Findings (template ``F``) are gold too: priced from the per-gram gold rate, so held to Gram.
FINDING = "F-G-18KT-75.4-Y"
FINDING_PURITY = 75.4
#: A finding stocked in Carat: flagged and batch controlled, but the per-gram rate cannot price it.
FINDING_IN_CARAT = "F-G-22KT-91.75-Y-SW"
#: Unflagged. The second one's code says 24KT, which earns it nothing on a receipt.
UNFLAGGED_22KT = "M-G-22KT-91.9-Y"
UNFLAGGED_24KT = "M-G-24KT-99.9-W"

NOT_ENABLED = "is not enabled for Customer Goods"
#: The Item field's real label, spelled out rather than imported so a renamed constant fails here.
FLAG_LABEL = "Inventory Type Can be Customer Goods"


def _master(name, variant_of, stock_uom="Gram", **overrides):
	"""An Item as ``_receipt_item_details`` returns it. Healthy unless overridden."""
	item = frappe._dict(
		name=name,
		variant_of=variant_of,
		disabled=0,
		is_stock_item=1,
		has_batch_no=1,
		stock_uom=stock_uom,
	)
	item.update(overrides)
	return item


ITEMS = {
	ITEM: _master(ITEM, "M"),
	GOLD_22KT: _master(GOLD_22KT, "M"),
	DIAMOND: _master(DIAMOND, "D", "Carat"),
	GEMSTONE: _master(GEMSTONE, "G", "Carat"),
	GOLD_IN_CARAT: _master(GOLD_IN_CARAT, "M", "Carat"),
	STONE_WITHOUT_BATCH: _master(STONE_WITHOUT_BATCH, "D", "Carat", has_batch_no=0),
	FINDING: _master(FINDING, "F"),
	FINDING_IN_CARAT: _master(FINDING_IN_CARAT, "F", "Carat"),
	UNFLAGGED_22KT: _master(UNFLAGGED_22KT, "M"),
	UNFLAGGED_24KT: _master(UNFLAGGED_24KT, "M"),
}

#: ``Inventory Type Can be Customer Goods`` per item -- the only eligibility rule. Tests flip it
#: through ``patch.dict(ITEM_FLAGS, ...)`` so a change cannot leak into the next test.
ITEM_FLAGS = {
	ITEM: 1,
	GOLD_22KT: 1,
	DIAMOND: 1,
	GEMSTONE: 1,
	GOLD_IN_CARAT: 1,
	STONE_WITHOUT_BATCH: 1,
	FINDING: 1,
	FINDING_IN_CARAT: 1,
	UNFLAGGED_22KT: 0,
	UNFLAGGED_24KT: 0,
}


def _eligible(item_codes):
	"""Stand-in for ``get_customer_goods_eligible_items``. Reads ``ITEM_FLAGS`` at call time."""
	return {code for code in item_codes or () if code and ITEM_FLAGS.get(code)}


def _details(item_codes):
	"""Stand-in for ``_receipt_item_details``. Copies, so no test can edit the shared masters."""
	return {
		code: frappe._dict(ITEMS[code]) for code in item_codes or () if code in ITEMS
	}


def with_item_masters(cls):
	"""Answer the receipt's two Item-master reads from ``ITEMS`` and ``ITEM_FLAGS``.

	Needed by every class that reaches ``_validate_rows`` with the flow on. Unpatched, both reads
	are real ``frappe.get_all`` calls: they break under this suite's ``frappe.db.get_value`` fake,
	and on a real site their answer would depend on that site's Items. ``new=`` rather than a
	MagicMock, so the ``*_mocks`` every test receives are unchanged.
	"""
	cls = patch(f"{MOD}._receipt_item_details", new=_details)(cls)
	return patch(f"{MOD}.get_customer_goods_eligible_items", new=_eligible)(cls)


@with_item_masters
@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
@patch(f"{MOD}.reference_rate", return_value=None)
class TestCustomerGoldReceiptRules(IntegrationTestCase):
	"""Receipt eligibility, with the feature enabled."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_valid_receipt_passes(self, *_mocks):
		validate_customer_gold_receipt(_entry())

	def test_missing_customer_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(_entry(_customer=None))
		self.assertIn("Customer is mandatory", str(raised.exception))

	def test_row_customer_conflict_blocks(self, *_mocks):
		doc = _entry(items=[_item_row(customer=OTHER_CUSTOMER)])
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(doc)
		self.assertIn("does not match the receipt Customer", str(raised.exception))

	def test_blank_row_customer_is_backfilled(self, *_mocks):
		doc = _entry(items=[_item_row(customer=None)])
		validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].customer, CUSTOMER)

	# -- C15: the zero-valuation flag must be stamped here, not left to the earlier hook --
	def test_allow_zero_valuation_is_stamped_on_every_row(self, *_mocks):
		"""C15. Customer Goods metal must never reach erpnext without the flag.

		``doc_events.stock_entry.allow_zero_valuation`` runs EARLIER in the before_validate
		chain than this validator, and it keys on ``inventory_type``. At that point an
		API-created row still carries the blanket "Regular Stock" default, so the flag is
		left at 0 -- and this validator then flips ownership to Customer Goods. The row
		would reach erpnext owned by the customer but without the flag, and
		``get_valuation_rate(..., allow_zero_rate=0, raise_error_if_no_rate=True)`` would
		either throw or book company valuation onto customer-owned metal.
		"""
		doc = _entry()
		validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].allow_zero_valuation_rate, 1)

	def test_allow_zero_valuation_is_stamped_even_when_row_arrives_regular_stock(
		self, *_mocks
	):
		"""The exact API path: the blanket default already stamped Regular Stock."""
		doc = _entry(items=[_item_row(inventory_type="Regular Stock")])
		validate_customer_gold_receipt(doc)
		row = doc.get("items")[0]
		self.assertEqual(row.inventory_type, "Customer Goods")
		self.assertEqual(row.allow_zero_valuation_rate, 1)

	def test_allow_zero_valuation_is_stamped_on_all_rows(self, *_mocks):
		doc = _entry(items=[_item_row(), _item_row(), _item_row()])
		validate_customer_gold_receipt(doc)
		for row in doc.get("items"):
			self.assertEqual(row.allow_zero_valuation_rate, 1)

	def test_wrong_item_blocks(self, *_mocks):
		doc = _entry(items=[_item_row(item_code=UNFLAGGED_22KT)])
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(doc)
		self.assertIn(NOT_ENABLED, str(raised.exception))

	def test_other_24kt_item_still_blocks(self, *_mocks):
		"""A ``24KT`` code earns nothing on a receipt: eligibility is the Item's flag."""
		doc = _entry(items=[_item_row(item_code=UNFLAGGED_24KT)])
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(doc)
		self.assertIn(NOT_ENABLED, str(raised.exception))

	def test_wrong_inventory_type_blocks(self, *_mocks):
		"""A DELIBERATE other ownership class still hard-fails.

		"Customer Stock" is a real Inventory Type and a real ownership class -- nothing in
		the framework ever stamps it by default, so its presence is always a caller's
		choice and always wrong on a Customer Gold receipt.
		"""
		doc = _entry(items=[_item_row(inventory_type="Customer Stock")])
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(doc)
		self.assertIn("requires Inventory Type", str(raised.exception))

	def test_framework_default_regular_stock_is_overridden_not_blocked(self, *_mocks):
		"""Regression: "Regular Stock" is the framework's own default, not a caller choice.

		``doc_events.stock_entry.before_validate`` runs FIRST in the before_validate chain
		and ends with an unconditional ``if not row.inventory_type: row.inventory_type =
		"Regular Stock"``. This validator runs LAST, so on a REAL document every row always
		arrives carrying it. Rejecting it made every server-created Customer Gold receipt
		throw "requires Inventory Type Customer Goods, but Regular Stock was supplied" --
		only the browser got through, because stock_entry.js sets Customer Goods before the
		save is sent. Caught by driving an actual Stock Entry on alfarsi; the previous
		version of this test asserted the broken behaviour.
		"""
		doc = _entry(items=[_item_row(inventory_type="Regular Stock")])
		validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")

	def test_blank_inventory_type_is_set_server_side(self, *_mocks):
		doc = _entry(items=[_item_row(inventory_type=None)])
		validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")

	def test_zero_qty_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(_entry(items=[_item_row(qty=0)]))
		self.assertIn("Quantity must be greater than zero", str(raised.exception))

	def test_negative_qty_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_receipt(_entry(items=[_item_row(qty=-1)]))
		self.assertIn("Quantity must be greater than zero", str(raised.exception))

	def test_batch_rules_pass_for_own_batch(self, *_mocks):
		validate_customer_gold_batches(_entry())

	def test_missing_batch_blocks_at_submit(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as raised:
			validate_customer_gold_batches(_entry(items=[_item_row(batch_no=None)]))
		message = str(raised.exception)
		# "goods", not "gold": the rule covers every row of the receipt, stones included.
		self.assertIn("Customer goods must be batch tracked", message)
		self.assertIn(ITEM, message)

	def test_foreign_customer_batch_blocks(self, *_mocks):
		doc = _entry(items=[_item_row(batch_no="FOREIGN-BATCH")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_batches(doc)

	def test_regular_stock_batch_blocks(self, *_mocks):
		doc = _entry(items=[_item_row(batch_no="REGULAR-BATCH")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_batches(doc)

	def test_a_batch_with_no_inventory_type_blocks(self, *_mocks):
		"""C06: a batch never ownership-stamped must not be adopted by silence.

		`if batch.custom_inventory_type and ... != CUSTOMER_GOODS` passes a blank,
		so this shape reached submit and created a customer obligation against a
		batch whose owner was never recorded.
		"""
		doc = _entry(items=[_item_row(batch_no="UNSTAMPED-BATCH")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_batches(doc)

	def test_a_customer_goods_batch_with_no_customer_blocks(self, *_mocks):
		"""C06: Customer Goods with a NULL owner is unresolved, not acceptable.

		The sibling guard is `if batch.custom_customer and ... != customer`, which a
		blank also passes -- so the gold had a type but no owner.
		"""
		doc = _entry(items=[_item_row(batch_no="OWNERLESS-CG-BATCH")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_batches(doc)

	def test_a_correctly_owned_batch_still_passes(self, *_mocks):
		"""The tightening must not touch the normal path.

		create_parent_batches stamps customer and inventory type together
		(batch_rename.py:76-77), so every batch this flow mints looks like this.
		"""
		doc = _entry(items=[_item_row(batch_no=BATCH)])
		validate_customer_gold_batches(doc)

	def test_other_stock_entry_type_is_untouched(self, *_mocks):
		"""A different Stock Entry Type must not be validated, even with the flag on."""
		doc = _entry(stock_entry_type="Material Transfer (WORK ORDER)", _customer=None)
		validate_customer_gold_receipt(doc)
		validate_customer_gold_batches(doc)


@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=False)
class TestCustomerGoldReceiptDisabled(IntegrationTestCase):
	"""Regression: with the feature off, nothing is enforced and nothing is mutated."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_invalid_receipt_is_ignored_when_disabled(self, *_mocks):
		doc = _entry(
			_customer=None,
			items=[
				_item_row(item_code="ANY-ITEM", qty=0, inventory_type="Regular Stock")
			],
		)
		validate_customer_gold_receipt(doc)
		validate_customer_gold_batches(doc)
		self.assertEqual(doc.get("items")[0].inventory_type, "Regular Stock")
		self.assertEqual(doc.get("items")[0].item_code, "ANY-ITEM")

	def test_normal_material_receipt_is_untouched(self, *_mocks):
		doc = _entry(
			stock_entry_type="Material Receipt",
			_customer=None,
			items=[_item_row(item_code="ANY-ITEM", inventory_type="Regular Stock")],
		)
		validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].inventory_type, "Regular Stock")

	def test_the_item_flag_is_not_read_when_disabled(self, *_mocks):
		"""Flow off (gk, alfarsi): the configured type is ordinary stock, so no Item is looked up."""
		eligible = MagicMock(side_effect=_eligible)
		doc = _entry(
			items=[_item_row(item_code=UNFLAGGED_22KT, inventory_type="Regular Stock")]
		)
		with patch(f"{MOD}.get_customer_goods_eligible_items", new=eligible):
			validate_customer_gold_receipt(doc)
		eligible.assert_not_called()
		self.assertEqual(doc.get("items")[0].inventory_type, "Regular Stock")


NEW_RATE = frappe._dict(
	gold_rate_reference="R-2026-08-21",
	gold_rate_date="2026-08-21",
	requested_date="2026-08-21",
	rate_source="Jain Jewels",
	rate_field="live_rate",
	raw_rate=73000.0,
	rate_unit="Per 10 Gram",
	per_gram_rate=7300.0,
)


@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
@patch(f"{MOD}.reference_rate", return_value=None)
class TestCustomerGoldBatchIntegrity(IntegrationTestCase):
	"""C06 -- item, company and expiry/disabled, beyond the two ownership checks."""

	@classmethod
	def setUpClass(cls):
		pass

	def _submit(self, batch_no):
		doc = _entry(items=[_item_row(batch_no=batch_no)])
		validate_customer_gold_batches(doc)

	def test_healthy_batch_passes(self, *_mocks):
		self._submit(BATCH)

	# -- item -------------------------------------------------------------------
	def test_batch_of_another_item_blocks(self, *_mocks):
		"""erpnext catches this too, but only at on_submit -- too late to be useful."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._submit("WRONG-ITEM-BATCH")
		self.assertIn("belongs to Item", str(ctx.exception))

	# -- company ----------------------------------------------------------------
	def test_batch_of_another_company_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._submit("OTHER-COMPANY-BATCH")
		self.assertIn("belongs to Company", str(ctx.exception))

	def test_batch_of_the_same_company_passes(self, *_mocks):
		self._submit("SAME-COMPANY-BATCH")

	def test_batch_without_a_company_is_accepted(self, *_mocks):
		"""MUST pass. ``create_parent_batches`` never stamps ``custom_company``, so
		requiring it would reject this flow's own freshly minted batches."""
		self._submit(BATCH)

	# -- disabled / expiry ------------------------------------------------------
	def test_disabled_batch_blocks(self, *_mocks):
		"""A genuine gap: erpnext's own validate_batch skips Material Receipt entirely."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._submit("DISABLED-BATCH")
		self.assertIn("disabled", str(ctx.exception).lower())

	def test_expired_batch_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._submit("EXPIRED-BATCH")
		self.assertIn("expired", str(ctx.exception).lower())

	def test_batch_expiring_after_the_posting_date_passes(self, *_mocks):
		self._submit("FUTURE-EXPIRY-BATCH")

	def test_batch_without_an_expiry_passes(self, *_mocks):
		self._submit(BATCH)

	# -- portability -------------------------------------------------------------
	def test_optional_fields_are_omitted_when_the_site_lacks_them(self, *_mocks):
		"""``custom_company`` is NOT in ``custom_fields/batch.json``.

		A freshly installed site therefore has no such column, and naming it in the SELECT
		raises ``Unknown column 'custom_company'``. Caught by the integration suite on a
		clean site, so this pins it here too.
		"""

		with patch(f"{MOD}.frappe.db.has_column", return_value=False):
			fields = cg_receipt._batch_fields()

		self.assertNotIn("custom_company", fields)
		for required in (
			"item",
			"custom_customer",
			"custom_inventory_type",
			"disabled",
		):
			self.assertIn(required, fields)

	def test_optional_fields_are_included_when_present(self, *_mocks):
		with patch(f"{MOD}.frappe.db.has_column", return_value=True):
			self.assertIn("custom_company", cg_receipt._batch_fields())


@with_item_masters
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
@patch(f"{MOD}.reference_rate", return_value=None)
class TestCustomerGoldRateSnapshot(IntegrationTestCase):
	"""The receipt freezes the resolved rate as audit evidence."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_snapshot_fields_are_populated(self, *_mocks):
		doc = _entry()
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE):
			validate_customer_gold_receipt(doc)
		self.assertEqual(doc.custom_gold_rate_reference, "R-2026-08-15")
		self.assertEqual(doc.custom_gold_rate_date, "2026-08-15")
		self.assertEqual(doc.custom_gold_rate_source, "Jain Jewels")
		self.assertEqual(doc.custom_gold_rate_field, "live_rate")
		self.assertEqual(doc.custom_gold_rate_raw, 72000.0)
		self.assertEqual(doc.custom_gold_rate_unit, "Per 10 Gram")
		self.assertEqual(doc.custom_gold_rate_per_gram, 7200.0)

	def test_service_is_called_with_the_posting_date(self, *_mocks):
		"""Never today() -- the receipt's own posting date drives the lookup."""
		doc = _entry(posting_date="2026-08-15")
		with patch(
			f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE
		) as mock_resolve:
			validate_customer_gold_receipt(doc)
		self.assertEqual(mock_resolve.call_args[0][0], "2026-08-15")

	def test_api_supplied_snapshot_is_overwritten(self, *_mocks):
		"""A forged rate arriving over the API can never become financial truth."""
		doc = _entry(
			custom_gold_rate_reference="R-FORGED",
			custom_gold_rate_raw=1,
			custom_gold_rate_per_gram=1,
		)
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE):
			validate_customer_gold_receipt(doc)
		self.assertEqual(doc.custom_gold_rate_reference, "R-2026-08-15")
		self.assertEqual(doc.custom_gold_rate_raw, 72000.0)
		self.assertEqual(doc.custom_gold_rate_per_gram, 7200.0)

	def test_draft_re_resolves_when_posting_date_changes(self, *_mocks):
		doc = _entry(posting_date="2026-08-15")
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE):
			validate_customer_gold_receipt(doc)
		self.assertEqual(doc.custom_gold_rate_per_gram, 7200.0)

		doc.posting_date = "2026-08-21"
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=NEW_RATE):
			validate_customer_gold_receipt(doc)
		self.assertEqual(doc.custom_gold_rate_per_gram, 7300.0)
		self.assertEqual(doc.custom_gold_rate_reference, "R-2026-08-21")

	def test_submitted_snapshot_is_not_re_resolved(self, *_mocks):
		"""Editing the Gold Rates master later must not rewrite a frozen receipt.

		The freeze is structural: ``before_validate`` does not run on a submitted document,
		so the snapshot simply is never recomputed. This asserts the contract by resolving
		once, then changing what the service would return, and confirming the already
		frozen values are untouched.
		"""
		old = _entry()
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE):
			validate_customer_gold_receipt(old)
		frozen = old.custom_gold_rate_per_gram
		old.docstatus = 1

		# master edited -- the service would now return a different rate
		new = _entry()
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=NEW_RATE):
			validate_customer_gold_receipt(new)

		self.assertEqual(old.custom_gold_rate_per_gram, frozen)
		self.assertEqual(old.custom_gold_rate_raw, 72000.0)
		self.assertEqual(new.custom_gold_rate_per_gram, 7300.0)

	def test_cancellation_retains_the_snapshot(self, *_mocks):
		doc = _entry()
		with patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE):
			validate_customer_gold_receipt(doc)
		doc.docstatus = 2
		self.assertEqual(doc.custom_gold_rate_per_gram, 7200.0)
		self.assertEqual(doc.custom_gold_rate_reference, "R-2026-08-15")

	def test_other_stock_entry_type_gets_no_snapshot(self, *_mocks):
		doc = _entry(stock_entry_type="Material Transfer (WORK ORDER)", _customer=None)
		with patch(
			f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE
		) as mock_resolve:
			validate_customer_gold_receipt(doc)
		mock_resolve.assert_not_called()
		self.assertIsNone(doc.get("custom_gold_rate_per_gram"))


@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=False)
class TestCustomerGoldRateSnapshotDisabled(IntegrationTestCase):
	"""Regression: with the feature off, no rate is ever looked up."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_no_rate_lookup_when_disabled(self, *_mocks):
		doc = _entry()
		with patch(
			f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE
		) as mock_resolve:
			validate_customer_gold_receipt(doc)
		mock_resolve.assert_not_called()
		self.assertIsNone(doc.get("custom_gold_rate_per_gram"))

	def test_normal_material_receipt_needs_no_gold_rate(self, *_mocks):
		doc = _entry(
			stock_entry_type="Material Receipt",
			_customer=None,
			posting_date="2026-08-10",
		)
		with patch(
			f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE
		) as mock_resolve:
			validate_customer_gold_receipt(doc)
		mock_resolve.assert_not_called()


class TestPureQtyExclusion(IntegrationTestCase):
	"""``_pure_qty_excluded_types`` decides whether a customer receipt gets a pure quantity.

	The exclusion at ``doc_events.stock_entry.before_validate`` is why customer metal carried
	``custom_pure_qty = 0``: the rows were never reached. Un-excluding is scoped to the
	CONFIGURED receipt type and only while the flag is on, so a site with the flag off keeps
	byte-identical behaviour.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_flag_off_keeps_every_legacy_exclusion(self):
		with patch(f"{SE_MOD}.is_customer_gold_enabled", return_value=False):
			self.assertEqual(
				_pure_qty_excluded_types(), _PURE_QTY_LEGACY_EXCLUDED_TYPES
			)

	def test_flag_on_unexcludes_only_the_configured_type(self):
		settings = frappe._dict(
			customer_goods_stock_entry_type="Customer Goods Received"
		)
		with patch(f"{SE_MOD}.is_customer_gold_enabled", return_value=True), patch(
			f"{SE_MOD}.get_customer_gold_settings", return_value=settings
		):
			excluded = _pure_qty_excluded_types()
		self.assertNotIn("Customer Goods Received", excluded)
		# Transfer and Issue are deliberately untouched -- not analysed by this project.
		self.assertIn("Customer Goods Transfer", excluded)
		self.assertIn("Customer Goods Issue", excluded)

	def test_flag_on_but_unconfigured_keeps_legacy(self):
		settings = frappe._dict(customer_goods_stock_entry_type=None)
		with patch(f"{SE_MOD}.is_customer_gold_enabled", return_value=True), patch(
			f"{SE_MOD}.get_customer_gold_settings", return_value=settings
		):
			self.assertEqual(
				_pure_qty_excluded_types(), _PURE_QTY_LEGACY_EXCLUDED_TYPES
			)

	def test_a_differently_named_configured_type_is_honoured(self):
		settings = frappe._dict(customer_goods_stock_entry_type="CG Intake")
		with patch(f"{SE_MOD}.is_customer_gold_enabled", return_value=True), patch(
			f"{SE_MOD}.get_customer_gold_settings", return_value=settings
		):
			# Nothing is un-excluded, because "CG Intake" was never in the legacy list.
			self.assertEqual(
				_pure_qty_excluded_types(), _PURE_QTY_LEGACY_EXCLUDED_TYPES
			)


@with_item_masters
@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
@patch(f"{MOD}.reference_rate", return_value=None)
class TestCustomerGoldValuationPolicy(IntegrationTestCase):
	"""C01 -- which valuation fields a receipt stamps, per configured policy.

	The two policies are deliberately mutually exclusive on the row, even though erpnext
	would tolerate both being set. ``set_basic_rate_manually`` short-circuits at
	``stock_entry.py:1615-1619`` before ``allow_zero_valuation_rate`` is ever read, so
	stamping both would mean stamping a flag that can never be consulted. A row should say
	what it means.

	Nominal is NOT enabled by these tests being green: D01 is the open decision about whether
	it is the approved policy. The default is Zero Value and every existing site keeps it.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	#: Stand-in for the per-company account resolver. The nominal branch calls it once per
	#: document, and it reads Subcontracting Settings for real -- which this suite does not
	#: have. Patched rather than widened into ``_db_get_value`` because it is a resolver, not
	#: a raw field read: a narrow dependency boundary, per C03.
	ACCOUNTS = frappe._dict(
		liability_account="Customer Gold Liability - GEPL",
		cogs_adjustment_account="Customer Gold COGS Adjustment - GEPL",
	)

	def _rows_under(self, policy, **entry_kwargs):
		doc = _entry(**entry_kwargs)
		with (
			patch(f"{MOD}.get_customer_gold_valuation_policy", return_value=policy),
			patch(
				f"{MOD}.get_customer_gold_company_settings", return_value=self.ACCOUNTS
			),
		):
			validate_customer_gold_receipt(doc)
		return doc.get("items")

	# -- Zero Value: today's behaviour, and the default ---------------------------
	def test_zero_value_stamps_allow_zero_and_not_the_manual_rate(self, *_mocks):
		row = self._rows_under("Zero Value")[0]
		self.assertEqual(row.allow_zero_valuation_rate, 1)
		self.assertEqual(row.set_basic_rate_manually, 0)

	def test_zero_value_does_not_book_a_rate(self, *_mocks):
		"""The resolved rate stays evidence only -- it must not reach basic_rate."""
		row = self._rows_under("Zero Value")[0]
		self.assertFalse(row.get("basic_rate"))

	def test_an_unknown_policy_falls_back_to_zero_value(self, *_mocks):
		"""Fail-safe: anything that is not exactly Nominal behaves as Zero Value."""
		for policy in ("", None, "nominal", "Something Else"):
			with self.subTest(policy=policy):
				row = self._rows_under(policy)[0]
				self.assertEqual(row.allow_zero_valuation_rate, 1)
				self.assertFalse(row.get("basic_rate"))

	# -- Nominal ------------------------------------------------------------------
	def test_nominal_books_the_frozen_per_gram_rate(self, *_mocks):
		row = self._rows_under("Nominal")[0]
		self.assertEqual(row.basic_rate, RATE.per_gram_rate)

	def test_nominal_sets_the_manual_rate_flag(self, *_mocks):
		"""``set_basic_rate_manually`` is what makes the entered rate survive erpnext.

		Without it the row falls through to the allow-zero wipe and the valuation fallback.
		"""
		row = self._rows_under("Nominal")[0]
		self.assertEqual(row.set_basic_rate_manually, 1)

	def test_nominal_does_not_stamp_allow_zero(self, *_mocks):
		row = self._rows_under("Nominal")[0]
		self.assertEqual(row.allow_zero_valuation_rate, 0)

	def test_the_two_flags_are_mutually_exclusive_under_both_policies(self, *_mocks):
		for policy in ("Zero Value", "Nominal"):
			with self.subTest(policy=policy):
				row = self._rows_under(policy)[0]
				self.assertNotEqual(
					bool(row.allow_zero_valuation_rate),
					bool(row.set_basic_rate_manually),
					"exactly one of the two valuation flags must be set",
				)

	def test_nominal_applies_to_every_row(self, *_mocks):
		rows = self._rows_under(
			"Nominal", items=[_item_row(), _item_row(), _item_row()]
		)
		for row in rows:
			self.assertEqual(row.basic_rate, RATE.per_gram_rate)
			self.assertEqual(row.set_basic_rate_manually, 1)

	def test_nominal_rate_comes_from_the_snapshot_not_a_fresh_lookup(self, *_mocks):
		"""The booked rate must be the frozen evidence, resolved once."""
		doc = _entry()
		with (
			patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Nominal"),
			patch(
				f"{MOD}.get_customer_gold_company_settings", return_value=self.ACCOUNTS
			),
		):
			validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].basic_rate, doc.custom_gold_rate_per_gram)

	def test_nominal_stamps_the_liability_account_as_the_contra(self, *_mocks):
		"""For a Stock Entry the credit leg is the row's ``expense_account``.

		A Liability-root account is legitimate there -- ``check_expense_account`` exempts
		Stock Entry from its P&L requirement, ``validate_difference_account`` rejects only
		``account_type == "Stock"``, and GL Entry has no root-type check. So the standard
		path credits the liability directly and no reclassification Journal Entry is needed.
		"""
		row = self._rows_under("Nominal")[0]
		self.assertEqual(row.expense_account, self.ACCOUNTS.liability_account)

	def test_zero_value_does_not_stamp_a_contra_account(self, *_mocks):
		row = self._rows_under("Zero Value")[0]
		self.assertFalse(row.get("expense_account"))


#: A second purity a customer may hand over. Real on this bench's chart of items.
SECOND_ITEM = "M-G-24KT-99.5-Y"
#: The fixtures' purities, so every figure below can be rechecked against the masters.
PRIMARY_PURITY = 99.9
SECOND_PURITY = 99.5


class TestPurityScaledRate(IntegrationTestCase):
	"""Customers hand over more than one purity, and the rate must follow the fine content.

	The configured Gold Rate is quoted per gram of the Customer 24KT Item (decision D02). Applying
	that same rupees-per-gram to a LOWER purity over-credits the customer, every receipt, in the
	same direction. Gold is bought on fine content, so the rate is restated by the purity ratio.

	WHY THE PURITY IS READ FROM THE ATTRIBUTE VALUE
	-----------------------------------------------
	``metal_utils.get_purity_percentage`` reads ``Attribute Value.purity_percentage``, and that
	column is wrong on this bench: the row named ``99.9`` carries **100.0**. Scaling against 100.0
	instead of 99.9 mis-rates every non-primary purity by 0.1%. The last test here pins the
	choice, because it is the kind of detail a later refactor "simplifies" back to the broken
	helper.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_the_reference_item_is_not_rescaled(self):
		"""The path every existing site takes. Must be bit-for-bit what it was before."""
		self.assertEqual(cg_receipt._rate_for_item(7200.0, ITEM, ITEM), 7200.0)

	def test_a_lower_purity_is_rated_down(self):
		"""7,200.00 x 99.5 / 99.9. Hand-computed, not derived from the code under test."""
		with patch.object(
			cg_receipt,
			"_metal_purity",
			side_effect=lambda i: {ITEM: PRIMARY_PURITY, SECOND_ITEM: SECOND_PURITY}[i],
		):
			self.assertAlmostEqual(
				cg_receipt._rate_for_item(7200.0, SECOND_ITEM, ITEM),
				7171.1712,
				places=4,
				msg="99.5 metal must not be booked at the 99.9 rate",
			)

	def test_the_difference_is_material_on_a_real_receipt(self):
		"""Not a rounding nicety: state the money, so nobody writes this off as noise."""
		with patch.object(
			cg_receipt,
			"_metal_purity",
			side_effect=lambda i: {ITEM: PRIMARY_PURITY, SECOND_ITEM: SECOND_PURITY}[i],
		):
			scaled = cg_receipt._rate_for_item(7164.83, SECOND_ITEM, ITEM)

		self.assertAlmostEqual(
			(7164.83 - scaled) * 10,
			286.88,
			places=2,
			msg="10 g of 99.5 booked at the 99.9 rate over-credits by this much",
		)

	def test_an_unresolvable_purity_refuses_rather_than_guessing(self):
		"""``repack.get_purity`` defaults to 99.9 and ``batch_rename.get_purity`` to 100.

		Two different guesses for the same unknown, already in this codebase. A third guess
		here would set a customer's booked value, so this one refuses instead.
		"""
		with patch.object(cg_receipt, "_metal_purity", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				cg_receipt._rate_for_item(7200.0, SECOND_ITEM, ITEM)

	def test_a_zero_rate_stays_zero_without_touching_purity(self):
		"""Nothing to scale, so nothing is read -- and no throw for a missing purity."""
		with patch.object(cg_receipt, "_metal_purity", return_value=None):
			self.assertEqual(cg_receipt._rate_for_item(0.0, SECOND_ITEM, ITEM), 0.0)

	def test_purity_comes_from_the_attribute_value_not_the_broken_column(self):
		"""D04's defence. Reading ``purity_percentage`` would return 100.0 for this item."""
		captured = {}

		def _get_all(doctype, **kwargs):
			captured["doctype"] = doctype
			captured["fields"] = kwargs.get("fields")
			captured["filters"] = kwargs.get("filters")
			return [frappe._dict(attribute_value="99.9")]

		with patch.object(cg_receipt.frappe, "get_all", side_effect=_get_all):
			self.assertEqual(cg_receipt._metal_purity(ITEM), 99.9)

		self.assertEqual(captured["doctype"], "Item Variant Attribute")
		self.assertEqual(captured["fields"], ["attribute_value"])
		self.assertEqual(captured["filters"]["attribute"], "Metal Purity")

	def test_a_missing_attribute_reads_as_unknown(self):
		with patch.object(cg_receipt.frappe, "get_all", return_value=[]):
			self.assertIsNone(cg_receipt._metal_purity(ITEM))

	def test_a_non_positive_attribute_reads_as_unknown(self):
		"""``91.75`` carries 0.0 on this bench -- the same class of master defect as ``99.9``."""
		for value in ("0", "", "not a number"):
			with self.subTest(value=value):
				with patch.object(
					cg_receipt.frappe,
					"get_all",
					return_value=[frappe._dict(attribute_value=value)],
				):
					self.assertIsNone(cg_receipt._metal_purity(ITEM))


@with_item_masters
@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
@patch(f"{MOD}.reference_rate", return_value=None)
class TestCustomerGoodsItemFlag(IntegrationTestCase):
	"""The Item's ``Inventory Type Can be Customer Goods`` flag is the only eligibility rule.

	Subcontracting Settings used to keep a second list (``customer_gold_items``) and the two
	disagreed on kg-gk: the rate-reference 24KT item was unflagged while flagged items were missing
	from the list. The list is gone; ``customer_24kt_item`` is only the rate reference now, and is
	not admitted implicitly either.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _validate(self, *rows):
		doc = _entry(items=list(rows))
		validate_customer_gold_receipt(doc)
		return doc

	def _refused(self, *rows):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate(*rows)
		return str(raised.exception)

	def test_a_flagged_item_passes(self, *_mocks):
		"""T01."""
		doc = self._validate(_item_row(inventory_type="Regular Stock"))
		self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")

	def test_an_unflagged_item_is_refused_and_told_which_field_to_tick(self, *_mocks):
		"""T02. The message names the Item field, so the user knows exactly what to change."""
		message = self._refused(_item_row(item_code=UNFLAGGED_22KT))
		self.assertIn(NOT_ENABLED, message)
		self.assertIn(UNFLAGGED_22KT, message)
		self.assertIn(FLAG_LABEL, message)

	def test_a_flagged_item_that_is_not_the_rate_reference_passes(self, *_mocks):
		"""T03. Nothing lists it: SETTINGS names only the reference item, and this is not it."""
		self.assertNotIn("customer_gold_items", SETTINGS)
		self.assertNotEqual(GOLD_22KT, SETTINGS.customer_24kt_item)
		doc = self._validate(
			_item_row(item_code=GOLD_22KT, inventory_type="Regular Stock")
		)
		self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")

	def test_a_leftover_settings_list_is_ignored(self, *_mocks):
		"""T04. An unmigrated site still carries the retired list on its Settings.

		It must neither admit an unflagged item it names nor be needed by a flagged one.
		"""
		legacy = frappe._dict(
			SETTINGS, customer_gold_items=[frappe._dict(item=UNFLAGGED_22KT)]
		)
		with patch(f"{MOD}.get_customer_gold_settings", return_value=legacy):
			message = self._refused(_item_row(item_code=UNFLAGGED_22KT))
			self._validate(_item_row(item_code=GOLD_22KT))
		self.assertIn(NOT_ENABLED, message)

	def test_ticking_the_flag_admits_the_item_on_the_next_validate(self, *_mocks):
		"""T05. No cache between the Item and the receipt -- the kg-gk fix is ticking one box."""
		doc = _entry(items=[_item_row(inventory_type="Regular Stock")])
		with patch.dict(ITEM_FLAGS, {ITEM: 0}):
			with self.assertRaises(frappe.ValidationError) as raised:
				validate_customer_gold_receipt(doc)
		self.assertIn(NOT_ENABLED, str(raised.exception))

		with patch.dict(ITEM_FLAGS, {ITEM: 1}):
			validate_customer_gold_receipt(doc)
		self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")

	def test_clearing_the_flag_refuses_the_item_on_the_next_validate(self, *_mocks):
		"""T06. The same draft, re-validated after the flag was cleared, is refused."""
		doc = _entry()
		with patch.dict(ITEM_FLAGS, {ITEM: 1}):
			validate_customer_gold_receipt(doc)

		with patch.dict(ITEM_FLAGS, {ITEM: 0}):
			with self.assertRaises(frappe.ValidationError) as raised:
				validate_customer_gold_receipt(doc)
		self.assertIn(NOT_ENABLED, str(raised.exception))

	def test_a_refused_row_leaves_every_row_as_it_arrived(self, *_mocks):
		"""T07. All rows are checked before any is changed, so row 3 failing touches nothing.

		No row is stamped Customer Goods or with a valuation flag, and no gold rate is resolved
		or frozen -- a refused receipt must not look half-processed.
		"""
		doc = _entry(
			items=[
				_item_row(1, inventory_type="Regular Stock"),
				_item_row(2, item_code=GOLD_22KT, inventory_type="Regular Stock"),
				_item_row(3, item_code=UNFLAGGED_22KT, inventory_type="Regular Stock"),
			]
		)
		with patch(
			f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE
		) as resolve:
			with self.assertRaises(frappe.ValidationError) as raised:
				validate_customer_gold_receipt(doc)

		self.assertIn("Row #3", str(raised.exception))
		self.assertIn(NOT_ENABLED, str(raised.exception))
		resolve.assert_not_called()
		self.assertIsNone(doc.get("custom_gold_rate_per_gram"))
		for row in doc.get("items"):
			with self.subTest(row=row.idx):
				self.assertEqual(row.inventory_type, "Regular Stock")
				self.assertIsNone(row.get("allow_zero_valuation_rate"))
				self.assertIsNone(row.get("set_basic_rate_manually"))

	def test_the_item_master_is_read_once_per_receipt(self, *_mocks):
		"""Two queries for the whole receipt, never one per row."""
		eligible = MagicMock(side_effect=_eligible)
		details = MagicMock(side_effect=_details)
		codes = [ITEM, GOLD_22KT, DIAMOND]
		with (
			patch(f"{MOD}.get_customer_goods_eligible_items", new=eligible),
			patch(f"{MOD}._receipt_item_details", new=details),
		):
			self._validate(
				*(_item_row(idx, item_code=code) for idx, code in enumerate(codes, 1))
			)
		eligible.assert_called_once_with(codes)
		details.assert_called_once_with(codes)

	def test_other_stock_entry_types_never_read_the_flag(self, *_mocks):
		"""T11/T12. With the flow ON, only the configured type is a Customer Gold receipt.

		A plain Material Receipt of an unflagged item is ordinary stock: not refused, not
		re-tagged, and its Item is not even looked up.
		"""
		for stock_entry_type in ("Material Receipt", "Material Transfer (WORK ORDER)"):
			with self.subTest(stock_entry_type=stock_entry_type):
				eligible = MagicMock(side_effect=_eligible)
				doc = _entry(
					stock_entry_type=stock_entry_type,
					_customer=None,
					items=[
						_item_row(
							item_code=UNFLAGGED_22KT, inventory_type="Regular Stock"
						)
					],
				)
				with patch(f"{MOD}.get_customer_goods_eligible_items", new=eligible):
					validate_customer_gold_receipt(doc)
				eligible.assert_not_called()
				row = doc.get("items")[0]
				self.assertEqual(row.inventory_type, "Regular Stock")
				self.assertIsNone(row.get("allow_zero_valuation_rate"))


def _stone_row(idx=1, item_code=DIAMOND, basic_rate=0, **overrides):
	"""A stone row: counted in Carat, rate typed by the user."""
	return _item_row(
		idx, item_code=item_code, qty=2.5, basic_rate=basic_rate, **overrides
	)


#: Evidence a draft froze while it still had a gold row.
STALE_RATE_EVIDENCE = frappe._dict(
	custom_gold_rate_reference="R-2026-08-15",
	custom_gold_rate_date="2026-08-15",
	custom_gold_rate_source="Jain Jewels",
	custom_gold_rate_field="live_rate",
	custom_gold_rate_raw=72000.0,
	custom_gold_rate_unit="Per 10 Gram",
	custom_gold_rate_per_gram=7200.0,
	custom_gold_rate_factor=10.0,
	custom_gold_rate_currency="INR",
	custom_gold_rate_check_reference=7000.0,
	custom_gold_rate_check_source="Purchase Receipt PR-1",
	custom_gold_rate_check_ratio=1.03,
	custom_gold_rate_override_by="Administrator",
)


@with_item_masters
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
class TestCustomerGoodsStones(IntegrationTestCase):
	"""Diamonds and gemstones on a Customer Gold receipt.

	Gold and findings (templates ``M`` / ``F``) are priced from the per-gram gold rate, so they
	must be stocked in grams. A stone is received in its own UOM at the rate the user types, and
	zero is a valid rate: the gold rate is never fetched for it. Every other gate -- flag, exists,
	enabled, stock item, batch controlled -- is the same for both.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	ACCOUNTS = TestCustomerGoldValuationPolicy.ACCOUNTS

	def _validate(self, doc, policy="Zero Value"):
		"""Validate ``doc`` under ``policy``. Returns the rate resolver and the check's reference."""
		with (
			patch(f"{MOD}.get_customer_gold_valuation_policy", return_value=policy),
			patch(
				f"{MOD}.get_customer_gold_company_settings", return_value=self.ACCOUNTS
			),
			patch(
				f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE
			) as resolve,
			patch(f"{MOD}.reference_rate", return_value=None) as reference,
		):
			validate_customer_gold_receipt(doc)
		return resolve, reference

	def _refused(self, *rows):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate(_entry(items=list(rows)))
		return str(raised.exception)

	# -- gates --------------------------------------------------------------------
	def test_a_flagged_carat_stone_passes(self, *_mocks):
		"""Stones are not held to Gram -- the reason the user could not receive them before."""
		for code in (DIAMOND, GEMSTONE):
			with self.subTest(item=code):
				doc = _entry(
					items=[_stone_row(item_code=code, inventory_type="Regular Stock")]
				)
				self._validate(doc)
				self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")

	def test_gold_stocked_in_carat_is_still_refused(self, *_mocks):
		"""The per-gram gold rate cannot price a Carat of gold, so the Gram gate stays for gold."""
		message = self._refused(_item_row(item_code=GOLD_IN_CARAT))
		self.assertIn("Row #1: Item", message)
		self.assertIn("has Stock UOM", message)
		self.assertIn("Carat", message)

	def test_a_flagged_finding_in_gram_passes_and_resolves_the_gold_rate(self, *_mocks):
		"""A finding is gold, not a stone: a findings-only receipt still freezes the gold rate."""
		doc = _entry(
			items=[_item_row(item_code=FINDING, inventory_type="Regular Stock")]
		)
		resolve, _reference = self._validate(doc)
		resolve.assert_called_once()
		self.assertEqual(doc.get("items")[0].inventory_type, "Customer Goods")
		self.assertEqual(doc.custom_gold_rate_per_gram, RATE.per_gram_rate)

	def test_a_finding_in_carat_is_refused_like_gold(self, *_mocks):
		"""Template ``F`` is held to Gram exactly as ``M`` is. Were it classed as a stone, this
		Carat finding would pass at a typed rate."""
		message = self._refused(_item_row(item_code=FINDING_IN_CARAT))
		self.assertIn("Row #1: Item", message)
		self.assertIn(FINDING_IN_CARAT, message)
		self.assertIn("has Stock UOM", message)
		self.assertIn("Carat", message)

	def test_a_stone_without_batches_is_refused(self, *_mocks):
		message = self._refused(_stone_row(item_code=STONE_WITHOUT_BATCH))
		self.assertIn("must be batch controlled", message)

	def test_a_negative_stone_rate_is_refused(self, *_mocks):
		message = self._refused(_stone_row(basic_rate=-1))
		self.assertIn("cannot be negative", message)
		self.assertIn(DIAMOND, message)

	# -- the gold rate is for gold only -------------------------------------------
	def test_a_stones_only_receipt_resolves_no_gold_rate(self, *_mocks):
		"""No fetch, no check, no evidence -- so a missing feed cannot block a stone receipt."""
		doc = _entry(items=[_stone_row(1), _stone_row(2, item_code=GEMSTONE)])
		resolve, reference = self._validate(doc)
		resolve.assert_not_called()
		reference.assert_not_called()
		for fieldname in STALE_RATE_EVIDENCE:
			self.assertIsNone(doc.get(fieldname), fieldname)

	def test_a_draft_that_lost_its_gold_row_drops_the_old_rate(self, *_mocks):
		"""Saved with gold, edited to stones only: the frozen rate no longer describes it."""
		doc = _entry(items=[_stone_row()], **STALE_RATE_EVIDENCE)
		self._validate(doc)
		for fieldname in STALE_RATE_EVIDENCE:
			self.assertIsNone(doc.get(fieldname), fieldname)

	# -- valuation ----------------------------------------------------------------
	def test_nominal_keeps_a_typed_stone_rate(self, *_mocks):
		"""Above zero it is booked like a gold row: manual rate, liability contra."""
		doc = _entry(items=[_stone_row(basic_rate=1500)])
		self._validate(doc, "Nominal")
		row = doc.get("items")[0]
		self.assertEqual(row.basic_rate, 1500)
		self.assertEqual(row.set_basic_rate_manually, 1)
		self.assertEqual(row.allow_zero_valuation_rate, 0)
		self.assertEqual(row.expense_account, self.ACCOUNTS.liability_account)

	def test_nominal_zero_values_a_stone_typed_at_zero(self, *_mocks):
		"""Nothing to book, so the allow-zero flag and no contra account."""
		doc = _entry(items=[_stone_row(basic_rate=0)])
		self._validate(doc, "Nominal")
		row = doc.get("items")[0]
		self.assertEqual(row.allow_zero_valuation_rate, 1)
		self.assertEqual(row.set_basic_rate_manually, 0)
		self.assertFalse(row.get("expense_account"))

	def test_nominal_prices_gold_from_the_rate_and_stones_from_the_row(self, *_mocks):
		"""A mixed receipt: the rate is resolved once, and reaches the gold row only."""
		doc = _entry(items=[_item_row(1), _stone_row(2, basic_rate=1500)])
		resolve, _reference = self._validate(doc, "Nominal")
		gold, stone = doc.get("items")
		resolve.assert_called_once()
		self.assertEqual(doc.custom_gold_rate_per_gram, RATE.per_gram_rate)
		# ITEM is the rate reference, so its purity scale is 1.
		self.assertEqual(gold.basic_rate, RATE.per_gram_rate)
		self.assertEqual(stone.basic_rate, 1500)
		for row in (gold, stone):
			self.assertEqual(row.set_basic_rate_manually, 1)
			self.assertEqual(row.expense_account, self.ACCOUNTS.liability_account)

	def test_nominal_prices_a_finding_in_gram_from_the_gold_rate(self, *_mocks):
		"""The finding's rate is the gold rate restated by its purity, not a typed one.

		On the stone path this untyped row would be zero-valued with the allow-zero flag instead.
		"""
		doc = _entry(items=[_item_row(item_code=FINDING)])
		purities = {ITEM: PRIMARY_PURITY, FINDING: FINDING_PURITY}
		with (
			patch.object(cg_receipt, "_metal_purity", side_effect=purities.__getitem__),
			patch.object(
				cg_receipt, "_rate_for_item", wraps=cg_receipt._rate_for_item
			) as rate_for_item,
		):
			resolve, _reference = self._validate(doc, "Nominal")
		row = doc.get("items")[0]
		resolve.assert_called_once()
		rate_for_item.assert_called_once_with(RATE.per_gram_rate, FINDING, ITEM)
		# 7,200.00 x 75.4 / 99.9. Hand-computed, not derived from the code under test.
		self.assertAlmostEqual(row.basic_rate, 5434.2342, places=4)
		self.assertEqual(row.set_basic_rate_manually, 1)
		self.assertEqual(row.allow_zero_valuation_rate, 0)
		self.assertEqual(row.expense_account, self.ACCOUNTS.liability_account)

	def test_zero_value_zero_values_a_typed_stone_rate(self, *_mocks):
		"""Under Zero Value every row is zero-valued, stones included, whatever was typed."""
		doc = _entry(items=[_stone_row(basic_rate=1500)])
		self._validate(doc, "Zero Value")
		row = doc.get("items")[0]
		self.assertEqual(row.allow_zero_valuation_rate, 1)
		self.assertEqual(row.set_basic_rate_manually, 0)
		self.assertFalse(row.get("expense_account"))


class TestCustomerGoodsEligibility(IntegrationTestCase):
	"""``customer_goods_eligibility`` -- the one rule, read from the Item master.

	Its query shape is the contract: one read per document, the flag in the filter, nothing
	fetched for nothing. ``_receipt_item_details`` is the receipt's second read, pinned the same way.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_one_query_with_the_flag_in_the_filter(self):
		with patch(
			f"{ELIGIBILITY_MOD}.frappe.get_all", return_value=["A-ITEM"]
		) as get_all:
			eligible = customer_goods_eligibility.get_customer_goods_eligible_items(
				["B-ITEM", "A-ITEM", "", None, "B-ITEM"]
			)
		get_all.assert_called_once_with(
			"Item",
			filters={
				"name": ["in", ["A-ITEM", "B-ITEM"]],
				"custom_inventory_type_can_be_customer_goods": 1,
			},
			pluck="name",
		)
		self.assertEqual(eligible, {"A-ITEM"})
		self.assertIsInstance(eligible, set)

	def test_batch_controlled_also_requires_batches(self):
		"""For a caller about to mint a batch: a flagged item without batches is not eligible."""
		with patch(
			f"{ELIGIBILITY_MOD}.frappe.get_all", return_value=["A-ITEM"]
		) as get_all:
			eligible = customer_goods_eligibility.get_customer_goods_eligible_items(
				["B-ITEM", "A-ITEM"], batch_controlled=True
			)
		get_all.assert_called_once_with(
			"Item",
			filters={
				"name": ["in", ["A-ITEM", "B-ITEM"]],
				"custom_inventory_type_can_be_customer_goods": 1,
				"has_batch_no": 1,
			},
			pluck="name",
		)
		self.assertEqual(eligible, {"A-ITEM"})

	def test_the_default_call_does_not_require_batches(self):
		"""The receipt reads the flag alone, then refuses a batchless item with its own message."""
		for kwargs in ({}, {"batch_controlled": False}):
			with self.subTest(kwargs=kwargs):
				with patch(
					f"{ELIGIBILITY_MOD}.frappe.get_all", return_value=[]
				) as get_all:
					customer_goods_eligibility.get_customer_goods_eligible_items(
						["A-ITEM"], **kwargs
					)
				filters = get_all.call_args.kwargs["filters"]
				self.assertNotIn("has_batch_no", filters)
				self.assertEqual(
					filters["custom_inventory_type_can_be_customer_goods"], 1
				)

	def test_every_call_is_a_fresh_query(self):
		"""No cache: a flag cleared between two documents is seen by the second."""
		with patch(
			f"{ELIGIBILITY_MOD}.frappe.get_all", side_effect=[["A-ITEM"], []]
		) as get_all:
			first = customer_goods_eligibility.get_customer_goods_eligible_items(
				["A-ITEM"]
			)
			second = customer_goods_eligibility.get_customer_goods_eligible_items(
				["A-ITEM"]
			)
		self.assertEqual(get_all.call_count, 2)
		self.assertEqual(first, {"A-ITEM"})
		self.assertEqual(second, set())

	def test_nothing_to_look_up_costs_no_query(self):
		for codes in ([], [None, ""], None):
			with self.subTest(codes=codes):
				with patch(f"{ELIGIBILITY_MOD}.frappe.get_all") as get_all:
					self.assertEqual(
						customer_goods_eligibility.get_customer_goods_eligible_items(
							codes
						),
						set(),
					)
				get_all.assert_not_called()

	def test_the_single_item_form_reads_the_flag(self):
		for stored, expected in ((1, True), (0, False), (None, False)):
			with self.subTest(stored=stored):
				with patch(
					f"{ELIGIBILITY_MOD}.frappe.db.get_value", return_value=stored
				) as get_value:
					self.assertIs(
						customer_goods_eligibility.can_be_customer_goods(ITEM), expected
					)
				get_value.assert_called_once_with(
					"Item", ITEM, "custom_inventory_type_can_be_customer_goods"
				)

	def test_the_single_item_form_is_false_for_no_item(self):
		with patch(f"{ELIGIBILITY_MOD}.frappe.db.get_value") as get_value:
			self.assertFalse(customer_goods_eligibility.can_be_customer_goods(None))
			self.assertFalse(customer_goods_eligibility.can_be_customer_goods(""))
		get_value.assert_not_called()

	def test_the_gold_templates_are_one_constant(self):
		"""Settlement and the receipt must agree on what is gold; fulfilment re-exports it."""
		self.assertEqual(customer_goods_eligibility.CUSTOMER_GOLD_TEMPLATES, ("M", "F"))
		self.assertIs(
			customer_gold_fulfilment.CUSTOMER_GOLD_TEMPLATES,
			customer_goods_eligibility.CUSTOMER_GOLD_TEMPLATES,
		)

	def test_the_receipt_reads_its_item_details_in_one_query(self):
		masters = [_master(DIAMOND, "D", "Carat"), _master(ITEM, "M")]
		with patch(f"{MOD}.frappe.get_all", return_value=masters) as get_all:
			details = cg_receipt._receipt_item_details([ITEM, "", DIAMOND, ITEM])
		get_all.assert_called_once_with(
			"Item",
			filters={"name": ["in", [DIAMOND, ITEM]]},
			fields=[
				"name",
				"variant_of",
				"disabled",
				"is_stock_item",
				"has_batch_no",
				"stock_uom",
			],
		)
		self.assertEqual(set(details), {DIAMOND, ITEM})
		self.assertEqual(details[DIAMOND].stock_uom, "Carat")

	def test_the_receipt_reads_no_item_details_for_no_rows(self):
		with patch(f"{MOD}.frappe.get_all") as get_all:
			self.assertEqual(cg_receipt._receipt_item_details([None, ""]), {})
		get_all.assert_not_called()
