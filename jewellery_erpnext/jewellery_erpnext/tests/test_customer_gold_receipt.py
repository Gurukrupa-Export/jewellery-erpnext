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
		doc = _entry(items=[_item_row(inventory_type="Regular Stock")])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_gold_receipt(doc)

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
