# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the Customer Gold receipt eligibility rules.

Pure-logic per the suite convention: ``setUpClass`` is neutralized, Stock Entries are
``frappe._dict`` fakes and every DB read is patched. No document is created.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.customer_gold_receipt import (
	validate_customer_gold_batches,
	validate_customer_gold_receipt,
)

MOD = "jewellery_erpnext.customer_subcontracting.customer_gold_receipt"

ITEM = "M-G-24KT-99.9-Y"
SE_TYPE = "Customer Goods Received"
CUSTOMER = "GJCU0009"
OTHER_CUSTOMER = "MHCU0012"
BATCH = "GJCU0009-2F07-M-G-24KT-99.9-Y-01"

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
		_customer=CUSTOMER,
		items=[_item_row()],
	)
	doc.update(overrides)
	return doc


def _db_get_value(doctype, name, fieldname=None, as_dict=False):
	if doctype == "Stock Entry Type":
		return "Material Receipt"
	if doctype == "Batch":
		if name == BATCH:
			return frappe._dict(
				custom_customer=CUSTOMER, custom_inventory_type="Customer Goods"
			)
		if name == "FOREIGN-BATCH":
			return frappe._dict(
				custom_customer=OTHER_CUSTOMER, custom_inventory_type="Customer Goods"
			)
		if name == "REGULAR-BATCH":
			return frappe._dict(
				custom_customer=None, custom_inventory_type="Regular Stock"
			)
		return None
	return None


@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
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

	def test_other_stock_entry_type_is_untouched(self, *_mocks):
		"""A different Stock Entry Type must not be validated, even with the flag on."""
		doc = _entry(stock_entry_type="Material Transfer (WORK ORDER)", _customer=None)
		validate_customer_gold_receipt(doc)
		validate_customer_gold_batches(doc)


@patch(f"{MOD}.resolve_customer_gold_rate_for_date", return_value=RATE)
@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
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


@patch(f"{MOD}.frappe.db.get_value", side_effect=_db_get_value)
@patch(f"{MOD}.get_customer_gold_settings", return_value=SETTINGS)
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
