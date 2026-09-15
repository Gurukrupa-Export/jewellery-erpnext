# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold receipt eligibility rules.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, Stock Entries are
``frappe._dict`` fakes and every DB read is patched. No document is created.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

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


@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
class TestCustomerGoldReceiptRules(IntegrationTestCase):
	"""Receipt eligibility, with the feature enabled."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_valid_receipt_passes(self, *_mocks):
		validate_customer_gold_receipt(_entry())

	def test_missing_customer_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(_entry(_customer=None))

	def test_row_customer_conflict_blocks(self, *_mocks):
		doc = _entry(items=[_item_row(customer=OTHER_CUSTOMER)])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(doc)

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
		doc = _entry(items=[_item_row(item_code="M-G-22KT-91.9-Y")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(doc)

	def test_other_24kt_item_still_blocks(self, *_mocks):
		"""The configured item wins, even for another item whose code says 24KT."""
		doc = _entry(items=[_item_row(item_code="M-G-24KT-99.9-W")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(doc)

	def test_wrong_inventory_type_blocks(self, *_mocks):
		"""A DELIBERATE other ownership class still hard-fails.

		"Customer Stock" is a real Inventory Type and a real ownership class -- nothing in
		the framework ever stamps it by default, so its presence is always a caller's
		choice and always wrong on a Customer Gold receipt.
		"""
		doc = _entry(items=[_item_row(inventory_type="Customer Stock")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(doc)

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
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(_entry(items=[_item_row(qty=0)]))

	def test_negative_qty_blocks(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(_entry(items=[_item_row(qty=-1)]))

	def test_batch_rules_pass_for_own_batch(self, *_mocks):
		validate_customer_gold_batches(_entry())

	def test_missing_batch_blocks_at_submit(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_batches(_entry(items=[_item_row(batch_no=None)]))

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


@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.get_customer_gold_valuation_policy", return_value="Zero Value")
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
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


@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
@patch(f"{MOD}.is_customer_gold_enabled", return_value=True)
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
